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
from .registrations import ANTIGRAVITY_HOOK_OPTIONS, HOOK_OPTIONS, MCP_OPTIONS, Registrations, validate_registration
from .native_plugins import PluginPlanner, validate_receipt
from .resource_options import merge_options
from .skill_controls import invocation_controls, register_skill_control, validate_skill_control
from .agent_options import agent_options, definition_options
from .resource_conflicts import ResourceConflicts, validate_review
from .resource_references import package_references, instruction_body
from .schema import NAME, REF, RESOURCE_KINDS, SchemaError, closed, mapping, pointer, relative, require, within
from .transaction import FileChange, file_fingerprint

START, END = '<!-- claude-config:begin -->', '<!-- claude-config:end -->'
ANTIGRAVITY_RULE_TRIGGERS = ('always_on', 'model_decision', 'glob', 'manual')
ANTIGRAVITY_RULE_MAX_BYTES = 24000


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
               'hook': ANTIGRAVITY_HOOK_OPTIONS if agent == 'antigravity' else (HOOK_OPTIONS if package.item['compatibility'][agent]['mode'] != 'status-line' else ()),
               'instruction': (), 'rule': (), 'agent': ('model', 'model_reasoning_effort') if agent == 'codex' else ('model',)}[kind]
    closed(options, allowed, (), 'resource options ' + package.ref)
    if kind == 'plugin' and 'enabled' in options:
        require(type(options['enabled']) is bool, 'Plugin enabled must be Boolean')
    files = dict(package.files)
    identity = package.ref.split('@')[0]
    if kind == 'plugin' and agent == 'antigravity':
        require(package.root.is_dir() and package.entry in ('plugin.json', '.antigravity-plugin/plugin.json'),
                'Antigravity plugin requires a complete directory with plugin.json or .antigravity-plugin/plugin.json')
        manifest = parse_json(files[package.entry])
        no_secrets(manifest)
        name = manifest.get('name')
        require(isinstance(name, str) and bool(NAME.fullmatch(name)), 'Antigravity plugin manifest requires a safe name')
        if 'version' in manifest:
            require(isinstance(manifest['version'], str) and bool(manifest['version']), 'Antigravity plugin manifest version must be a non-empty string')
        if 'disabled' in manifest:
            require(type(manifest['disabled']) is bool, 'Antigravity plugin manifest disabled must be Boolean')
        identity = name
    elif kind == 'skill':
        require(package.root.is_dir() and package.entry == 'SKILL.md', 'Independent skill must select a complete directory with root SKILL.md')
        meta, body, _ = frontmatter(files[package.entry])
        no_secrets(meta)
        require(all(isinstance(meta.get(key), str) and meta[key] for key in ('name', 'description')), 'Skill requires name and description')
        identity = meta['name']
        control = options.get('control', {})
        require(not (agent == 'antigravity' and control),
                'Antigravity does not support per-skill controls; use Plugin enabled or retire-skills')
        allowed_control = () if agent == 'antigravity' else ('allow_implicit_invocation', 'enabled') if agent == 'codex' else ('enabled', 'disable-model-invocation', 'user-invocable')
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
        if agent in ('claude-code', 'antigravity'):
            require(Path(package.entry).suffix == '.md', ('Claude' if agent == 'claude-code' else 'Antigravity') + ' agent requires Markdown')
            meta, body, _ = frontmatter(files[package.entry])
            require(all(isinstance(meta.get(key), str) and meta[key] for key in ('name', 'description')), 'Agent requires name and description')
            if agent == 'antigravity':
                require('mcpServers' not in meta and 'hooks' not in meta and 'skills' not in meta,
                        'Agent-local MCP/Hook/Skill adapter is not connected yet')
                if 'subagent' in meta:
                    require(type(meta['subagent']) is bool, 'Antigravity agent subagent must be Boolean')
                if 'mainAgent' in meta:
                    require(type(meta['mainAgent']) is bool, 'Antigravity agent mainAgent must be Boolean')
                if 'tools' in meta:
                    require(isinstance(meta['tools'], list) and all(isinstance(tool, str) and bool(tool) for tool in meta['tools']),
                            'Antigravity agent tools must be nonempty strings')
            else:
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
        suffix = '.md' if agent in ('claude-code', 'antigravity') else '.rules'
        require(Path(package.entry).suffix == suffix, 'Rule entry has the wrong native format')
        text = files[package.entry].decode()
        if agent == 'claude-code' and text.startswith('---'):
            meta, _, _ = frontmatter(files[package.entry])
            no_secrets(meta)
            if 'paths' in meta:
                require(isinstance(meta['paths'], list) and all(isinstance(path, str) for path in meta['paths']), 'Rule paths must be native glob strings')
        elif agent == 'antigravity':
            meta, _, _ = frontmatter(files[package.entry])
            no_secrets(meta)
            closed(meta, ('trigger', 'description', 'globs'), ('trigger',), 'Antigravity rule frontmatter')
            require(isinstance(meta['trigger'], str) and meta['trigger'] in ANTIGRAVITY_RULE_TRIGGERS,
                    'Invalid Antigravity rule trigger')
            if 'description' in meta:
                require(isinstance(meta['description'], str) and bool(meta['description']),
                        'Antigravity rule description must be a nonempty string')
            if meta['trigger'] == 'model_decision':
                require('description' in meta, 'Antigravity model_decision rule requires description')
            if 'globs' in meta:
                globs = meta['globs']
                valid_globs = (isinstance(globs, str) and bool(globs)) or (
                    isinstance(globs, list) and bool(globs) and all(isinstance(item, str) and bool(item) for item in globs)
                )
                require(valid_globs, 'Antigravity rule globs must be a nonempty string or list of strings')
            if meta['trigger'] == 'glob':
                require('globs' in meta, 'Antigravity glob rule requires globs')
    elif kind == 'instruction':
        content = files[package.entry].decode()
        require(START not in content and END not in content, 'Instruction body contains reserved ownership markers')
        if agent == 'antigravity':
            require(re.match(r'\A---\r?\n.*?\r?\n---(?:\r?\n|$)', content, re.S) is None,
                    'Antigravity global instruction AGENTS.md must not contain YAML frontmatter')
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
        closed(record['native'], ('identity', 'root', 'global_block', 'registration', 'plugin', 'antigravity_plugin', 'skill_control', 'review', 'mcp_entry'), (), 'native resource identity')
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
        if 'antigravity_plugin' in record['native']:
            require(record['kind'] == 'plugin', 'Antigravity plugin state on another resource kind')
            plugin_state = record['native']['antigravity_plugin']
            closed(plugin_state, ('dir', 'root', 'entry', 'version', 'enabled', 'config_enabled', 'manifest_disabled'),
                   ('dir', 'root', 'entry'), 'antigravity plugin state')
            require(isinstance(plugin_state['dir'], str) and bool(NAME.fullmatch(plugin_state['dir'])), 'Invalid Antigravity plugin dir')
            require(isinstance(plugin_state['root'], str) and Path(plugin_state['root']).is_absolute(), 'Invalid Antigravity plugin root')
            require(plugin_state['entry'] in ('plugin.json', '.antigravity-plugin/plugin.json'), 'Invalid Antigravity plugin manifest entry')
            if 'version' in plugin_state:
                require(isinstance(plugin_state['version'], str) and bool(plugin_state['version']), 'Invalid Antigravity plugin version')
            for key in ('enabled', 'config_enabled', 'manifest_disabled'):
                if key in plugin_state:
                    require(type(plugin_state[key]) is bool, f'Invalid Antigravity plugin {key} flag')
        if 'registration' in record['native']:
            validate_registration(record['native']['registration'])
            require(record['native']['registration']['kind'] == record['kind'], 'Resource registration kind disagrees with state')
        if 'global_block' in record['native']:
            block = record['native']['global_block']
            closed(block, ('path', 'sha256'), ('path', 'sha256'), 'instruction block snapshot')
            require(Path(block['path']).is_absolute() and isinstance(block['sha256'], str) and re.fullmatch(r'[0-9a-f]{64}', block['sha256']), 'Invalid instruction snapshot')


class ResourcePlan:
    def __init__(self, manager, state, profile, native=None, original=b'', run_policy=None, resolutions=None, *, partial=False):
        self.manager, self.profile = manager, profile
        self.run_policy = run_policy or {}
        self.resolver = CatalogResolver(manager)
        self.binder = Bindings(manager)
        self.previous = state.get('resources', {})
        self.records = deepcopy(self.previous)
        self.partial = partial
        if not partial:
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
        self._observed_files = {}
        self._dir_fingerprints = {}
        self._resolved_dirs = {}
        self._scanned_files = {}

    def _scan_dir_files(self, root):
        """Return (resolved_files_set, rel_paths_list, has_symlink) via os.scandir without per-file Path/relative_to allocations."""
        root_key = Path(root)
        cached = self._scanned_files.get(root_key)
        if cached is not None:
            return cached
        resolved_root_str = str(root_key.resolve())
        existing = set()
        rel_files = []
        has_symlink = False
        stack = [('', str(root_key))]
        while stack:
            rel_prefix, current_dir = stack.pop()
            with os.scandir(current_dir) as it:
                for entry in it:
                    name = entry.name
                    if name in ('__pycache__', '.DS_Store', '.in_use'):
                        continue
                    rel_path = name if not rel_prefix else rel_prefix + '/' + name
                    if entry.is_symlink():
                        has_symlink = True
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        stack.append((rel_path, entry.path))
                    elif entry.is_file(follow_symlinks=False):
                        existing.add(Path(resolved_root_str + '/' + rel_path))
                        rel_files.append(rel_path)
        result = (frozenset(existing), tuple(rel_files), has_symlink)
        self._scanned_files[root_key] = result
        return result

    def _file_state(self, path):
        key = Path(path).absolute()
        cached = self._observed_files.get(key)
        if cached is not None:
            return dict(cached)
        _, snap = snapshot_read(key)
        self._observed_files[key] = snap
        return dict(snap)

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

    def _target(self, root, rel_path):
        relative(rel_path, 'source path')
        root_path = Path(root)
        resolved_root = self._resolved_dirs.get(root_path)
        if resolved_root is None:
            resolved_root = root_path.resolve()
            self._resolved_dirs[root_path] = resolved_root
        candidate = root_path / rel_path
        parent = candidate.parent
        resolved_parent = self._resolved_dirs.get(parent)
        if resolved_parent is None:
            resolved_parent = parent.resolve()
            self._resolved_dirs[parent] = resolved_parent
        fast = resolved_parent / candidate.name
        root_str = str(resolved_root)
        root_prefix = '/' if root_str == '/' else root_str + '/'
        fast_str = str(fast)
        if fast.is_symlink() or candidate.name in ('', '.', '..') or not (fast_str == root_str or fast_str.startswith(root_prefix)):
            target = within(root, rel_path, must_exist=False)
            target_str = str(target)
        else:
            target = fast
            target_str = fast_str
        repo_roots = tuple(dict.fromkeys(self.resolver.repositories))
        require(
            not any(target_str == str(r) or target_str.startswith('/' if str(r) == '/' else str(r) + '/') for r in repo_roots),
            'Resource destination overlaps an immutable source checkout',
        )
        self.manager.references[candidate.absolute()] = target
        return target

    def _write(self, ref, target, content, mode):
        require(target not in self.targets or self.targets[target] == ref, 'Multiple resources own the same native file')
        self.targets[target] = ref
        current = self._file_state(target)
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
            if Path(path).is_relative_to(root) and self._file_state(path) != expected and ref not in self.conflicts.restore:
                self._block(ref, 'resource_drift', 'Previously owned resource file changed or disappeared')
                return
        existing = set()
        if root.exists():
            require(root.is_dir() and not root.is_symlink(), 'Resource directory cannot be a symlink or non-directory')
            scanned_existing, _, has_symlink = self._scan_dir_files(root)
            require(not has_symlink, 'Deployed resource contains an unexpected symlink')
            existing = set(scanned_existing)
        wanted = {self._target(root, relative): (content, modes[relative]) for relative, content in files.items()}
        obsolete = []
        for path in existing - set(wanted):
            expected = previous.get(str(path))
            if expected is None:
                self._block(ref, 'unmanaged_resource_collision', 'Resource directory contains unknown extra files')
                continue
            actual = self._file_state(path)
            if actual != expected and ref not in self.conflicts.restore:
                self._block(ref, 'resource_drift', 'Obsolete resource file was externally changed')
                continue
            self._decision(ref, [str(path)], actual, None, 'remove_obsolete_resource_file', 'Explicit fixed package update removes a previously owned file')
            obsolete.append(FileChange(path, b'', 'resource:' + ref, lambda data: None, actual, delete=True))
            self.records[ref]['files'].pop(str(path), None)
        for path, (content, mode) in sorted(wanted.items(), key=lambda row: (row[0].name == 'SKILL.md', row[0] in existing, str(row[0]))):
            self._write(ref, path, content, mode)
        self.changes.extend(obsolete)

    def _antigravity_plugin(self, ref, item):
        manifest = parse_json(item['files'][item['package'].entry])
        plugin_dir = item['identity']
        plugin_root = self._target(self.manager.native_path.parent / 'plugins', plugin_dir)
        item['cache'] = plugin_root
        self.rendered[ref]['cache'] = plugin_root
        self.records[ref]['cache'] = str(plugin_root)
        previous = self.previous.get(ref)
        old_ag = previous.get('native', {}).get('antigravity_plugin') if previous else None
        if old_ag is not None and old_ag['dir'] != plugin_dir:
            self._block(ref, 'plugin_identity_changed', 'Antigravity plugin directory name changed under the same resource reference')
            return
        require(self.registrations is not None, 'Native registration context is required')
        config_path, document = self.registrations.document(self.manager.native_path)
        plugins_map = mapping(document.get('plugins', {}), 'antigravity plugins')
        current_entry = plugins_map.get(plugin_dir)
        if current_entry is not None:
            require(isinstance(current_entry, dict), 'Antigravity plugin config entry must be an object')
        current_enabled = current_entry.get('enabled') if current_entry is not None else None
        if current_enabled is not None:
            require(type(current_enabled) is bool, 'Antigravity plugin enabled must be Boolean')
        if old_ag is None and current_enabled is not None:
            self._block(ref, 'unmanaged_plugin_collision',
                        f"Antigravity config.json already tracks plugins.{plugin_dir}.enabled outside this resource")
            return
        if old_ag is not None and current_enabled != old_ag.get('config_enabled') and ref not in self.conflicts.restore:
            self._block(ref, 'plugin_drift',
                        f"Antigravity plugins.{plugin_dir}.enabled was modified outside managed state")
            return
        self._directory(ref, plugin_root, item['files'], item['modes'])
        selected_opts = item['package'].selection.get('options', {})
        incoming_opts = {'enabled': selected_opts['enabled']} if 'enabled' in selected_opts else {}
        if ref in self.conflicts.restore:
            current_opts = {'enabled': old_ag['config_enabled']} if old_ag and 'config_enabled' in old_ag else {}
        else:
            current_opts = {'enabled': current_enabled} if current_enabled is not None else {}
        baseline_opts = {'enabled': old_ag['config_enabled']} if old_ag and 'config_enabled' in old_ag else None
        merged = merge_options(self, ref, incoming_opts, current_opts, baseline=baseline_opts,
                               report_prefix=(str(config_path), 'plugins', plugin_dir))
        if 'enabled' in merged:
            document.setdefault('plugins', {})
            require(isinstance(document['plugins'], dict), 'Antigravity plugins config must be an object')
            entry = dict(current_entry or {})
            entry['enabled'] = merged['enabled']
            document['plugins'][plugin_dir] = entry
        elif 'plugins' in document and plugin_dir in document['plugins'] and isinstance(document['plugins'][plugin_dir], dict):
            entry = dict(document['plugins'][plugin_dir])
            entry.pop('enabled', None)
            if entry:
                document['plugins'][plugin_dir] = entry
            else:
                document['plugins'].pop(plugin_dir, None)
        if config_path == self.registrations.main_path:
            self.registrations.main_roots.add('plugins')
        manifest_disabled = manifest.get('disabled') if type(manifest.get('disabled')) is bool else None
        effective_enabled = merged['enabled'] if 'enabled' in merged else (not manifest_disabled if manifest_disabled is not None else True)
        state_entry = {
            'dir': plugin_dir,
            'root': str(plugin_root),
            'entry': item['package'].entry,
            'enabled': effective_enabled,
        }
        if isinstance(manifest.get('version'), str) and manifest['version']:
            state_entry['version'] = manifest['version']
        if 'enabled' in merged:
            state_entry['config_enabled'] = merged['enabled']
        if manifest_disabled is not None:
            state_entry['manifest_disabled'] = manifest_disabled
        self.records[ref]['native']['antigravity_plugin'] = state_entry
        self.checks.append({'name': 'antigravity_plugin_package', 'ref': ref, 'status': 'passed',
                            'level': 'documented-config-interface',
                            'reason': 'Plugin files deployed under plugins/<dir> and enablement recorded in config.json'})

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
                            'mode': 'claude-native-import' if self.manager.agent == 'claude-code' else 'antigravity-local-links' if self.manager.agent == 'antigravity' else 'codex-local-links',
                            'reference_count': item['references']['count'], 'block_sha256': hashlib.sha256(block.encode()).hexdigest()},
                           'unchanged' if before == block else 'replace_reference_layout' if before else 'add',
                           'Preserve fixed package bytes; bind Claude imports or relocate Codex/Antigravity entry links to that package, without expanding or merging the body')
        if self.manager.agent == 'antigravity' and len(result.encode('utf-8')) > ANTIGRAVITY_RULE_MAX_BYTES:
            self.checks.append({
                'name': 'antigravity_rule_size',
                'ref': ref,
                'status': 'warning',
                'code': 'rule_size_exceeds_24kb',
                'reason': 'Antigravity truncates instruction/rule files exceeding 24,000 bytes',
            })
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
            extension = '.md' if self.manager.agent in ('claude-code', 'antigravity') else '.toml' if kind == 'agent' else '.rules'
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

    def _scoped_siblings(self, packages):
        from .resource_references import LINK, prose
        from urllib.parse import unquote, urlsplit
        import posixpath
        for ref, package in packages.items():
            if package.item['kind'] != 'skill':
                continue
            identity = frontmatter(package.files[package.entry])[0]['name']
            root = (Path(self.binder.resolve(package.selection['bindings']['install_root'], 'path', allow_missing=True))
                    if self.manager.agent == 'codex' else self.manager.native_path.parent / 'skills').resolve()
            siblings = set(package.sibling_files)
            for other, record in self.previous.items():
                if other in packages or record['kind'] != 'skill' or Path(record['native']['root']).parent != root:
                    continue
                require(record['native']['identity'] != identity, 'Selected skill identity collides with another managed resource')
                prefix = '../' + record['native']['identity'] + '/'
                referenced = set()
                for name, data in package.files.items():
                    if not name.endswith('.md'):
                        continue
                    for match in LINK.finditer(prose(data.decode())):
                        target = match[1].strip('<>')
                        parsed = urlsplit(target)
                        if parsed.scheme or parsed.netloc:
                            continue
                        target = posixpath.normpath(str(Path(name).parent / unquote(parsed.path)))
                        if target == prefix[:-1] or target.startswith(prefix):
                            referenced.add(target)
                for location, expected in record['files'].items():
                    path = Path(location)
                    if not path.is_relative_to(Path(record['native']['root'])):
                        continue
                    relative = prefix + path.relative_to(record['native']['root']).as_posix()
                    if any(relative == target or relative.startswith(target + '/') for target in referenced):
                        _, actual = snapshot_read(path)
                        require(actual == expected, 'Referenced retained skill file differs from its managed version')
                        self.manager.sources[path] = actual
                        siblings.add(relative)
            package.sibling_files = frozenset(siblings)

    def _check_retained_consumers(self):
        """A scoped rename/removal must not break a retained skill's links."""
        from .resource_references import LINK, prose
        from urllib.parse import unquote, urlsplit
        import os
        removed = set()
        for ref, item in self.rendered.items():
            old = self.previous.get(ref)
            if not old or old['kind'] != 'skill' or ref in self.conflicts.kept:
                continue
            old_root = Path(old['native']['root'])
            new_root = Path(self.records[ref]['native'].get('root', old_root))
            desired = {str(new_root / name) for name in item['files']}
            removed.update(path for path in old['files'] if Path(path).is_relative_to(old_root) and path not in desired)
        if not removed:
            return
        for ref, record in self.previous.items():
            if ref in self.selected_refs or record['kind'] != 'skill':
                continue
            root = Path(record['native']['root'])
            snapshots = record['native'].get('review', {}).get('files', {})
            for location, expected in record['files'].items():
                path = Path(location)
                if not path.is_relative_to(root) or path.suffix.lower() != '.md':
                    continue
                if location in snapshots:
                    data = self.conflicts.load(snapshots[location], expected['sha256'])
                else:
                    data, actual = snapshot_read(path)
                    require(actual == expected, 'Retained consumer cannot be checked; include it in the deployment')
                    self.manager.sources[path] = actual
                for match in LINK.finditer(prose(data.decode())):
                    parsed = urlsplit(match[1].strip('<>'))
                    if parsed.scheme or parsed.netloc or not parsed.path:
                        continue
                    target = os.path.normpath(str(path.parent / unquote(parsed.path)))
                    if any(target == name or name.startswith(target + '/') for name in removed):
                        self.checks.append({'name': 'retained_consumer', 'status': 'blocked',
                            'code': 'scope_expansion_required', 'ref': ref,
                            'reason': 'A retained skill references a removed path; include the consumer with an updated reference'})
                        break

    def prepare(self, deploy=True, binding_scope=None, resource_refs=None):
        bindings = {key: value for key, value in self.profile.get('bindings', {}).items() if binding_scope is None or key in binding_scope}
        selected = resource_refs if deploy else {arg['resource'] for entry in bindings.values() for arg in entry['args'] if isinstance(arg, dict)}
        if selected is not None:
            selected = set(selected) | {arg['resource'] for entry in bindings.values() for arg in entry['args'] if isinstance(arg, dict)}
        while True:
            packages = self.resolver.resolve(self.profile, selected=selected)
            if not deploy or selected is None:
                break
            # A resource's content-addressed cache is used by generated commands.
            # Include all arguments of affected bindings so they remain coherent.
            affected = {key: entry for key, entry in self.profile.get('bindings', {}).items()
                        if any(isinstance(arg, dict) and arg['resource'] in packages for arg in entry['args'])}
            bindings.update(affected)
            expanded = set(packages) | {arg['resource'] for entry in bindings.values() for arg in entry['args'] if isinstance(arg, dict)}
            if expanded == set(packages):
                break
            selected = expanded
        self.selected_refs = set(packages)
        self.binding_fields = set(bindings)
        if self.partial:
            self._scoped_siblings(packages)
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
            from .runtime_requirements import check_runtime
            runtime_check = check_runtime(package, slots)
            if runtime_check:
                self.checks.append(runtime_check)
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
                if self.manager.agent == 'antigravity':
                    self._antigravity_plugin(ref, item)
                    continue
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
                elif kind == 'rule' and self.manager.agent == 'antigravity' and len(item['files'][package.entry]) > ANTIGRAVITY_RULE_MAX_BYTES:
                    self.checks.append({
                        'name': 'antigravity_rule_size',
                        'ref': ref,
                        'status': 'warning',
                        'code': 'rule_size_exceeds_24kb',
                        'reason': 'Antigravity truncates instruction/rule files exceeding 24,000 bytes',
                    })
                extension = '.md' if self.manager.agent in ('claude-code', 'antigravity') else '.toml' if kind == 'agent' else '.rules'
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
            if self.partial:
                self._check_retained_consumers()
            self.plugins.finish()
            if self.registrations is not None:
                self.changes += self.registrations.changes()
            self.conflicts.freeze()
            for ref, previous in self.previous.items():
                if not self.partial and ref not in packages and previous['active']:
                    self.decisions.append({'path': ['resources', previous['group'], ref], 'source': {'kind': 'profile'},
                                           'target': {'kind': 'installed-resource'}, 'before_exists': True, 'before': {'active': True},
                                           'incoming_exists': False, 'after_exists': True, 'after': {'active': False, 'native_content': 'retained'},
                                           'action': 'residual', 'category': 'policy_merge', 'policy': 'absence', 'policy_origin': 'structural',
                                           'reason': 'Selection removed; installed content retained and ownership remembered for explicit reselection',
                                           'alternatives': ['explicit_uninstall', 'reselect'], 'check_status': 'passed'})
        return self
