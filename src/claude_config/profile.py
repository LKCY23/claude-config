#!/usr/bin/env python3
"""Strict Agent and App Profile plans. No legacy fallback."""
import argparse
import errno
import json
from pathlib import Path
import sys
import yaml

from claude_config.core.manager import ProfileManager
from claude_config.core.app_manager import AppProfileManager
from claude_config.core.native import parse_json, snapshot_read
from claude_config.core.schema import SchemaError
from claude_config.core.transaction import PlanStale, UnfinishedRun
from claude_config.core.execution_context import progress_output, progress
from claude_config.core.journal import unfinished
from claude_config.core.recovery import rollback_plan
from claude_config.core.baseline import adopt_baseline
from claude_config.core.reporting import build_report, encode_json, exit_code, fallback_state_root, render_text, save_report
from claude_config.core.report_viewer import show_report, terminal_interactive


def render(report, mode):
    if mode == 'json':
        return encode_json(report)
    return render_text(report)


class UsageError(ValueError):
    pass


class ReportParser(argparse.ArgumentParser):
    def error(self, message):
        # argparse may echo arbitrary argument values. Do not persist those as
        # diagnostics; the supported usage remains available through --help.
        if '--no-input' in message:
            raise UsageError('命令参数 --no-input 已移除；静态文字请使用 --format text。')
        raise UsageError('命令参数不完整或不合法；请用 --help 核对参数及允许值。')


def output_report(report, mode):
    progress(None)
    viewed = False
    if report['artifacts']['saved'] and terminal_interactive(mode):
        viewed = show_report(report)
    print(render(report, 'json') if mode == 'json' else render_text(report, brief=viewed))
    return exit_code(report)


def report_format(argv):
    # Honor the final explicit format even when argument parsing fails.
    mode = 'auto'
    for index, item in enumerate(argv):
        candidate = item.partition('=')[2] if item.startswith('--format=') else argv[index + 1] if item == '--format' and index + 1 < len(argv) else None
        if candidate in ('auto', 'json', 'text'):
            mode = candidate
    return mode


def main(argv=None):
    with progress_output(sys.stderr):
        return _entry(argv)


def _entry(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    operations = ('plan', 'validate', 'apply', 'export', 'switch', 'rollback', 'adopt-baseline', 'review-conflicts', 'reconcile-run')
    operation = next((item for item in argv if item in operations), 'unknown')
    mode = report_format(argv)
    try:
        return _main(argv)
    except UsageError as exc:
        report = build_report({}, operation=operation,
                              error={'code': 'invalid_arguments', 'phase': 'arguments',
                                     'reason': str(exc), 'exit_code': 2})
        save_report(report, fallback_state_root())
        return output_report(report, mode)


def _main(argv):
    parser = ReportParser(description=__doc__, prog="claude-config")
    parser.add_argument('operation', choices=('plan', 'validate', 'apply', 'export', 'switch', 'rollback', 'adopt-baseline', 'review-conflicts', 'reconcile-run'))
    parser.add_argument('--machine', type=Path, default=Path.home() / '.claude-config-tool/machine.yaml')
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument('--agent', choices=('claude-code', 'codex'))
    target.add_argument('--app', choices=('codex-app', 'claude-desktop'))
    parser.add_argument('--profile', help='Config-dir-relative Profile of the selected type')
    parser.add_argument('--run', help='New-format run ID to restore; rollback previews by default')
    parser.add_argument('--field', action='append', default=[], help='Export atomic field as /settings/...')
    parser.add_argument('--resource', action='append', default=[], help='Export portable usage of an explicitly declared ref@catalog')
    parser.add_argument('--adopt-plugin', action='append', default=[], metavar='REF=SELECTOR', help='Explicitly adopt an exact existing native plugin; no implicit upgrade')
    parser.add_argument('--replace-plugin', action='append', default=[], metavar='REF=FILE', help='Explicit existing plugin version update with pinned local recovery declaration')
    parser.add_argument('--catalog', help='Existing catalog alias for baseline adoption')
    parser.add_argument('--commit', help='Full released catalog commit')
    parser.add_argument('--checkout', type=Path, help='Already prepared exact catalog repository root')
    parser.add_argument('--release-record', type=Path, help='Prepared publisher baseline record')
    parser.add_argument('--source-map', type=Path, help='Explicit exact old/new catalog repository, commit and path mapping')
    parser.add_argument('--policy', choices=('source_wins', 'keep_target', 'fail_if_different'))
    parser.add_argument('--field-policy', action='append', default=[], metavar='POINTER=POLICY')
    parser.add_argument('--resolutions', type=Path, help='Fingerprint-scoped drift resolutions JSON')
    parser.add_argument('--expect-plan-id', help='Block if fresh plan differs from reviewed preview')
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--write', action='store_true', help='Write an export, rollback, baseline selection or private conflict review; all preview by default')
    parser.add_argument('--format', choices=('auto', 'json', 'text'), default='auto', help='auto: interactive TTY or static text; json: machine output')
    args = parser.parse_args(argv)
    if (args.adopt_plugin or args.replace_plugin) and (args.app or args.operation not in ('plan', 'validate', 'apply', 'switch')):
        parser.error('Plugin adoption/replacement inputs are only for Agent plan/validate/apply/switch')
    if args.app and (args.resource or args.operation in ('adopt-baseline', 'review-conflicts', 'reconcile-run')):
        parser.error('App Profiles do not manage Agent resources or catalog baselines')
    if args.operation == 'switch' and not args.profile:
        parser.error('switch requires --profile')
    if args.write and args.operation not in ('export', 'rollback', 'adopt-baseline', 'review-conflicts', 'reconcile-run'):
        parser.error('--write is only for export/rollback/adopt-baseline/review-conflicts')
    baseline_args = (args.catalog, args.commit, args.checkout, args.release_record)
    if args.operation == 'adopt-baseline' and not all(baseline_args):
        parser.error('adopt-baseline requires --catalog, --commit, --checkout and --release-record')
    if args.operation != 'adopt-baseline' and any(baseline_args):
        parser.error('Baseline arguments are only for adopt-baseline')
    if args.source_map and args.operation != 'adopt-baseline':
        parser.error('--source-map is only for adopt-baseline')
    if args.operation == 'adopt-baseline' and (args.policy or args.field_policy or args.resolutions):
        parser.error('Baseline adoption selects an exact release, not a native usage merge policy')
    if (args.operation in ('rollback', 'reconcile-run')) != bool(args.run):
        parser.error('rollback/reconcile-run require --run')
    if args.operation in ('rollback', 'reconcile-run') and (args.policy or args.field_policy or args.resolutions):
        parser.error('rollback uses recorded snapshots, not settings merge policies')
    if args.field and args.operation != 'export':
        parser.error('--field is only for export')
    if args.resource and args.operation != 'export':
        parser.error('--resource is only for export')
    if args.profile and args.operation not in ('switch', 'export'):
        parser.error('--profile is only for switch/export; other operations use the Machine binding')
    plan, manager, error = None, None, None
    write = args.operation in ('apply', 'switch') or args.operation in ('export', 'rollback', 'adopt-baseline', 'review-conflicts', 'reconcile-run') and args.write
    started, preview_only = False, args.dry_run or not write
    phase = 'planning'
    try:
        args.machine = args.machine.expanduser().absolute()
        if not args.machine.is_file():
            raise SchemaError('未找到 Machine；请运行 claude-config init，或指定 --machine。')
        policy = {}
        if args.policy:
            policy['default'] = args.policy
        for value in args.field_policy:
            key, separator, action = value.partition('=')
            if not separator:
                raise SchemaError('field-policy requires POINTER=POLICY')
            if key in policy.setdefault('fields', {}):
                raise SchemaError('Duplicate field-policy')
            policy['fields'][key] = action
        resolution_source = snapshot_read(args.resolutions) if args.resolutions else None
        resolutions = parse_json(resolution_source[0]) if resolution_source else None
        manager = AppProfileManager(args.machine, args.app) if args.app else ProfileManager(args.machine, args.agent)
        progress('检查 ' + (args.app or args.agent) + ' 的配置、资源及未完成事务')
        if args.operation in ('plan', 'validate', 'apply', 'switch'):
            pending = unfinished(manager.state_root, [str(manager.native_path.resolve())])
            if pending:
                raise UnfinishedRun(pending)
        if resolution_source:
            manager.sources[args.resolutions.absolute()] = resolution_source[1]
        if args.operation == 'adopt-baseline':
            plan = adopt_baseline(manager, args.catalog, args.commit, args.checkout, args.release_record, args.source_map)
        elif args.operation == 'reconcile-run':
            from .core.run_reconciliation import reconcile_plan
            plan = reconcile_plan(manager, args.run)
        elif args.operation == 'rollback':
            plan = rollback_plan(manager, args.run)
        elif args.operation == 'export':
            if resolutions:
                raise SchemaError('Drift resolutions belong to native apply/switch')
            extra = {} if args.app else {'resource_refs': args.resource}
            plan = manager.export(args.field, profile=args.profile, run_policy=policy, **extra)
        elif args.operation == 'review-conflicts':
            plan = manager.review_conflicts(run_policy=policy, resolutions=resolutions)
        else:
            adoption = {}
            for item in args.adopt_plugin:
                ref, separator, selector = item.partition('=')
                if not separator or not ref or not selector or ref in adoption:
                    raise SchemaError('adopt-plugin requires a unique REF=SELECTOR mapping')
                adoption[ref] = selector
            replacements = {}
            for item in args.replace_plugin:
                ref, separator, file = item.partition('=')
                if not separator or not ref or not file or ref in replacements:
                    raise SchemaError('replace-plugin requires a unique REF=FILE mapping')
                replacements[ref] = file
            extra = {**({'adopt_plugins': adoption} if adoption else {}), **({'replace_plugins': replacements} if replacements else {})}
            plan = manager.plan(profile=args.profile, run_policy=policy, resolutions=resolutions, **extra)
        report = plan.report
        report['mode'] = args.operation
        if args.expect_plan_id and report['plan_id'] != args.expect_plan_id:
            raise PlanStale('Reviewed plan has changed; generate a new preview')
        report['preview_only'] = preview_only
        if write and not args.dry_run and report['ready']:
            phase = 'execution'
            started = True
            report = plan.apply(manager.state_root)
    except (Exception, KeyboardInterrupt) as exc:
        # Native parser/error text can embed input values. Classification and an
        # existing safe plan explain failures without dumping original config.
        report = plan.report if plan else {'checks': [], 'decisions': [], 'blockers': [], 'results': []}
        started = started and bool(report.get('recovery'))
        syntax_error = isinstance(exc, (yaml.YAMLError, json.JSONDecodeError)) or isinstance(exc, ValueError) and str(exc) == 'Native configuration cannot be parsed; no writes are allowed'
        code = ('unfinished_run' if isinstance(exc, UnfinishedRun) else 'interrupted' if isinstance(exc, KeyboardInterrupt) else 'plan_stale' if isinstance(exc, PlanStale)
                else 'invalid_configuration' if isinstance(exc, SchemaError) else 'invalid_configuration_syntax' if syntax_error
                else 'file_access_failed' if isinstance(exc, OSError) else 'operation_failed')
        reason = {'unfinished_run': '目标存在未完成事务；先使用 runs 检查，再预览 rollback 或 reconcile-run。',
                  'interrupted': '操作被中断；请核对本次修改与恢复记录。',
                  'plan_stale': '来源、目标或处理决策已变化；请重新预览后再执行。',
                  'invalid_configuration': '配置声明或原生接口检查不通过；请核对选定 Profile、字段范围及本机绑定。',
                  'invalid_configuration_syntax': '配置文件无法解析；请检查 JSON、TOML 或 YAML 的语法。',
                  'file_access_failed': '文件或程序无法访问；请检查目标路径和访问权限。',
                  'operation_failed': '配置操作未完成；请核对出错阶段、目标路径及恢复日志。'}[code]
        error = {'code': code, 'phase': phase, 'error_type': type(exc).__name__, 'reason': reason,
                 'exit_code': 130 if isinstance(exc, KeyboardInterrupt) else 1 if not started and (syntax_error or isinstance(exc, (SchemaError, PlanStale))) else 2}
        if isinstance(exc, PlanStale):
            error['detail'] = exc.detail
        if isinstance(exc, UnfinishedRun):
            error['runs'] = exc.runs
        if isinstance(exc, OSError):
            error['errno'] = exc.errno
            if exc.filename:
                error['file'] = str(exc.filename)
            error['reason'] = {errno.ENOENT: '找不到所需文件或程序；请核对本机绑定和目标路径。',
                               errno.EACCES: '没有访问目标文件或程序的权限。', errno.ENOSPC: '磁盘空间不足。',
                               errno.EROFS: '目标文件系统只读。'}.get(exc.errno, reason)
        if isinstance(exc, SchemaError):
            # SchemaError is authored by the framework with field identities
            # and constraints; native parser/subprocess exception text is never
            # included. Existing secret-field redaction still applies upstream.
            error['detail'] = str(exc)
        report['blockers'].append({'code': code, 'reason': error['reason']})
    context = {'machine_file': str(args.machine.absolute()), 'agent_type': args.agent, 'app': args.app, 'profile': args.profile}
    report = build_report(report, operation=args.operation, preview_only=preview_only,
                          manager=manager, context=context, error=error, execution_started=started)
    save_report(report, manager.state_root if manager else fallback_state_root())
    return output_report(report, args.format)


if __name__ == '__main__':
    sys.exit(main())
