"""Read-only global status dashboard across machine bindings, baselines, agents, and apps."""
import hashlib
from pathlib import Path
import sys

from .baseline_cmd import inspect_baselines
from .core.app_manager import AppProfileManager
from .core.execution_context import progress
from .core.machine_profiles import MachineProfiles
from .core.manager import ProfileManager
from .core.report_viewer import terminal_color_enabled
from .core.reporting import _logical_changes, build_report, encode_json, public_value
from .core.schema import SchemaError, state_directory
from .profile import ReportParser, UsageError, report_format

AGENT_ORDER = ('claude-code', 'codex', 'antigravity')
APP_ORDER = ('codex-app', 'claude-desktop')

_ANSI_GREEN = '\033[32m'
_ANSI_YELLOW = '\033[33m'
_ANSI_CYAN = '\033[36m'
_ANSI_RED = '\033[31m'
_ANSI_BOLD = '\033[1m'
_ANSI_RESET = '\033[0m'


def _style(text, code, color):
    return f'{code}{text}{_ANSI_RESET}' if color else text


def _short_id(value):
    return value[:12] if isinstance(value, str) and len(value) >= 12 else value


def _ordered_keys(mapping, preferred):
    seen = []
    for key in preferred:
        if key in mapping:
            seen.append(key)
    for key in mapping:
        if key not in seen:
            seen.append(key)
    return seen


def effective_file_writes(plan):
    """Count planned file or native-operation writes that actually mutate target state."""
    writes = len(getattr(plan, 'native_operations', ()))
    paths = getattr(plan, 'paths', {})
    for change in getattr(plan, 'changes', ()):
        entry = paths.get(Path(change.path).absolute(), {})
        expected = entry.get('file') if isinstance(entry, dict) else None
        if not isinstance(expected, dict):
            expected = change.expected if isinstance(change.expected, dict) else {}
        exists = bool(expected.get('exists', False))
        if change.delete:
            if exists:
                writes += 1
            continue
        if not exists:
            writes += 1
            continue
        if hashlib.sha256(change.content).hexdigest() != expected.get('sha256'):
            writes += 1
            continue
        if change.mode is not None and (change.mode & 0o777) != expected.get('mode'):
            writes += 1
    return writes


def _classify_target_plan(kind, name, profile_name, plan, baselines):
    raw = plan.report
    built = build_report(raw, operation='plan', preview_only=True)
    logical_raw = _logical_changes(built.get('changes', []))
    logical = {
        'settings': logical_raw['settings'],
        'resources': logical_raw['packages'],
        'other': logical_raw['other'],
        'total': logical_raw['settings'] + logical_raw['packages'] + logical_raw['other'],
    }
    pending_writes = effective_file_writes(plan)
    blockers = list(raw.get('blockers', []))
    decisions = list(raw.get('decisions', []))
    resource_conflicts = list(raw.get('resource_conflicts', []))

    has_conflict = bool(resource_conflicts) or any(d.get('action') == 'conflict' for d in decisions)
    conflict_count = len(resource_conflicts) + sum(
        1 for d in decisions if d.get('action') == 'conflict'
    )

    if has_conflict:
        state = 'conflict'
        hint = f'claude-config resolve --agent {name}' if kind == 'agent' else f'claude-config plan --app {name}'
    elif blockers:
        state = 'blocked'
        hint = f'claude-config plan --{"agent" if kind == "agent" else "app"} {name}'
    elif logical['total'] > 0 or pending_writes > 0:
        state = 'pending'
        hint = f'claude-config sync --{kind} {name}'
    else:
        state = 'synced'
        hint = None

    reason = blockers[0].get('reason') if blockers else None

    return {
        'kind': kind,
        'name': name,
        'profile': profile_name,
        'state': state,
        'plan_id': raw.get('plan_id'),
        'logical_changes': logical,
        'pending_file_writes': pending_writes,
        'conflicts_count': conflict_count,
        'blockers': [
            {k: b[k] for k in ('code', 'path', 'reason') if k in b}
            for b in blockers
        ],
        'reason': reason,
        'hint': hint,
    }


def inspect_status(machine_path, *, agent_filter=None, app_filter=None):
    """Collect a strictly read-only global overview of machine, baselines, and bound targets."""
    machine_file = Path(machine_path).expanduser().absolute()
    profiles = MachineProfiles(machine_file, strict=False)
    machine = profiles.effective
    state_root = state_directory(machine)

    try:
        baselines = inspect_baselines(machine_file)
    except (SchemaError, UsageError, OSError) as exc:
        baselines = {
            'aligned': False,
            'error': str(exc),
            'groups': [],
            'catalogs': [],
        }

    bound_agents = machine.get('agents', {})
    bound_apps = machine.get('apps', {})

    if agent_filter is not None:
        for agent in agent_filter:
            if agent not in bound_agents:
                raise UsageError(f'machine.yaml 中未绑定 agent: {agent}')
    if app_filter is not None:
        for app in app_filter:
            if app not in bound_apps:
                raise UsageError(f'machine.yaml 中未绑定 app: {app}')

    if agent_filter is None and app_filter is None:
        selected_agents = _ordered_keys(bound_agents, AGENT_ORDER)
        selected_apps = _ordered_keys(bound_apps, APP_ORDER)
    else:
        selected_agents = list(agent_filter or ()) if agent_filter else []
        selected_apps = list(app_filter or ()) if app_filter else []

    targets = []
    empty_logical = {'settings': 0, 'resources': 0, 'other': 0, 'total': 0}
    for agent in selected_agents:
        profile_name = bound_agents[agent].get('profile')
        if not profile_name:
            targets.append({
                'kind': 'agent',
                'name': agent,
                'profile': None,
                'state': 'error',
                'plan_id': None,
                'logical_changes': dict(empty_logical),
                'pending_file_writes': 0,
                'conflicts_count': 0,
                'blockers': [],
                'reason': f'{agent} 仅存在于本机 machine.yaml，缺少 machines/<id>.yaml 中的 Profile 声明',
                'hint': 'claude-config doctor',
            })
            continue
        progress(f'检查 {agent} 状态')
        try:
            mgr = ProfileManager(machine_file, agent, machine_strict=False)
            plan = mgr.plan(prepare=False)
            targets.append(_classify_target_plan('agent', agent, profile_name, plan, baselines))
        except Exception as exc:
            reason = str(exc) if isinstance(exc, SchemaError) else f'{agent} 状态检查失败'
            targets.append({
                'kind': 'agent',
                'name': agent,
                'profile': profile_name,
                'state': 'error',
                'plan_id': None,
                'logical_changes': dict(empty_logical),
                'pending_file_writes': 0,
                'conflicts_count': 0,
                'blockers': [],
                'reason': reason,
                'hint': 'claude-config baseline sync --write' if not baselines.get('aligned', True) else f'claude-config plan --agent {agent}',
            })

    for app in selected_apps:
        profile_name = bound_apps[app].get('profile')
        if not profile_name:
            targets.append({
                'kind': 'app',
                'name': app,
                'profile': None,
                'state': 'error',
                'plan_id': None,
                'logical_changes': dict(empty_logical),
                'pending_file_writes': 0,
                'conflicts_count': 0,
                'blockers': [],
                'reason': f'{app} 仅存在于本机 machine.yaml，缺少 machines/<id>.yaml 中的 Profile 声明',
                'hint': 'claude-config doctor',
            })
            continue
        progress(f'检查 {app} 状态')
        try:
            mgr = AppProfileManager(machine_file, app, machine_strict=False)
            plan = mgr.plan(prepare=False)
            targets.append(_classify_target_plan('app', app, profile_name, plan, baselines))
        except Exception as exc:
            reason = str(exc) if isinstance(exc, SchemaError) else f'{app} 状态检查失败'
            targets.append({
                'kind': 'app',
                'name': app,
                'profile': profile_name,
                'state': 'error',
                'plan_id': None,
                'logical_changes': dict(empty_logical),
                'pending_file_writes': 0,
                'conflicts_count': 0,
                'blockers': [],
                'reason': reason,
                'hint': f'claude-config plan --app {app}',
            })

    counts = {'synced': 0, 'pending': 0, 'conflict': 0, 'blocked': 0, 'error': 0}
    for item in targets:
        counts[item['state']] = counts.get(item['state'], 0) + 1

    return {
        'schema_version': 1,
        'operation': 'status',
        'read_only': True,
        'machine_id': machine['machine_id'],
        'machine_file': str(machine_file),
        'state_root': str(state_root),
        'mismatches': list(profiles.mismatches),
        'baselines': {
            'aligned': baselines.get('aligned', True),
            'error': baselines.get('error'),
            'groups': [
                {
                    'primary': g['primary'],
                    'aliases': g['aliases'],
                    'repo': g['repo'],
                    'catalog_commit': g['catalog_commit'],
                    'cached_commit': g['cached_commit'],
                    'status': g['status'],
                    'needs_sync': g['needs_sync'],
                }
                for g in baselines.get('groups', [])
            ],
        },
        'targets': targets,
        'summary': counts,
    }


def _describe_target(item, *, color=False):
    state = item['state']
    logical = item['logical_changes']
    if state == 'synced':
        badge = _style('✓ 已同步', _ANSI_GREEN, color)
        detail = '无待应用变更'
    elif state == 'pending':
        badge = _style('→ 待同步', _ANSI_CYAN, color)
        parts = []
        if logical.get('settings'):
            parts.append(f"{logical['settings']} 项设置")
        if logical.get('resources'):
            parts.append(f"{logical['resources']} 项资源")
        if not parts:
            parts.append(f"{item['pending_file_writes']} 项状态更新")
        detail = '、'.join(parts)
        if item.get('plan_id'):
            detail += f" (plan {_short_id(item['plan_id'])})"
    elif state == 'conflict':
        badge = _style('! 有冲突', _ANSI_YELLOW, color)
        detail = f"{item['conflicts_count']} 处冲突待解决"
    elif state == 'blocked':
        badge = _style('✗ 被阻塞', _ANSI_RED, color)
        detail = item.get('reason') or '存在阻塞条件'
    else:
        badge = _style('✗ 检查失败', _ANSI_RED, color)
        detail = item.get('reason') or '检查失败'
    return badge, detail


def render_status(report, *, color=False):
    lines = [
        _style(f"全局配置状态 ({report['machine_id']})", _ANSI_BOLD, color),
        f"  Machine: {report['machine_file']}",
    ]

    mismatches = report.get('mismatches', [])
    if mismatches:
        lines.append(_style('  ⚠ machine.yaml 与 machines/<id>.yaml 存在声明差异（已降级只读展示）：', _ANSI_YELLOW, color))
        for row in mismatches[:3]:
            msg = row['message'] if isinstance(row, dict) and 'message' in row else str(row)
            lines.append(f'    - {msg}')
        if len(mismatches) > 3:
            lines.append(f'    - 其余 {len(mismatches) - 3} 项请运行 claude-config doctor 查看')

    lines.append('')
    lines.append(_style('基线缓存：', _ANSI_BOLD, color))
    baselines = report['baselines']
    if baselines.get('error'):
        lines.append(f"  {_style('✗ 读取失败', _ANSI_RED, color)}  {baselines['error']}")
    elif not baselines.get('groups'):
        lines.append('  (未声明 catalog)')
    else:
        for g in baselines['groups']:
            aliases = ', '.join(g['aliases'])
            declared = (g['catalog_commit'] or '-')[:8]
            cached = (g['cached_commit'] or '-')[:8]
            if not g['needs_sync']:
                badge = _style('✓ 已对齐', _ANSI_GREEN, color)
                lines.append(f'  {badge}  {aliases} @ {declared}')
            else:
                badge = _style('! 需同步', _ANSI_YELLOW, color)
                lines.append(f'  {badge}  {aliases}: 本地 {cached} -> 声明 {declared}')

    lines.append('')
    lines.append(_style('配置目标：', _ANSI_BOLD, color))
    if not report['targets']:
        lines.append('  (未绑定 agent 或 app)')
    for item in report['targets']:
        badge, detail = _describe_target(item, color=color)
        profile_label = item.get('profile') or '-'
        lines.append(f"  {badge}  {item['name']} [{profile_label}] — {detail}")

    hints = []
    if not baselines.get('aligned', True):
        hints.append('claude-config baseline sync --write')
    for item in report['targets']:
        if item['state'] == 'conflict' and item.get('hint'):
            hints.append(item['hint'])
    if any(item['state'] == 'pending' for item in report['targets']):
        hints.append('claude-config sync')
    if hints:
        lines.append('')
        lines.append('建议下一步：')
        for cmd in dict.fromkeys(hints):
            lines.append(f'  {cmd}')
    return '\n'.join(lines)


def _build_parser():
    parser = ReportParser(
        prog='claude-config status',
        description='只读查看基线缓存及所有已绑定 Agent/App 的同步状态',
    )
    parser.add_argument('--machine', type=Path, default=Path.home() / '.claude-config-tool/machine.yaml')
    parser.add_argument('--agent', action='append', choices=AGENT_ORDER, help='只查看指定 Agent（可重复）')
    parser.add_argument('--app', action='append', choices=APP_ORDER, help='只查看指定 App（可重复）')
    parser.add_argument('--format', choices=('auto', 'text', 'json'), default='auto')
    parser.add_argument('--verbose', action='store_true')
    return parser


def main(argv):
    parser = _build_parser()
    fmt = report_format(argv)
    try:
        args = parser.parse_args(argv)
        color = terminal_color_enabled('text' if fmt == 'json' else fmt)
        report = inspect_status(args.machine, agent_filter=args.agent, app_filter=args.app)
        progress(None)
        print(encode_json(public_value(report)) if fmt == 'json' else render_status(report, color=color))
        return 0
    except (Exception, KeyboardInterrupt) as exc:
        progress(None)
        reason = str(exc) if isinstance(exc, (UsageError, SchemaError, ValueError)) else '状态检查失败；请检查 machine.yaml 与绑定路径。'
        code = 'interrupted' if isinstance(exc, KeyboardInterrupt) else 'invalid_arguments' if isinstance(exc, UsageError) else 'status_failed'
        exit_status = 130 if isinstance(exc, KeyboardInterrupt) else 2
        err_report = {
            'schema_version': 1,
            'operation': 'status',
            'ready': False,
            'error': {'code': code, 'reason': reason, 'exit_code': exit_status},
        }
        if fmt == 'json':
            print(encode_json(public_value(err_report)))
        else:
            print(f'错误：{reason}', file=sys.stderr)
        return exit_status
