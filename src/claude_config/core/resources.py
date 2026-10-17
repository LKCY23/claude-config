"""Pinned resource bodies, static native adapters and typed command bindings."""
from copy import deepcopy
import hashlib
import os
from pathlib import Path
import re
import subprocess
import tempfile

import tomlkit
import yaml

from .bindings import Bindings
from .catalog import CatalogResolver
from .merge import fingerprint
from .native import parse_json, parse_yaml, put, snapshot_read
from .registrations import HOOK_OPTIONS, MCP_OPTIONS, Registrations, validate_registration
from .native_plugins import PluginPlanner, validate_receipt
from .skill_controls import invocation_controls, register_skill_control, validate_skill_control
from .agent_options import agent_options, definition_options
from .resource_conflicts import ResourceConflicts, validate_review
from .resource_references import package_references, instruction_body
from .schema import NAME, REF, RESOURCE_KINDS, SchemaError, closed, pointer, require, within
from .transaction import FileChange, file_fingerprint

START, END = '<!-- claude-config:begin -->', '<!-- claude-config:end -->'


def tree_digest(files, modes):
    return fingerprint({name: {'sha256': hashlib.sha256(data).hexdigest(), 'executable': bool(modes[name] & 0o111)} for name, data in files.items()})


def frontmatter(content):
    text = content.decode()
    match = re.match(r'\A---\r?\n(.*?)\r?\n---(?:\r?\n|$)', text, re.S)
    require(match is not None, 'Native Markdown entry requires frontmatter')
    return parse_yaml(match[1].encode()), text[match.end():], match


def no_secrets(value):
    if isinstance(value, dict):
        for key, child in value.items():
            require(not re.search(r'(?:^|_)(?:api_key|apikey|password|auth_token|access_token|refresh_token|client_secret|authorization|bearer_token)$', key.lower().replace('-', '_')),
                    'Resource definition must use logical credential bindings')
            no_secrets(child)
    elif isinstance(value, list):
        for child in value:
            no_secrets(child)


def check_execpolicy(executable, files, entry):
    with tempfile.TemporaryDirectory(prefix='resource-policy-check-') as directory:
        root = Path(directory)
        for relative, content in files.items():
            path = within(root, relative, must_exist=False)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        env = {'PATH': os.environ.get('PATH', ''), 'HOME': directory, 'CODEX_HOME': directory, 'TMPDIR': directory}
        result = subprocess.run([str(executable), 'execpolicy', 'check', '--rules', str(root / entry), '__claude_config_syntax_check__'],
                                cwd=root, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)
        return result.returncode == 0


def render_package(package, agent, options=None):
    kind = package.item['kind']
    options = package.selection.get('options', {}) if options is None else options
    allowed = {'plugin': ('enabled',), 'skill': ('control',), 'mcp': (*MCP_OPTIONS[agent], 'entry'),
               'hook': HOOK_OPTIONS if package.item['compatibility'][agent]['mode'] != 'status-line' else (),
               'instruction': (), 'rule': (), 'agent': ('model', 'model_reasoning_effort') if agent == 'codex' else ('model',)}[kind]
    closed(options, allowed, (), 'resource options ' + package.ref)
    if kind == 'plugin' and 'enabled' in options:
        require(type(options['enabled']) is bool, 'Plugin enabled must be Boolean')
    files = dict(package.files)
    identity = package.ref.split('@')[0]
    if kind == 'skill':
        require(package.root.is_dir() and package.entry == 'SKILL.md', 'Independent skill must select a complete directory with root SKILL.md')
        meta, body, _ = frontmatter(files[package.entry])
        no_secrets(meta)
        require(all(isinstance(meta.get(key), str) and meta[key] for key in ('name', 'description')), 'Skill requires name and description')
        identity = meta['name']
        control = options.get('control', {})
        allowed_control = ('allow_implicit_invocation', 'enabled') if agent == 'codex' else ('enabled', 'disable-model-invocation', 'user-invocable')
        closed(control, allowed_control, (), 'native skill control')
        require(all(type(value) is bool for value in control.values()), 'Skill controls must be Boolean')
        claude_control = {key: value for key, value in control.items() if key != 'enabled'}
        if claude_control and agent == 'claude-code':
            meta.update(claude_control)
            files[package.entry] = ('---\n' + yaml.safe_dump(meta, sort_keys=False, allow_unicode=True) + '---\n' + body).encode()
        invocation = {key: value for key, value in control.items() if key != 'enabled'}
        if invocation and agent == 'codex':
            relative = str(Path(package.entry).parent / 'agents/openai.yaml')
            data = parse_yaml(files[relative]) if relative in files else {}
            require(isinstance(data.get('policy', {}), dict), 'Skill invocation policy must be an object')
            data.setdefault('policy', {}).update(invocation)
            files[relative] = yaml.safe_dump(data, sort_keys=False, allow_unicode=True).encode()
    elif kind == 'agent':
        if agent == 'claude-code':
            require(Path(package.entry).suffix == '.md', 'Claude agent requires Markdown')
            meta, body, _ = frontmatter(files[package.entry])
            require(all(isinstance(meta.get(key), str) and meta[key] for key in ('name', 'description')), 'Agent requires name and description')
            require(not meta.get('mcpServers') and not meta.get('hooks'), 'Agent-local MCP/Hook adapter is not connected yet')
            no_secrets(meta)
            identity = meta['name']
            for key, value in options.items():
                require(isinstance(value, str) and value, 'Agent model override must be a string')
                meta[key] = value
            if options:
                files[package.entry] = ('---\n' + yaml.safe_dump(meta, sort_keys=False, allow_unicode=True) + '---\n' + body).encode()
        else:
            require(Path(package.entry).suffix == '.toml', 'Codex agent requires TOML')
            document = tomlkit.parse(files[package.entry].decode())
            require(all(isinstance(document.get(key), str) and document[key] for key in ('name', 'description', 'developer_instructions')),
                    'Codex agent requires name, description and developer_instructions')
            no_secrets(document.unwrap())
            require(not any(key in document for key in ('mcp_servers', 'hooks', 'skills', 'model_instructions_file')), 'Agent local-path/resource adapters are not connected yet')
            identity = document['name']
            for key, value in options.items():
                require(isinstance(value, str) and value, 'Agent model/reasoning override must be a string')
                document[key] = value
            if options:
                files[package.entry] = tomlkit.dumps(document).encode()
        definition_options(agent, files[package.entry])
    elif kind == 'rule':
        suffix = '.md' if agent == 'claude-code' else '.rules'
        require(Path(package.entry).suffix == suffix, 'Rule entry has the wrong native format')
        text = files[package.entry].decode()
        if agent == 'claude-code' and text.startswith('---'):
            meta, _, _ = frontmatter(files[package.entry])
            no_secrets(meta)
            if 'paths' in meta:
                require(isinstance(meta['paths'], list) and all(isinstance(path, str) for path in meta['paths']), 'Rule paths must be native glob strings')
    elif kind == 'instruction':
        content = files[package.entry].decode()
        require(START not in content and END not in content, 'Instruction body contains reserved ownership markers')
    references = package_references(files, package.entry, native_imports=kind == 'instruction' and agent == 'claude-code',
                                    sibling_files=package.sibling_files if kind == 'skill' else frozenset())
    require(isinstance(identity, str) and NAME.fullmatch(identity), 'Unsafe native resource identity')
    modes = {name: package.modes.get(name, 0o600) for name in files}
    digest = tree_digest(files, modes)
    if kind == 'mcp' and package.item['compatibility'][agent].get('entries'):
        digest = fingerprint({'files': digest, 'entry': package.entry})
    return {'package': package, 'files': files, 'modes': modes, 'identity': identity,
            'rendered_fingerprint': digest, 'references': references}


def validate_resource_state(records):
    require(isinstance(records, dict), 'Resource state must be a mapping')
    for ref, record in records.items():
        require(isinstance(ref, str) and REF.fullmatch(ref), 'Invalid resource state identity')
        closed(record, ('group', 'kind', 'active', 'catalog_commit', 'source_fingerprint', 'rendered_fingerprint', 'cache', 'files', 'native'),
               ('group', 'kind', 'active', 'catalog_commit', 'source_fingerprint', 'rendered_fingerprint', 'cache', 'files', 'native'), 'resource state')
        require(type(record['active']) is bool and isinstance(record['files'], dict) and isinstance(record['native'], dict), 'Invalid resource state shape')
        require(record['group'] in RESOURCE_KINDS and record['kind'] == RESOURCE_KINDS[record['group']], 'Resource state kind and group disagree')
        for key, size in (('catalog_commit', 40), ('source_fingerprint', 64), ('rendered_fingerprint', 64)):
            value = record[key]
            require(isinstance(value, str) and len(value) == size and all(char in '0123456789abcdef' for char in value), 'Invalid resource state pin')
        require(isinstance(record['cache'], str) and Path(record['cache']).is_absolute(), 'Resource cache must be an absolute path')
        for path, expected in record['files'].items():
            require(Path(path).is_absolute(), 'Resource file identity must be absolute')
            closed(expected, ('exists', 'sha256', 'mode'), ('exists', 'sha256', 'mode'), 'resource file snapshot')
            require(expected['exists'] is True and type(expected['mode']) is int and 0 <= expected['mode'] <= 0o777
                    and isinstance(expected['sha256'], str) and len(expected['sha256']) == 64 and all(char in '0123456789abcdef' for char in expected['sha256']),
                    'Invalid resource file snapshot')
        closed(record['native'], ('identity', 'root', 'global_block', 'registration', 'plugin', 'skill_control', 'review', 'mcp_entry'), (), 'native resource identity')
        if 'mcp_entry' in record['native']:
            from .schema import relative
            require(record['kind'] == 'mcp', 'MCP entry state on another resource kind')
            relative(record['native']['mcp_entry'], 'managed MCP entry')
        if 'review' in record['native']:
            validate_review(record['native']['review'])
        if 'skill_control' in record['native']:
            validate_skill_control(record['native']['skill_control'])
            require(record['kind'] == 'skill', 'Skill activation baseline on another resource kind')
        if 'plugin' in record['native']:
            validate_receipt(record['native']['plugin'])
            require(record['kind'] == 'plugin', 'Plugin receipt on another resource kind')
        if 'registration' in record['native']:
            validate_registration(record['native']['registration'])
            require(record['native']['registration']['kind'] == record['kind'], 'Resource registration kind disagrees with state')
        if 'global_block' in record['native']:
            block = record['native']['global_block']
            closed(block, ('path', 'sha256'), ('path', 'sha256'), 'instruction block snapshot')
            require(Path(block['path']).is_absolute() and isinstance(block['sha256'], str) and re.fullmatch(r'[0-9a-f]{64}', block['sha256']), 'Invalid instruction snapshot')


class ResourcePlan:
    def __init__(self, manager, state, profile, native=None, original=b'', run_policy=None, resolutions=None):
        self.manager, self.profile = manager, profile
        self.run_policy = run_policy or {}
        self.resolver = CatalogResolver(manager)
        self.binder = Bindings(manager)
        self.previous = state.get('resources', {})
        self.records = deepcopy(self.previous)
        for record in self.records.values():
            record['active'] = False
        self.rendered, self.changes, self.decisions, self.checks = {}, [], [], []
        self.generated = {}
        self.directories = set()
        self.targets = {}
        self.registrations = Registrations(self, native or {}, original) if native is not None else None
        self.plugins = PluginPlanner(self)
        self.conflicts = ResourceConflicts(self, resolutions)
        self.processed = set()

    def _decision(self, ref, path, before, after, action, reason):
        package = self.rendered[ref]['package']
        self.decisions.append({'path': ['resources', package.group, ref, *path], 'source': {'kind': 'catalog-resource', 'ref': ref, 'commit': package.commit},
                               'target': {'kind': 'native-resource', 'agent_type': self.manager.agent},
                               'before_exists': before is not None, 'before': before, 'incoming_exists': after is not None, 'incoming': after,
                               'after_exists': (before if action == 'conflict' else after) is not None,
                               'after': before if action == 'conflict' else after, 'action': action,
                               'category': 'unresolved_conflict' if action == 'conflict' else 'unchanged' if action == 'unchanged' else 'direct_add' if action == 'add' else 'policy_merge',
                               'policy': 'known-resource-replacement', 'policy_origin': 'resource_adapter', 'reason': reason,
                               'alternatives': ['retain_native', 'replan'], 'check_status': 'blocked' if action == 'conflict' else 'passed'})

    def _block(self, ref, code, reason):
        self.checks.append({'name': 'resource_preflight', 'path': ['resources', self.rendered[ref]['package'].group, ref],
                            'status': 'blocked', 'code': code, 'reason': reason})

    def _target(self, root, relative):
        target = within(root, relative, must_exist=False)
        require(not any(target.is_relative_to(repository) for repository in self.resolver.repositories), 'Resource destination overlaps an immutable source checkout')
        self.manager.references[(Path(root) / relative).absolute()] = target
        return target

    def _write(self, ref, target, content, mode):
        require(target not in self.targets or self.targets[target] == ref, 'Multiple resources own the same native file')
        self.targets[target] = ref
        current = file_fingerprint(target)
        if target.exists():
            _, current = snapshot_read(target)
        previous = self.previous.get(ref, {}).get('files', {}).get(str(target))
        desired = {'exists': True, 'sha256': hashlib.sha256(content).hexdigest(), 'mode': mode}
        if previous is None and current['exists']:
            self._decision(ref, [str(target)], current, desired, 'conflict', 'Existing resource file has unknown management ownership')
            self._block(ref, 'unmanaged_resource_collision', 'Existing resource file is not managed by this resource')
            return
        if previous is not None and current != previous and ref not in self.conflicts.restore:
            self._decision(ref, [str(target)], current, desired, 'conflict', 'Known resource file was externally modified or removed')
            self._block(ref, 'resource_drift', 'Resource content or mode differs from the managed baseline')
            return
        action = 'unchanged' if current == desired else 'add' if not current['exists'] else 'replace_content'
        self._decision(ref, [str(target)], current if current['exists'] else None, desired, action,
                       'Exact selected resource body; unrelated resources are retained')
        self.changes.append(FileChange(target, content, 'resource:' + ref, lambda data: None, current, mode))
        self.records[ref]['files'][str(target)] = desired

    def _directory(self, ref, root, files, modes):
        self.directories.add(root)
        previous = self.previous.get(ref, {}).get('files', {})
        for path, expected in previous.items():
            if Path(path).is_relative_to(root) and file_fingerprint(path) != expected and ref not in self.conflicts.restore:
                self._block(ref, 'resource_drift', 'Previously owned resource file changed or disappeared')
                return
        existing = set()
        if root.exists():
            require(root.is_dir() and not root.is_symlink(), 'Resource directory cannot be a symlink or non-directory')
            for path in root.rglob('*'):
                if any(part in ('__pycache__', '.DS_Store', '.in_use') for part in path.relative_to(root).parts):
                    continue
                require(not path.is_symlink(), 'Deployed resource contains an unexpected symlink')
                if path.is_file():
                    existing.add(path.resolve())
        wanted = {self._target(root, relative): (content, modes[relative]) for relative, content in files.items()}
        for path in existing - set(wanted):
            expected = previous.get(str(path))
            if expected is None:
                self._block(ref, 'unmanaged_resource_collision', 'Resource directory contains unknown extra files')
                continue
            actual = file_fingerprint(path)
            if actual != expected and ref not in self.conflicts.restore:
                self._block(ref, 'resource_drift', 'Obsolete resource file was externally changed')
                continue
            self._decision(ref, [str(path)], actual, None, 'remove_obsolete_resource_file', 'Explicit fixed package update removes a previously owned file')
            self.changes.append(FileChange(path, b'', 'resource:' + ref, lambda data: None, actual, delete=True))
            self.records[ref]['files'].pop(str(path), None)
        for path, (content, mode) in wanted.items():
            self._write(ref, path, content, mode)

    def _instructions(self, ref, item):
        path = self._target(self.manager.native_path.parent, 'CLAUDE.md' if self.manager.agent == 'claude-code' else 'AGENTS.md')
        current, expected = snapshot_read(path)
        self.manager.sources[path] = expected
        text = current.decode()
        require(text.count(START) == text.count(END) and text.count(START) <= 1, 'Malformed native instruction ownership markers')
        before = ''
        if START in text:
            before = text[text.index(START):text.index(END) + len(END)]
            owner = next((key for key, value in self.previous.items() if value['native'].get('global_block')), None)
            if owner is None or self.previous[owner]['native']['global_block']['path'] != str(path) or (
                    hashlib.sha256(before.encode()).hexdigest() != self.previous[owner]['native']['global_block']['sha256'] and ref not in self.conflicts.restore):
                self._block(ref, 'instruction_drift', 'Existing instruction block has unknown ownership or external changes')
                return
        content = instruction_body(item, self.manager.agent)
        block = START + '\n' + content.rstrip() + '\n' + END
        result = text.replace(before, block, 1) if before else text.rstrip() + ('\n\n' if text.strip() else '') + block + '\n'
        for record in self.records.values():
            record['native'].pop('global_block', None)
        self.records[ref]['native']['global_block'] = {'path': str(path), 'sha256': hashlib.sha256(block.encode()).hexdigest()}
        self._decision(ref, ['instruction_block'], {'sha256': hashlib.sha256(before.encode()).hexdigest()} if before else None,
                       self.records[ref]['native']['global_block'], 'unchanged' if before == block else 'replace_instruction_block' if before else 'add',
                       'Only one managed block is replaced; surrounding native text is retained')
        if item['references']['count']:
            self._decision(ref, ['reference_layout'],
                           {'cache': self.previous[owner]['cache'], 'block_sha256': hashlib.sha256(before.encode()).hexdigest()} if before else None,
                           {'cache': str(item['cache']), 'entry': item['package'].entry,
                            'mode': 'claude-native-import' if self.manager.agent == 'claude-code' else 'codex-local-links',
                            'reference_count': item['references']['count'], 'block_sha256': hashlib.sha256(block.encode()).hexdigest()},
                           'unchanged' if before == block else 'replace_reference_layout' if before else 'add',
                           'Preserve fixed package bytes; bind Claude imports or relocate Codex entry links to that package, without expanding or merging the body')
        self.changes.append(FileChange(path, current if result == text else result.encode(), 'resource:' + ref,
                                      lambda data: require(data.decode().count(START) == data.decode().count(END) == 1, 'Invalid resulting instruction block'), expected))

    def _retire_identity(self, ref, item):
        previous = self.previous.get(ref)
        if previous is None or previous['native'].get('identity') in (None, item['identity']):
            return
        kind = item['package'].item['kind']
        if kind == 'skill':
            old = Path(previous['native']['root'])
            # Moving a Machine binding does not implicitly uninstall the old home.
            current_root = Path(self.records[ref]['native']['root'])
            if old.parent != current_root.parent:
                return
            obsolete = [Path(path) for path in previous['files'] if Path(path).is_relative_to(old)]
            self.directories.add(old)
        else:
            extension = '.md' if self.manager.agent == 'claude-code' else '.toml' if kind == 'agent' else '.rules'
            obsolete = [self._target(self.manager.native_path.parent, ('agents' if kind == 'agent' else 'rules') + '/' + previous['native']['identity'] + extension)]
        for path in obsolete:
            expected = previous['files'].get(str(path))
            current = file_fingerprint(path)
            if expected is None or current != expected and ref not in self.conflicts.restore:
                self._block(ref, 'resource_drift', 'Previous native identity was externally changed')
                continue
            self._decision(ref, [str(path)], current, None, 'retire_previous_native_identity', 'Fixed resource update changes native identity; only previous owned files are retired')
            self.changes.append(FileChange(path, b'', 'resource:' + ref, lambda data: None, current, delete=True))
            self.records[ref]['files'].pop(str(path), None)

    def prepare(self, deploy=True, binding_scope=None):
        bindings = {key: value for key, value in self.profile.get('bindings', {}).items() if binding_scope is None or key in binding_scope}
        selected = None if deploy else {arg['resource'] for entry in bindings.values() for arg in entry['args'] if isinstance(arg, dict)}
        packages = self.resolver.resolve(self.profile, selected=selected)
        deferred = set()
        for ref, package in packages.items():
            item = render_package(package, self.manager.agent)
            identifier, alias = ref.split('@')
            item['cache'] = self._target(self.manager.native_path.parent, '.claude-config-resources/' + alias + '/' + identifier + '/' + item['rendered_fingerprint'])
            self.rendered[ref] = item
            old = self.previous.get(ref)
            require(old is None or old['kind'] == package.item['kind'], 'Registered resource kind changed under the same identity')
            self.records[ref] = {'group': package.group, 'kind': package.item['kind'], 'active': True, 'catalog_commit': package.commit,
                                 'source_fingerprint': package.source_fingerprint, 'rendered_fingerprint': item['rendered_fingerprint'],
                                 'cache': str(item['cache']), 'files': deepcopy(old.get('files', {})) if old else {}, 'native': {}}
            slots = self.binder.slots(package)
            item['slots'] = slots
            self.checks.append({'name': 'fixed_resource_source', 'ref': ref, 'status': 'passed', 'commit': package.commit, 'source_fingerprint': package.source_fingerprint,
                                'level': 'pinned-content'})
            self.checks.append({'name': 'package_references', 'ref': ref, 'status': 'passed', 'level': 'tracked-package-layout',
                                'reference_count': item['references']['count'], 'reason': 'Package-local Markdown targets and native import graph checked'})
            validation = package.item['compatibility'][self.manager.agent]['validation']
            self.checks.append({'name': 'resource_runtime', 'ref': ref, 'status': 'not_verified', 'reason': 'This plan does not run resource business logic or grant native trust',
                                'published_validation': validation})
            if not deploy:
                continue
            if self.conflicts.inspect(ref, item, packages):
                deferred.add(ref)
                continue
            self.processed.add(ref)
            kind = package.item['kind']
            skill_root = None
            if kind == 'skill':
                if self.manager.agent == 'codex':
                    require('install_root' in slots and Path(slots['install_root']).parts[-2:] == ('.agents', 'skills'),
                            'Codex skill requires an explicit install_root path binding ending in .agents/skills')
                    root = Path(slots['install_root'])
                else:
                    root = self.manager.native_path.parent / 'skills'
                skill_root = self._target(root, item['identity'])
                item = invocation_controls(self, ref, item, skill_root)
                item['cache'] = self._target(self.manager.native_path.parent, '.claude-config-resources/' + alias + '/' + identifier + '/' + item['rendered_fingerprint'])
                self.rendered[ref] = item
                self.records[ref]['cache'] = str(item['cache'])
                self.records[ref]['rendered_fingerprint'] = item['rendered_fingerprint']
            if kind == 'agent':
                item = agent_options(self, ref, item)
                item['cache'] = self._target(self.manager.native_path.parent, '.claude-config-resources/' + alias + '/' + identifier + '/' + item['rendered_fingerprint'])
                self.rendered[ref] = item
                self.records[ref]['cache'] = str(item['cache'])
                self.records[ref]['rendered_fingerprint'] = item['rendered_fingerprint']
            if kind == 'plugin':
                self.plugins.add(ref, item)
                self._directory(ref, item['cache'], item['files'], item['modes'])
                continue
            self._directory(ref, item['cache'], item['files'], item['modes'])
            if kind == 'mcp':
                require(self.registrations is not None, 'Native registration context is required')
                self.registrations.add_mcp(ref, item)
            elif kind == 'hook' and package.item['compatibility'][self.manager.agent]['mode'] != 'status-line':
                require(self.registrations is not None, 'Native registration context is required')
                self.registrations.add_hooks(ref, item)
            elif kind == 'instruction':
                self._instructions(ref, item)
            elif kind in ('agent', 'rule'):
                if kind == 'rule' and self.manager.agent == 'codex':
                    executable = Path(self.manager.entry['binding']['executable']).resolve()
                    valid = check_execpolicy(executable, item['files'], package.entry)
                    self.checks.append({'name': 'native_rule_syntax', 'ref': ref, 'status': 'passed' if valid else 'blocked',
                                        'code': 'native_rule_rejected', 'reason': 'Native parser checks policy syntax without executing a command', 'level': 'native-parser'})
                extension = '.md' if self.manager.agent == 'claude-code' else '.toml' if kind == 'agent' else '.rules'
                folder = 'agents' if kind == 'agent' else 'rules'
                native = self._target(self.manager.native_path.parent, folder + '/' + item['identity'] + extension)
                self._write(ref, native, item['files'][package.entry], 0o600)
                self.records[ref]['native']['identity'] = item['identity']
                self._retire_identity(ref, item)
            elif kind == 'skill':
                self._directory(ref, skill_root, item['files'], item['modes'])
                self.records[ref]['native'].update(identity=item['identity'], root=str(skill_root))
                register_skill_control(self, ref, item, skill_root)
                self._retire_identity(ref, item)
        self.conflicts.check_kept_dependencies(packages, bindings)
        for destination, declaration in bindings.items():
            if any(isinstance(arg, dict) and arg['resource'] in deferred - self.conflicts.kept for arg in declaration['args']):
                self.checks.append({'name': 'resource_dependency', 'path': list(pointer(destination)), 'status': 'blocked',
                                    'code': 'unresolved_resource_dependency', 'reason': 'Bound command depends on an unresolved resource'})
                continue
            command = self.binder.command(destination, declaration, self.rendered)
            path = pointer(destination)[1:]
            put(self.generated, path, command)
        self.checks += self.binder.checks
        if deploy:
            self.plugins.finish()
            if self.registrations is not None:
                self.changes += self.registrations.changes()
            self.conflicts.freeze()
            for ref, previous in self.previous.items():
                if ref not in packages and previous['active']:
                    self.decisions.append({'path': ['resources', previous['group'], ref], 'source': {'kind': 'profile'},
                                           'target': {'kind': 'installed-resource'}, 'before_exists': True, 'before': {'active': True},
                                           'incoming_exists': False, 'after_exists': True, 'after': {'active': False, 'native_content': 'retained'},
                                           'action': 'residual', 'category': 'policy_merge', 'policy': 'absence', 'policy_origin': 'structural',
                                           'reason': 'Selection removed; installed content retained and ownership remembered for explicit reselection',
                                           'alternatives': ['explicit_uninstall', 'reselect'], 'check_status': 'passed'})
        return self
