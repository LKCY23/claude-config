#!/usr/bin/env python3
"""Combined client configuration deployment and whole-run recovery; software is installed separately."""
from pathlib import Path
import errno
import sys
sys.dont_write_bytecode = True

from claude_config.profile import ReportParser, UsageError, output_report, report_format
from claude_config.core.cutover import Cutover
from claude_config.core.recovery import rollback_plan
from claude_config.core.reporting import build_report, fallback_state_root, save_report
from claude_config.core.schema import SchemaError
from claude_config.core.transaction import PlanStale
from claude_config.core.execution_context import progress, progress_output


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        with progress_output(sys.stderr):
            return _main(argv)
    except UsageError as exc:
        report = build_report({}, operation='unknown', error={'code': 'invalid_arguments', 'phase': 'arguments', 'reason': str(exc), 'exit_code': 2})
        save_report(report, fallback_state_root())
        mode = report_format(argv)
        return output_report(report, mode)


def _main(argv):
    parser = ReportParser(description=__doc__, prog="claude-config cutover")
    parser.add_argument('operation', choices=('plan', 'apply', 'rollback'))
    parser.add_argument('--spec', type=Path)
    parser.add_argument('--state-dir', type=Path)
    parser.add_argument('--run')
    parser.add_argument('--write', action='store_true')
    parser.add_argument('--expect-plan-id')
    parser.add_argument('--format', choices=('auto', 'json', 'text'), default='auto')
    args = parser.parse_args(argv)
    manager, plan, error, started = None, None, None, False
    try:
        if not args.spec:
            raise SchemaError('Cutover operations require --spec')
        if bool(args.run) != (args.operation == 'rollback') or args.write and args.operation != 'rollback':
            raise SchemaError('rollback requires --run; --write is only for rollback')
        progress('读取迁移声明和本机绑定；尚未写入')
        manager = Cutover(args.spec)
        plan = rollback_plan(manager, args.run) if args.operation == 'rollback' else manager.plan()
        raw = plan.report
        if args.expect_plan_id and raw['plan_id'] != args.expect_plan_id:
            raise PlanStale('Reviewed cutover has changed')
        if (args.operation == 'apply' or args.write) and raw['ready']:
            started = True
            raw = plan.apply(manager.state_root)
    except (Exception, KeyboardInterrupt) as exc:
        raw = plan.report if plan else {'checks': [], 'decisions': [], 'blockers': [], 'results': []}
        error = {'code': 'interrupted' if isinstance(exc, KeyboardInterrupt) else 'plan_stale' if isinstance(exc, PlanStale) else 'cutover_failed',
                 'phase': 'execution' if started else 'planning', 'error_type': type(exc).__name__,
                 'reason': '切换未完成；请查看检查结果、备份和恢复日志。', 'exit_code': 130 if isinstance(exc, KeyboardInterrupt) else 2}
        if isinstance(exc, SchemaError):
            error['detail'] = str(exc)
        if isinstance(exc, OSError):
            error['errno'] = exc.errno
            if exc.errno == errno.EMFILE:
                error['reason'] = '打开的文件数量达到系统上限；迁移未完成。'
        if started and plan is not None and not raw.get('recovery'):
            # apply can fail while acquiring its lock, before any config writes.
            started = False
        raw['blockers'].append({'code': error['code'], 'reason': error['reason']})
    report = build_report(raw, operation=args.operation, preview_only=args.operation in ('plan', 'rollback') and not args.write,
                          manager=manager, context={'machine_file': str(args.spec) if args.spec else None, 'target_label': '多客户端配置'},
                          error=error, execution_started=started)
    save_report(report, manager.state_root if manager else args.state_dir or fallback_state_root())
    return output_report(report, args.format)


if __name__ == '__main__':
    sys.exit(main())
