#!/usr/bin/env python3
"""Inventory or convert legacy inputs into unpublished local review candidates."""
from pathlib import Path
import sys

from claude_config.profile import ReportParser, UsageError, output_report, report_format
from claude_config.core.migration_inventory import inventory
from claude_config.core.migration_convert import conversion
from claude_config.core.native import parse_yaml
from claude_config.core.reporting import build_report, fallback_state_root, save_report


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    mode = report_format(argv)
    parser = ReportParser(description=__doc__)
    parser.add_argument('operation', choices=('inventory', 'convert'))
    parser.add_argument('--data-dir', type=Path, required=True)
    parser.add_argument('--state-dir', type=Path)
    parser.add_argument('--format', choices=('auto', 'text', 'json'), default='auto')
    parser.add_argument('--legacy-machine', type=Path)
    parser.add_argument('--output-dir', type=Path)
    parser.add_argument('--write', action='store_true')
    parser.add_argument('--app-profile', action='append', default=[])
    parser.add_argument('--expect-plan-id')
    try:
        args = parser.parse_args(argv)
    except UsageError as exc:
        report = build_report({}, operation='migration-inventory', error={'code': 'invalid_arguments', 'phase': 'arguments', 'reason': str(exc), 'exit_code': 2})
        save_report(report, fallback_state_root())
        return output_report(report, mode)
    error = None
    try:
        if args.operation == 'convert':
            if not args.output_dir or not args.legacy_machine:
                raise ValueError('Conversion requires explicit output and old Machine locations')
            machine = parse_yaml(args.legacy_machine.read_bytes())
            app_profiles = {}
            for item in args.app_profile:
                name, separator, path = item.partition('=')
                if not separator or not path or name in app_profiles:
                    raise ValueError('App input must be a unique app=profile-path')
                app_profiles[name] = Path(path)
            raw = conversion(args.data_dir, args.output_dir, machine.get('catalog_roots', {}), write=args.write, app_profiles=app_profiles, expect_plan_id=args.expect_plan_id)
        else:
            if args.output_dir or args.legacy_machine or args.write or args.app_profile or args.expect_plan_id:
                raise ValueError('Inventory cannot write conversion candidates')
            raw = inventory(args.data_dir)
    except Exception as exc:
        raw = {}
        error = {'code': 'migration_' + args.operation + '_failed', 'phase': args.operation, 'error_type': type(exc).__name__,
                 'reason': '旧数据处理失败；请检查声明、来源和输出目录。转换不覆盖已有候选。', 'exit_code': 2}
    report = build_report(raw, operation='migration-' + args.operation, preview_only=not (args.operation == 'convert' and args.write),
                          context={'data_root': str(args.data_dir.absolute())}, error=error)
    if args.operation == 'convert':
        report['scope']['note'] = '旧声明到新版本地候选的转换；未部署、安装或接管本机配置。'
        report['artifacts'].update(raw.get('artifacts', {}))
    else:
        report['scope']['note'] = '只读取旧声明及其本地引用；尚未生成新版数据，也未部署或接管原生配置。'
    save_report(report, args.state_dir or fallback_state_root())
    return output_report(report, args.format)


if __name__ == '__main__':
    raise SystemExit(main())
