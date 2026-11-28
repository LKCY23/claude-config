"""Unified plugin commands. Never updates this CLI, authenticates, or restarts an app."""
from pathlib import Path
import sys

from .profile import ReportParser, UsageError, report_format, report_verbose
from .core.data_repo import format_dirty_hint
from .core.execution_context import prompt_input
from .core.manager import ProfileManager
from .core.plugin_manager import PluginManager
from .core.schema import SchemaError, require
from .core.source_transport import DEFAULT_GIT_TRANSPORT, GIT_TRANSPORTS
from .core.terminal_ui import badge, heading, meta_line, pad_display, style
from .core.transaction import PlanStale, UnfinishedRun, plan_id_matches
from .core.report_viewer import show_report, terminal_color_enabled, terminal_interactive
from .core.reporting import build_report, encode_json, exit_code, render_text, save_report


def summary(report, *, color=False):
    intent = report.get('plugin_management')
    if intent:
        before, after = intent.get('before') or {}, intent['after']
        flag = lambda value: '启用' if value is True else '禁用' if value is False else '未声明'
        scope_label = {
            'local': '本机临时 (--local-only，不修改仓库锁文件)',
            'lock': 'Profile 锁文件 (*.plugins.lock.yaml)',
            'unlocked': '已解除钉死（跟随 catalogs.yaml）',
        }.get(intent.get('pin_scope'), intent.get('pin_scope'))
        lines = [
            heading(f"插件管理：{intent['resource']}", color=color),
            meta_line('Profile', intent['profile'], color=color),
            meta_line('版本', f"{before.get('version') or '未安装'} → {after['version'] or '已移除'}", color=color),
            meta_line('状态', f"{flag(before.get('enabled'))} → {'已移除' if intent['action'] == 'remove' else flag(after['enabled'])}", color=color),
            meta_line('范围', '当前 Agent 用户级插件；共用配置目录的 CLI / Desktop 会受影响', color=color),
        ]
        if scope_label and intent['action'] in ('install', 'upgrade', 'unlock'):
            lines.append(meta_line('钉死方式', scope_label, color=color))
        if intent.get('also_profiles'):
            lines.append(meta_line('联动更新锁文件', '、'.join(intent['also_profiles']), color=color))
        if report.get('plan_id'):
            lines.append(f"  {style('计划 ID：', 'muted', color=color)}{style(report['plan_id'], 'cmd', color=color)}")
        if intent.get('source'):
            lines.append(meta_line('固定提交', intent['source']['commit'], color=color))
        if intent.get('resolution', {}).get('kind') == 'commit':
            lines.append(meta_line('来源', '明确指定的 Git 提交；不代表已发布 Release', color=color))
        if intent.get('prepared_sources'):
            lines.append(style('  ! 已准备来源缓存；预览尚未改变 Profile 或插件安装', 'hint', color=color))
        repo_sync = intent.get('data_repo_sync') or {}
        if repo_sync.get('status') == 'behind_overlap':
            lines.append(style(
                f"  ! 配置仓库落后 {repo_sync.get('upstream')} {repo_sync.get('behind')} 个提交且远端已修改同名文件，已阻断写入",
                'error', color=color,
            ))
        elif repo_sync.get('status') == 'behind_disjoint':
            lines.append(style(
                f"  ! 配置仓库落后 {repo_sync.get('upstream')} {repo_sync.get('behind')} 个提交（改动文件不重叠，后续 data publish 可自动变基）",
                'warning', color=color,
            ))
        elif repo_sync.get('status') == 'fetch_failed':
            lines.append(style('  · 配置仓库远端检查失败（已降级按本地仓库继续规划）', 'muted', color=color))
        if not report.get('preview_only', True):
            lines.append(style('  · 会话效果：未验证；如有需要请重新加载插件或重开会话', 'muted', color=color))
        return '\n'.join(lines)
    if report.get('errors') and 'plugins' not in report:
        return badge('error', '插件操作未完成。', color=color)
    rows = report.get('plugins', [])
    if not rows:
        return style('当前 Profile 未选择插件。', 'muted', color=color)
    lines = [heading(f"插件状态（共 {len(rows)} 项）", color=color)]
    name_w = max((len(str(row['resource'])) for row in rows), default=12)
    for row in rows:
        st = row['status']
        kind = 'success' if st in ('已同步', 'in_sync', 'ok', 'installed') else 'warning' if '待' in st or '未' in st or '冲突' in st or 'not_' in st else 'muted'
        session_note = '会话=已验证' if row.get('session_verified') else '会话=未验证'
        lines.append(
            f"  {badge(kind, st, color=color)}  {pad_display(row['resource'], name_w)}  "
            f"已装={row.get('installed_version') or '无'}  "
            f"声明={row.get('selected_version') or '未验证'}  来源={row.get('pin_source') or 'catalog'}  "
            f"期望启用={row.get('desired_enabled')}  实际启用={row.get('enabled')}  "
            f"{style(session_note, 'muted', color=color)}"
        )
    return '\n'.join(lines)


def _is_clean_noop(report):
    return (
        bool(report.get('plugin_management'))
        and bool(report.get('ready'))
        and not report.get('errors')
        and (report.get('summary') or {}).get('changes', 0) == 0
        and (report.get('execution') or {}).get('status') in ('unchanged', 'preview')
    )


def emit(report, mode, *, verbose=False):
    if mode == 'json':
        print(encode_json(report))
        return exit_code(report)
    viewed = False
    if mode == 'tui' and report['artifacts']['saved'] and terminal_interactive(mode):
        viewed = show_report(report)
    color = False if (mode != 'auto' or viewed) else terminal_color_enabled(mode)
    if not verbose and not viewed and _is_clean_noop(report):
        status_line = (
            badge('success', '预览完成，当前已与目标一致，无需变更。', color=color)
            if report.get('preview_only')
            else badge('success', '执行完成，未发生配置变化。', color=color)
        )
        tail = [summary(report, color=color), '', status_line]
        if report['artifacts'].get('saved') and report['artifacts'].get('json_path'):
            tail.append(style(f"完整 JSON：{report['artifacts']['json_path']}", 'muted', color=color))
        tail.append(style('如需完整明细请追加 --verbose 或运行 claude-config report last。', 'muted', color=color))
        print('\n'.join(tail))
        return exit_code(report)
    print(summary(report, color=color) + '\n\n' + render_text(report, brief=viewed, verbose=verbose, color=color))
    return exit_code(report)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    mode = report_format(argv)
    verbose = report_verbose(argv)
    manager = plan = service = None
    report, error = {}, None
    started = False
    preview = True
    operation = 'plugin'
    context = {}
    try:
        parser = ReportParser(prog='claude-config plugin', description='Manage one declared Claude Code or Antigravity plugin with a checked Profile/native transaction.')
        parser.add_argument('action', choices=('install', 'upgrade', 'unlock', 'enable', 'disable', 'remove', 'list', 'status'))
        parser.add_argument('plugin', nargs='?', help='name@catalog, or an unambiguous short name')
        parser.add_argument('--agent', required=True, choices=('claude-code', 'antigravity'))
        parser.add_argument('--machine', type=Path, default=Path.home() / '.claude-config-tool/machine.yaml')
        source = parser.add_mutually_exclusive_group()
        source.add_argument('--version', help='Exact version from a published GitHub release')
        source.add_argument('--commit', help='Full source SHA in the registered repository/path')
        parser.add_argument('--enable', action='store_true', help='Also enable while upgrading; otherwise preserve enablement')
        parser.add_argument('--disabled', action='store_true', help='Install new plugin without enabling it')
        parser.add_argument('--binding', action='append', default=[], metavar='NAME=MACHINE_REFERENCE')
        pin_scope_group = parser.add_mutually_exclusive_group()
        pin_scope_group.add_argument('--local-only', action='store_true', help='Pin version in machine-local .state/local-pins.yaml without modifying Git locks')
        pin_scope_group.add_argument('--promote', action='store_true', help='Promote a --local-only pin (or new pin) into Profile .plugins.lock.yaml')
        parser.add_argument('--also-profiles', action='append', default=[], metavar='PROFILE', help='Also update another Profile lock file with this verified pin')
        parser.add_argument('--git-transport', choices=GIT_TRANSPORTS, default=DEFAULT_GIT_TRANSPORT,
                            help='Git transport for preparing missing sources (default: gh-https)')
        execution = parser.add_mutually_exclusive_group()
        execution.add_argument('--dry-run', action='store_true')
        execution.add_argument('--write', action='store_true', help='Generate, validate and execute a fresh plan; no manual plan ID needed')
        parser.add_argument('--expect-plan-id', '--plan-id', dest='expect_plan_id',
                            help='Optional: require a reviewed plan ID (full or 8+ char prefix)')
        parser.add_argument('--format', choices=('auto', 'text', 'tui', 'json'), default='auto')
        parser.add_argument('--verbose', action='store_true', help='显示完整保留决策与细粒度输出')
        args = parser.parse_args(argv)
        context = {'agent_type': args.agent}
        if args.action == 'remove' and args.agent != 'antigravity':
            raise UsageError('remove is currently supported for --agent antigravity only')
        mode = args.format
        operation = 'plugin-' + args.action
        query = args.action in ('list', 'status')
        if (args.action == 'list') != (args.plugin is None):
            raise UsageError('list takes no plugin; all other actions require one')
        if query and (args.version or args.commit or args.enable or args.disabled or args.binding or args.local_only or args.promote or args.also_profiles or args.write or args.expect_plan_id or args.git_transport != DEFAULT_GIT_TRANSPORT):
            raise UsageError('list/status are read-only queries')
        if args.action in ('enable', 'disable', 'remove', 'unlock') and (args.version or args.commit or args.enable or args.disabled or args.binding or args.local_only or args.promote or args.also_profiles or args.git_transport != DEFAULT_GIT_TRANSPORT):
            raise UsageError('enable/disable/remove/unlock cannot select versions, bindings, or pin-scope flags')
        if args.local_only and args.also_profiles:
            raise UsageError('--also-profiles cannot be combined with --local-only')
        if args.enable and args.action != 'upgrade' or args.disabled and args.action != 'install' or args.binding and args.action != 'install':
            raise UsageError('Invalid flag for this plugin action')
        bindings = {}
        for item in args.binding:
            key, sep, value = item.partition('=')
            require(bool(sep and key and value) and key not in bindings, 'Binding must be a unique NAME=MACHINE_REFERENCE')
            bindings[key] = value
        manager = ProfileManager(args.machine, args.agent)
        service = PluginManager(manager, git_transport=args.git_transport)
        if query:
            report = service.inventory(args.plugin)
        else:
            plan = service.plan(args.action, args.plugin, version=args.version, commit=args.commit,
                                enable=args.enable, disabled=args.disabled, bindings=bindings,
                                local_only=args.local_only, promote=args.promote, also_profiles=args.also_profiles)
            report = plan.report
            if args.expect_plan_id and not plan_id_matches(report['plan_id'], args.expect_plan_id, strict_format=False):
                raise PlanStale('Plugin plan changed; preview again')
            interactive = sys.stdin.isatty() and sys.stdout.isatty() and args.format != 'json'
            execute = args.write
            if not args.write and not args.dry_run and interactive and report['ready']:
                confirm_color = terminal_color_enabled(args.format)
                shown = build_report(report, operation=operation, preview_only=True, manager=manager, context=context)
                print(summary(shown, color=confirm_color) + '\n\n' + render_text(shown, color=confirm_color))
                execute = prompt_input('执行这份插件计划？[y/N] ').strip().lower() in ('y', 'yes')
            if execute and report['ready']:
                preview = False
                started = True
                report = plan.apply(manager.state_root)
        report = build_report(report, operation=operation, preview_only=preview, manager=manager, context=context, execution_started=started)
    except (Exception, KeyboardInterrupt) as exc:
        report = plan.report if plan else report
        if service is not None and service.prepared:
            report['prepared_sources'] = service.prepared
        code = ('invalid_arguments' if isinstance(exc, UsageError) else 'plan_stale' if isinstance(exc, PlanStale)
                else 'unfinished_run' if isinstance(exc, UnfinishedRun) else 'interrupted' if isinstance(exc, KeyboardInterrupt)
                else 'invalid_configuration' if isinstance(exc, SchemaError) else 'operation_failed')
        # Only framework-authored diagnostics are safe to expose. Native command
        # stderr and arbitrary YAML values can contain credentials.
        message = str(exc) if isinstance(exc, (UsageError, SchemaError)) else {
            'plan_stale': '预览后来源、配置或安装已变化；重新预览后再执行。',
            'unfinished_run': '存在未完成执行；先用 runs 检查，再 rollback 或 reconcile-run。',
            'interrupted': '操作中断；请检查报告中的恢复状态。'}.get(code, '插件操作失败；请检查执行与恢复记录。')
        error = {'code': code, 'phase': 'execution' if started else 'planning', 'error_type': type(exc).__name__,
                 'reason': message, 'exit_code': 130 if isinstance(exc, KeyboardInterrupt) else 2 if started else 1}
        if isinstance(exc, UnfinishedRun):
            error['runs'] = exc.runs
        report.setdefault('blockers', []).append({'code': code, 'reason': message})
        report = build_report(report, operation=operation, preview_only=preview, manager=manager, context=context, error=error, execution_started=started)
    # Query is read-only even with respect to diagnostic state. Plans may save a
    # private report and source cache; only the checked executor changes installs.
    if operation not in ('plugin-list', 'plugin-status') and manager is not None:
        save_report(report, manager.state_root)
    code = emit(report, mode, verbose=verbose)
    if code == 0 and mode != 'json' and not preview and error is None and manager is not None:
        hint = format_dirty_hint(manager.root)
        if hint:
            print('\n' + hint)
    return code
