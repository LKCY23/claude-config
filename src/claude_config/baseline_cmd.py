"""Unified catalog baseline status, sync, and adoption workflow."""
from pathlib import Path
import sys

from .core.baseline import adopt_baseline, baseline_group, catalog_target, default_agent_for_machine
from .core.catalog import git
from .core.execution_context import progress
from .core.machine_profiles import MachineProfiles
from .core.manager import ProfileManager
from .core.native import parse_yaml, snapshot_read
from .core.report_viewer import terminal_color_enabled
from .core.reporting import build_report, encode_json, public_value, save_report
from .core.schema import COMMIT, SchemaError, state_directory, validate_catalogs
from .core.source_transport import DEFAULT_GIT_TRANSPORT, GIT_TRANSPORTS
from .fetch_baseline import fetch
from .profile import ReportParser, UsageError, report_format

_ANSI_GREEN = '\033[32m'
_ANSI_YELLOW = '\033[33m'
_ANSI_CYAN = '\033[36m'
_ANSI_RED = '\033[31m'
_ANSI_DIM = '\033[2m'
_ANSI_BOLD = '\033[1m'
_ANSI_RESET = '\033[0m'


def parse_commit_or_release(value):
    """Normalize a 40-char commit SHA or `baseline-<sha>` release tag."""
    if not isinstance(value, str) or not value:
        raise UsageError('基线版本必须是 40 位小写 commit SHA 或 baseline-<40位SHA>')
    commit = value[9:] if value.startswith('baseline-') else value
    if not COMMIT.fullmatch(commit):
        raise UsageError('基线版本必须是 40 位小写 commit SHA 或 baseline-<40位SHA>')
    return commit, 'baseline-' + commit


def _cached_git_head(checkout_path):
    """Best-effort read-only check of the HEAD commit in a cached catalog checkout."""
    path = Path(checkout_path)
    if not path.exists():
        return None
    try:
        sha = git(path, 'rev-parse', 'HEAD').decode().strip()
        return sha if COMMIT.fullmatch(sha) else None
    except (OSError, SchemaError):
        return None


def _short(sha):
    return sha[:8] if isinstance(sha, str) and len(sha) >= 8 else (sha or '-')


def _style(text, code, color):
    return f'{code}{text}{_ANSI_RESET}' if color else text


def inspect_baselines(machine_path, *, catalog_filter=None):
    """Read-only inspection of declared catalog commits vs local machine cache bindings."""
    machine_file = Path(machine_path).expanduser().absolute()
    profiles = MachineProfiles(machine_file, strict=False)
    machine = profiles.effective
    root = Path(machine['config_dir'])
    state_root = state_directory(machine)
    catalog_roots = machine.get('catalog_roots', {})
    catalogs_path = root / 'catalogs.yaml'
    if not catalogs_path.exists():
        if not catalog_roots:
            return {
                'schema_version': 1,
                'operation': 'baseline-status',
                'machine_id': machine['machine_id'],
                'machine_file': str(machine_file),
                'state_root': str(state_root),
                'aligned': True,
                'groups': [],
                'catalogs': [],
                'mismatches': list(profiles.mismatches),
            }
        raise SchemaError('catalogs.yaml 不存在，但 machine.yaml 声明了 catalog_roots')
    catalogs = validate_catalogs(parse_yaml(snapshot_read(catalogs_path)[0]))
    entries = catalogs['catalogs']
    if catalog_filter is not None and catalog_filter not in entries:
        raise UsageError(f'catalogs.yaml 中未声明 catalog: {catalog_filter}')

    selected_aliases = [catalog_filter] if catalog_filter else list(entries.keys())

    # Group aliases belonging to the same baseline unit while preserving declaration order.
    items_by_alias = {}
    for alias, entry in entries.items():
        repo = entry['repo']
        commit = entry['commit']
        expected_checkout = state_root / 'sources' / repo.replace('/', '--') / commit
        expected_record = state_root / 'releases' / repo.replace('/', '--') / ('baseline-' + commit) / (alias + '.baseline.json')
        bound_root = catalog_roots.get(alias)
        cached_commit = _cached_git_head(bound_root) if bound_root else None

        if bound_root is None:
            status = 'unbound'
        elif not Path(bound_root).exists():
            status = 'missing_checkout'
        elif cached_commit != commit:
            status = 'stale_cache'
        else:
            status = 'aligned'

        items_by_alias[alias] = {
            'alias': alias,
            'repo': repo,
            'catalog_commit': commit,
            'catalog_path': entry['path'],
            'catalog_root': bound_root,
            'cached_commit': cached_commit,
            'expected_checkout': str(expected_checkout),
            'expected_checkout_exists': expected_checkout.exists(),
            'release_record': str(expected_record),
            'release_record_exists': expected_record.exists(),
            'status': status,
            'needs_sync': status != 'aligned',
        }

    ordered_groups = []
    seen_groups = set()
    for alias in selected_aliases:
        group_aliases = baseline_group(catalogs, alias)
        key = tuple(group_aliases)
        if key in seen_groups:
            continue
        seen_groups.add(key)
        primary = 'claudespace' if 'claudespace' in group_aliases else group_aliases[0]
        members = [items_by_alias[name] for name in group_aliases]
        primary_item = items_by_alias[primary]
        ordered_groups.append({
            'primary': primary,
            'aliases': list(group_aliases),
            'repo': primary_item['repo'],
            'catalog_commit': primary_item['catalog_commit'],
            'cached_commit': primary_item['cached_commit'],
            'status': next((m['status'] for m in members if m['status'] != 'aligned'), 'aligned'),
            'needs_sync': any(m['needs_sync'] for m in members),
            'members': members,
        })

    return {
        'schema_version': 1,
        'operation': 'baseline-status',
        'machine_id': machine['machine_id'],
        'machine_file': str(machine_file),
        'state_root': str(state_root),
        'aligned': all(not g['needs_sync'] for g in ordered_groups),
        'groups': ordered_groups,
        'catalogs': [items_by_alias[a] for a in selected_aliases],
        'mismatches': list(profiles.mismatches),
    }


def _adopt_with_cache(manager, alias, target_commit, release, source_map, *, git_transport):
    """Fetch release/checkout if missing locally, then run adopt_baseline and apply."""
    catalogs = validate_catalogs(parse_yaml(snapshot_read(manager.root / 'catalogs.yaml')[0]))
    old = catalogs['catalogs'][alias]
    mapping = parse_yaml(snapshot_read(Path(source_map).absolute())[0]) if source_map else None
    target = catalog_target(old, alias, target_commit, mapping)
    aliases = baseline_group(catalogs, alias)
    checkout = manager.state_root / 'sources' / target['repo'].replace('/', '--') / target_commit
    release_dir = manager.state_root / 'releases' / target['repo'].replace('/', '--') / release
    primary_record = release_dir / (alias + '.baseline.json')
    all_records_exist = all((release_dir / (name + '.baseline.json')).exists() for name in aliases)

    fetched = False
    if checkout.exists() and all_records_exist:
        try:
            plan = adopt_baseline(manager, alias, target_commit, checkout, primary_record, source_map)
        except SchemaError:
            fetched = True
    else:
        fetched = True

    if fetched:
        progress(f'拉取基线 {alias}@{_short(target_commit)}')
        fetch(manager, alias, release, source_map, git_transport=git_transport)
        plan = adopt_baseline(manager, alias, target_commit, checkout, primary_record, source_map)

    return plan, fetched, checkout, primary_record


def sync_baseline_groups(
    machine_path,
    *,
    catalog_filter=None,
    release=None,
    source_map=None,
    agent=None,
    git_transport=DEFAULT_GIT_TRANSPORT,
    write=False,
    force=False,
):
    """Synchronize declared catalog baselines with local prepared checkouts and machine.yaml."""
    machine_file = Path(machine_path).expanduser().absolute()
    inspection = inspect_baselines(machine_file, catalog_filter=catalog_filter)
    groups = inspection['groups']

    if release is not None:
        target_commit, _ = parse_commit_or_release(release)
        if len(groups) > 1 and catalog_filter is None:
            raise UsageError('存在多个基线组时指定 --release 需同时指定 --catalog')
    else:
        target_commit = None

    if source_map is not None and len(groups) > 1 and catalog_filter is None:
        raise UsageError('指定 --source-map 时需同时指定 --catalog')

    chosen_agent = agent or default_agent_for_machine(machine_file)
    actions = []
    any_pending = False
    applied_reports = []

    for group in groups:
        alias = group['primary']
        desired_commit = target_commit or group['catalog_commit']
        desired_release = 'baseline-' + desired_commit
        needs_commit_update = desired_commit != group['catalog_commit']
        needs_cache_update = group['needs_sync'] or needs_commit_update
        if not needs_cache_update and not force:
            actions.append({
                'catalog': alias,
                'aliases': group['aliases'],
                'repo': group['repo'],
                'from_commit': group['cached_commit'],
                'to_commit': desired_commit,
                'status': 'aligned',
                'needs_sync': False,
                'fetched': False,
                'applied': False,
            })
            continue

        any_pending = True
        if not write:
            # Preview without mutating machine/catalogs or triggering network transfers when uncached.
            config_dir = Path(MachineProfiles(machine_file, strict=False).effective['config_dir'])
            catalogs = validate_catalogs(parse_yaml(snapshot_read(config_dir / 'catalogs.yaml')[0]))
            mapping = parse_yaml(snapshot_read(Path(source_map).absolute())[0]) if source_map else None
            target = catalog_target(catalogs['catalogs'][alias], alias, desired_commit, mapping)
            state_root = Path(inspection['state_root'])
            checkout = state_root / 'sources' / target['repo'].replace('/', '--') / desired_commit
            release_dir = state_root / 'releases' / target['repo'].replace('/', '--') / desired_release
            cached_ready = checkout.exists() and all((release_dir / (name + '.baseline.json')).exists() for name in group['aliases'])
            impacts = []
            plan_id = None
            if cached_ready:
                try:
                    mgr = ProfileManager(machine_file, chosen_agent)
                    plan = adopt_baseline(mgr, alias, desired_commit, checkout, release_dir / (alias + '.baseline.json'), source_map)
                    impacts = plan.report.get('impacts', [])
                    plan_id = plan.report.get('plan_id')
                except SchemaError:
                    cached_ready = False
            actions.append({
                'catalog': alias,
                'aliases': group['aliases'],
                'repo': target['repo'],
                'from_commit': group['cached_commit'],
                'to_commit': desired_commit,
                'status': 'pending',
                'needs_sync': True,
                'fetch_needed': not cached_ready,
                'fetched': False,
                'applied': False,
                'plan_id': plan_id,
                'impacts': impacts,
            })
            continue

        mgr = ProfileManager(machine_file, chosen_agent)
        progress(f'同步基线 {alias} -> {_short(desired_commit)}')
        plan, fetched, _, _ = _adopt_with_cache(
            mgr, alias, desired_commit, desired_release, source_map, git_transport=git_transport,
        )
        raw_result = plan.apply(mgr.state_root)
        applied_reports.append(raw_result)
        actions.append({
            'catalog': alias,
            'aliases': group['aliases'],
            'repo': plan.report['catalog_source']['selected']['repo'],
            'from_commit': group['cached_commit'],
            'to_commit': desired_commit,
            'status': 'synced',
            'needs_sync': False,
            'fetched': fetched,
            'applied': True,
            'plan_id': plan.report.get('plan_id'),
            'run_id': raw_result.get('run_id') or (Path(raw_result['recovery']).parent.name if isinstance(raw_result.get('recovery'), str) else None),
            'impacts': plan.report.get('impacts', []),
        })

    return {
        'schema_version': 1,
        'operation': 'baseline-sync',
        'machine_id': inspection['machine_id'],
        'machine_file': inspection['machine_file'],
        'state_root': inspection['state_root'],
        'preview_only': not write,
        'pending': any_pending,
        'aligned': (not any_pending) or write,
        'actions': actions,
        'applied_reports': applied_reports,
    }


def render_baseline_status(report, *, color=False):
    lines = [
        _style(f"基线状态 ({report['machine_id']})", _ANSI_BOLD, color),
        f"  Machine: {report['machine_file']}",
    ]
    if report.get('mismatches'):
        lines.append(_style('  ⚠ 存在 machine.yaml 与 machines/<id>.yaml 声明差异，请运行 doctor 查看详情', _ANSI_YELLOW, color))
    for group in report['groups']:
        aliases = ', '.join(group['aliases'])
        declared = _short(group['catalog_commit'])
        cached = _short(group['cached_commit'])
        if not group['needs_sync']:
            badge = _style('✓ 已对齐', _ANSI_GREEN, color)
            lines.append(f'  {badge}  {aliases} ({group["repo"]} @ {declared})')
        else:
            reason_map = {
                'unbound': 'machine.yaml 未绑定 catalog_roots',
                'missing_checkout': '本地缓存目录缺失',
                'stale_cache': f'缓存版本落后 (本地 {cached} -> 声明 {declared})',
            }
            reason = reason_map.get(group['status'], group['status'])
            badge = _style('! 需同步', _ANSI_YELLOW, color)
            lines.append(f'  {badge}  {aliases}: {reason}')
    if not report['aligned']:
        lines.append('')
        lines.append('下一步：')
        lines.append('  claude-config baseline sync --write')
    return '\n'.join(lines)


def render_baseline_sync(report, *, color=False):
    header = '基线同步预览' if report['preview_only'] else '基线同步结果'
    lines = [_style(f"{header} ({report['machine_id']})", _ANSI_BOLD, color)]
    for item in report['actions']:
        aliases = ', '.join(item['aliases'])
        if item['status'] == 'aligned':
            badge = _style('✓ 已对齐', _ANSI_GREEN, color)
            lines.append(f'  {badge}  {aliases} @ {_short(item["to_commit"])}')
        elif item['status'] == 'pending':
            badge = _style('→ 待同步', _ANSI_CYAN, color)
            fetch_note = '（需先拉取源码与 Release）' if item.get('fetch_needed') else '（本地缓存已就绪，仅更新指向）'
            lines.append(f'  {badge}  {aliases}: {_short(item["from_commit"])} -> {_short(item["to_commit"])} {fetch_note}')
        else:
            badge = _style('✓ 已同步', _ANSI_GREEN, color)
            fetch_note = '（含源码拉取与基线采用）' if item.get('fetched') else '（使用本地缓存完成基线采用）'
            lines.append(f'  {badge}  {aliases}: {_short(item["from_commit"])} -> {_short(item["to_commit"])} {fetch_note}')
    if report['preview_only'] and report['pending']:
        lines.append('')
        lines.append('提示：当前仅预览，未写入文件；如需执行请追加 --write 或 --yes。')
    elif not report['preview_only'] and any(item['applied'] for item in report['actions']):
        lines.append('')
        lines.append('下一步：可运行 claude-config status 或 claude-config sync 同步各端配置。')
    return '\n'.join(lines)


def _confirm_baseline_sync(report, color):
    print(render_baseline_sync(report, color=color))
    try:
        answer = input('\n确认执行以上基线同步吗？[y/N]: ').strip().lower()
    except (EOFError, KeyboardInterrupt):
        return False
    return answer in ('y', 'yes')


def _build_baseline_parser():
    parser = ReportParser(
        prog='claude-config baseline',
        description='查看或一键同步 catalog 基线（自动串联 fetch-baseline + adopt-baseline）',
    )
    sub = parser.add_subparsers(dest='subcommand', required=True)

    common = ReportParser(add_help=False)
    common.add_argument('--machine', type=Path, default=Path.home() / '.claude-config-tool/machine.yaml')
    common.add_argument('--catalog', help='仅检查/同步指定 catalog 别名')
    common.add_argument('--format', choices=('auto', 'text', 'json'), default='auto')
    common.add_argument('--verbose', action='store_true')

    sub.add_parser('status', parents=[common], help='只读检查 catalogs.yaml 与本地缓存是否对齐')

    p_sync = sub.add_parser('sync', parents=[common], help='同步本地缓存与 catalogs.yaml / machine.yaml 基线指向')
    p_sync.add_argument('--release', help='显式目标版本（baseline-<40位SHA> 或 40 位 SHA）')
    p_sync.add_argument('--source-map', type=Path)
    p_sync.add_argument('--agent', choices=('claude-code', 'codex', 'antigravity'))
    p_sync.add_argument('--git-transport', choices=GIT_TRANSPORTS, default=DEFAULT_GIT_TRANSPORT)
    p_sync.add_argument('--dry-run', action='store_true', help='仅预览，不拉取或写入任何文件')
    p_sync.add_argument('--write', '--yes', dest='write', action='store_true', help='直接执行同步与基线采用')

    p_adopt = sub.add_parser('adopt', parents=[common], help='一步拉取并采用指定的 40 位 commit SHA 或 baseline-<sha>')
    p_adopt.add_argument('target', help='目标 40 位 commit SHA 或 baseline-<40位SHA>')
    p_adopt.add_argument('--source-map', type=Path)
    p_adopt.add_argument('--agent', choices=('claude-code', 'codex', 'antigravity'))
    p_adopt.add_argument('--git-transport', choices=GIT_TRANSPORTS, default=DEFAULT_GIT_TRANSPORT)
    p_adopt.add_argument('--dry-run', action='store_true', help='仅预览，不拉取或写入任何文件')
    p_adopt.add_argument('--write', '--yes', dest='write', action='store_true', help='直接执行同步与基线采用')

    return parser


def main(argv):
    parser = _build_baseline_parser()
    fmt = report_format(argv)
    try:
        args = parser.parse_args(argv)
        machine_path = args.machine.expanduser().absolute()
        color = terminal_color_enabled('text' if fmt == 'json' else fmt)
        if args.subcommand == 'status':
            report = inspect_baselines(machine_path, catalog_filter=args.catalog)
            progress(None)
            print(encode_json(public_value(report)) if fmt == 'json' else render_baseline_status(report, color=color))
            return 0

        release = args.target if args.subcommand == 'adopt' else args.release
        if args.dry_run and args.write:
            raise UsageError('--dry-run 与 --write/--yes 不能同时使用')

        interactive = (not args.dry_run and not args.write and fmt != 'json'
                       and sys.stdin.isatty() and sys.stdout.isatty())

        preview = sync_baseline_groups(
            machine_path,
            catalog_filter=args.catalog,
            release=release,
            source_map=args.source_map,
            agent=args.agent,
            git_transport=args.git_transport,
            write=False,
        )
        should_write = bool(args.write)
        if not should_write and preview['pending'] and interactive:
            progress(None)
            should_write = _confirm_baseline_sync(preview, color)
            if not should_write:
                print('已取消，未修改任何文件。')
                return 0

        if should_write and preview['pending']:
            result = sync_baseline_groups(
                machine_path,
                catalog_filter=args.catalog,
                release=release,
                source_map=args.source_map,
                agent=args.agent,
                git_transport=args.git_transport,
                write=True,
            )
            # Persist per-adoption standard report for auditability without altering CLI output contract.
            state_root = Path(result['state_root'])
            for raw_item in result.get('applied_reports', []):
                save_report(
                    build_report(
                        raw_item,
                        operation='adopt-baseline',
                        preview_only=False,
                        execution_started=True,
                        context={
                            'target_label': '固定资源基线采用',
                            'machine_file': result['machine_file'],
                            'machine_id': result['machine_id'],
                        },
                    ),
                    state_root,
                )
            display = {k: v for k, v in result.items() if k != 'applied_reports'}
        else:
            display = {k: v for k, v in preview.items() if k != 'applied_reports'}

        progress(None)
        print(encode_json(public_value(display)) if fmt == 'json' else render_baseline_sync(display, color=color))
        return 0
    except (Exception, KeyboardInterrupt) as exc:
        progress(None)
        reason = str(exc) if isinstance(exc, (UsageError, SchemaError, ValueError)) else '基线操作失败；请检查 catalogs.yaml、machine.yaml 及网络/仓库状态。'
        code = 'interrupted' if isinstance(exc, KeyboardInterrupt) else 'invalid_arguments' if isinstance(exc, UsageError) else 'baseline_failed'
        exit_status = 130 if isinstance(exc, KeyboardInterrupt) else 2
        err_report = {
            'schema_version': 1,
            'operation': 'baseline',
            'ready': False,
            'error': {'code': code, 'reason': reason, 'exit_code': exit_status},
        }
        if fmt == 'json':
            print(encode_json(public_value(err_report)))
        else:
            print(f'错误：{reason}', file=sys.stderr)
        return exit_status
