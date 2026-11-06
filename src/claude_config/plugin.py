"""Unified plugin commands. Never updates this CLI, authenticates, or restarts an app."""
from pathlib import Path
import sys

from .profile import ReportParser, UsageError, report_format
from .core.manager import ProfileManager
from .core.plugin_manager import PluginManager
from .core.schema import SchemaError, require
from .core.transaction import PlanStale, UnfinishedRun
from .core.reporting import build_report, encode_json, exit_code, render_text, save_report


def summary(report):
    intent = report.get('plugin_management')
    if intent:
        before, after = intent.get('before') or {}, intent['after']
        flag = lambda value: '启用' if value is True else '禁用' if value is False else '未声明'
        lines = [f"插件：{intent['resource']}", f"Profile：{intent['profile']}",
                 f"版本：{before.get('version') or '未安装'} → {after['version']}",
                 f"状态：{flag(before.get('enabled'))} → {flag(after['enabled'])}",
                 '范围：当前 Agent 用户级插件；共用配置目录的 CLI / Desktop 会受影响']
        if intent.get('source'):
            lines.append('固定提交：' + intent['source']['commit'])
        if intent.get('resolution', {}).get('kind') == 'commit':
            lines.append('来源：明确指定的 Git 提交；不代表已发布 Release')
        if intent.get('prepared_sources'):
            lines.append('已准备来源缓存；预览尚未改变 Profile 或插件安装')
        if not report.get('preview_only', True):
            lines.append('会话效果：未验证；如有需要请重新加载插件或重开会话')
        return '\n'.join(lines)
    if report.get('errors') and 'plugins' not in report:
        return '插件操作未完成。'
    return '\n'.join(f"{row['resource']}  已装={row.get('installed_version') or '无'}  "
                     f"声明版本={row.get('selected_version') or '未验证'}  期望启用={row.get('desired_enabled')}  "
                     f"实际启用={row.get('enabled')}  状态={row['status']}  会话=未验证"
                     for row in report.get('plugins', [])) or '当前 Profile 未选择插件。'


def emit(report, mode):
    print(encode_json(report) if mode == 'json' else summary(report) + '\n\n' + render_text(report))
    return exit_code(report)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    mode = report_format(argv)
    manager = plan = service = None
    report, error = {}, None
    started = False
    preview = True
    operation = 'plugin'
    try:
        parser = ReportParser(prog='claude-config plugin', description='Manage one declared Claude Code plugin with a checked Profile/native transaction.')
        parser.add_argument('action', choices=('install', 'upgrade', 'enable', 'disable', 'list', 'status'))
        parser.add_argument('plugin', nargs='?', help='name@catalog, or an unambiguous short name')
        parser.add_argument('--agent', required=True, choices=('claude-code',))
        parser.add_argument('--machine', type=Path, default=Path.home() / '.claude-config-tool/machine.yaml')
        source = parser.add_mutually_exclusive_group()
        source.add_argument('--version', help='Exact version from a published GitHub release')
        source.add_argument('--commit', help='Full source SHA in the registered repository/path')
        parser.add_argument('--enable', action='store_true', help='Also enable while upgrading; otherwise preserve enablement')
        parser.add_argument('--disabled', action='store_true', help='Install new plugin without enabling it')
        parser.add_argument('--binding', action='append', default=[], metavar='NAME=MACHINE_REFERENCE')
        execution = parser.add_mutually_exclusive_group()
        execution.add_argument('--dry-run', action='store_true')
        execution.add_argument('--write', action='store_true')
        parser.add_argument('--expect-plan-id')
        parser.add_argument('--format', choices=('auto', 'text', 'json'), default='auto')
        args = parser.parse_args(argv)
        mode = args.format
        operation = 'plugin-' + args.action
        query = args.action in ('list', 'status')
        if (args.action == 'list') != (args.plugin is None):
            raise UsageError('list takes no plugin; all other actions require one')
        if query and (args.version or args.commit or args.enable or args.disabled or args.binding or args.write or args.expect_plan_id):
            raise UsageError('list/status are read-only queries')
        if args.action in ('enable', 'disable') and (args.version or args.commit or args.enable or args.disabled or args.binding):
            raise UsageError('enable/disable change usage only, not version or bindings')
        if args.enable and args.action != 'upgrade' or args.disabled and args.action != 'install' or args.binding and args.action != 'install':
            raise UsageError('Invalid flag for this plugin action')
        if args.write and not args.expect_plan_id:
            raise UsageError('--write requires --expect-plan-id from a reviewed preview')
        bindings = {}
        for item in args.binding:
            key, sep, value = item.partition('=')
            require(bool(sep and key and value) and key not in bindings, 'Binding must be a unique NAME=MACHINE_REFERENCE')
            bindings[key] = value
        manager = ProfileManager(args.machine, args.agent)
        service = PluginManager(manager)
        if query:
            report = service.inventory(args.plugin)
        else:
            plan = service.plan(args.action, args.plugin, version=args.version, commit=args.commit,
                                enable=args.enable, disabled=args.disabled, bindings=bindings)
            report = plan.report
            if args.expect_plan_id and args.expect_plan_id != report['plan_id']:
                raise PlanStale('Plugin plan changed; preview again')
            interactive = sys.stdin.isatty() and sys.stdout.isatty() and args.format != 'json'
            execute = args.write
            if not args.write and not args.dry_run and interactive and report['ready']:
                shown = build_report(report, operation=operation, preview_only=True, manager=manager)
                print(summary(shown) + '\n\n' + render_text(shown))
                execute = input('执行这份插件计划？[y/N] ').strip().lower() in ('y', 'yes')
            if execute and report['ready']:
                preview = False
                started = True
                report = plan.apply(manager.state_root)
        report = build_report(report, operation=operation, preview_only=preview, manager=manager, execution_started=started)
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
        report = build_report(report, operation=operation, preview_only=preview, manager=manager, error=error, execution_started=started)
    # Query is read-only even with respect to diagnostic state. Plans may save a
    # private report and source cache; only the checked executor changes installs.
    if operation not in ('plugin-list', 'plugin-status') and manager is not None:
        save_report(report, manager.state_root)
    return emit(report, mode)
