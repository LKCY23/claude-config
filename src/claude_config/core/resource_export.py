"""Observe explicitly declared resources without installation or ownership adoption."""
from copy import deepcopy
from dataclasses import replace
from pathlib import Path

import tomlkit

from .bindings import Bindings
from .catalog import CatalogResolver
from .merge import fingerprint, same
from .native import parse_json, parse_yaml, snapshot_read
from .native_plugins import PluginOperation, VERSION, marketplace_name
from .registrations import MCP_OPTIONS, mcp_definition, hook_definition, claude_user_config
from .resources import END, START, frontmatter, render_package, tree_digest
from .resource_references import instruction_body
from .agent_options import definition_options
from .schema import require
from .transaction import directory_fingerprint, file_fingerprint


class ResourceExport:
    def __init__(self, manager, profile, native, state):
        self.manager, self.profile, self.native, self.state = manager, profile, native, state
        self.resolver = CatalogResolver(manager)
        self.binder = Bindings(manager)
        self.decisions, self.checks, self.directories = [], [], set()
        self.plugins = type('NoNativeOperations', (), {'operation': None})()
        self.incoming, self.scope = {}, []

    def decision(self, ref, action, reason, status='passed'):
        group = self.packages[ref].group
        self.decisions.append({'path': ['resources', group, ref], 'source': {'kind': 'native-resource', 'ref': ref},
                               'target': {'kind': 'profile-reference'}, 'before_exists': True, 'before': self.profile['resources'][group][ref],
                               'incoming_exists': action != 'preserve_missing', 'after_exists': True, 'after': self.profile['resources'][group][ref],
                               'action': action, 'category': 'unresolved_conflict' if status == 'blocked' else 'policy_merge',
                               'policy': 'fixed-source-reverse-map', 'policy_origin': 'resource_export', 'reason': reason,
                               'alternatives': ['retain_profile', 'reconcile_source_and_replan'], 'check_status': status})
        if status == 'blocked':
            self.checks.append({'name': 'resource_reverse_map', 'path': ['resources', group, ref], 'status': 'blocked', 'code': 'resource_mapping_conflict', 'reason': reason})

    def read(self, path):
        data, expected = snapshot_read(path)
        self.manager.sources[Path(path)] = expected
        return data, expected['exists']

    def tree(self, root):
        self.directories.add(root)
        snapshot = directory_fingerprint(root)
        require(all('sha256' in info for info in snapshot['entries'].values()), 'Installed resource contains an unexpected symlink')
        files, modes = {}, {}
        for relative, expected in snapshot['entries'].items():
            data, actual = snapshot_read(root / relative)
            require(actual == expected, 'Resource changed while reading its complete body')
            self.manager.sources[root / relative] = expected
            files[relative], modes[relative] = data, expected['mode']
        return files, modes

    def options_item(self, package, options):
        selection = deepcopy(package.selection)
        selection['options'] = options
        item = render_package(replace(package, selection=selection), self.manager.agent)
        identifier, alias = package.ref.split('@')
        item['cache'] = self.manager.native_path.parent.resolve() / '.claude-config-resources' / alias / identifier / item['rendered_fingerprint']
        item['slots'] = self.binder.slots(package)
        return item

    def skill(self, package):
        agent = self.manager.agent
        initial = self.options_item(package, package.selection.get('options', {}))
        if agent == 'codex':
            slots = initial['slots']
            require('install_root' in slots and Path(slots['install_root']).parts[-2:] == ('.agents', 'skills'), 'Codex skill export needs its explicit USER installation binding')
            root = Path(slots['install_root']) / initial['identity']
        else:
            root = self.manager.native_path.parent / 'skills' / initial['identity']
        if not root.exists():
            self.decision(package.ref, 'preserve_missing', 'Installed skill is absent; Profile selection and options are retained')
            return None
        actual, modes = self.tree(root)
        require('SKILL.md' in actual, 'Installed skill has no root SKILL.md')
        controls = {}
        if agent == 'codex':
            if 'agents/openai.yaml' in actual:
                policy = parse_yaml(actual['agents/openai.yaml']).get('policy', {})
                require(isinstance(policy, dict), 'Skill invocation policy must be an object')
                if 'allow_implicit_invocation' in policy:
                    controls['allow_implicit_invocation'] = policy['allow_implicit_invocation']
            rows = self.native.get('skills', {}).get('config', [])
            require(isinstance(rows, list) and all(isinstance(row, dict) for row in rows), 'Native skill activation inventory must be a list')
            path = str((root / 'SKILL.md').resolve())
            require(not any(row.get('name') == initial['identity'] for row in rows), 'Name-based activation overlaps the skill identity')
            matches = [row for row in rows if isinstance(row.get('path'), str) and Path(row['path']).is_absolute() and str(Path(row['path']).resolve()) == path]
            require(len(matches) <= 1, 'Ambiguous duplicated native skill path selectors')
            if matches:
                require(set(matches[0]) == {'path', 'enabled'}, 'Skill activation has unsupported extra selectors')
                controls['enabled'] = matches[0]['enabled']
        else:
            meta, _, _ = frontmatter(actual['SKILL.md'])
            controls = {key: meta[key] for key in ('disable-model-invocation', 'user-invocable') if key in meta}
            overrides = self.native.get('skillOverrides', {})
            require(isinstance(overrides, dict), 'skillOverrides must be a map')
            if initial['identity'] in overrides:
                value = overrides[initial['identity']]
                require(value in ('on', 'off'), 'This enabled option cannot represent name-only or user-invocable-only')
                controls['enabled'] = value == 'on'
            from .skill_controls import claude_skill_plugin_selector
            selector = claude_skill_plugin_selector(initial)
            if selector:
                plugins = self.native.get('enabledPlugins', {})
                require(isinstance(plugins, dict), 'enabledPlugins must be a map')
                enabled = plugins.get(selector, True)
                require(type(enabled) is bool, 'Automatic skill plugin activation must be Boolean')
                require('enabled' not in controls or controls['enabled'] == enabled,
                        'Skill override and automatic plugin activation disagree; reconcile before export')
                if selector in plugins:
                    controls['enabled'] = enabled
        require(all(type(value) is bool for value in controls.values()), 'Observed skill controls must be Boolean')
        # A missing native usage field is not deletion. Remove stale Profile
        # overrides only from the comparison render, never from exported data.
        item = self.options_item(package, {'control': controls})
        require(tree_digest(actual, modes) == item['rendered_fingerprint'], 'Skill body differs from the fixed package outside portable usage controls')
        return {'control': controls} if controls else {}

    def mcp(self, package):
        agent = self.manager.agent
        item = self.options_item(package, package.selection.get('options', {}))
        name, expected = mcp_definition(item, agent)
        path = claude_user_config(self.manager.entry['binding']) if agent == 'claude-code' else self.manager.native_path
        content, _ = self.read(path)
        document = parse_json(content) if agent == 'claude-code' else self.native
        entries = document.get('mcpServers' if agent == 'claude-code' else 'mcp_servers', {})
        require(isinstance(entries, dict), 'Native MCP inventory must be an object')
        if name not in entries:
            self.decision(package.ref, 'preserve_missing', 'Native MCP declaration is absent; logical source and options are retained')
            return None
        actual = entries[name]
        require(isinstance(actual, dict), 'Native MCP service definition must be an object')
        controls = MCP_OPTIONS[agent]
        require(same({key: value for key, value in actual.items() if key not in controls},
                     {key: value for key, value in expected.items() if key not in controls}), 'MCP connection or logical bindings differ; local values are not exported')
        options = {key: actual[key] for key in controls if key in actual}
        mcp_definition(self.options_item(package, options), agent)
        return options

    def plugin(self, package):
        agent = self.manager.agent
        item = self.options_item(package, package.selection.get('options', {}))
        entry = '.claude-plugin/plugin.json' if agent == 'claude-code' else '.codex-plugin/plugin.json'
        require(package.entry == entry, 'Plugin export needs its complete native manifest')
        manifest = parse_json(item['files'][entry])
        name, version = manifest.get('name'), manifest.get('version')
        require(isinstance(name, str) and isinstance(version, str) and VERSION.fullmatch(version), 'Plugin native identity/version is not fixed')
        owned = self.state.get('resources', {}).get(package.ref, {}).get('native', {}).get('plugin')
        if owned:
            require(owned['selector'].split('@')[0] == name, 'Native plugin name differs from the adopted identity')
        market = owned['marketplace'] if owned else marketplace_name(package.ref.split('@')[1])
        root = self.manager.native_path.parent.resolve()
        market_root = root / '.claude-config-resources/marketplaces' / market
        source = market_root / 'packages' / name / item['rendered_fingerprint']
        receipt = {'selector': name + '@' + market, 'version': version, 'image': item['rendered_fingerprint'], 'source': str(source),
                   'marketplace': market, 'market_root': str(market_root), 'manifest': str(market_root / ('.claude-plugin/marketplace.json' if agent == 'claude-code' else '.agents/plugins/marketplace.json')),
                   'manifest_snapshot': {'exists': False}, 'enabled': True,
                   'history': {version: {'source': str(source), 'image': item['rendered_fingerprint']}}}
        executable = Path(self.manager.entry['binding']['executable']).resolve()
        operation = PluginOperation({'agent': agent, 'root': str(root), 'user_config': str(claude_user_config(self.manager.entry['binding']).resolve()) if agent == 'claude-code' else None,
                                     'executable': str(executable), 'executable_snapshot': file_fingerprint(executable),
                                     'plugins': [{'ref': package.ref, 'desired': receipt, 'previous': None}]})
        for path in operation.paths:
            self.read(path)
        self.directories.update(operation.directories)
        actual = operation.native(receipt)
        if not actual['installed']:
            self.decision(package.ref, 'preserve_missing', 'Selected native plugin is not installed; no inventory selection is inferred')
            return None
        require(actual['marketplace'] and actual['version'] == version and actual['image'] == receipt['image'], 'Native plugin does not match the fixed catalog version/content/registration')
        if agent == 'claude-code':
            values = self.native.get('enabledPlugins', {})
            require(receipt['selector'] not in values or type(values[receipt['selector']]) is bool, 'Explicit plugin enabled must be Boolean')
            return {'enabled': values[receipt['selector']]} if receipt['selector'] in values else {}
        values = self.native.get('plugins', {}).get(receipt['selector'], {})
        require('enabled' not in values or type(values['enabled']) is bool, 'Explicit plugin enabled must be Boolean')
        return {'enabled': values['enabled']} if 'enabled' in values else {}

    def static(self, package):
        item = self.options_item(package, package.selection.get('options', {}))
        agent, kind = self.manager.agent, package.item['kind']
        if kind == 'instruction':
            path = self.manager.native_path.parent / ('CLAUDE.md' if agent == 'claude-code' else 'AGENTS.md')
            data, exists = self.read(path)
            if not exists or START not in data.decode():
                self.decision(package.ref, 'preserve_missing', 'Managed global instruction block is absent; declaration retained')
                return None
            text = data.decode()
            require(text.count(START) == text.count(END) == 1, 'Ambiguous global instruction block')
            expected = START + '\n' + instruction_body(item, agent) + '\n' + END
            require(text[text.index(START):text.index(END) + len(END)] == expected, 'Instruction block differs from the fixed source')
            if item['references']['count']:
                files, modes = self.tree(item['cache'])
                require(tree_digest(files, modes) == item['rendered_fingerprint'], 'Instruction package or referenced file differs from the fixed source')
            return {}
        folder = 'agents' if kind == 'agent' else 'rules'
        suffix = '.md' if agent == 'claude-code' else '.toml' if kind == 'agent' else '.rules'
        path = self.manager.native_path.parent / folder / (item['identity'] + suffix)
        data, exists = self.read(path)
        if not exists:
            self.decision(package.ref, 'preserve_missing', 'Selected native definition is absent; declaration retained')
            return None
        options = {}
        if kind == 'agent':
            options = definition_options(agent, data)
            item = self.options_item(package, options)
        require(data == item['files'][package.entry], 'Native definition differs from the fixed rendered source')
        return options

    def hook(self, package):
        require(package.item['compatibility'][self.manager.agent]['mode'] != 'status-line', 'Status-line resource is exported through its registered command binding field')
        item = self.options_item(package, package.selection.get('options', {}))
        wanted = hook_definition(item, self.manager.agent)
        path = self.manager.native_path if self.manager.agent == 'claude-code' else self.manager.native_path.parent / 'hooks.json'
        content, _ = self.read(path)
        actual = parse_json(content).get('hooks', {})
        require(isinstance(actual, dict), 'Native Hook inventory must be an object')
        keys = ('timeout', 'async', 'statusMessage')
        observed = {key: [] for key in keys}
        matched = 0
        for event, groups in wanted.items():
            rows = actual.get(event, [])
            require(isinstance(rows, list), 'Native Hook event must be a list')
            def body(group):
                require(isinstance(group, dict) and isinstance(group.get('hooks'), list), 'Invalid native Hook group')
                return {**group, 'hooks': [{key: value for key, value in handler.items() if key not in keys} for handler in group['hooks']]}
            for group in groups:
                candidates = [row for row in rows if body(row) == body(group)]
                if not candidates:
                    candidates = [row for row in rows if row.get('matcher') == group.get('matcher')]
                    require(not candidates, 'Hook logical command differs; local commands cannot be exported')
                require(len(candidates) <= 1, 'Hook reverse mapping has ambiguous matching groups')
                if not candidates:
                    continue
                matched += 1
                for handler in candidates[0]['hooks']:
                    for key in keys:
                        if key in handler:
                            observed[key].append(handler[key])
        if matched == 0:
            self.decision(package.ref, 'preserve_missing', 'No exact bound Hook group is present; no local command is exported')
            return None
        require(matched == sum(map(len, wanted.values())), 'Some Hook groups or logical commands differ from the fixed source')
        files, modes = self.tree(item['cache'])
        require(tree_digest(files, modes) == item['rendered_fingerprint'], 'Hook script package differs from the fixed source')
        count = sum(len(group['hooks']) for groups in wanted.values() for group in groups)
        options = {}
        for key, values in observed.items():
            if values:
                require(len(values) == count and all(same(values[0], value) for value in values), 'Per-handler Hook controls cannot be represented by a uniform resource option')
                options[key] = values[0]
        hook_definition(self.options_item(package, options), self.manager.agent)
        return options

    def observe(self, refs):
        require(len(refs) == len(set(refs)), 'Resource export selections must be unique')
        available = {ref for entries in self.profile.get('resources', {}).values() for ref in entries}
        require(set(refs) <= available, 'Resource export needs an explicit existing Profile source and binding declaration')
        selected = set(refs)
        skills = self.profile.get('resources', {}).get('skills', {})
        if selected.intersection(skills):
            # Keep selected siblings as read-only link-validation context. Only
            # the requested refs below contribute exported fields or decisions.
            selected.update(skills)
        self.packages = self.resolver.resolve(self.profile, selected=selected)
        for ref in refs:
            package = self.packages[ref]
            try:
                kind = package.item['kind']
                options = self.skill(package) if kind == 'skill' else self.mcp(package) if kind == 'mcp' else self.plugin(package) if kind == 'plugin' else self.hook(package) if kind == 'hook' else self.static(package)
                if options is not None:
                    self.incoming.setdefault('resources', {}).setdefault(package.group, {})[ref] = {'options': options}
                    self.decision(ref, 'preserve_reference', 'Fixed source and logical bindings match; only explicit portable usage options are exported')
                from .schema import as_pointer
                self.scope.append(as_pointer(('resources', package.group, ref, 'options')))
            except (ValueError, OSError) as exc:
                self.decision(ref, 'mapping_conflict', str(exc) if isinstance(exc, ValueError) and type(exc).__name__ == 'SchemaError' else 'Resource body or native declaration cannot be safely reverse-mapped', 'blocked')
        self.checks += self.binder.checks
        self.checks.append({'name': 'management_ownership', 'status': 'not_verified', 'reason': 'Export verifies source correspondence; it does not adopt unknown native ownership or write runtime state'})
        return self
