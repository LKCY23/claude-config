"""Combine data installation and disjoint client writes in one journal."""
from copy import deepcopy
from dataclasses import replace
import hashlib
import json
import yaml
from pathlib import Path

from .app_manager import AppProfileManager
from .app_fields import validate_settings as validate_app_settings
from .fields import validate_settings
from .manager import ProfileManager
from .merge import fingerprint
from .native import parse_native, parse_yaml, render_native, snapshot_read
from .schema import closed, require, state_directory, validate_catalogs, validate_machine, validate_profile
from .transaction import FileChange, FilePlan, file_fingerprint
from .execution_context import progress


class Cutover:
    def __init__(self, declaration, *, agent_probe=None, app_probe=None):
        self.spec_path = Path(declaration).absolute()
        raw, expected = snapshot_read(self.spec_path)
        spec = parse_yaml(raw)
        keys = ('schema_version', 'machine', 'machine_target', 'data_target')
        closed(spec, (*keys, 'migrate_existing', 'adopt_plugins', 'replace_plugins'), keys, 'cutover declaration')
        require(type(spec['schema_version']) is int and spec['schema_version'] == 2, 'Unknown cutover schema')
        require(type(spec.get('migrate_existing', False)) is bool, 'migrate_existing must be Boolean')
        for key in keys[1:]:
            require(isinstance(spec[key], str) and Path(spec[key]).is_absolute(), 'Cutover paths must be absolute')
        self.machine_path = Path(spec['machine'])
        data, pin = snapshot_read(self.machine_path)
        self.machine = validate_machine(parse_yaml(data))
        self.sources = {self.spec_path: expected, self.machine_path: pin}
        self.root = Path(self.machine['config_dir'])
        self.state_root = state_directory(self.machine, config_dir=spec['data_target'])
        self.native_path, self.entry = Path(spec['machine_target']), {}
        self.agents = {agent: ProfileManager(self.machine_path, agent, probe=agent_probe, state_root=self.state_root) for agent in self.machine['agents']}
        self.apps = {app: AppProfileManager(self.machine_path, app, probe=app_probe, state_root=self.state_root) for app in self.machine['apps']}
        require(len({manager.native_path.parent.resolve() for manager in self.agents.values()}) == len(self.agents), 'Different Agents cannot share a plugin configuration root')
        self.recovery_scopes = sorted({str(self.native_path.resolve()), str(Path(spec['data_target']).resolve()),
                                      *(str(manager.native_path.resolve()) for manager in self.agents.values()),
                                      *(scope for manager in self.apps.values() for scope in manager.recovery_scopes)})
        self.spec = spec
        for key in ('adopt_plugins', 'replace_plugins'):
            require(isinstance(spec.get(key, {}), dict) and set(spec.get(key, {})) <= set(self.agents), 'Plugin migration inputs must name bound Agents')

    def check_native_operation(self, operation):
        manager = self.agents.get(operation.agent)
        require(manager is not None and operation.root == manager.native_path.parent.resolve()
                and operation.definition['executable'] == str(Path(manager.entry['binding']['executable']).resolve()), 'Native recovery belongs to another cutover binding')
        if operation.agent == 'claude-code':
            from .registrations import claude_user_config
            require(operation.user_config == claude_user_config(manager.entry['binding']).resolve(), 'Native recovery user file binding differs')

    def recovery_check(self, target):
        if target is None:
            for manager in self.apps.values():
                manager.probe.assert_stopped(manager.application)
            return
        target = Path(target).resolve()
        for app, manager in self.apps.items():
            agent = self.agents.get('codex' if app == 'codex-app' else 'claude-code')
            if agent is not None and target == agent.native_path.resolve():
                manager.probe.assert_stopped(manager.application)
            else:
                manager.recovery_check(target)

    def recovery_phase_check(self, paths):
        self.recovery_check(None)

    def data_changes(self):
        target = Path(self.spec['data_target'])
        require(not target.is_symlink(), 'Configuration data target cannot be a symlink')
        files = [self.root / 'catalogs.yaml', *sorted((self.root / 'profiles').rglob('*.yaml'))]
        require(bool(files[1:]), 'Cutover data has no independent Profiles')
        changes = []
        self.data_decisions = []
        ignore_path = target / '.gitignore'
        original_ignore, ignore_pin = snapshot_read(ignore_path)
        lines = original_ignore.splitlines()
        ignore = original_ignore
        if not lines or lines[-1] != b'/.state/':
            ignore += (b'' if not ignore or ignore.endswith(b'\n') else b'\n') + b'/.state/\n'
        def validate_ignore(data):
            require(data.splitlines()[-1:] == [b'/.state/'], 'Local state must be excluded from Git synchronization')
        ignore_after = {'exists': True, 'sha256': hashlib.sha256(ignore).hexdigest(), 'mode': ignore_pin.get('mode', 0o600)}
        changes.append(FileChange(ignore_path, ignore, 'profile-data-install', validate_ignore, ignore_pin))
        self.data_decisions.append({'path': ['data', '.gitignore'], 'before_exists': ignore_pin['exists'], 'before': ignore_pin,
                                    'incoming_exists': True, 'incoming': ignore_after, 'after_exists': True, 'after': ignore_after,
                                    'action': 'unchanged' if original_ignore == ignore else 'add' if not ignore_pin['exists'] else 'replace',
                                    'category': 'unchanged' if original_ignore == ignore else 'policy_merge',
                                    'policy': 'local-state-exclusion', 'policy_origin': 'cutover_adapter',
                                    'reason': '保留现有 Git 忽略规则，将本机报告、基线和恢复备份排除在同步之外'})
        for source in files:
            relative = source.relative_to(self.root)
            data, pin = snapshot_read(source)
            self.sources[source] = pin
            if relative == Path('catalogs.yaml'):
                validator = lambda data: validate_catalogs(parse_yaml(data))
            else:
                parts = relative.parts
                require(len(parts) == 4 and parts[:1] == ('profiles',) and parts[1] in ('agents', 'apps'), 'Invalid data Profile layout')
                def validator(data, parts=parts):
                    value = validate_profile(parse_yaml(data), kind='agent' if parts[1] == 'agents' else 'app', expected=parts[2])
                    (validate_settings if parts[1] == 'agents' else validate_app_settings)(parts[2], value.get('settings', {}))
            validator(data)
            destination = target / relative
            current = file_fingerprint(destination)
            require(not current['exists'] or current['sha256'] == pin['sha256'], 'Data destination differs; reconcile before cutover: ' + str(relative))
            changes.append(FileChange(destination, data, 'profile-data-install', validator, current, 0o600))
            self.data_decisions.append({'path': ['data', str(relative)], 'before_exists': current['exists'], 'before': current,
                                        'incoming_exists': True, 'incoming': pin, 'after_exists': True, 'after': pin,
                                        'action': 'unchanged' if current['exists'] else 'add', 'category': 'unchanged' if current['exists'] else 'direct_add',
                                        'policy': 'exact-profile-data', 'policy_origin': 'cutover_adapter', 'reason': 'Copy validated Profile declarations; existing different data requires explicit reconciliation'})
        self.data_members = [str(path) for path in files]
        if self.spec.get('migrate_existing'):
            for name in ('agents.yaml', 'plugins.yaml', 'manifest.yaml'):
                path = target / name
                expected = file_fingerprint(path)
                if expected['exists']:
                    changes.append(FileChange(path, b'', 'retire-legacy-declaration', lambda data: None, expected, delete=True))
                    self.data_decisions.append({'path': ['data', name], 'before_exists': True, 'before': expected,
                                                'incoming_exists': False, 'incoming': None, 'after_exists': False, 'after': None,
                                                'action': 'delete', 'category': 'policy_merge', 'policy': 'one-time-format-migration',
                                                'policy_origin': 'cutover_adapter', 'reason': 'Retire legacy runtime declarations; private run backups preserve exact recovery without parsing old fields'})
        return changes

    @staticmethod
    def combine_file(old, incoming):
        if old.owner == incoming.owner == 'resource-baseline':
            require(old.content == incoming.content and old.mode == incoming.mode and old.expected == incoming.expected
                    and not old.delete and not incoming.delete, 'Shared immutable resource snapshots disagree')
            def validate(data):
                old.validator(data)
                incoming.validator(data)
            return replace(old, validator=validate)
        require(old.path.resolve() == incoming.path.resolve() and old.owner == 'agent-settings' and incoming.owner == 'app-preferences',
                'Two unrelated owners write the same file')
        require(old.path.name == 'config.toml' and old.expected == incoming.expected and not old.delete and not incoming.delete, 'Shared native file snapshots disagree')
        original, _ = snapshot_read(old.path)
        agent, app = parse_native('codex', old.content), parse_native('codex', incoming.content)
        initial = parse_native('codex', original)
        require(agent.get('desktop', {}) == initial.get('desktop', {})
                and {key: value for key, value in app.items() if key != 'desktop'} == {key: value for key, value in initial.items() if key != 'desktop'},
                'Agent/App native write scopes overlap')
        if 'desktop' in app:
            agent['desktop'] = deepcopy(app['desktop'])
        content = render_native('codex', original, agent)
        def validate(data):
            old.validator(data)
            incoming.validator(data)
        return replace(old, content=content, validator=validate)

    def plan(self):
        progress('检查配置目标与 Machine 绑定')
        machine = deepcopy(self.machine)
        machine['config_dir'] = self.spec['data_target']
        current, pin = snapshot_read(self.native_path)
        require(not pin['exists'] or self.native_path == self.machine_path or parse_yaml(current) == machine,
                'Machine destination differs; reconcile before cutover')
        content = yaml.safe_dump(machine, allow_unicode=True, sort_keys=False).encode()
        change = FileChange(self.native_path, content, 'machine-binding', lambda data: validate_machine(parse_yaml(data)), pin)
        after = {'exists': True, 'sha256': hashlib.sha256(content).hexdigest(), 'mode': pin.get('mode', 0o600)}
        decision = {'path': ['machine', str(self.native_path)], 'before_exists': pin['exists'], 'before': pin,
                    'incoming_exists': True, 'incoming': after, 'after_exists': True, 'after': after,
                    'action': 'unchanged' if current == content else 'replace' if pin['exists'] else 'add',
                    'category': 'unchanged' if current == content else 'policy_merge', 'policy': 'explicit-data-binding',
                    'policy_origin': 'cutover', 'reason': '更新配置数据目录绑定；程序安装由包管理器负责'}
        boot = FilePlan([change], report={'ready': True, 'checks': [], 'decisions': [decision], 'blockers': [],
                                        'plan_id': fingerprint(machine)}, prepare=False)
        plans = [boot]
        for agent, manager in self.agents.items():
            progress('检查 ' + {'claude-code': 'Claude Code', 'codex': 'Codex'}[agent] + ' 配置与资源')
            plans.append(manager.plan(adopt_plugins=self.spec.get('adopt_plugins', {}).get(agent),
                                      replace_plugins=self.spec.get('replace_plugins', {}).get(agent), prepare=False))
        for app, manager in self.apps.items():
            progress('检查 ' + {'claude-desktop': 'Claude Desktop', 'codex-app': 'Codex App'}[app] + ' 偏好')
            plans.append(manager.plan(prepare=False))
        progress('合并各客户端计划，检查整次迁移')
        report = {'ready': True, 'checks': [], 'decisions': [], 'blockers': [], 'client_plans': []}
        sources, directories, native = dict(self.sources), set(), []
        files = {}
        # Client resources/settings first; installed entries and machine pointer last.
        for change in self.data_changes():
            files[change.path.resolve()] = change
        report['decisions'] += self.data_decisions
        sources.update(self.sources)
        for plan in plans[1:]:
            report['client_plans'].append({'agent_type': plan.report.get('agent_type'), 'app': plan.report.get('app'), 'plan_id': plan.report['plan_id']})
            for change in plan.changes:
                key = change.path.resolve()
                if key in files:
                    combined = self.combine_file(files[key], change)
                    snapshot = files[key].owner == 'resource-baseline'
                    report['decisions'].append({'path': ['shared_files', str(key)], 'before_exists': bool(combined.expected and combined.expected['exists']),
                                                'before': combined.expected, 'incoming_exists': True,
                                                'incoming': {'sha256': hashlib.sha256(combined.content).hexdigest()}, 'after_exists': True,
                                                'after': {'sha256': hashlib.sha256(combined.content).hexdigest()},
                                                'action': 'coalesce_snapshot' if snapshot else 'combine_client_fields', 'category': 'policy_merge',
                                                'policy': 'identical-immutable-snapshot' if snapshot else 'disjoint-client-fields', 'policy_origin': 'cutover_adapter',
                                                'reason': 'Identical immutable bytes, modes and input snapshots share one write; both validators run' if snapshot else
                                                          'Compose disjoint Agent settings and App desktop preferences into one native write; preserve other fields'})
                    files[key] = combined
                else:
                    files[key] = change
            native.extend(plan.native_operations)
        for change in boot.changes:
            require(change.path.resolve() not in files, 'Machine binding overlaps a native/data target')
            files[change.path.resolve()] = change
        for plan in plans:
            client = plan.report.get('agent_type') or plan.report.get('app')
            for key in ('checks', 'decisions', 'blockers'):
                rows = deepcopy(plan.report[key])
                if client:
                    for row in rows:
                        row['client'] = client
                        if 'path' in row:
                            row['path'] = ['clients', client, *row['path']]
                report[key] += rows
            for path, snapshot in plan.paths.items():
                if path in sources:
                    require(sources[path] == snapshot['file'], 'Child plans observed different input snapshots')
                sources[path] = snapshot['file']
            directories.update(plan.directories)
        for app, manager in self.apps.items():
            try:
                manager.probe.assert_stopped(manager.application)
                report['checks'].append({'name': 'app_lifecycle', 'app': app, 'status': 'passed'})
            except (OSError, RuntimeError, ValueError):
                reason = 'Close bound Apps normally before the combined cutover'
                path = ['clients', app]
                report['checks'].append({'name': 'app_lifecycle', 'app': app, 'path': path, 'status': 'blocked', 'code': 'app_running', 'reason': reason})
                report['blockers'].append({'code': 'app_running', 'path': path, 'reason': reason})
        report['ready'] = not report['blockers']
        report['deployment_ready'] = report['ready']
        report['plan_id'] = fingerprint({'children': [plan.report['plan_id'] for plan in plans], 'spec': self.spec})
        source_roots = {*(root.resolve() for manager in self.agents.values() for root in manager.last_resources.resolver.repositories)}
        for change in files.values():
            require(not any(change.path.resolve().is_relative_to(root) for root in source_roots), 'Cutover write overlaps a fixed source repository')
        def inventory_guard():
            members = [str(self.root / 'catalogs.yaml'), *(str(path) for path in sorted((self.root / 'profiles').rglob('*.yaml')))]
            require(members == self.data_members, 'Profile data inventory changed')
        def lifecycle_guard():
            if report['ready']:
                self.recovery_check(None)
        def step_guard(change):
            if not isinstance(change, FileChange) or change.owner in ('agent-settings', 'app-preferences'):
                self.recovery_check(None)
        changes = list(files.values())
        early = [change for change in changes if change.owner not in ('machine-binding', 'managed-baseline', 'app-managed-baseline')]
        states = [change for change in changes if change.owner in ('managed-baseline', 'app-managed-baseline')]
        installation = [change for change in changes if change.owner == 'machine-binding']
        steps = [*early, *native, *states, *installation]
        return FilePlan(changes, sources=sources, directories=directories, scopes=self.recovery_scopes, report=report,
                        native_operations=native, steps=steps, recovery_guard=lambda: self.recovery_check(None),
                        guards=[inventory_guard, lifecycle_guard, *(guard for plan in plans for guard in plan.guards)], step_guard=step_guard)
