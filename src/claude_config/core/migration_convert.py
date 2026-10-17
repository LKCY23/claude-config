"""Standalone old-data converter. Produces unpublished review candidates only."""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

import yaml

from .app_fields import native_paths, validate_settings as validate_app
from .catalog_schema import validate_catalog
from .fields import leaves, lookup, validate_settings
from .migration_inventory import inventory
from .merge import MISSING, fingerprint, get
from .native import parse_json, parse_native, parse_yaml, projection, put, snapshot_read
from .resources import tree_digest
from .schema import require, validate_catalogs, validate_machine, validate_profile, within
from .transaction import file_fingerprint


def encoded(value):
    return (json.dumps(value, ensure_ascii=False, indent=2) + '\n').encode()


class Conversion:
    def __init__(self, data_root, output_root, catalog_roots, *, executables=None, applications=None, app_profiles=None):
        self.root, self.output = Path(data_root).absolute(), Path(output_root).absolute()
        require(not self.output.exists(), 'Candidate output must not already exist')
        require(not self.output.resolve().is_relative_to(self.root.resolve()), 'Candidate must be outside the active data repository')
        self.roots = {key: Path(value).absolute() for key, value in catalog_roots.items()}
        require(all(not self.output.resolve().is_relative_to(p.resolve()) for p in self.roots.values()), 'Candidate cannot be inside a source catalog')
        self.executables = executables or {}
        self.applications = applications or {}
        self.app_profiles = app_profiles or {}
        self.report = inventory(self.root)
        require(self.report['ready'], 'Legacy inventory contains blockers')
        self.files, self.modes, self.snapshots = {}, {}, dict(self.report['file_snapshots'])
        self.package_members = {}
        self.links = {}
        self.agents, self.apps, self.catalogs = {}, {}, {}
        self.machine = {'schema_version': 1, 'machine_id': 'migration-candidate', 'config_dir': str(self.output / 'data'),
                        'catalog_roots': {}, 'agents': {}, 'apps': {}, 'bindings': {'executables': {}},
                        'runtime': {'state_dir': str(self.output / 'state')}}
        self.report['migration_conversion'] = {'output_root': str(self.output), 'profiles': [], 'resources': [], 'review_required': []}
        self.decisions = self.report['decisions']

    def read(self, path):
        path = Path(path)
        require(not path.is_symlink(), 'Local asset symlink needs an explicit source adapter')
        data, snapshot = snapshot_read(path)
        require(snapshot['exists'], 'Conversion input is missing')
        self.snapshots[str(path)] = snapshot
        return data

    def asset(self, reference):
        return self.read(within(self.root, reference))

    def decision(self, path, action, reason, *, before=MISSING, after=MISSING, incoming=MISSING):
        incoming = after if incoming is MISSING else incoming
        self.decisions.append({'path': path, 'action': action, 'category': 'policy_merge',
                               'policy': 'explicit-migration-adapter', 'policy_origin': 'migration_converter',
                               'before_exists': before is not MISSING, 'before': None if before is MISSING else deepcopy(before),
                               'incoming_exists': incoming is not MISSING, 'incoming': None if incoming is MISSING else deepcopy(incoming),
                               'after_exists': after is not MISSING, 'after': None if after is MISSING else deepcopy(after),
                               'reason': reason, 'check_status': 'passed'})

    def add(self, path, content, mode=0o600):
        require(path not in self.files, 'Two conversion outputs own the same file')
        self.files[path], self.modes[path] = content, mode

    def settings(self, value, target, *, primary=False):
        profile = self.agents.setdefault(target, {'schema_version': 1, 'agent_type': target, 'settings': {}, 'resources': {}})
        for path, item in leaves(value):
            field = lookup(target, path)
            if field and not field.binding:
                field.check(item, path)
                if not primary and get(profile['settings'], path) is not MISSING:
                    current = get(profile['settings'], path)
                    self.decision(['profiles', target, 'settings', *path], 'preserve', '采用当前 Agent 声明，旧 base 中的重复值不覆盖它。', before=current, incoming=item, after=current)
                else:
                    put(profile['settings'], path, item)
                    self.decision(['profiles', target, 'settings', *path], 'add', '将已选字段写入独立 Profile，不补原生默认值。', after=item)
            elif path == ('includeCoAuthoredBy',):
                require(item is False, 'Enabled legacy attribution needs explicit text before conversion')
                for key in ('commit', 'pr'):
                    if key not in profile['settings'].get('attribution', {}):
                        put(profile['settings'], ('attribution', key), '')
                self.decision(['profiles', target, 'settings', 'attribution'], 'overwrite', '旧 includeCoAuthoredBy=false 映射为提交和 PR 的空署名，不保留旧字段。', before=False, after=profile['settings']['attribution'])
            elif target == 'codex' and path == ('agents', 'max_threads'):
                put(profile['settings'], ('agents', 'max_concurrent_threads_per_session'), item)
                self.decision(['profiles', target, 'settings', 'agents', 'max_concurrent_threads_per_session'], 'overwrite', '旧并发字段映射为当前明确的原生字段。', before={'max_threads': item}, after=item)
            else:
                require(path[:1] not in (('hooks',), ('mcp_servers',), ('plugins',), ('skills',)),
                        'Legacy inline resources need a specific conversion adapter')
                self.decision(['exclusions', target, *path], 'skip', '此字段不纳入新版；不复制值，也不修改本机现有值。')

    def package(self, root):
        root = root.resolve()
        files, modes = {}, {}
        for path in sorted(root.rglob('*')):
            relative = path.relative_to(root)
            if set(relative.parts) & {'.git', '__pycache__', 'node_modules'} or path.name in ('.DS_Store', '.in_use', '.orphaned_at'):
                continue
            if path.is_symlink():
                require(path.resolve().is_relative_to(root) and path.resolve().is_file(), 'Package symlink escapes the declared source')
                path_for_read = path.resolve()
                self.links[str(path)] = str(path_for_read)
            elif path.is_file():
                path_for_read = path
            else:
                continue
            content = self.read(path_for_read)
            files[relative.as_posix()] = content
            modes[relative.as_posix()] = 0o700 if path_for_read.stat().st_mode & 0o111 else 0o600
        require(bool(files), 'Selected package is empty')
        self.package_members[str(root)] = sorted(files)
        return files, modes

    def resource(self, alias, name, kind, files, modes, compatibility, *, origin=None, requires=None):
        catalog = self.catalogs.setdefault(alias, {'schema_version': 1, 'name': alias, 'resources': {}})
        require(name not in catalog['resources'], 'Duplicate converted resource identity')
        relative = 'packages/' + name
        for filename, content in files.items():
            self.add('source/catalogs/' + alias + '/' + relative + '/' + filename, content, modes.get(filename, 0o600))
        digest = tree_digest(files, modes)
        catalog['resources'][name] = {'kind': kind, 'description': 'Converted declared resource: ' + name,
             'origin': origin or {'type': 'self', 'author': 'configuration owner'},
             'maintenance': {'owner': 'ours', 'mode': 'adapter'},
             'source': {'kind': 'snapshot', 'path': relative, 'baseline': 'reviewed legacy conversion candidate', 'sha256': digest},
             'compatibility': {host: {'mode': mode, 'entry': entry, 'validation': {'status': 'not-evaluated'}} for host, (mode, entry) in compatibility.items()},
             'requires': requires or {}, 'update': {'strategy': 'manual'}}
        self.report['migration_conversion']['resources'].append({'ref': name + '@' + alias, 'kind': kind, 'sha256': digest})

    def plugins(self, declaration):
        selected = {}
        for old_host, entry in declaration['agents'].items():
            host = 'claude-code' if old_host == 'claude' else old_host
            for ref in entry.get('plugins', []):
                selected.setdefault(ref, []).append(host)
                self.agents[host]['resources'].setdefault('plugins', {})[ref] = {}
        repos = set()
        for ref, hosts in selected.items():
            name, alias = ref.split('@')
            declared = declaration['catalogs'][alias]
            require(declared['repo'] in self.roots, 'Old catalog repository requires an explicit local root')
            repos.add(declared['repo'])
            catalog_root = within(self.roots[declared['repo']], declared['path'])
            old_catalog = parse_json(self.read(catalog_root / 'catalog.json'))
            require(old_catalog.get('version') == 1 and old_catalog.get('name') == alias, 'Legacy catalog identity mismatch')
            matches = [row for row in old_catalog['plugins'] if row['name'] == name]
            require(len(matches) == 1, 'Selected plugin has no unique legacy registration')
            row = matches[0]
            package = within(catalog_root, row['source'])
            files, modes = self.package(package)
            manifest = parse_json(files['.claude-plugin/plugin.json'])
            require(manifest.get('name') == name, 'Native plugin identity differs from the selection')
            if 'codex' in hosts and '.codex-plugin/plugin.json' not in files:
                old_version = manifest['version']
                new_version = old_version + ('.migration.1' if '+' in old_version else '+claudespace.migration.1')
                manifest['version'] = new_version
                files['.claude-plugin/plugin.json'] = encoded(manifest)
                codex = {key: deepcopy(value) for key, value in manifest.items() if key in ('name', 'version', 'description', 'author', 'license', 'skills')}
                files['.codex-plugin/plugin.json'] = encoded(codex)
                modes['.codex-plugin/plugin.json'] = 0o600
                self.report['migration_conversion']['review_required'].append({'ref': ref, 'code': 'native_manifest_adapter',
                    'before_version': old_version, 'candidate_version': new_version,
                    'reason': 'Add required Codex manifest; packaging bytes change and require a distinct version, not an in-place same-version publication'})
                self.decision(['resources', 'plugins', ref, 'version'], 'overwrite', '补齐 Codex manifest 需要不同的包装版本；只生成候选，不更新现有安装。', before=old_version, after=new_version)
            upstream = row.get('upstream', {})
            origin = {'type': 'third-party', 'author': row.get('author', 'legacy catalog author')}
            if re.fullmatch(r'[0-9a-f]{40}', upstream.get('sha', '')):
                origin['upstream'] = {'repo': upstream['repo'], 'commit': upstream['sha'], 'path': upstream.get('path', '.')}
            else:
                origin['baseline'] = 'Legacy declared publishing/snapshot baseline; upstream commit content is not claimed verified'
            self.resource(alias, name, 'plugin', files, modes,
                {host: ('native', '.codex-plugin/plugin.json' if host == 'codex' else '.claude-plugin/plugin.json') for host in hosts}, origin=origin)
            converted = self.catalogs[alias]['resources'][name]
            if row.get('imports'):
                converted['source']['imports'] = deepcopy(row['imports'])
            if row.get('packaging_revision'):
                converted['packaging_revision'] = row['packaging_revision']
            self.machine['catalog_roots'][alias] = str(self.output / 'source')
        require(len(repos) <= 1, 'Multiple catalog repositories require separate fixed candidate source roots')
        return next(iter(repos), 'owner/configuration-resources')

    def build(self):
        active = parse_yaml(self.asset('agents.yaml'))
        manifest = parse_yaml(self.asset('manifest.yaml')) if (self.root / 'manifest.yaml').exists() else {}
        for old_host, declaration in active['agents'].items():
            host = 'claude-code' if old_host == 'claude' else old_host
            self.agents[host] = {'schema_version': 1, 'agent_type': host, 'settings': {}, 'resources': {}}
            require(not set(declaration) - {'plugins', 'config'}, 'Additional legacy Agent resources need an explicit adapter')
            if 'config' in declaration:
                self.settings(parse_native(host, self.asset(declaration['config']['source'])), host, primary=True)
        if 'claude-code' in self.agents:
            for row in manifest.get('settings', {}).values():
                self.settings(parse_json(self.asset(row['source'])), 'claude-code')
            for row in manifest.get('permissions', {}).values():
                self.settings(parse_json(self.asset(row['source'])), 'claude-code')
            self.settings({'env': manifest.get('env', {}).get('fixed', {})}, 'claude-code')
        repo = self.plugins(active)
        alias = 'configuration-resources'
        self.machine['catalog_roots'][alias] = str(self.output / 'source')
        require(not manifest.get('rules') and not manifest.get('agents'), 'Legacy role/rule declarations need an explicit source adapter')
        for name in manifest.get('skills', {}):
            require(name == 'claude-config', 'Legacy independent skill needs a selected source adapter')
            self.decision(['skills', name], 'preserve', '框架技能通过 Machine 的工具运行入口管理，不重复安装为普通技能。', after='claude-config installed command')
        items = manifest.get('memory', {}).get('items', {})
        if items:
            require('claude-code' in self.agents, 'Memory consumer is not declared')
            files = {name + '.md': self.asset(row['source']) for name, row in items.items()}
            files['global.md'] = ('\n'.join('@./' + name + '.md' for name in items) + '\n').encode()
            self.resource(alias, 'global-instructions', 'instruction', files, {name: 0o600 for name in files}, {'claude-code': ('native', 'global.md')})
            self.agents['claude-code']['resources']['instructions'] = {'global-instructions@' + alias: {}}
            self.decision(['resources', 'instructions'], 'overwrite', '按原声明顺序引用三份正文，形成唯一指令入口；不自动共享给 Codex。', before=list(items), after='global-instructions@' + alias)
        hooks = manifest.get('hooks', {})
        require(not set(hooks) - {'statusline'}, 'Other legacy Hooks require explicit handler adapters')
        if hooks:
            require('claude-hud@claude-hud' in self.agents.get('claude-code', {}).get('resources', {}).get('plugins', {}), 'Statusline requires explicitly selected HUD')
            hud = self.catalogs['claude-hud']['resources']['claude-hud']
            base = 'source/catalogs/claude-hud/' + hud['source']['path'] + '/'
            bun, node = shutil.which('bun'), shutil.which('node')
            runtime, path = (bun, 'src/index.ts') if bun and base + 'src/index.ts' in self.files else (node, 'dist/index.js')
            require(runtime and base + path in self.files, 'HUD runtime or tracked entry is missing')
            self.machine['bindings']['executables']['hud-runtime'] = str(Path(runtime).absolute())
            profile = self.agents['claude-code']
            profile['settings'].setdefault('statusLine', {})['type'] = 'command'
            profile['bindings'] = {'/settings/statusLine/command': {'kind': 'command', 'executable': 'executables.hud-runtime',
                                   'args': [{'resource': 'claude-hud@claude-hud', 'path': path}]}}
            self.report['migration_conversion']['review_required'].append({'code': 'statusline_fixed_source',
                'reason': 'Replace latest-native-cache scanning with the explicitly selected fixed HUD package and Machine runtime; business output still needs validation'})
        for key, row in manifest.get('archives', {}).get('claude_desktop', {}).items():
            require(key == 'mac', 'Only the current Mac Desktop source has a verified adapter')
            value = parse_json(self.asset(row['source']))
            settings = projection(value, native_paths('claude-desktop'))
            validate_app('claude-desktop', settings)
            self.apps['claude-desktop'] = {'schema_version': 1, 'app': 'claude-desktop', 'settings': settings}
        for app, path in self.app_profiles.items():
            require(app in ('codex-app', 'claude-desktop'), 'Unknown explicit App Profile input')
            value = parse_yaml(self.read(Path(path)))
            validate_profile(value, kind='app', expected=app)
            self.apps[app] = value
            self.decision(['profiles', app], 'add', '采用显式提供的新版 App 导出文件；不扫描或复制原生账号和缓存。', after=str(path))
        for host, profile in self.agents.items():
            validate_settings(host, profile['settings']); validate_profile(profile)
            name = f'profiles/agents/{host}/current.yaml'
            executable = self.executables.get(host) or shutil.which('claude' if host == 'claude-code' else 'codex')
            require(executable, 'Agent executable requires a Machine binding')
            self.machine['agents'][host] = {'profile': name, 'binding': {'executable': str(Path(executable).absolute()),
                'config_root': str(Path.home() / ('.claude' if host == 'claude-code' else '.codex'))}}
            self.add('data/' + name, yaml.safe_dump(profile, sort_keys=False, allow_unicode=True).encode())
            self.report['migration_conversion']['profiles'].append(name)
        for app, profile in self.apps.items():
            validate_profile(profile, kind='app')
            name = f'profiles/apps/{app}/current.yaml'
            application = self.applications.get(app) or ('/Applications/Claude.app' if app == 'claude-desktop' else '/Applications/ChatGPT.app')
            data_root = str(Path.home() / 'Library/Application Support/Claude') if app == 'claude-desktop' else str(Path.home() / '.codex')
            self.machine['apps'][app] = {'profile': name, 'binding': {'application': str(Path(application).absolute()),
                'data_root': data_root}}
            if app == 'codex-app':
                self.machine['apps'][app]['binding']['backend'] = str(Path(application) / 'Contents/Resources/codex-cli/bin/codex')
            self.add('data/' + name, yaml.safe_dump(profile, sort_keys=False, allow_unicode=True).encode())
            self.report['migration_conversion']['profiles'].append(name)
        for name, catalog in self.catalogs.items():
            validate_catalog(catalog)
            self.add('source/catalogs/' + name + '/catalog.json', encoded(catalog))
        self.repo = repo
        retained = {'purpose': 'Review metadata only; never a runtime legacy input',
                    'repository_metadata': manifest.get('metadata', {}), 'platform_names': manifest.get('platforms', {}),
                    'native_auth_and_local_environment': manifest.get('env', {}).get('expected', []),
                    'excluded_native_paths': manifest.get('exclude', []),
                    'external_setup_metadata': {key: {field: row[field] for field in ('source', 'url', 'type', 'strategy', 'description') if field in row}
                                                for key, row in manifest.get('external', {}).items()}}
        self.add('review/retained-boundaries.json', encoded(retained))
        self.report['deployment_blockers'] = [{'code': 'candidate_not_published', 'reason': 'Candidate source baseline needs review and formal publication'},
             {'code': 'native_inventory_adoption_required', 'reason': 'Verify existing registration identities and managed ownership before unified cutover; do not duplicate existing installs'},
             {'code': 'framework_not_switched', 'reason': 'Reviewed framework source must be installed only after the complete candidate is approved'}]
        self.report['scope'] = [['profiles', name] for name in (*self.agents, *self.apps)]
        self.report['ready'] = True
        self.report['migration_conversion']['planned_file_count'] = len(self.files) + 2
        self.report['plan_id'] = fingerprint({'inputs': self.snapshots, 'package_members': self.package_members, 'links': self.links, 'machine': self.machine,
             'files': {name: {'sha256': hashlib.sha256(content).hexdigest(), 'mode': self.modes[name]} for name, content in self.files.items()},
             'framework': {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in Path(__file__).parent.glob('*.py')}})
        self.report['file_snapshots'] = deepcopy(self.snapshots)
        return self

    def check_sources(self):
        require(all(file_fingerprint(Path(path)) == snapshot for path, snapshot in self.snapshots.items()), 'Conversion source changed after preview')
        require(all(str(Path(path).resolve()) == expected for path, expected in self.links.items()), 'Source symlink changed after preview')
        for root, expected in self.package_members.items():
            actual = sorted(p.relative_to(root).as_posix() for p in Path(root).rglob('*') if (p.is_file() or p.is_symlink())
                    and not set(p.relative_to(root).parts) & {'.git', '__pycache__', 'node_modules'}
                    and p.name not in ('.DS_Store', '.in_use', '.orphaned_at'))
            require(actual == expected, 'Source package membership changed after preview')

    def write(self):
        self.check_sources()
        require(not self.output.exists(), 'Candidate output appeared after planning')
        self.output.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='.migration-candidate-', dir=self.output.parent) as directory:
            stage = Path(directory) / 'candidate';stage.mkdir(mode=0o700)
            for relative, content in self.files.items():
                path = stage / relative;path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content);path.chmod(self.modes[relative])
            source = stage / 'source'
            def git(*args):
                env = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
                p = subprocess.run(['git', '-c', 'core.hooksPath=/dev/null', '-C', str(source), *args], env=env, capture_output=True, timeout=30)
                require(p.returncode == 0, 'Cannot prepare the local fixed candidate source')
                return p.stdout.decode().strip()
            git('init', '-q');git('config', 'user.name', 'Local migration candidate');git('config', 'user.email', 'migration@local.invalid')
            git('remote', 'add', 'origin', 'https://github.com/' + self.repo + '.git')
            # These are explicitly reviewed snapshots, including published
            # dist/assets that an upstream development .gitignore may omit.
            git('add', '--force', '.');git('commit', '-qm', 'Unpublished configuration migration candidate')
            commit = git('rev-parse', 'HEAD')
            catalogs = {'schema_version': 1, 'catalogs': {name: {'repo': self.repo, 'commit': commit, 'path': 'catalogs/' + name} for name in self.catalogs}}
            validate_catalogs(catalogs);validate_machine(self.machine)
            for relative, value in [('data/catalogs.yaml', catalogs), ('machine.proposed.yaml', self.machine)]:
                path = stage / relative;path.write_text(yaml.safe_dump(value, sort_keys=False, allow_unicode=True));path.chmod(0o600)
            self.check_sources()
            require(not self.output.exists(), 'Candidate output appeared before publication')
            stage.rename(self.output)
        self.report['migration_conversion']['written'] = True
        self.report['migration_conversion']['source_commit'] = commit
        self.report['artifacts'] = {'candidate_root': str(self.output), 'candidate_data': str(self.output / 'data'),
                                    'candidate_machine': str(self.output / 'machine.proposed.yaml')}
        self.report['results'] = [{'status': 'applied', 'owner': 'migration-candidate', 'path': str(self.output),
                                   'reason': 'Local candidate files written; active data and native state are unchanged'}]
        return self.report


def conversion(data_root, output_root, catalog_roots, *, write=False, executables=None, applications=None, app_profiles=None, expect_plan_id=None):
    candidate = Conversion(data_root, output_root, catalog_roots, executables=executables, applications=applications, app_profiles=app_profiles).build()
    require(expect_plan_id is None or expect_plan_id == candidate.report['plan_id'], 'Conversion preview changed; review the new plan')
    return candidate.write() if write else candidate.report
