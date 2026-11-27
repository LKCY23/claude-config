"""Previewable local bootstrap; package installation remains the package manager's job."""
from copy import deepcopy
from importlib.resources import files
import hashlib
import os
from pathlib import Path
import shutil
import sys
import yaml

from .profile import ReportParser, UsageError, output_report, report_format, report_verbose
from .core.merge import fingerprint
from .core.machine_profiles import MachineProfiles
from .core.native import parse_json, parse_yaml, snapshot_read
from .core.recovery import rollback_plan
from .core.reporting import build_report, fallback_state_root, save_report
from .core.schema import (AGENTS, APPS, SchemaError, pointer, profile_path, profile_yaml_files,
                          require, resolve_catalog_root, state_directory, validate_catalogs, validate_machine,
                          validate_machine_profiles, validate_profile, within)
from .core.transaction import FileChange, FilePlan, PlanStale, file_fingerprint, plan_id_matches

OLD_RUNTIME = ('python', 'framework_source', 'framework_commit')


def default_machine():
    return Path.home() / '.claude-config-tool/machine.yaml'


def skill_body(command, machine, agent):
    return files('claude_config').joinpath('resources/SKILL.md').read_text().format(
        command=command, machine=machine, agent=agent).encode()


class Setup:
    def __init__(self, args):
        self.args = args
        self.machine_path = args.machine.expanduser().absolute()
        self.native_path = self.machine_path
        self.entry = {}
        self.sources = {}
        source = (args.from_machine or self.machine_path).expanduser().absolute()
        data, pin = snapshot_read(source)
        self.sources[source] = pin
        require(not args.from_machine or pin['exists'], '--from-machine 文件不存在')
        self.machine = parse_yaml(data) if pin['exists'] else {
            'schema_version': 1, 'machine_id': 'local',
            'config_dir': str(Path.home() / 'claude-config-data'), 'catalog_roots': {},
            'agents': {}, 'apps': {}, 'bindings': {}}
        self.machine = deepcopy(self.machine)
        runtime = self.machine.get('runtime', {})
        require(isinstance(runtime, dict), 'runtime 必须是映射')
        for key in OLD_RUNTIME:
            runtime.pop(key, None)
        if not runtime:
            self.machine.pop('runtime', None)
        if args.config_dir:
            self.machine['config_dir'] = str(args.config_dir.expanduser().absolute())
        if getattr(args, 'auto_catalog_roots', False):
            for alias in list(self.machine.get('catalog_roots', {})):
                self.machine['catalog_roots'][alias] = 'auto'
        sync_selection = bool(getattr(args, 'sync_selection', False))
        select_args = list(getattr(args, 'select', ()))
        require(not select_args or sync_selection, '--select 仅可与 --sync-selection 配合使用')
        require(not (sync_selection and getattr(args, 'profile_selection', None)),
                '--sync-selection 与 --profile-selection 不能同时使用')
        require(not (sync_selection and getattr(args, 'run', None)),
                '--sync-selection 与 --run 不能同时使用')
        if sync_selection:
            require('profile_selection' in self.machine, '--sync-selection 需要 machine.yaml 已配置 profile_selection')
        if getattr(args, 'profile_selection', None):
            previous = {group: {name: entry.get('profile') for name, entry in self.machine[group].items()}
                        for group in ('agents', 'apps')}
            self.machine['profile_selection'] = args.profile_selection
            for group in ('agents', 'apps'):
                for entry in self.machine[group].values():
                    entry.pop('profile', None)
        resolved = MachineProfiles(source, self.machine, expected=pin, strict=not sync_selection)
        self.machine_profiles = resolved
        if getattr(args, 'profile_selection', None):
            require(all(profile is None or profile == resolved.effective[group][name]['profile']
                        for group, entries in previous.items() for name, profile in entries.items()),
                    'Repository choices differ from the current Machine; reconcile before adopting the selection file')
        self.sources.update(resolved.sources)
        self.updated_selection = None
        self.selection_path = resolved.selection_path
        if sync_selection:
            self.updated_selection = self._reconcile_selection(resolved.selection, select_args)
        self.state_root = state_directory(self.machine)
        self.skills = {}
        for item in args.skill:
            agent, sep, value = item.partition('=')
            require(sep and agent in self.machine['agents'] and agent not in self.skills,
                    '--skill 要求已绑定且不重复的 agent=文件路径')
            path = Path(value).expanduser().absolute()
            require(path.name == 'SKILL.md', 'Skill 入口必须命名为 SKILL.md')
            self.skills[agent] = path
        require(len(set(self.skills.values())) == len(self.skills) and self.machine_path not in self.skills.values(),
                'Machine 与 Skill 目标不得重叠')
        scopes = {str(self.machine_path.parent.resolve()), *(str(p.resolve()) for p in self.skills.values())}
        if sync_selection and self.selection_path is not None:
            scopes.add(str(self.selection_path.parent.resolve()))
        self.recovery_scopes = sorted(scopes)

    def _reconcile_selection(self, base_selection, select_args):
        explicit = {}
        for item in select_args:
            target, sep, rel = item.partition('=')
            require(bool(sep) and bool(target) and bool(rel), '--select 必须使用 TARGET=PROFILE 格式')
            if target in AGENTS:
                group = 'agents'
            elif target in APPS:
                group = 'apps'
            else:
                raise SchemaError(f'--select 指定的目标未知: {target}')
            require(target in self.machine[group], f'--select 指定的目标未在本机 machine.yaml 中绑定: {target}')
            require(target not in explicit, f'--select 重复指定目标: {target}')
            profile_path(rel, group, target)
            explicit[target] = (group, rel)

        updated = deepcopy(base_selection)
        config_dir = Path(self.machine['config_dir'])
        for group in ('agents', 'apps'):
            current = updated[group]
            local_targets = self.machine[group]
            for stale in [key for key in current if key not in local_targets]:
                del current[stale]
            for name in sorted(local_targets):
                if name in explicit:
                    current[name] = explicit[name][1]
                elif name not in current:
                    profile_dir = config_dir / 'profiles' / group / name
                    candidates = [
                        p.relative_to(config_dir).as_posix()
                        for p in profile_yaml_files(profile_dir)
                    ] if profile_dir.is_dir() else []
                    if len(candidates) == 1:
                        current[name] = candidates[0]
                    elif not candidates:
                        raise SchemaError(
                            f'{name} 缺少可用 Profile 文件（{profile_dir} 下无 .yaml）；请先创建 Profile 或使用 --select {name}=<profile>'
                        )
                    else:
                        raise SchemaError(
                            f'{name} 存在多个候选 Profile（{", ".join(candidates)}），请使用 --select {name}=<profile> 明确指定'
                        )
                sel_rel = current[name]
                prof_file = within(config_dir, sel_rel)
                raw_prof, prof_pin = snapshot_read(prof_file)
                self.sources[prof_file] = prof_pin
                prof_doc = parse_yaml(raw_prof)
                validate_profile(prof_doc, kind='agent' if group == 'agents' else 'app', expected=name)
                if group == 'agents':
                    from .core.fields import validate_settings
                    validate_settings(
                        name,
                        prof_doc.get('settings', {}),
                        generated={pointer(p)[1:] for p in prof_doc.get('bindings', {})},
                    )
        validate_machine_profiles(updated, self.machine['machine_id'])
        return updated

    def plan(self):
        changes, decisions = [], []
        def add(path, content, *, delete=False, expected=None):
            path = Path(path)
            old = file_fingerprint(path) if expected is None else expected
            after = {'exists': False} if delete else {'exists': True, 'sha256': hashlib.sha256(content).hexdigest(), 'mode': 0o600}
            if old == after:
                return
            changes.append(FileChange(path, content, 'local-bootstrap', lambda data: None, old, 0o600, delete=delete))
            decisions.append({'path': ['bootstrap', str(path)], 'before_exists': old['exists'], 'before': old,
                              'incoming_exists': not delete, 'incoming': after, 'after_exists': not delete, 'after': after,
                              'action': 'delete' if delete else 'replace' if old['exists'] else 'add',
                              'category': 'policy_merge' if old['exists'] else 'direct_add',
                              'policy': 'explicit-local-bootstrap', 'policy_origin': 'init',
                              'reason': '更新本机绑定/明确指定的 Skill；退役文件只按已核对安装清单移除。'})
        current, pin = snapshot_read(self.machine_path)
        if pin['exists'] and self.args.from_machine and parse_yaml(current) != self.machine:
            # In-place migration is explicit; another machine must not overwrite an existing binding.
            require(self.args.from_machine.expanduser().absolute() == self.machine_path,
                    '目标已有不同 Machine；请选择新目标或先明确整理绑定')
        add(self.machine_path, yaml.safe_dump(self.machine, allow_unicode=True, sort_keys=False).encode(), expected=pin)
        if getattr(self, 'updated_selection', None) is not None and self.selection_path is not None:
            selection_bytes = yaml.safe_dump(self.updated_selection, allow_unicode=True, sort_keys=False).encode()
            old_pin = self.sources[self.selection_path]
            after_pin = {'exists': True, 'sha256': hashlib.sha256(selection_bytes).hexdigest(), 'mode': 0o600}
            if old_pin != after_pin:
                changes.append(
                    FileChange(
                        self.selection_path,
                        selection_bytes,
                        'machine-selection',
                        lambda data: validate_machine_profiles(parse_yaml(data), self.machine['machine_id']),
                        old_pin,
                        0o600,
                    )
                )
                decisions.append({
                    'path': ['bootstrap', str(self.selection_path)],
                    'before_exists': old_pin['exists'],
                    'before': old_pin,
                    'incoming_exists': True,
                    'incoming': after_pin,
                    'after_exists': True,
                    'after': after_pin,
                    'action': 'replace',
                    'category': 'policy_merge',
                    'policy': 'explicit-local-bootstrap',
                    'policy_origin': 'init',
                    'reason': '对齐本机 machine.yaml 与 machines/<id>.yaml 的目标选择清单。',
                })
        invoked = Path(sys.argv[0]).absolute()
        command = (str(invoked) if invoked.name == 'claude-config' and invoked.is_file()
                   and os.access(invoked, os.X_OK) else shutil.which('claude-config'))
        if command:
            command = str(Path(command).absolute())
        require(not self.skills or command, '安装 claude-config 命令后再创建 Skill 入口')
        for agent, path in self.skills.items():
            add(path, skill_body(command, self.machine_path, agent))
        if self.args.retire_script_install:
            root = self.machine_path.parent
            receipt_path = root / '.profile-framework-install.json'
            raw, expected = snapshot_read(receipt_path)
            require(expected['exists'], '旧脚本安装清单不存在；不能推测应删除的文件')
            receipt = parse_json(raw)
            require(receipt.get('kind') == 'profile-framework-install' and receipt.get('schema_version') == 1,
                    '未知旧安装清单')
            self.sources[receipt_path] = expected
            for name, expected_file in receipt['files'].items():
                path = Path(name)
                require(path.is_absolute(), '旧安装清单路径必须绝对')
                if path == self.machine_path or path in self.skills.values():
                    require(file_fingerprint(path) == expected_file, '旧绑定或 Skill 已变更，请先整理：' + name)
                    continue
                relative = path.relative_to(root) if path.is_relative_to(root) else None
                allowed = relative is not None and (str(relative) == 'SKILL.md' or
                    len(relative.parts) == 2 and relative.parts[0] == 'references' and relative.suffix == '.md' or
                    len(relative.parts) == 2 and relative.parts[0] == 'scripts' and relative.name in ('profile_config.py','migration_config.py','cutover_config.py') or
                    len(relative.parts) == 3 and relative.parts[:2] == ('scripts','profile_core') and relative.suffix == '.py')
                require(allowed, '清单含未明确接管的路径；请用 --skill 指定入口或检查清单：' + name)
                require(path.resolve().is_relative_to(root.resolve()), '旧文件路径越界')
                require(file_fingerprint(path) == expected_file, '旧工具文件已修改；保留并停止迁移：' + name)
                add(path, b'', delete=True, expected=expected_file)
            add(receipt_path, b'', delete=True, expected=expected)
        report = {'ready': True, 'checks': [], 'blockers': [], 'decisions': decisions,
                  'plan_id': fingerprint({'machine': self.machine, 'decisions': decisions})}
        return FilePlan(changes, sources=self.sources, scopes=self.recovery_scopes, report=report)


def doctor(machine_path, *, resources=False, processes=False, storage=False):
    machine_path = machine_path.expanduser().absolute()
    checks = [{'name': 'python_runtime', 'status': 'passed', 'reason': sys.executable}]
    raw, pin = snapshot_read(machine_path)
    require(pin['exists'], '未找到 Machine；请先运行 claude-config init，或指定 --machine')
    selected = MachineProfiles(machine_path, strict=False)
    machine = selected.effective
    checks.append({'name': 'machine_binding', 'status': 'passed', 'reason': str(machine_path)})
    if selected.mismatches:
        for mismatch in selected.mismatches:
            repo_only = ', '.join(mismatch['repository_only']) or '无'
            local_only = ', '.join(mismatch['local_only']) or '无'
            checks.append({
                'name': 'machine_profile_selection',
                'status': 'failed',
                'subject': mismatch['group'],
                'reason': (
                    f"{selected.selection_path} 与本机 machine.yaml 的 {mismatch['group']} 绑定不一致："
                    f"仓库多出 [{repo_only}]，本机多出 [{local_only}]"
                ),
                'repository_only': mismatch['repository_only'],
                'local_only': mismatch['local_only'],
            })
    elif selected.selection_path is not None:
        checks.append({'name': 'machine_profile_selection', 'status': 'passed', 'reason': str(selected.selection_path)})
    checks.append({'name': 'configuration_directory', 'status': 'passed' if Path(machine['config_dir']).is_dir() else 'warning',
                   'reason': machine['config_dir']})
    for group in ('agents','apps'):
        for name, entry in machine[group].items():
            if 'profile' not in entry:
                checks.append({
                    'name': 'profile_file',
                    'status': 'failed',
                    'subject': name,
                    'reason': f'{name} 仅存在于本机 machine.yaml，缺少仓库 profile_selection 中的 Profile 选择',
                })
            else:
                profile = Path(machine['config_dir']) / entry['profile']
                checks.append({'name': 'profile_file', 'status': 'passed' if profile.is_file() else 'failed', 'subject': name, 'reason': str(profile)})
                if profile.is_file():
                    from .core.schema import validate_profile, pointer
                    from .core.fields import validate_settings
                    try:
                        value = parse_yaml(profile.read_bytes())
                        validate_profile(value, kind='agent' if group == 'agents' else 'app', expected=name)
                        if group == 'agents':
                            validate_settings(name, value.get('settings', {}),
                                              generated={pointer(p)[1:] for p in value.get('bindings', {})})
                        checks.append({'name': 'profile_compatibility', 'status': 'passed', 'subject': name,
                                       'minimum_tool_version': value.get('minimum_tool_version')})
                    except (ValueError, OSError) as exc:
                        checks.append({'name': 'profile_compatibility', 'status': 'failed', 'subject': name,
                                       'reason': str(exc) if isinstance(exc, SchemaError) else 'Profile cannot be parsed'})
            if group == 'agents' and name == 'antigravity':
                binding = entry['binding']
                if 'executable' in binding:
                    target = Path(binding['executable'])
                    valid = target.is_file() and os.access(target, os.X_OK)
                    checks.append({'name': 'native_entry', 'status': 'passed' if valid else 'failed', 'subject': name, 'reason': str(target)})
                if 'application' in binding:
                    target = Path(binding['application'])
                    valid = target.is_dir()
                    checks.append({'name': 'native_entry', 'status': 'passed' if valid else 'failed', 'subject': name, 'reason': str(target)})
            else:
                key = 'executable' if group == 'agents' else 'application'
                target = Path(entry['binding'][key])
                valid = target.is_dir() if group == 'apps' else target.is_file() and os.access(target, os.X_OK)
                if group == 'apps' and sys.platform != 'darwin':
                    checks.append({'name': 'app_platform', 'status': 'failed', 'subject': name, 'reason': '此 App 适配仅支持 macOS。'})
                checks.append({'name': 'native_entry', 'status': 'passed' if valid else 'failed', 'subject': name, 'reason': str(target)})
    for name, value in machine.get('bindings', {}).get('executables', {}).items():
        checks.append({'name': 'local_dependency', 'subject': name,
                       'status': 'passed' if Path(value).is_file() and os.access(value, os.X_OK) else 'failed', 'reason': value})
    catalogs_doc = None
    catalogs_error = None
    if any(value == 'auto' for value in machine['catalog_roots'].values()):
        catalogs_path = Path(machine['config_dir']) / 'catalogs.yaml'
        if not catalogs_path.is_file():
            catalogs_error = 'catalog_roots 包含 auto，但 catalogs.yaml 不存在'
        else:
            try:
                catalogs_doc = validate_catalogs(parse_yaml(snapshot_read(catalogs_path)[0]))['catalogs']
            except (SchemaError, ValueError, OSError) as exc:
                catalogs_error = str(exc) if isinstance(exc, SchemaError) else 'catalogs.yaml 无法解析'
    for name, value in machine['catalog_roots'].items():
        if value == 'auto':
            if catalogs_error:
                checks.append({'name': 'catalog_directory', 'subject': name, 'status': 'failed', 'reason': catalogs_error})
            elif name not in catalogs_doc:
                checks.append({'name': 'catalog_directory', 'subject': name, 'status': 'failed', 'reason': f'catalogs.yaml 中未声明 {name}'})
            else:
                resolved = resolve_catalog_root(machine, name, catalogs_doc[name])
                checks.append({
                    'name': 'catalog_directory',
                    'subject': name,
                    'status': 'passed' if resolved.is_dir() else 'failed',
                    'reason': f'auto -> {resolved}',
                })
        else:
            checks.append({'name': 'catalog_directory', 'subject': name, 'status': 'passed' if Path(value).is_dir() else 'failed', 'reason': value})
    if not machine['agents'] and not machine['apps']:
        checks.append({'name': 'client_bindings', 'status': 'warning', 'reason': '尚未绑定 Agent 或 App；init 不推测客户端和 Profile。'})
    from .core.diagnostics import agent_processes, tool_identity, app_instances, storage_summary
    result = {'machine_id': machine['machine_id'], 'checks': checks, 'decisions': [],
              'installation': tool_identity(),
              'profile_selection': {'kind': 'repository' if selected.selection_path else 'embedded',
                                    'path': str(selected.selection_path or machine_path)},
              'client_profiles': {group: {name: entry.get('profile') for name, entry in machine[group].items()}
                                  for group in ('agents', 'apps')}}
    if resources and not selected.mismatches:
        from .core.manager import ProfileManager
        result['resource_inventories'] = []
        for agent in machine['agents']:
            plan = ProfileManager(machine_path, agent).plan(prepare=False)
            inventory = plan.report.get('resource_inventory', {'agent_type': agent, 'skills': []})
            result['resource_inventories'].append(inventory)
            if 'antigravity' in inventory:
                result['antigravity_inventory'] = inventory['antigravity']
            checks.extend(inventory.get('checks', []))
            for row in inventory.get('skills', []):
                if row['selected'] and row['file_changes']:
                    checks.append({'name': 'skill_inventory_drift', 'status': 'blocked',
                                   'path': ['resources', 'skills', row['ref']], 'code': 'resource_drift',
                                   'reason': 'Managed skill files differ: ' + str(row['change_counts'])})
            # A missing inventory cannot turn failed source/native validation green.
            checks.extend(row for row in plan.report.get('checks', [])
                          if row['status'] in ('blocked', 'failed') and row not in checks)
    elif 'antigravity' in machine['agents']:
        from .core.resource_inventory import inspect_antigravity
        ag_inventory = inspect_antigravity(machine['agents']['antigravity']['binding']['config_root'])
        result['antigravity_inventory'] = ag_inventory
        checks.extend(ag_inventory['checks'])
    if processes:
        result['app_processes'] = app_instances(machine)
        if 'antigravity' in machine['agents']:
            result['agent_processes'] = agent_processes(machine)
    if storage:
        result['storage'] = storage_summary(state_directory(machine))
    failed = any(row['status'] in ('failed', 'blocked') for row in checks)
    result.update(ready=not failed, blockers=[{'code': 'doctor_checks_failed',
                                             'reason': '本机绑定或资源检查未通过；见检查明细。'}] if failed else [])
    return result, state_directory(machine)


def main(argv):
    parser = ReportParser(description=__doc__, prog='claude-config ' + argv[0])
    parser.add_argument('--machine', type=Path, default=default_machine())
    parser.add_argument('--format', choices=('auto', 'text', 'tui', 'json'), default='auto')
    parser.add_argument('--verbose', action='store_true', help='显示完整保留决策与细粒度输出')
    if argv[0] == 'doctor':
        parser.add_argument('--resources', action='store_true', help='Inspect selected skills, drift and external managers; no skill execution')
        parser.add_argument('--processes', action='store_true', help='Observe bound macOS App processes without collecting environment values')
        parser.add_argument('--storage', action='store_true', help='Summarize private state retention categories; never delete')
        parser.add_argument('--no-save', action='store_true', help='Return diagnostics without persisting a report')
    if argv[0] == 'init':
        parser.add_argument('--from-machine', type=Path)
        parser.add_argument('--config-dir', type=Path)
        parser.add_argument('--profile-selection', help='Adopt machines/<machine_id>.yaml without changing current Profile choices')
        parser.add_argument('--auto-catalog-roots', action='store_true', help='将 catalog_roots 中的目录绑定改为 auto，随 catalogs.yaml 自动定位源码缓存')
        parser.add_argument('--sync-selection', action='store_true', help='同步本机 machine.yaml 与 machines/<machine_id>.yaml 的目标清单差异（默认仅预览）')
        parser.add_argument('--select', action='append', default=[], metavar='TARGET=PROFILE',
                            help='配合 --sync-selection 显式指定新增 Agent/App 的 Profile 路径')
        parser.add_argument('--skill', action='append', default=[], metavar='AGENT=SKILL.md')
        parser.add_argument('--retire-script-install', action='store_true')
        parser.add_argument('--write', action='store_true')
        parser.add_argument('--expect-plan-id', '--plan-id', dest='expect_plan_id')
        parser.add_argument('--run', help='恢复一次 init 的运行 ID；需使用相同 Machine 和 Skill 目标')
    plan = manager = None
    raw, error, started = {}, None, False
    state = fallback_state_root()
    requested_write = '--write' in argv
    operation = argv[0]
    try:
        args = parser.parse_args(argv[1:])
        operation = 'init-rollback' if getattr(args, 'run', None) else argv[0]
        if argv[0] == 'doctor':
            raw, state = doctor(args.machine, resources=args.resources, processes=args.processes, storage=args.storage)
        else:
            manager = Setup(args)
            state = manager.state_root
            if args.run:
                from .core.journal import read_journal
                run_journal = state / 'runs' / args.run / 'journal.json'
                if run_journal.is_file() and manager.selection_path is not None:
                    with_selection = sorted({*manager.recovery_scopes, str(manager.selection_path.parent.resolve())})
                    if read_journal(run_journal).get('scopes') == with_selection:
                        manager.recovery_scopes = with_selection
            plan = rollback_plan(manager, args.run) if args.run else manager.plan()
            raw = plan.report
            if args.expect_plan_id and not plan_id_matches(raw['plan_id'], args.expect_plan_id):
                raise PlanStale('计划已变化，请重新预览')
            if args.write and raw['ready']:
                started = True
                raw = plan.apply(state)
    except (Exception, KeyboardInterrupt) as exc:
        error = {'code': 'invalid_arguments' if isinstance(exc, UsageError) else 'interrupted' if isinstance(exc, KeyboardInterrupt) else 'setup_failed',
                 'phase': 'execution' if started else 'planning', 'reason': str(exc) if isinstance(exc, (UsageError, SchemaError, PlanStale)) else '本机设置检查失败；请检查绑定路径和文件权限。',
                 'exit_code': 130 if isinstance(exc, KeyboardInterrupt) else 2}
    if error and started and plan is not None and not raw.get('recovery'):
        started = False
    report = build_report(raw, operation=operation, preview_only=not requested_write, manager=manager,
                          context={'target_label':'工具与本机绑定', 'machine_file': str(args.machine) if 'args' in locals() else None, 'machine_id': raw.get('machine_id')},
                          error=error, execution_started=started)
    if not (argv[0] == 'doctor' and 'args' in locals() and args.no_save):
        save_report(report,state)
    return output_report(report, report_format(argv), verbose=report_verbose(argv))
