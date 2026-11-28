"""Explicit self-update command using published GitHub Releases and install.sh."""
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

from . import __version__
from .core.execution_context import progress, prompt_input
from .core.report_viewer import terminal_color_enabled
from .core.reporting import encode_json, public_value
from .core.terminal_ui import badge, heading, meta_line, style
from .profile import ReportParser, UsageError, report_format

DEFAULT_REPO = 'LKCY23/claude-config'
_SEMVER_RE = re.compile(r'^v?(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?$')


def parse_semver(value):
    """Return comparable version tuple `(major, minor, patch, is_release, pre)` or None."""
    match = _SEMVER_RE.fullmatch(str(value or '').strip())
    if not match:
        return None
    major, minor, patch = int(match.group(1)), int(match.group(2)), int(match.group(3))
    pre = match.group(4) or ''
    return (major, minor, patch, 1 if not pre else 0, pre)


def normalize_tag(version_arg):
    if not version_arg:
        return None
    raw = str(version_arg).strip()
    if not raw.startswith('v') and _SEMVER_RE.fullmatch(raw):
        return 'v' + raw
    return raw


def _fetch_release_info(repo, version_arg=None):
    if not shutil.which('gh'):
        return None, '未找到 gh 命令；请先安装 GitHub CLI 并执行 `gh auth login`。'
    cmd = ['gh', 'release', 'view']
    tag = normalize_tag(version_arg)
    if tag:
        cmd.append(tag)
    cmd.extend(['--repo', repo, '--json', 'tagName,url,isDraft,isPrerelease,assets'])
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=60)
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or 'gh release view failed').strip()
        return None, f'获取 GitHub Release 失败：{detail}'
    try:
        data = json.loads(proc.stdout)
    except ValueError:
        return None, 'GitHub Release 返回格式异常。'
    if not isinstance(data, dict) or not data.get('tagName'):
        return None, 'GitHub Release 缺少 tagName 信息。'
    return data, None


def _select_assets(release_data):
    tag_name = release_data['tagName']
    target_ver = tag_name[1:] if tag_name.startswith('v') else tag_name
    asset_names = [
        item.get('name') for item in release_data.get('assets', [])
        if isinstance(item, dict) and item.get('name')
    ]
    expected_wheel = f'claude_config-{target_ver}-py3-none-any.whl'
    wheel = expected_wheel if expected_wheel in asset_names else next(
        (name for name in asset_names if name.startswith('claude_config-') and name.endswith('.whl')),
        None,
    )
    missing = []
    if not wheel:
        missing.append(expected_wheel)
    for req in ('constraints.txt', 'install.sh'):
        if req not in asset_names:
            missing.append(req)
    return {
        'tag_name': tag_name,
        'target_version': target_ver,
        'wheel': wheel,
        'assets': asset_names,
        'missing': missing,
    }


def _render_self_update(report, *, color=False, interactive_confirm=False):
    header = '工具升级预览' if report.get('preview_only') else '工具升级结果'
    lines = [
        heading(header, color=color),
        meta_line('当前版本', report.get('current_version', '-'), color=color),
    ]
    if report.get('target_tag'):
        lines.append(meta_line('目标版本', report['target_tag'], color=color))
    if report.get('repo'):
        lines.append(meta_line('发布仓库', report['repo'], color=color))
    if report.get('release_url'):
        lines.append(meta_line('Release', report['release_url'], color=color))

    if not report.get('ready'):
        lines.append(f"  {badge('error', '无法升级', color=color)}  {report.get('reason')}")
        return '\n'.join(lines)

    status = report.get('status')
    if status == 'up_to_date':
        note = report.get('reason') or '当前已是最新版本，无需升级。'
        lines.append(f"  {badge('success', '已是最新', color=color)}  {note}")
        return '\n'.join(lines)
    if status == 'pending':
        lines.append(f"  {badge('pending', '待升级', color=color)}  {report['current_version']} → {report['target_version']}")
        lines.append(style('  · 将通过 uv tool 升级 CLI；本机 machine.yaml 与客户端配置不受影响', 'muted', color=color))
        if not interactive_confirm:
            lines.append('')
            lines.append(style('提示：当前仅预览，尚未下载或安装；如需执行请追加 --yes。', 'hint', color=color))
        return '\n'.join(lines)

    lines.append(f"  {badge('success', '已升级', color=color)}  {report['current_version']} → {report['target_version']}")
    lines.append(style('  · 软件升级未修改本机 Profiles 或 Agent/App 配置', 'muted', color=color))
    return '\n'.join(lines)


def _confirm_update(report, color):
    print(_render_self_update(report, color=color, interactive_confirm=True))
    try:
        answer = prompt_input(f"\n确认下载并升级至 {report['target_tag']} 吗？[y/N]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return False
    return answer in ('y', 'yes')


def _run_install(repo, tag_name, wheel_name):
    if not shutil.which('uv'):
        return False, '未找到 uv 命令；请先安装 uv: https://docs.astral.sh/uv/getting-started/installation/'
    with tempfile.TemporaryDirectory(prefix='claude-config-self-update-') as tmp:
        tmp_dir = Path(tmp)
        progress(f'下载 {tag_name} 发布包')
        dl = subprocess.run(
            [
                'gh', 'release', 'download', tag_name, '--repo', repo,
                '--dir', str(tmp_dir),
                '--pattern', '*.whl', '--pattern', 'constraints.txt', '--pattern', 'install.sh',
            ],
            capture_output=True, text=True, check=False, timeout=180,
        )
        if dl.returncode != 0:
            detail = (dl.stderr or dl.stdout or 'gh release download failed').strip()
            return False, f'下载发布包失败：{detail}'
        install_sh = tmp_dir / 'install.sh'
        wheel_path = tmp_dir / wheel_name
        constraints = tmp_dir / 'constraints.txt'
        if not (install_sh.is_file() and wheel_path.is_file() and constraints.is_file()):
            return False, '下载后的发布文件不完整（缺少 install.sh / wheel / constraints.txt）。'
        progress(f'安装 {tag_name}')
        inst = subprocess.run(
            ['bash', str(install_sh), str(wheel_path)],
            capture_output=True, text=True, check=False, timeout=300,
        )
        if inst.returncode != 0:
            detail = (inst.stderr or inst.stdout or 'install.sh failed').strip()
            return False, f'执行 install.sh 失败：{detail}'
    return True, None


def _build_parser():
    parser = ReportParser(
        prog='claude-config self-update',
        description='检查 GitHub Release 并通过 install.sh 安全升级 claude-config CLI（默认仅预览）',
    )
    parser.add_argument('--repo', default=DEFAULT_REPO, help=f'GitHub Release 仓库（默认 {DEFAULT_REPO}）')
    parser.add_argument('--version', help='指定目标版本或 tag（如 0.12.0 或 v0.12.0；默认最新正式 Release）')
    parser.add_argument('--force', action='store_true', help='即使当前已等于或高于目标版本仍强制重装')
    parser.add_argument('--dry-run', action='store_true', help='仅检查版本与附件清单，不下载或安装')
    parser.add_argument('--write', '--yes', dest='yes', action='store_true', help='跳过交互确认直接下载并升级')
    parser.add_argument('--format', choices=('auto', 'text', 'json'), default='auto')
    return parser


def main(argv):
    parser = _build_parser()
    fmt = report_format(argv)
    try:
        args = parser.parse_args(argv)
        if args.dry_run and args.yes:
            raise UsageError('--dry-run 与 --yes/--write 不能同时使用')
        color = terminal_color_enabled('text' if fmt == 'json' else fmt)

        progress(f'检查 {args.repo} 最新发布版本')
        release_data, err = _fetch_release_info(args.repo, args.version)
        base_report = {
            'schema_version': 1,
            'operation': 'self-update',
            'preview_only': True,
            'current_version': __version__,
            'repo': args.repo,
        }
        if err:
            base_report.update(ready=False, status='fetch_failed', reason=err)
            progress(None)
            print(encode_json(public_value(base_report)) if fmt == 'json' else _render_self_update(base_report, color=color))
            return 1

        if release_data.get('isDraft'):
            base_report.update(
                ready=False, status='draft_release', target_tag=release_data.get('tagName'),
                release_url=release_data.get('url'), reason='目标 Release 仍处于 Draft 状态，暂不可安装。',
            )
            progress(None)
            print(encode_json(public_value(base_report)) if fmt == 'json' else _render_self_update(base_report, color=color))
            return 1

        asset_info = _select_assets(release_data)
        base_report.update(
            target_tag=asset_info['tag_name'],
            target_version=asset_info['target_version'],
            release_url=release_data.get('url'),
            wheel=asset_info['wheel'],
        )
        if asset_info['missing']:
            base_report.update(
                ready=False,
                status='missing_assets',
                missing_assets=asset_info['missing'],
                reason=f"Release {asset_info['tag_name']} 缺少必要安装附件：{', '.join(asset_info['missing'])}",
            )
            progress(None)
            print(encode_json(public_value(base_report)) if fmt == 'json' else _render_self_update(base_report, color=color))
            return 1

        cur_ver = parse_semver(__version__)
        tgt_ver = parse_semver(asset_info['target_version'])
        if cur_ver is not None and tgt_ver is not None and not args.force:
            if tgt_ver == cur_ver:
                base_report.update(ready=True, status='up_to_date', reason='当前已是最新版本，无需升级。')
                progress(None)
                print(encode_json(public_value(base_report)) if fmt == 'json' else _render_self_update(base_report, color=color))
                return 0
            if tgt_ver < cur_ver:
                base_report.update(
                    ready=True,
                    status='up_to_date',
                    reason=f"远端目标版本 ({asset_info['target_version']}) 低于当前版本 ({__version__})；如需降级请追加 --force。",
                )
                progress(None)
                print(encode_json(public_value(base_report)) if fmt == 'json' else _render_self_update(base_report, color=color))
                return 0

        preview_report = {**base_report, 'ready': True, 'status': 'pending'}
        if args.dry_run:
            progress(None)
            print(encode_json(public_value(preview_report)) if fmt == 'json' else _render_self_update(preview_report, color=color))
            return 0

        execute = bool(args.yes)
        if not execute:
            interactive = fmt != 'json' and sys.stdin.isatty() and sys.stdout.isatty()
            if interactive:
                progress(None)
                if not _confirm_update(preview_report, color):
                    print('已取消，未修改安装版本。')
                    return 0
                execute = True
            else:
                progress(None)
                print(encode_json(public_value(preview_report)) if fmt == 'json' else _render_self_update(preview_report, color=color))
                return 0

        if execute:
            ok, install_err = _run_install(args.repo, asset_info['tag_name'], asset_info['wheel'])
            progress(None)
            final_report = {
                **base_report,
                'preview_only': False,
                'ready': ok,
                'status': 'updated' if ok else 'install_failed',
                'reason': install_err,
            }
            print(encode_json(public_value(final_report)) if fmt == 'json' else _render_self_update(final_report, color=color))
            return 0 if ok else 1
    except (Exception, KeyboardInterrupt) as exc:
        progress(None)
        reason = str(exc) if isinstance(exc, UsageError) else '检查或执行升级失败；请检查网络、gh 与 uv 环境。'
        code = 'interrupted' if isinstance(exc, KeyboardInterrupt) else 'invalid_arguments' if isinstance(exc, UsageError) else 'self_update_failed'
        exit_status = 130 if isinstance(exc, KeyboardInterrupt) else 2
        err_report = {
            'schema_version': 1,
            'operation': 'self-update',
            'ready': False,
            'error': {'code': code, 'reason': reason, 'exit_code': exit_status},
        }
        if fmt == 'json':
            print(encode_json(public_value(err_report)))
        else:
            print(f'错误：{reason}', file=sys.stderr)
        return exit_status
