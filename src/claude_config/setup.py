"""Previewable local bootstrap; package installation remains the package manager's job."""
from copy import deepcopy
from importlib.resources import files
import hashlib
import os
from pathlib import Path
import shutil
import sys
import yaml

from .profile import ReportParser, UsageError, output_report, report_format
from .core.merge import fingerprint
from .core.machine_profiles import MachineProfiles
from .core.native import parse_json, parse_yaml, snapshot_read
from .core.recovery import rollback_plan
from .core.reporting import build_report, fallback_state_root, save_report
from .core.schema import SchemaError, require, state_directory, validate_machine
from .core.transaction import FileChange, FilePlan, PlanStale, file_fingerprint

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
        if getattr(args, 'profile_selection', None):
            previous = {group: {name: entry.get('profile') for name, entry in self.machine[group].items()}
                        for group in ('agents', 'apps')}
            self.machine['profile_selection'] = args.profile_selection
            for group in ('agents', 'apps'):
                for entry in self.machine[group].values():
                    entry.pop('profile', None)
        resolved = MachineProfiles(source, self.machine, expected=pin)
        self.machine_profiles = resolved
        if getattr(args, 'profile_selection', None):
            require(all(profile is None or profile == resolved.effective[group][name]['profile']
                        for group, entries in previous.items() for name, profile in entries.items()),
                    'Repository choices differ from the current Machine; reconcile before adopting the selection file')
        self.sources.update(resolved.sources)
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
        self.recovery_scopes = sorted({str(self.machine_path.parent.resolve()),
                                       *(str(p.resolve()) for p in self.skills.values())})

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


def doctor(machine_path):
    machine_path = machine_path.expanduser().absolute()
    checks = [{'name': 'python_runtime', 'status': 'passed', 'reason': sys.executable}]
    raw, pin = snapshot_read(machine_path)
    require(pin['exists'], '未找到 Machine；请先运行 claude-config init，或指定 --machine')
    selected = MachineProfiles(machine_path)
    machine = selected.effective
    checks.append({'name': 'machine_binding', 'status': 'passed', 'reason': str(machine_path)})
    if selected.selection_path is not None:
        checks.append({'name': 'machine_profile_selection', 'status': 'passed', 'reason': str(selected.selection_path)})
    checks.append({'name': 'configuration_directory', 'status': 'passed' if Path(machine['config_dir']).is_dir() else 'warning',
                   'reason': machine['config_dir']})
    for group in ('agents','apps'):
        for name, entry in machine[group].items():
            profile = Path(machine['config_dir']) / entry['profile']
            checks.append({'name': 'profile_file', 'status': 'passed' if profile.is_file() else 'failed', 'subject': name, 'reason': str(profile)})
            key = 'executable' if group == 'agents' else 'application'
            target = Path(entry['binding'][key])
            valid = target.is_dir() if group == 'apps' else target.is_file() and os.access(target, os.X_OK)
            if group == 'apps' and sys.platform != 'darwin':
                checks.append({'name': 'app_platform', 'status': 'failed', 'subject': name, 'reason': '此 App 适配仅支持 macOS。'})
            checks.append({'name': 'native_entry', 'status': 'passed' if valid else 'failed', 'subject': name, 'reason': str(target)})
    for name, value in machine.get('bindings', {}).get('executables', {}).items():
        checks.append({'name': 'local_dependency', 'subject': name,
                       'status': 'passed' if Path(value).is_file() and os.access(value, os.X_OK) else 'failed', 'reason': value})
    for name, value in machine['catalog_roots'].items():
        checks.append({'name': 'catalog_directory', 'subject': name, 'status': 'passed' if Path(value).is_dir() else 'failed', 'reason': value})
    if not machine['agents'] and not machine['apps']:
        checks.append({'name': 'client_bindings', 'status': 'warning', 'reason': '尚未绑定 Agent 或 App；init 不推测客户端和 Profile。'})
    failed = any(row['status'] == 'failed' for row in checks)
    return {'machine_id': machine['machine_id'], 'ready': not failed, 'checks': checks, 'decisions': [],
            'blockers': [{'code':'local_dependency_missing','reason':'本机绑定所需文件或程序不存在；见检查明细。'}] if failed else []}, state_directory(machine)


def main(argv):
    parser = ReportParser(description=__doc__, prog='claude-config ' + argv[0])
    parser.add_argument('--machine', type=Path, default=default_machine())
    parser.add_argument('--format', choices=('auto','text','json'), default='auto')
    if argv[0] == 'init':
        parser.add_argument('--from-machine', type=Path)
        parser.add_argument('--config-dir', type=Path)
        parser.add_argument('--profile-selection', help='Adopt machines/<machine_id>.yaml without changing current Profile choices')
        parser.add_argument('--skill', action='append', default=[], metavar='AGENT=SKILL.md')
        parser.add_argument('--retire-script-install', action='store_true')
        parser.add_argument('--write', action='store_true')
        parser.add_argument('--expect-plan-id')
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
            raw, state = doctor(args.machine)
        else:
            manager = Setup(args)
            state = manager.state_root
            plan = rollback_plan(manager, args.run) if args.run else manager.plan()
            raw = plan.report
            if args.expect_plan_id and args.expect_plan_id != raw['plan_id']:
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
    save_report(report,state)
    return output_report(report, report_format(argv))
