"""Unified baseline + agent + app sync workflow."""
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from copy import deepcopy
from pathlib import Path
import sys

from .baseline_cmd import inspect_baselines, sync_baseline_groups
from .core.app_manager import AppProfileManager
from .core.execution_context import progress
from .core.machine_profiles import MachineProfiles
from .core.manager import ProfileManager
from .core.report_viewer import terminal_color_enabled
from .core.reporting import build_report, encode_json, public_value, save_report
from .core.schema import SchemaError, state_directory
from .core.source_transport import DEFAULT_GIT_TRANSPORT, GIT_TRANSPORTS
from .core.transaction import PlanStale
from .profile import ReportParser, UsageError, report_format
from .status_cmd import AGENT_ORDER, APP_ORDER, _classify_target_plan, _ordered_keys, effective_file_writes

_ANSI_GREEN = '\033[32m'
_ANSI_YELLOW = '\033[33m'
_ANSI_CYAN = '\033[36m'
_ANSI_RED = '\033[31m'
_ANSI_BOLD = '\033[1m'
_ANSI_RESET = '\033[0m'


def _style(text, code, color):
    return f'{code}{text}{_ANSI_RESET}' if color else text


def _select_targets(machine, agent_filter, app_filter, *, agents_only=False):
    bound_agents = machine.get('agents', {})
    bound_apps = machine.get('apps', {})
    if agents_only and app_filter:
        raise UsageError('--agents-only 与 --app 不能同时使用')
    if agent_filter:
        for agent in agent_filter:
            if agent not in bound_agents:
                raise UsageError(f'machine.yaml 中未绑定 agent: {agent}')
    if app_filter:
        for app in app_filter:
            if app not in bound_apps:
                raise UsageError(f'machine.yaml 中未绑定 app: {app}')
    if agents_only:
        agents = list(agent_filter) if agent_filter else _ordered_keys(bound_agents, AGENT_ORDER)
        return agents, [], []
    if agent_filter is None and app_filter is None:
        all_apps = _ordered_keys(bound_apps, APP_ORDER)
        auto_apps = [app for app in all_apps if bound_apps[app].get('sync_mode', 'auto') != 'manual']
        manual_apps = [app for app in all_apps if bound_apps[app].get('sync_mode', 'auto') == 'manual']
        return _ordered_keys(bound_agents, AGENT_ORDER), auto_apps, manual_apps
    return list(agent_filter or ()), list(app_filter or ()), []


def _plan_one_target(machine_file, kind, name, baselines, *, prepare=False):
    progress(f'规划 {name} 同步')
    try:
        mgr = ProfileManager(machine_file, name) if kind == 'agent' else AppProfileManager(machine_file, name)
        plan = mgr.plan(prepare=prepare)
        item = _classify_target_plan(kind, name, mgr.entry.get('profile'), plan, baselines)
        return mgr, plan, item
    except Exception as exc:
        reason = str(exc) if isinstance(exc, SchemaError) else f'{name} 规划失败'
        item = {
            'kind': kind,
            'name': name,
            'profile': None,
            'state': 'error',
            'plan_id': None,
            'logical_changes': {'settings': 0, 'resources': 0, 'other': 0, 'total': 0},
            'pending_file_writes': 0,
            'conflicts_count': 0,
            'blockers': [],
            'reason': reason,
            'hint': f'claude-config plan --{kind} {name}',
        }
        return None, None, item


def _plan_all_targets(machine_file, selected_agents, selected_apps, baselines, *, prepare=False):
    specs = [('agent', agent) for agent in selected_agents] + [('app', app) for app in selected_apps]
    if not prepare and len(specs) > 1:
        with ThreadPoolExecutor(max_workers=min(4, len(specs))) as pool:
            futures = [
                pool.submit(copy_context().run, _plan_one_target, machine_file, kind, name, baselines, prepare=False)
                for kind, name in specs
            ]
            plans = [future.result() for future in futures]
    else:
        plans = [_plan_one_target(machine_file, kind, name, baselines, prepare=prepare) for kind, name in specs]
    items = [item for _, _, item in plans]
    return plans, items


def _save_target_report(mgr, raw, state_root, *, operation):
    context = {
        'machine_file': str(mgr.machine_path),
        'machine_id': mgr.machine['machine_id'],
        'profile': raw.get('binding', {}).get('profile') or mgr.entry.get('profile'),
    }
    if hasattr(mgr, 'agent'):
        context['agent_type'] = mgr.agent
    else:
        context['app'] = mgr.app
        context['data_root'] = str(mgr.data_root)
    report = build_report(
        raw,
        operation=operation,
        preview_only=False,
        manager=mgr,
        context=context,
        execution_started=True,
    )
    save_report(report, state_root)


def render_sync(report, *, color=False):
    header = '全局同步预览' if report['preview_only'] else '全局同步结果'
    lines = [
        _style(f"{header} ({report['machine_id']})", _ANSI_BOLD, color),
        f"  Machine: {report['machine_file']}",
    ]

    baseline_stage = report['baseline']
    if not baseline_stage.get('skipped'):
        lines.append('')
        lines.append(_style('1. 基线状态：', _ANSI_BOLD, color))
        actions = baseline_stage.get('actions', [])
        if not actions:
            lines.append(f"  {_style('✓ 已对齐', _ANSI_GREEN, color)}")
        for item in actions:
            aliases = ', '.join(item['aliases'])
            if item['status'] == 'aligned':
                lines.append(f"  {_style('✓ 已对齐', _ANSI_GREEN, color)}  {aliases}")
            elif item['status'] == 'pending':
                lines.append(f"  {_style('→ 待同步', _ANSI_CYAN, color)}  {aliases}: {(item['from_commit'] or '-')[:8]} -> {item['to_commit'][:8]}")
            else:
                lines.append(f"  {_style('✓ 已同步', _ANSI_GREEN, color)}  {aliases}: {(item['from_commit'] or '-')[:8]} -> {item['to_commit'][:8]}")

    lines.append('')
    lines.append(_style('2. 目标配置：', _ANSI_BOLD, color))
    if not report['targets']:
        lines.append('  (无选定目标)')
    for item in report['targets']:
        outcome = item['outcome']
        name = item['name']
        profile = item.get('profile') or '-'
        if outcome == 'applied':
            badge = _style('✓ 已同步', _ANSI_GREEN, color)
            lines.append(f'  {badge}  {name} [{profile}] — 已写入 {item.get("applied_changes", 0)} 项')
        elif outcome == 'synced':
            badge = _style('✓ 已最新', _ANSI_GREEN, color)
            lines.append(f'  {badge}  {name} [{profile}] — 无需更新')
        elif outcome == 'pending':
            badge = _style('→ 待同步', _ANSI_CYAN, color)
            logical = item.get('logical_changes', {})
            lines.append(f"  {badge}  {name} [{profile}] — 待应用 {logical.get('total', 0) or item.get('pending_file_writes', 0)} 项")
        elif outcome == 'skipped_conflict':
            badge = _style('! 已跳过(冲突)', _ANSI_YELLOW, color)
            lines.append(f"  {badge}  {name} [{profile}] — {item.get('conflicts_count', 0)} 处冲突，需先执行 {item.get('hint')}")
        elif outcome == 'skipped_blocked':
            badge = _style('✗ 已跳过(阻塞)', _ANSI_RED, color)
            lines.append(f"  {badge}  {name} [{profile}] — {item.get('reason') or '存在阻塞条件'}")
        else:
            badge = _style('✗ 失败', _ANSI_RED, color)
            lines.append(f"  {badge}  {name} [{profile}] — {item.get('reason') or '执行失败'}")

    manual_skipped = report.get('skipped_manual_apps', [])
    if manual_skipped:
        lines.append(f"  · 已跳过 manual App：{', '.join(manual_skipped)}（如需同步请显式指定 --app）")

    if report['preview_only'] and report.get('has_pending'):
        lines.append('')
        lines.append('提示：当前仅预览，未写入任何目标；如需执行请追加 --yes。')
    conflicts = [t for t in report['targets'] if t['outcome'] == 'skipped_conflict']
    if conflicts:
        lines.append('')
        lines.append('解决冲突后可再次运行 sync：')
        for item in conflicts:
            lines.append(f"  {item['hint']}")
    return '\n'.join(lines)


def _confirm_sync(report, color):
    print(render_sync(report, color=color))
    try:
        answer = input('\n确认执行以上同步吗？[y/N]: ').strip().lower()
    except (EOFError, KeyboardInterrupt):
        return False
    return answer in ('y', 'yes')


def _sync_exit_code(targets):
    for item in targets:
        if item['outcome'] in ('skipped_conflict', 'skipped_blocked', 'error'):
            return 1
    return 0


def run_sync(
    machine_path,
    *,
    agent_filter=None,
    app_filter=None,
    agents_only=False,
    skip_baseline=False,
    git_transport=DEFAULT_GIT_TRANSPORT,
    dry_run=False,
    yes=False,
    confirm_fn=None,
    fmt='auto',
    color=False,
):
    machine_file = Path(machine_path).expanduser().absolute()
    profiles = MachineProfiles(machine_file, strict=True)
    machine = profiles.effective
    state_root = state_directory(machine)
    selected_agents, selected_apps, skipped_manual_apps = _select_targets(
        machine, agent_filter, app_filter, agents_only=agents_only,
    )

    baselines = {'aligned': True, 'groups': []} if skip_baseline else inspect_baselines(machine_file)
    baseline_preview = None if skip_baseline else sync_baseline_groups(machine_file, write=False)

    # First pass: read-only preview across selected targets.
    _, preview_items = _plan_all_targets(machine_file, selected_agents, selected_apps, baselines, prepare=False)
    for item in preview_items:
        item['outcome'] = {
            'synced': 'synced',
            'pending': 'pending',
            'conflict': 'skipped_conflict',
            'blocked': 'skipped_blocked',
            'error': 'error',
        }[item['state']]

    baseline_pending = bool(baseline_preview and baseline_preview['pending'])
    targets_pending = any(item['state'] == 'pending' for item in preview_items)
    has_pending = baseline_pending or targets_pending

    baseline_stage = {
        'skipped': skip_baseline,
        'status': 'skipped' if skip_baseline else ('pending' if baseline_pending else 'aligned'),
        'actions': [] if skip_baseline else baseline_preview['actions'],
    }
    preview_report = {
        'schema_version': 1,
        'operation': 'sync',
        'preview_only': True,
        'machine_id': machine['machine_id'],
        'machine_file': str(machine_file),
        'state_root': str(state_root),
        'has_pending': has_pending,
        'agents_only': agents_only,
        'skipped_manual_apps': skipped_manual_apps,
        'baseline': baseline_stage,
        'targets': preview_items,
    }

    if dry_run or not has_pending:
        return preview_report, _sync_exit_code(preview_items)

    execute = bool(yes)
    if not execute:
        interactive = fmt != 'json' and sys.stdin.isatty() and sys.stdout.isatty()
        if interactive and confirm_fn is not None:
            execute = confirm_fn(preview_report, color)
            if not execute:
                preview_report['cancelled'] = True
                return preview_report, 0
        else:
            return preview_report, _sync_exit_code(preview_items)

    # Stage 1: sync baseline if pending.
    baseline_updated = False
    if not skip_baseline and baseline_pending:
        progress('阶段 1：同步基线缓存')
        b_result = sync_baseline_groups(machine_file, git_transport=git_transport, write=True)
        for raw_item in b_result.get('applied_reports', []):
            save_report(
                build_report(
                    raw_item,
                    operation='adopt-baseline',
                    preview_only=False,
                    execution_started=True,
                    context={
                        'target_label': '固定资源基线采用',
                        'machine_file': str(machine_file),
                        'machine_id': machine['machine_id'],
                    },
                ),
                state_root,
            )
        baseline_stage = {'skipped': False, 'status': 'synced', 'actions': b_result['actions']}
        baselines = inspect_baselines(machine_file)
        baseline_updated = True

    # Stage 2 & 4: fresh plan(prepare=True) and apply ready targets per target.
    if baseline_updated:
        plans, _ = _plan_all_targets(machine_file, selected_agents, selected_apps, baselines, prepare=True)
    else:
        plans = []
        for preview_item in preview_items:
            if preview_item['state'] == 'pending':
                plans.append(_plan_one_target(
                    machine_file, preview_item['kind'], preview_item['name'], baselines, prepare=True,
                ))
            else:
                plans.append((None, None, deepcopy(preview_item)))

    result_targets = []
    for mgr, plan, item in plans:
        if mgr is None or plan is None:
            item['outcome'] = {
                'synced': 'synced',
                'conflict': 'skipped_conflict',
                'blocked': 'skipped_blocked',
            }.get(item.get('state'), 'error')
            result_targets.append(item)
            continue
        if item['state'] == 'conflict':
            item['outcome'] = 'skipped_conflict'
            result_targets.append(item)
            continue
        if item['state'] == 'blocked':
            item['outcome'] = 'skipped_blocked'
            result_targets.append(item)
            continue
        if item['state'] == 'synced' or effective_file_writes(plan) == 0:
            item['outcome'] = 'synced'
            result_targets.append(item)
            continue
        progress(f"同步 {item['name']}")
        try:
            raw = plan.apply(mgr.state_root)
            _save_target_report(mgr, raw, mgr.state_root, operation=raw.get('operation', 'apply'))
            item['outcome'] = 'applied'
            item['run_id'] = raw.get('run_id') or (Path(raw['recovery']).parent.name if isinstance(raw.get('recovery'), str) else None)
            item['applied_changes'] = len([r for r in raw.get('results', []) if r.get('status') == 'applied'])
        except (SchemaError, PlanStale, OSError) as exc:
            item['outcome'] = 'error'
            item['reason'] = str(exc)
        result_targets.append(item)

    final_report = {
        'schema_version': 1,
        'operation': 'sync',
        'preview_only': False,
        'machine_id': machine['machine_id'],
        'machine_file': str(machine_file),
        'state_root': str(state_root),
        'has_pending': False,
        'agents_only': agents_only,
        'skipped_manual_apps': skipped_manual_apps,
        'baseline': baseline_stage,
        'targets': result_targets,
    }
    return final_report, _sync_exit_code(result_targets)


def _build_parser():
    parser = ReportParser(
        prog='claude-config sync',
        description='一键编排：先检查/同步基线缓存，再部署所有就绪的 Agent 与 App 配置',
    )
    parser.add_argument('--machine', type=Path, default=Path.home() / '.claude-config-tool/machine.yaml')
    parser.add_argument('--agent', action='append', choices=AGENT_ORDER, help='仅同步指定 Agent（可重复）')
    parser.add_argument('--app', action='append', choices=APP_ORDER, help='仅同步指定 App（可重复）')
    parser.add_argument('--agents-only', action='store_true', help='仅同步 Agent 配置，跳过所有桌面 App')
    parser.add_argument('--skip-baseline', action='store_true', help='跳过第 1 阶段基线检查与同步')
    parser.add_argument('--git-transport', choices=GIT_TRANSPORTS, default=DEFAULT_GIT_TRANSPORT)
    parser.add_argument('--dry-run', action='store_true', help='仅预览各阶段变更，不拉取或写入文件')
    parser.add_argument('--yes', '--write', dest='yes', action='store_true', help='跳过确认提示直接执行同步')
    parser.add_argument('--format', choices=('auto', 'text', 'json'), default='auto')
    parser.add_argument('--verbose', action='store_true')
    return parser


def main(argv):
    parser = _build_parser()
    fmt = report_format(argv)
    try:
        args = parser.parse_args(argv)
        if args.dry_run and args.yes:
            raise UsageError('--dry-run 与 --yes/--write 不能同时使用')
        color = terminal_color_enabled('text' if fmt == 'json' else fmt)
        report, code = run_sync(
            args.machine,
            agent_filter=args.agent,
            app_filter=args.app,
            agents_only=args.agents_only,
            skip_baseline=args.skip_baseline,
            git_transport=args.git_transport,
            dry_run=args.dry_run,
            yes=args.yes,
            confirm_fn=lambda rep, c: (progress(None), _confirm_sync(rep, c))[1],
            fmt=fmt,
            color=color,
        )
        progress(None)
        if report.get('cancelled'):
            print('已取消，未修改任何文件。')
            return 0
        print(encode_json(public_value(report)) if fmt == 'json' else render_sync(report, color=color))
        return code
    except (Exception, KeyboardInterrupt) as exc:
        progress(None)
        reason = str(exc) if isinstance(exc, (UsageError, SchemaError, PlanStale, ValueError)) else '同步执行失败；请检查 machine.yaml、基线及目标路径。'
        code = 'interrupted' if isinstance(exc, KeyboardInterrupt) else 'invalid_arguments' if isinstance(exc, UsageError) else 'sync_failed'
        exit_status = 130 if isinstance(exc, KeyboardInterrupt) else 2
        err_report = {
            'schema_version': 1,
            'operation': 'sync',
            'ready': False,
            'error': {'code': code, 'reason': reason, 'exit_code': exit_status},
        }
        if fmt == 'json':
            print(encode_json(public_value(err_report)))
        else:
            print(f'错误：{reason}', file=sys.stderr)
        return exit_status
