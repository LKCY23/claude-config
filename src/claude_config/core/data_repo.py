"""Read-only inspection, pull, validation, and guarded publish helpers for the config_dir Git repository."""
from datetime import datetime
import json
from pathlib import Path
import subprocess

from .native import parse_json, parse_yaml, snapshot_read
from .plugin_locks import validate_locks
from .schema import (AGENTS, APPS, PLUGIN_LOCK_SUFFIX, SchemaError, mapping, profile_yaml_files,
                     validate_catalogs, validate_machine_profiles, validate_profile, within)
from .fields import validate_settings


def _run_git(root, args, *, read_only=True, timeout=60):
    cmd = ['git']
    if read_only:
        cmd.append('--no-optional-locks')
    cmd.extend(['-C', str(root), *args])
    return subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=timeout)


def _is_state_path(rel_path):
    clean = rel_path.rstrip('/')
    return clean == '.state' or clean.startswith('.state/')


def _parse_porcelain_z(raw_output):
    dirty_files = []
    untracked_files = []
    entries = raw_output.split('\0')
    idx = 0
    while idx < len(entries):
        entry = entries[idx]
        idx += 1
        if not entry:
            continue
        status = entry[:2]
        path = entry[3:] if len(entry) >= 4 else ''
        extra_path = None
        if status and (status[0] in ('R', 'C') or status[1] in ('R', 'C')):
            if idx < len(entries):
                extra_path = entries[idx]
                idx += 1
        if _is_state_path(path):
            continue
        if status == '??':
            untracked_files.append(path)
        else:
            dirty_files.append(path)
            if extra_path and not _is_state_path(extra_path):
                dirty_files.append(extra_path)
    return sorted(set(dirty_files)), sorted(set(untracked_files))


def inspect_repo(config_dir, *, fetch=False):
    """Inspect config_dir Git state strictly read-only unless fetch=True is requested."""
    root = Path(config_dir).expanduser().absolute()
    base = {
        'is_git': False,
        'path': str(root),
        'state': 'not_git',
        'branch': None,
        'upstream': None,
        'head': None,
        'head_full': None,
        'ahead': 0,
        'behind': 0,
        'diverged': False,
        'dirty': False,
        'dirty_files': [],
        'untracked_files': [],
        'fetched': False,
        'fetch_error': None,
    }
    if not root.is_dir():
        return base

    probe = _run_git(root, ['rev-parse', '--is-inside-work-tree'], read_only=True)
    if probe.returncode != 0 or probe.stdout.strip() != 'true':
        return base

    fetched = False
    fetch_error = None
    if fetch:
        res_fetch = _run_git(root, ['fetch', '--quiet'], read_only=False)
        if res_fetch.returncode == 0:
            fetched = True
        else:
            fetch_error = (res_fetch.stderr or res_fetch.stdout or 'git fetch failed').strip()

    head_res = _run_git(root, ['rev-parse', '--short', 'HEAD'], read_only=True)
    head_full_res = _run_git(root, ['rev-parse', 'HEAD'], read_only=True)
    head = head_res.stdout.strip() if head_res.returncode == 0 else None
    head_full = head_full_res.stdout.strip() if head_full_res.returncode == 0 else None

    branch_res = _run_git(root, ['symbolic-ref', '--quiet', '--short', 'HEAD'], read_only=True)
    branch = branch_res.stdout.strip() if branch_res.returncode == 0 else 'HEAD (detached)'

    upstream_res = _run_git(root, ['rev-parse', '--abbrev-ref', '--symbolic-full-name', '@{upstream}'], read_only=True)
    upstream = upstream_res.stdout.strip() if upstream_res.returncode == 0 else None

    ahead = 0
    behind = 0
    if upstream and head_full:
        lr = _run_git(root, ['rev-list', '--left-right', '--count', f'HEAD...{upstream}'], read_only=True)
        if lr.returncode == 0:
            parts = lr.stdout.strip().split()
            if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
                ahead, behind = int(parts[0]), int(parts[1])

    diverged = ahead > 0 and behind > 0

    status_res = _run_git(root, ['status', '--porcelain=v1', '-z', '-uall'], read_only=True)
    dirty_files, untracked_files = _parse_porcelain_z(status_res.stdout) if status_res.returncode == 0 else ([], [])
    dirty = bool(dirty_files or untracked_files)

    if dirty:
        state = 'dirty'
    elif diverged:
        state = 'diverged'
    elif behind > 0:
        state = 'behind'
    elif ahead > 0:
        state = 'ahead'
    else:
        state = 'clean'

    return {
        'is_git': True,
        'path': str(root),
        'state': state,
        'branch': branch,
        'upstream': upstream,
        'head': head,
        'head_full': head_full,
        'ahead': ahead,
        'behind': behind,
        'diverged': diverged,
        'dirty': dirty,
        'dirty_files': dirty_files,
        'untracked_files': untracked_files,
        'fetched': fetched,
        'fetch_error': fetch_error,
    }


def pull_repo(config_dir, *, dry_run=False, fetch=True):
    """Safely fast-forward pull config_dir, refusing dirty or diverged trees."""
    root = Path(config_dir).expanduser().absolute()
    info = inspect_repo(root, fetch=fetch)

    if not info['is_git']:
        return {'ready': False, 'preview_only': dry_run, 'status': 'not_git',
                'pulled': False, 'reason': f'配置目录不是 Git 仓库：{root}', 'repo': info, 'updated_files': []}
    if info['fetch_error']:
        return {'ready': False, 'preview_only': dry_run, 'status': 'fetch_failed',
                'pulled': False, 'reason': f"拉取远端失败：{info['fetch_error']}", 'repo': info, 'updated_files': []}
    if info['dirty']:
        return {'ready': False, 'preview_only': dry_run, 'status': 'dirty',
                'pulled': False,
                'reason': '配置仓库存在未提交改动，已阻止拉取；请先执行 claude-config data publish 或手动处理改动。',
                'repo': info, 'updated_files': []}
    if not info['upstream']:
        return {'ready': False, 'preview_only': dry_run, 'status': 'no_upstream',
                'pulled': False, 'reason': '当前分支未设置上游跟踪分支，无法执行 pull --ff-only。',
                'repo': info, 'updated_files': []}
    if info['diverged']:
        return {'ready': False, 'preview_only': dry_run, 'status': 'diverged',
                'pulled': False,
                'reason': f"本地领先 {info['ahead']} 个提交且落后远端 {info['behind']} 个提交（已分叉），请先手动 rebase/merge。",
                'repo': info, 'updated_files': []}
    if info['behind'] == 0:
        return {'ready': True, 'preview_only': dry_run, 'status': 'up_to_date',
                'pulled': False, 'would_pull_commits': 0, 'repo': info, 'updated_files': []}

    diff_res = _run_git(root, ['diff', '--name-only', f"HEAD..{info['upstream']}"], read_only=True)
    updated_files = [line for line in diff_res.stdout.splitlines() if line.strip()] if diff_res.returncode == 0 else []

    if dry_run:
        return {
            'ready': True,
            'preview_only': True,
            'status': 'pending',
            'pulled': False,
            'would_pull_commits': info['behind'],
            'before_head': info['head'],
            'repo': info,
            'updated_files': updated_files,
        }

    before_head = info['head_full']
    pull_res = _run_git(root, ['pull', '--ff-only'], read_only=False)
    if pull_res.returncode != 0:
        detail = (pull_res.stderr or pull_res.stdout or 'git pull --ff-only failed').strip()
        return {'ready': False, 'preview_only': False, 'status': 'pull_failed',
                'pulled': False, 'reason': f'git pull --ff-only 失败：{detail}',
                'repo': inspect_repo(root, fetch=False), 'updated_files': []}

    after_info = inspect_repo(root, fetch=False)
    if before_head and after_info['head_full']:
        changed_res = _run_git(root, ['diff', '--name-only', f"{before_head}..{after_info['head_full']}"], read_only=True)
        if changed_res.returncode == 0:
            updated_files = [line for line in changed_res.stdout.splitlines() if line.strip()]

    return {
        'ready': True,
        'preview_only': False,
        'status': 'pulled',
        'pulled': True,
        'before_head': before_head[:8] if before_head else None,
        'after_head': after_info['head'],
        'pulled_commits': info['behind'],
        'repo': after_info,
        'updated_files': updated_files,
    }


def validate_data_repo(config_dir):
    """Run local CI-equivalent structural validation against config_dir."""
    root = Path(config_dir).expanduser().absolute()
    errors = []
    checked = 0

    def _record_error(rel, exc):
        errors.append({'file': str(rel), 'reason': str(exc)})

    catalogs_path = root / 'catalogs.yaml'
    if catalogs_path.exists():
        checked += 1
        try:
            raw, _ = snapshot_read(catalogs_path)
            validate_catalogs(parse_yaml(raw))
        except (SchemaError, ValueError, OSError) as exc:
            _record_error('catalogs.yaml', exc)

    machines_dir = root / 'machines'
    if machines_dir.is_dir():
        for path in sorted(machines_dir.glob('*.yaml')):
            if not path.is_file():
                continue
            rel = path.relative_to(root).as_posix()
            checked += 1
            try:
                raw, _ = snapshot_read(path)
                data = validate_machine_profiles(parse_yaml(raw), path.stem)
                for group in ('agents', 'apps'):
                    for target_name, profile_rel in data[group].items():
                        within(root, profile_rel, must_exist=True)
            except (SchemaError, ValueError, OSError) as exc:
                _record_error(rel, exc)

    agents_dir = root / 'profiles' / 'agents'
    if agents_dir.is_dir():
        for agent_dir in sorted(agents_dir.iterdir()):
            if not agent_dir.is_dir():
                continue
            agent = agent_dir.name
            if agent not in AGENTS:
                _record_error(agent_dir.relative_to(root).as_posix(), f'未知 agent 目录：{agent}')
                continue
            for path in profile_yaml_files(agent_dir):
                rel = path.relative_to(root).as_posix()
                checked += 1
                try:
                    raw, _ = snapshot_read(path)
                    data = parse_yaml(raw)
                    validate_profile(data, kind='agent', expected=agent)
                    validate_settings(agent, data.get('settings', {}))
                except (SchemaError, ValueError, OSError) as exc:
                    _record_error(rel, exc)
            for lock_file in sorted(agent_dir.glob('*' + PLUGIN_LOCK_SUFFIX)):
                if not lock_file.is_file():
                    continue
                rel = lock_file.relative_to(root).as_posix()
                checked += 1
                stem = lock_file.name[:-len(PLUGIN_LOCK_SUFFIX)]
                expected_profile_rel = (Path('profiles') / 'agents' / agent / (stem + '.yaml')).as_posix()
                try:
                    if not (root / expected_profile_rel).is_file():
                        raise SchemaError(f'锁文件缺少对应的 Profile：{expected_profile_rel}')
                    raw, _ = snapshot_read(lock_file)
                    validate_locks(parse_yaml(raw), agent, expected_profile_rel)
                except (SchemaError, ValueError, OSError) as exc:
                    _record_error(rel, exc)

    apps_dir = root / 'profiles' / 'apps'
    if apps_dir.is_dir():
        for app_dir in sorted(apps_dir.iterdir()):
            if not app_dir.is_dir():
                continue
            app = app_dir.name
            if app not in APPS:
                _record_error(app_dir.relative_to(root).as_posix(), f'未知 app 目录：{app}')
                continue
            for path in profile_yaml_files(app_dir):
                rel = path.relative_to(root).as_posix()
                checked += 1
                try:
                    raw, _ = snapshot_read(path)
                    data = parse_yaml(raw)
                    validate_profile(data, kind='app', expected=app)
                except (SchemaError, ValueError, OSError) as exc:
                    _record_error(rel, exc)

    migrations_dir = root / 'catalog-migrations'
    if migrations_dir.is_dir():
        for path in sorted(migrations_dir.iterdir()):
            if not path.is_file():
                continue
            rel = path.relative_to(root).as_posix()
            if path.suffix in ('.yaml', '.yml'):
                checked += 1
                try:
                    raw, _ = snapshot_read(path)
                    mapping(parse_yaml(raw), rel)
                except (SchemaError, ValueError, OSError) as exc:
                    _record_error(rel, exc)
            elif path.suffix == '.json':
                checked += 1
                try:
                    raw, _ = snapshot_read(path)
                    mapping(parse_json(raw), rel)
                except (SchemaError, ValueError, OSError, json.JSONDecodeError) as exc:
                    _record_error(rel, exc)

    return {'ok': len(errors) == 0, 'checked_files': checked, 'errors': errors}


def _default_commit_message(files):
    if not files:
        return 'chore(config): sync configuration updates'
    labels = []
    for item in files[:3]:
        if item.endswith(PLUGIN_LOCK_SUFFIX):
            p = Path(item)
            agent = p.parent.name
            prof = p.name[:-len(PLUGIN_LOCK_SUFFIX)]
            labels.append(f'{agent}/{prof} locks')
        elif item.startswith('profiles/agents/'):
            p = Path(item)
            labels.append(f'{p.parent.name}/{p.stem}')
        elif item.startswith('profiles/apps/'):
            p = Path(item)
            labels.append(f'{p.parent.name}/{p.stem}')
        elif item.startswith('machines/'):
            labels.append(Path(item).stem)
        else:
            labels.append(Path(item).name)
    suffix = f' (+{len(files) - 3} more)' if len(files) > 3 else ''
    return f"chore(config): update {', '.join(labels)}{suffix}"


def publish_repo(config_dir, *, message=None, create_pr=False, dry_run=False):
    """Validate config_dir, show diff preview, or stage/commit/push (or open PR)."""
    root = Path(config_dir).expanduser().absolute()
    info = inspect_repo(root, fetch=False)
    if not info['is_git']:
        return {'ready': False, 'preview_only': dry_run, 'status': 'not_git',
                'reason': f'配置目录不是 Git 仓库：{root}', 'repo': info}

    if info['upstream'] and not dry_run and not create_pr:
        info = inspect_repo(root, fetch=True)
        if info['fetch_error']:
            return {'ready': False, 'preview_only': False, 'status': 'fetch_failed',
                    'reason': f"发布前检查远端失败：{info['fetch_error']}", 'repo': info}

    validation = validate_data_repo(root)
    if not validation['ok']:
        first = validation['errors'][0]
        return {
            'ready': False,
            'preview_only': dry_run,
            'status': 'validation_failed',
            'reason': f"本地校验未通过（{first['file']}）：{first['reason']}",
            'validation': validation,
            'repo': info,
        }

    files = sorted(set(info['dirty_files'] + info['untracked_files']))
    if not files and info['ahead'] == 0:
        return {
            'ready': True,
            'preview_only': dry_run,
            'status': 'nothing_to_publish',
            'files': [],
            'diff_stat': '',
            'proposed_message': None,
            'ahead': 0,
            'create_pr': create_pr,
            'validation': validation,
            'repo': info,
        }

    if info['behind'] > 0 and not create_pr:
        return {
            'ready': False,
            'preview_only': dry_run,
            'status': 'behind_remote',
            'reason': f"远端领先 {info['behind']} 个提交，请先执行 claude-config data pull。",
            'files': files,
            'validation': validation,
            'repo': info,
        }

    if not create_pr and not info['upstream']:
        return {
            'ready': False,
            'preview_only': dry_run,
            'status': 'no_upstream',
            'reason': '当前分支未配置上游跟踪分支，无法直接推送（可使用 --pr 或先配置 upstream）。',
            'files': files,
            'validation': validation,
            'repo': info,
        }

    proposed_message = message or _default_commit_message(files)
    stat_res = _run_git(root, ['diff', '--stat', '--', *info['dirty_files']], read_only=True) if info['dirty_files'] else None
    diff_stat = stat_res.stdout.strip() if stat_res and stat_res.returncode == 0 else ''
    if info['untracked_files']:
        extra = '\n'.join(f' {f} (new untracked file)' for f in info['untracked_files'])
        diff_stat = f'{diff_stat}\n{extra}'.strip()

    if dry_run:
        return {
            'ready': True,
            'preview_only': True,
            'status': 'pending',
            'files': files,
            'diff_stat': diff_stat,
            'proposed_message': proposed_message,
            'ahead': info['ahead'],
            'create_pr': create_pr,
            'validation': validation,
            'repo': info,
        }

    target_branch = info['branch']
    created_branch = None
    if create_pr and target_branch in ('master', 'main', 'HEAD (detached)'):
        stamp = datetime.now().strftime('%Y%m%d-%H%M%S')
        candidate = f'sync/{stamp}'
        suffix = 0
        while _run_git(root, ['show-ref', '--verify', '--quiet', f'refs/heads/{candidate}'], read_only=True).returncode == 0:
            suffix += 1
            candidate = f'sync/{stamp}-{suffix}'
        br_res = _run_git(root, ['checkout', '-b', candidate], read_only=False)
        if br_res.returncode != 0:
            detail = (br_res.stderr or br_res.stdout or 'git checkout -b failed').strip()
            return {'ready': False, 'preview_only': False, 'status': 'branch_failed',
                    'reason': f'创建同步分支失败：{detail}', 'repo': info, 'validation': validation}
        target_branch = candidate
        created_branch = candidate

    commit_sha = info['head']
    committed = False
    if files:
        add_res = _run_git(root, ['add', '-A', '--', *files], read_only=False)
        if add_res.returncode != 0:
            detail = (add_res.stderr or add_res.stdout or 'git add failed').strip()
            return {'ready': False, 'preview_only': False, 'status': 'git_add_failed',
                    'reason': f'暂存改动失败：{detail}', 'files': files, 'repo': inspect_repo(root), 'validation': validation}
        commit_res = _run_git(root, ['commit', '-m', proposed_message], read_only=False)
        if commit_res.returncode != 0:
            detail = (commit_res.stderr or commit_res.stdout or 'git commit failed').strip()
            return {'ready': False, 'preview_only': False, 'status': 'git_commit_failed',
                    'reason': f'提交失败：{detail}', 'files': files, 'repo': inspect_repo(root), 'validation': validation}
        committed = True
        commit_sha = inspect_repo(root)['head']

    push_args = ['push', '-u', 'origin', target_branch] if (create_pr or created_branch) else ['push']
    push_res = _run_git(root, push_args, read_only=False)
    if push_res.returncode != 0:
        detail = (push_res.stderr or push_res.stdout or 'git push failed').strip()
        return {
            'ready': False,
            'preview_only': False,
            'status': 'push_failed',
            'reason': f'推送远端失败：{detail}',
            'committed': committed,
            'commit': commit_sha,
            'branch': target_branch,
            'files': files,
            'validation': validation,
            'repo': inspect_repo(root),
        }

    pr_url = None
    pr_error = None
    if create_pr:
        body_lines = ['Automated config sync from `claude-config data publish --pr`.', '', 'Changed files:']
        body_lines.extend(f'- `{f}`' for f in files)
        gh_res = subprocess.run(
            ['gh', 'pr', 'create', '--title', proposed_message, '--body', '\n'.join(body_lines)],
            cwd=str(root), capture_output=True, text=True, check=False, timeout=60,
        )
        if gh_res.returncode == 0:
            pr_url = gh_res.stdout.strip().splitlines()[-1] if gh_res.stdout.strip() else None
        else:
            pr_error = (gh_res.stderr or gh_res.stdout or 'gh pr create failed').strip()

    after_info = inspect_repo(root, fetch=False)
    return {
        'ready': pr_error is None,
        'preview_only': False,
        'status': 'published' if pr_error is None else 'pr_failed',
        'committed': committed,
        'commit': commit_sha,
        'branch': target_branch,
        'files': files,
        'proposed_message': proposed_message,
        'pushed': True,
        'pr_url': pr_url,
        'reason': f'已推送分支 {target_branch}，但创建 PR 失败：{pr_error}' if pr_error else None,
        'validation': validation,
        'repo': after_info,
    }


def format_dirty_hint(config_dir):
    """Return a concise 1-line Chinese reminder if config_dir has unpublished local Git changes."""
    info = inspect_repo(config_dir, fetch=False)
    if not info['is_git']:
        return None
    changed = sorted(set(info['dirty_files'] + info['untracked_files']))
    if changed:
        preview = '、'.join(changed[:3])
        if len(changed) > 3:
            preview += f' 等 {len(changed)} 项'
        return f'提示：配置仓库存在 {len(changed)} 处本地声明变更（{preview}），可运行 `claude-config data publish` 校验并推送到远端。'
    if info['ahead'] > 0:
        return f"提示：配置仓库本地领先远端 {info['ahead']} 个提交，可运行 `claude-config data publish` 推送到远端。"
    return None
