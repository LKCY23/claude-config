"""Read-only viewer for saved claude-config JSON reports."""
from pathlib import Path
import re
import sys

from .core.native import parse_json, parse_yaml, snapshot_read
from .core.report_viewer import show_report, terminal_color_enabled, terminal_interactive
from .core.reporting import (
    build_report,
    encode_json,
    exit_code,
    fallback_state_root,
    render_text,
    save_report,
)
from .core.schema import SchemaError, require, state_directory, validate_machine
from .profile import ReportParser, UsageError, report_format, report_verbose


def _state_root_for_report(machine_path, state_dir=None):
    if state_dir is not None:
        return Path(state_dir).expanduser().absolute()
    machine_path = Path(machine_path).expanduser().absolute()
    if machine_path.is_file():
        raw, pin = snapshot_read(machine_path)
        if pin['exists']:
            machine = validate_machine(parse_yaml(raw))
            return state_directory(machine)
    return fallback_state_root()


def _resolve_report_file(target, state_root):
    reports_dir = state_root / 'reports'
    if target == 'last':
        require(reports_dir.is_dir(), '尚未找到已保存的报告目录；请先执行一次 plan、doctor 或其他命令。')
        candidates = [path for path in reports_dir.glob('*.json') if path.is_file() and not path.is_symlink()]
        require(bool(candidates), '报告目录中暂无已保存的报告。')
        candidates.sort(key=lambda item: (item.stat().st_mtime_ns, item.name), reverse=True)
        return candidates[0]
    normalized = target.lower()
    if re.fullmatch(r'[0-9a-f]{8,32}', normalized):
        require(reports_dir.is_dir(), '尚未找到已保存的报告目录。')
        matches = [
            path for path in reports_dir.glob('*.json')
            if path.stem.lower().startswith(normalized) and path.is_file() and not path.is_symlink()
        ]
        require(len(matches) > 0, f'未找到匹配报告 ID 的记录：{normalized}')
        require(len(matches) == 1, f'报告 ID 前缀不唯一：{normalized}（匹配 {len(matches)} 条，请提供更长前缀）')
        return matches[0]
    if target.endswith('.json'):
        candidate = Path(target).expanduser().absolute()
        require(candidate.is_file() and not candidate.is_symlink(), '找不到指定的报告 JSON 文件（或目标为符号链接）。')
        return candidate
    raise UsageError('报告目标应为 last、8-32 位十六进制报告 ID，或明确存在的 .json 文件路径。')


def _emit_loaded_report(report, mode, *, verbose=False):
    if mode == 'json':
        print(encode_json(report))
        return exit_code(report)
    launch_tui = mode == 'tui' or (mode == 'auto' and terminal_interactive('tui'))
    viewed = False
    if launch_tui and terminal_interactive('tui'):
        viewed = show_report(report)
    color = (mode == 'auto' and not viewed and terminal_color_enabled('auto'))
    print(render_text(report, brief=viewed, verbose=verbose, color=color))
    return exit_code(report)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    mode = report_format(argv)
    verbose = report_verbose(argv)
    parser = ReportParser(description=__doc__, prog='claude-config report')
    parser.add_argument('target', nargs='?', default='last', help='last（默认）、8-32 位 report ID 或 .json 文件路径')
    parser.add_argument('--machine', type=Path, default=Path.home() / '.claude-config-tool/machine.yaml')
    parser.add_argument('--state-dir', type=Path)
    parser.add_argument('--format', choices=('auto', 'tui', 'text', 'json'), default='auto')
    parser.add_argument('--verbose', action='store_true', help='显示完整保留决策与细粒度输出')
    try:
        args = parser.parse_args(argv)
        state_root = _state_root_for_report(args.machine, args.state_dir)
        path = _resolve_report_file(args.target, state_root)
        raw, _ = snapshot_read(path)
        report = parse_json(raw)
        require(isinstance(report, dict) and 'schema_version' in report and 'operation' in report,
                '指定文件不是有效的 claude-config 报告格式。')
        return _emit_loaded_report(report, args.format, verbose=args.verbose)
    except (UsageError, SchemaError, ValueError, OSError) as exc:
        code = 'invalid_arguments' if isinstance(exc, UsageError) else 'report_load_failed'
        error = {'code': code, 'phase': 'report', 'reason': str(exc), 'exit_code': 2}
        err_report = build_report({}, operation='report', error=error)
        save_report(err_report, fallback_state_root())
        if mode == 'json':
            print(encode_json(err_report))
        else:
            print(render_text(err_report, verbose=verbose, color=terminal_color_enabled(mode)))
        return exit_code(err_report)
