"""Manage the config_dir Git repository: status, safe fast-forward pull, validate & publish."""
from pathlib import Path
import sys

from .core.data_repo import inspect_repo, publish_repo, pull_repo, validate_data_repo
from .core.execution_context import progress, prompt_input
from .core.machine_profiles import MachineProfiles
from .core.report_viewer import terminal_color_enabled
from .core.reporting import encode_json, public_value
from .core.schema import SchemaError
from .core.terminal_ui import badge, heading, meta_line, next_steps, style
from .profile import ReportParser, UsageError, report_format


def render_data_repo_lines(info, *, color=False, indent='  '):
    """Render concise Git state lines for `data status` and `status`."""
    if not info.get('is_git'):
        status_badge = badge('warning', '非 Git 仓库', color=color)
        return [f"{indent}{status_badge}  {info.get('path')}"]

    branch = info.get('branch') or '-'
    upstream = info.get('upstream') or '未设置上游'
    head = info.get('head') or '-'
    state = info.get('state')

    if state == 'clean':
        status_badge = badge('success', '已同步', color=color)
        summary = f'{branch} -> {upstream} ({head})，工作区干净'
    elif state == 'dirty':
        count = len(info.get('dirty_files', [])) + len(info.get('untracked_files', []))
        status_badge = badge('warning', '有本地改动', color=color)
        summary = f'{branch} -> {upstream} ({head})，{count} 处未提交改动'
        if info.get('ahead') or info.get('behind'):
            summary += f"（领先 {info['ahead']} / 落后 {info['behind']}）"
    elif state == 'ahead':
        status_badge = style('↑ 本地领先', 'pending', color=color)
        summary = f"{branch} -> {upstream} ({head})，领先 {info['ahead']} 个提交待推送"
    elif state == 'behind':
        status_badge = style('↓ 落后远端', 'warning', color=color)
        summary = f"{branch} -> {upstream} ({head})，落后 {info['behind']} 个提交"
    else:
        status_badge = badge('error', '已分叉', color=color)
        summary = f"{branch} -> {upstream} ({head})，领先 {info['ahead']} / 落后 {info['behind']}"

    lines = [f'{indent}{status_badge}  {summary}']
    if info.get('fetch_error'):
        lines.append(f"{indent}  {style('⚠ 远端检查失败', 'warning', color=color)}：{info['fetch_error']}")
    changed = sorted(set(info.get('dirty_files', []) + info.get('untracked_files', [])))
    for rel in changed[:5]:
        tag = '新增' if rel in info.get('untracked_files', []) else '修改'
        lines.append(f'{indent}  · [{tag}] {rel}')
    if len(changed) > 5:
        lines.append(f'{indent}  · 其余 {len(changed) - 5} 项改动未展开')
    return lines


def _render_status(report, *, color=False):
    lines = [
        heading(f"配置仓库状态 ({report['machine_id']})", color=color),
        meta_line('目录', report['config_dir'], color=color),
    ]
    lines.extend(render_data_repo_lines(report['repo'], color=color))
    validation = report.get('validation')
    if validation:
        if validation['ok']:
            lines.append(f"  {badge('success', '校验通过', color=color)}  已检查 {validation['checked_files']} 个配置文件")
        else:
            lines.append(f"  {badge('error', '校验失败', color=color)}  {len(validation['errors'])} 处错误：")
            for err in validation['errors'][:5]:
                lines.append(f"    - {err['file']}: {err['reason']}")
    repo = report['repo']
    hints = []
    if repo.get('dirty') or repo.get('ahead', 0) > 0:
        hints.append('claude-config data publish')
    elif repo.get('behind', 0) > 0:
        hints.append('claude-config data pull')
    lines.extend(next_steps(hints, color=color))
    return '\n'.join(lines)


def _render_pull(report, *, color=False):
    header = '配置仓库拉取预览' if report.get('preview_only') else '配置仓库拉取结果'
    lines = [
        heading(f"{header} ({report.get('machine_id', '-')})", color=color),
        meta_line('目录', report.get('config_dir', '-'), color=color),
    ]
    status = report.get('status')
    if not report.get('ready'):
        lines.append(f"  {badge('error', '无法拉取', color=color)}  {report.get('reason')}")
        return '\n'.join(lines)
    if status == 'up_to_date':
        lines.append(f"  {badge('success', '已是最新', color=color)}  无需拉取")
    elif status == 'pending':
        lines.append(f"  {badge('pending', '待拉取', color=color)}  将 fast-forward 拉取 {report.get('would_pull_commits', 0)} 个提交")
        for f in report.get('updated_files', [])[:10]:
            lines.append(f'    · {f}')
        lines.append('')
        lines.append(style('提示：当前仅预览；去除 --dry-run 即可执行拉取。', 'hint', color=color))
    else:
        lines.append(f"  {badge('success', '已拉取', color=color)}  {report.get('before_head')} -> {report.get('after_head')} ({report.get('pulled_commits', 0)} 个提交)")
        for f in report.get('updated_files', [])[:10]:
            lines.append(f'    · {f}')
        if report.get('updated_files'):
            lines.extend(next_steps(['claude-config status', 'claude-config sync'], color=color))
    return '\n'.join(lines)


def _render_publish(report, *, color=False, interactive_confirm=False):
    header = '配置仓库发布预览' if report.get('preview_only') else '配置仓库发布结果'
    lines = [
        heading(f"{header} ({report.get('machine_id', '-')})", color=color),
        meta_line('目录', report.get('config_dir', '-'), color=color),
    ]
    repo = report.get('repo') or {}
    if repo.get('fetch_error') and report.get('preview_only'):
        lines.append(f"  {style('⚠ 远端检查失败（仅展示本地状态）', 'warning', color=color)}：{repo['fetch_error']}")
    if not report.get('ready'):
        lines.append(f"  {badge('error', '阻止发布', color=color)}  {report.get('reason')}")
        if report.get('metadata_only_lock_diff'):
            lines.append(f"  {style('· 注：重叠锁文件的插件版本与内容指纹均与远端一致，仅来源元数据（resolution）不同。', 'hint', color=color)}")
        if report.get('diff_vs_upstream'):
            lines.append('  · 与远端最新提交的差异预览：')
            for diff_line in report['diff_vs_upstream'].splitlines():
                lines.append(f'    {diff_line}')
            if report.get('diff_vs_upstream_truncated'):
                lines.append(f"    {style('...（差异已截断，请运行上方 git --no-pager diff 命令查看完整差异）', 'muted', color=color)}")
        validation = report.get('validation') or {}
        for err in validation.get('errors', [])[:5]:
            lines.append(f"    - {err['file']}: {err['reason']}")
        return '\n'.join(lines)
    status = report.get('status')
    if status == 'nothing_to_publish':
        lines.append(f"  {badge('success', '无需发布', color=color)}  工作区干净且无未推送提交")
        return '\n'.join(lines)
    if status == 'pending_reconcile_identical':
        lines.append(f"  {badge('pending', '待对齐', color=color)}  本地未提交改动与远端最新提交内容完全一致，将对齐至远端（拉取 {report.get('behind', 0)} 个提交，无需新提交）")
        for rel in report.get('files', [])[:15]:
            lines.append(f'    - {rel}')
        if not interactive_confirm:
            lines.append('')
            lines.append(style('提示：当前仅预览，尚未修改工作区；如需执行对齐请追加 --yes。', 'hint', color=color))
        return '\n'.join(lines)
    if status == 'reconciled_with_remote':
        lines.append(f"  {badge('success', '已对齐远端', color=color)}  本地改动已包含在远端提交中（已拉取 {report.get('pulled_commits', 0)} 个提交，当前 {report.get('commit')}）")
        for rel in report.get('files', [])[:10]:
            lines.append(f'    · {rel}')
        return '\n'.join(lines)
    if status == 'pending':
        lines.append(f"  {badge('pending', '待发布', color=color)}  提交信息：{report.get('proposed_message')}")
        if report.get('prefer_local'):
            overlap = '、'.join(report.get('overlapping_files', [])[:3])
            lines.append(f"  · 已启用 --prefer-local：将基于远端最新提交（领先 {report.get('behind', 0)} 个提交）保留本地改动并覆盖重叠文件（{overlap}），仅提交相对远端的净差异")
            if report.get('metadata_only_lock_diff'):
                lines.append(f"  {style('· 注：重叠锁文件的插件版本与内容指纹均与远端一致，仅来源元数据（resolution）不同。', 'hint', color=color)}")
        elif report.get('will_rebase'):
            lines.append(f"  · 远端领先 {report.get('behind', 0)} 个提交（改动文件无重叠），执行时将先安全变基再推送")
        if report.get('ahead'):
            lines.append(f"  · 包含 {report['ahead']} 个尚未推送的本地提交")
        if report.get('files'):
            lines.append(f"  · 变更文件 ({len(report['files'])}):")
            for rel in report['files'][:15]:
                lines.append(f'    - {rel}')
            if len(report['files']) > 15:
                lines.append(f"    - 其余 {len(report['files']) - 15} 个文件未展开")
        if report.get('diff_stat'):
            lines.append('')
            lines.append(report['diff_stat'])
        if report.get('prefer_local') and report.get('diff_vs_upstream'):
            lines.append('')
            lines.append('  · 相对远端最新提交的净差异预览：')
            for diff_line in report['diff_vs_upstream'].splitlines():
                lines.append(f'    {diff_line}')
            if report.get('diff_vs_upstream_truncated'):
                lines.append(f"    {style('...（净差异已截断）', 'muted', color=color)}")
        if not interactive_confirm:
            lines.append('')
            lines.append(style('提示：当前仅预览，未提交或推送；如需执行请追加 --yes。', 'hint', color=color))
        return '\n'.join(lines)
    if report.get('prefer_local'):
        extra = f"（已基于远端最新提交保留本地改动并同步 {report.get('pulled_commits', 0)} 个远端提交）"
    elif report.get('rebased'):
        extra = f"（已先安全变基 {report['pulled_commits']} 个远端提交）"
    else:
        extra = ''
    lines.append(f"  {badge('success', '已发布', color=color)}  分支 {report.get('branch')} ({report.get('commit')}){extra}")
    if report.get('pr_url'):
        lines.append(f"  · PR: {report['pr_url']}")
    for rel in report.get('files', [])[:10]:
        lines.append(f'    · {rel}')
    return '\n'.join(lines)


def _confirm_publish(report, color):
    print(_render_publish(report, color=color, interactive_confirm=True))
    if report.get('status') == 'pending_reconcile_identical':
        prompt = '\n确认丢弃重复本地改动并对齐到远端最新提交吗？[y/N]: '
    elif report.get('prefer_local'):
        prompt = '\n确认基于远端保留本地重叠改动、提交净差异并推送吗？[y/N]: '
    elif report.get('create_pr'):
        prompt = '\n确认创建分支、推送并创建 PR 吗？[y/N]: '
    else:
        prompt = '\n确认校验、提交并推送到远端吗？[y/N]: '
    try:
        answer = prompt_input(prompt).strip().lower()
    except (EOFError, KeyboardInterrupt):
        return False
    return answer in ('y', 'yes')


def _build_parser():
    parser = ReportParser(
        prog='claude-config data',
        description='管理配置数据仓库 (config_dir) 的 Git 同步：状态检查、安全拉取与校验发布',
    )
    sub = parser.add_subparsers(dest='subcommand', required=True)

    common = ReportParser(add_help=False)
    common.add_argument('--machine', type=Path, default=Path.home() / '.claude-config-tool/machine.yaml')
    common.add_argument('--format', choices=('auto', 'text', 'json'), default='auto')

    p_status = sub.add_parser('status', parents=[common], help='只读检查配置仓库的 Git 状态与本地声明校验')
    p_status.add_argument('--fetch', action='store_true', help='先执行 git fetch 刷新远端跟踪分支状态')

    p_pull = sub.add_parser('pull', parents=[common], help='安全执行 git pull --ff-only（存在未提交改动或分叉时阻止）')
    p_pull.add_argument('--dry-run', action='store_true', help='仅预览将拉取的提交与文件，不修改工作区')
    p_pull.add_argument('--write', '--yes', dest='write', action='store_true', help='显式执行拉取（与默认一致，便于脚本统一参数）')

    p_pub = sub.add_parser('publish', parents=[common], help='本地校验所有声明并预览或提交/推送改动到远端')
    p_pub.add_argument('-m', '--message', help='自定义提交信息')
    p_pub.add_argument('--pr', action='store_true', help='创建同步分支并调用 gh pr create 提交 PR')
    p_pub.add_argument('--prefer-local', action='store_true',
                       help='远端领先且改动重叠（本地无未推送提交）时，基于远端保留本地改动并预览/提交净差异')
    p_pub.add_argument('--no-fetch', action='store_true', help='跳过发布前的 git fetch 远端检查（离线快速预览）')
    p_pub.add_argument('--dry-run', action='store_true', help='仅校验并预览变更，不提交或推送')
    p_pub.add_argument('--write', '--yes', dest='write', action='store_true', help='跳过交互确认直接提交并推送')
    return parser


def main(argv):
    parser = _build_parser()
    fmt = report_format(argv)
    try:
        args = parser.parse_args(argv)
        machine_file = args.machine.expanduser().absolute()
        if not machine_file.is_file():
            raise SchemaError('未找到 Machine；请运行 claude-config init，或指定 --machine。')
        profiles = MachineProfiles(machine_file, strict=False)
        config_dir = Path(profiles.effective['config_dir']).expanduser().absolute()
        machine_id = profiles.effective['machine_id']
        color = terminal_color_enabled('text' if fmt == 'json' else fmt)

        if args.subcommand == 'status':
            progress('检查配置仓库状态')
            repo_info = inspect_repo(config_dir, fetch=args.fetch)
            validation = validate_data_repo(config_dir)
            report = {
                'schema_version': 1,
                'operation': 'data-status',
                'read_only': not args.fetch,
                'machine_id': machine_id,
                'config_dir': str(config_dir),
                'repo': repo_info,
                'validation': validation,
                'ready': validation['ok'],
            }
            progress(None)
            print(encode_json(public_value(report)) if fmt == 'json' else _render_status(report, color=color))
            return 0 if validation['ok'] else 1

        if args.dry_run and args.write:
            raise UsageError('--dry-run 与 --yes/--write 不能同时使用')

        if args.subcommand == 'pull':
            progress('检查配置仓库拉取状态' if args.dry_run else '拉取配置仓库更新')
            result = pull_repo(config_dir, dry_run=args.dry_run, fetch=True)
            result.update(schema_version=1, operation='data-pull', machine_id=machine_id, config_dir=str(config_dir))
            progress(None)
            print(encode_json(public_value(result)) if fmt == 'json' else _render_pull(result, color=color))
            return 0 if result['ready'] else 1

        # publish
        if args.prefer_local and args.pr:
            raise UsageError('--prefer-local 与 --pr 不能同时使用')
        progress('校验并预览配置仓库发布')
        preview = publish_repo(config_dir, message=args.message, create_pr=args.pr, dry_run=True,
                               fetch=not args.no_fetch, prefer_local=args.prefer_local)
        preview.update(schema_version=1, operation='data-publish', machine_id=machine_id, config_dir=str(config_dir))

        if not preview['ready'] or preview['status'] == 'nothing_to_publish' or args.dry_run:
            progress(None)
            print(encode_json(public_value(preview)) if fmt == 'json' else _render_publish(preview, color=color))
            return 0 if preview['ready'] else 1

        execute = bool(args.write)
        if not execute:
            interactive = fmt != 'json' and sys.stdin.isatty() and sys.stdout.isatty()
            if interactive:
                progress(None)
                if not _confirm_publish(preview, color):
                    print('已取消，未提交或推送。')
                    return 0
                execute = True
            else:
                progress(None)
                print(encode_json(public_value(preview)) if fmt == 'json' else _render_publish(preview, color=color))
                return 0

        if execute:
            progress('对齐远端并对齐发布' if preview.get('status') == 'pending_reconcile_identical' else '提交并推送配置仓库变更')
            result = publish_repo(config_dir, message=args.message, create_pr=args.pr, dry_run=False,
                                  fetch=not args.no_fetch, prefer_local=args.prefer_local)
            result.update(schema_version=1, operation='data-publish', machine_id=machine_id, config_dir=str(config_dir))
            progress(None)
            print(encode_json(public_value(result)) if fmt == 'json' else _render_publish(result, color=color))
            return 0 if result['ready'] else 1
    except (Exception, KeyboardInterrupt) as exc:
        progress(None)
        reason = str(exc) if isinstance(exc, (UsageError, SchemaError, ValueError)) else '配置仓库操作失败；请检查目录及 Git 状态。'
        code = 'interrupted' if isinstance(exc, KeyboardInterrupt) else 'invalid_arguments' if isinstance(exc, UsageError) else 'data_failed'
        exit_status = 130 if isinstance(exc, KeyboardInterrupt) else 2
        err_report = {
            'schema_version': 1,
            'operation': 'data',
            'ready': False,
            'error': {'code': code, 'reason': reason, 'exit_code': exit_status},
        }
        if fmt == 'json':
            print(encode_json(public_value(err_report)))
        else:
            print(f'错误：{reason}', file=sys.stderr)
        return exit_status
