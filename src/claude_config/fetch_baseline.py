"""Fetch an explicit GitHub baseline into private prepared-source directories."""
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
import hashlib
import os
from pathlib import Path
import shlex
import shutil
import subprocess
from types import SimpleNamespace

from .profile import ReportParser, UsageError, output_report, report_format, report_verbose
from .core.baseline import baseline_group, catalog_target, default_agent_for_machine, validate_record
from .core.catalog import CatalogResolver, git, verify_git
from .core.catalog_schema import NAME, validate_catalog
from .core.execution_context import progress
from .core.manager import ProfileManager
from .core.native import parse_json, parse_yaml, snapshot_read
from .core.reporting import build_report, fallback_state_root, save_report, _private_path
from .core.schema import COMMIT, SchemaError, profile_yaml_files, require, validate_catalogs, validate_profile, within
from .core.source_transport import (
    DEFAULT_GIT_TRANSPORT,
    GIT_TRANSPORTS,
    classify_command_error,
    github_api as _github_api,
    prepare_git as _prepare_git,
    validate_git_transport,
)
from .core.transaction import atomic_write

_ACCESS_STATE: ContextVar[dict | None] = ContextVar('claude_config_release_access_state', default=None)


@contextmanager
def _capture_access_state(state):
    token = _ACCESS_STATE.set(state)
    try:
        yield
    finally:
        _ACCESS_STATE.reset(token)


def command(args, *, timeout=120, extra_env=None):
    env = dict(os.environ, GIT_TERMINAL_PROMPT='0')
    if extra_env:
        env.update(extra_env)
    for key in ('GIT_DIR', 'GIT_WORK_TREE', 'GIT_INDEX_FILE', 'GIT_OBJECT_DIRECTORY', 'GIT_ALTERNATE_OBJECT_DIRECTORIES'):
        env.pop(key, None)
    result = subprocess.run(args, capture_output=True, env=env, timeout=timeout)
    if result.returncode:
        raise classify_command_error(result.stderr.decode(errors='replace'))
    return result.stdout


def api(path, *, binary=False):
    return _github_api(path, binary=binary, gh_runner=command, access_state=_ACCESS_STATE.get())


def release_record(repo, commit, alias):
    tag = 'baseline-' + commit
    release = api(f'repos/{repo}/releases/tags/{tag}')
    require(not release.get('draft') and release.get('tag_name') == tag,
            'Requested baseline is not a published release')
    obj = api(f'repos/{repo}/git/ref/tags/{tag}')['object']
    for _ in range(4):
        if obj.get('type') != 'tag':
            break
        obj = api(f'repos/{repo}/git/tags/{obj["sha"]}')['object']
    require(obj.get('type') == 'commit' and obj.get('sha') == commit,
            'Release tag does not identify the requested exact commit')
    assets = [row for row in release.get('assets', []) if row.get('name') == alias + '.baseline.json']
    require(len(assets) == 1, 'Release must contain one record for the selected catalog')
    asset = assets[0]
    require(type(asset.get('id')) is int and type(asset.get('size')) is int and 0 < asset['size'] <= 1024 * 1024,
            'Invalid baseline record asset')
    raw = api(f'repos/{repo}/releases/assets/{asset["id"]}', binary=True)
    require(len(raw) == asset['size'], 'Downloaded baseline record size differs from its release asset')
    return raw


def prepare_git(root, repo, commit, *, git_transport=DEFAULT_GIT_TRANSPORT):
    """Keep existing checkouts intact; publish a new checkout only when complete."""
    return _prepare_git(root, repo, commit, git_transport=git_transport, runner=command)


def fetch(manager, alias, release, source_map=None, *, report=None, git_transport=DEFAULT_GIT_TRANSPORT):
    validate_git_transport(git_transport)
    access_state = {'release_access': 'gh'}
    report = {} if report is None else report
    report.update(ready=False, checks=[], decisions=[], blockers=[], results=[], scope=[['catalogs', alias]],
                  fetch_programs={name: shutil.which(name) for name in ('git', 'gh')},
                  source_retrieval={'git_transport': git_transport, **access_state})
    require(isinstance(alias, str) and NAME.fullmatch(alias), 'Invalid catalog alias')
    require(release.startswith('baseline-') and COMMIT.fullmatch(release[9:]),
            '--release requires baseline- followed by the exact 40-character commit')
    commit = release[9:]
    catalogs, _, _ = manager._read(manager.root / 'catalogs.yaml', lambda raw: validate_catalogs(parse_yaml(raw)))
    require(alias in catalogs['catalogs'] and alias in manager.machine['catalog_roots'],
            'Fetch requires an existing declared and bound catalog')
    aliases = baseline_group(catalogs, alias)
    require(len(aliases) == 1 or source_map is None, 'Repository fetch expects stable catalog paths')
    report['scope'] = [['catalogs', name] for name in aliases]
    impacts = []
    for name in [sibling for sibling in aliases if sibling != alias] + [alias]:
        _fetch_one(manager, name, release, source_map, catalogs, commit, report, aliases,
                   git_transport=git_transport, access_state=access_state)
        impacts.extend(report['impacts'])
    report['impacts'] = impacts
    return report


def _fetch_one(manager, alias, release, source_map, catalogs, commit, report, aliases,
               *, git_transport=DEFAULT_GIT_TRANSPORT, access_state=None):
    mapping = manager._read(Path(source_map).absolute(), parse_yaml)[0] if source_map else None
    target = catalog_target(catalogs['catalogs'][alias], alias, commit, mapping)
    state = {'release_access': 'gh'} if access_state is None else access_state
    progress(f'获取 Release 记录 ({release[:17]}...)')
    with _capture_access_state(state):
        record_bytes = release_record(target['repo'], commit, alias)
    report['source_retrieval'] = {'git_transport': git_transport, **state}
    record = parse_json(record_bytes)
    validate_record(record, target)
    source_root = manager.state_root / 'sources'
    checkout = source_root / target['repo'].replace('/', '--') / commit
    results = report['results']

    def prepare(root, repo, pin):
        changed = (
            prepare_git(root, repo, pin)
            if git_transport == DEFAULT_GIT_TRANSPORT
            else prepare_git(root, repo, pin, git_transport=git_transport)
        )
        results.append({'path': str(root), 'owner': 'prepared-source', 'action': 'fetch',
                        'status': 'applied' if changed else 'unchanged'})

    progress(f"拉取主目录仓库 {target['repo']}@{commit[:8]} ({git_transport})")
    prepare(checkout, target['repo'], commit)
    file = within(checkout, target['path']) / 'catalog.json'
    catalog_bytes = snapshot_read(file)[0]
    value = parse_json(catalog_bytes)
    validate_catalog(value)
    require(value['name'] == alias and hashlib.sha256(catalog_bytes).hexdigest() == record['catalog_sha256']
            and catalog_bytes == git(checkout, 'show', commit + ':' + file.relative_to(checkout.resolve()).as_posix()),
            'Prepared catalog differs from the published release record')
    candidate = deepcopy(catalogs)
    for name in aliases:
        candidate['catalogs'][name]['commit'] = commit
    candidate['catalogs'][alias] = target
    machine = deepcopy(manager.machine)
    machine['catalog_roots'].update(dict.fromkeys(aliases, str(checkout)))
    prepared = set()
    impacts = []
    for agent in ('claude-code', 'codex', 'antigravity'):
        for path in profile_yaml_files(manager.root / 'profiles/agents' / agent):
            profile = manager._read(path, parse_yaml)[0]
            validate_profile(profile, expected=agent)
            selections = {ref for group in profile.get('resources', {}).values() for ref in group}
            selected = {ref for ref in selections if ref.split('@')[1] == alias}
            if not selected:
                continue
            preview = SimpleNamespace(agent=agent, root=manager.root, state_root=manager.state_root,
                                      machine=machine, sources=manager.sources, references=manager.references,
                                      _read=manager._read)
            resolver = CatalogResolver(preview)
            resolver.catalogs = candidate
            queue, seen = list(selected), set()
            while queue:
                ref = queue.pop()
                if ref in seen:
                    continue
                seen.add(ref)
                require(ref in selections, 'Resource dependency must be explicitly selected: ' + ref)
                identifier, source_alias = ref.split('@')
                repository, directory, _, catalog = resolver._catalog(source_alias)
                require(identifier in catalog['resources'], 'Resource is missing from the selected catalog: ' + ref)
                item = catalog['resources'][identifier]
                require(agent in item['compatibility'], 'Resource lacks the selected agent entry: ' + ref)
                source = item['source']
                if source['kind'] in ('external', 'submodule'):
                    if source['kind'] == 'external':
                        root = source_root / source['repo'].replace('/', '--') / source['commit']
                    else:
                        root = within(directory, source['path'])
                        rows = git(repository, 'ls-files', '--stage', '--', root.relative_to(repository).as_posix()).decode().splitlines()
                        require(len(rows) == 1 and rows[0].split()[:2] == ['160000', source['commit']],
                                'Selected submodule differs from its declared Git pin')
                    key = str(root), source['repo'], source['commit']
                    if key not in prepared:
                        progress(f"准备外部资源源 ({len(prepared) + 1}): {source['repo']}@{source['commit'][:8]}")
                        prepare(root, source['repo'], source['commit'])
                        prepared.add(key)
                queue.extend(item['requires'].get('resources', []))
            progress(f'校验受影响 Profile 与依赖完整性 ({agent})')
            resolver.resolve(profile, selected=selected, source_only=True)
            impacts.append({'profile': path.relative_to(manager.root).as_posix(), 'resources': sorted(selected)})
    directory = _private_path(manager.state_root / 'releases' / target['repo'].replace('/', '--') / release)
    record_path = directory / (alias + '.baseline.json')
    if record_path.exists():
        require(not record_path.is_symlink() and record_path.read_bytes() == record_bytes,
                'Existing prepared release record differs; preserve it and inspect the source')
    else:
        atomic_write(record_path, record_bytes)
        results.append({'path': str(record_path), 'owner': 'release-record', 'action': 'fetch', 'status': 'applied'})
    adopt = ['claude-config', 'adopt-baseline', '--machine', str(manager.machine_path), '--agent', manager.agent,
             '--catalog', alias, '--commit', commit, '--checkout', str(checkout), '--release-record', str(record_path)]
    if source_map:
        adopt += ['--source-map', str(Path(source_map).absolute())]
    report.update(ready=True, checks=[{'name': 'released_baseline', 'status': 'passed',
                                     'reason': 'Published tag, record, fixed Git sources and selected resource bodies verified'}],
                  prepared={'catalog': alias, 'source': target, 'release': release, 'checkout': str(checkout),
                            'release_record': str(record_path), 'adopt_command': shlex.join(adopt)}, impacts=impacts)
    return report


def main(argv):
    parser = ReportParser(prog='claude-config fetch-baseline', description=__doc__)
    parser.add_argument('--machine', type=Path, default=Path.home() / '.claude-config-tool/machine.yaml')
    parser.add_argument('--agent', choices=('claude-code', 'codex', 'antigravity'), default=None,
                        help='可选；省略时自动选择 machine.yaml 中第一个已绑定 Agent')
    parser.add_argument('--catalog', required=True)
    parser.add_argument('--release', required=True)
    parser.add_argument('--source-map', type=Path)
    parser.add_argument('--git-transport', choices=GIT_TRANSPORTS, default=DEFAULT_GIT_TRANSPORT,
                        help='Git transport for preparing fixed commits (default: gh-https)')
    parser.add_argument('--format', choices=('auto', 'text', 'tui', 'json'), default='auto')
    parser.add_argument('--verbose', action='store_true', help='显示完整保留决策与细粒度输出')
    manager, error, raw, started = None, None, {}, False
    state = fallback_state_root()
    try:
        args = parser.parse_args(argv)
        machine_path = args.machine.expanduser().absolute()
        agent = args.agent or default_agent_for_machine(machine_path)
        manager = ProfileManager(machine_path, agent)
        state = manager.state_root
        started = True
        raw = fetch(manager, args.catalog, args.release, args.source_map, report=raw,
                    git_transport=args.git_transport)
    except (Exception, KeyboardInterrupt) as exc:
        error = {'code': 'interrupted' if isinstance(exc, KeyboardInterrupt) else 'fetch_failed', 'phase': 'retrieval',
                 'reason': str(exc) if isinstance(exc, (UsageError, SchemaError, ValueError)) else '基线获取失败；检查 Git/gh、网络、权限和来源声明。',
                 'exit_code': 130 if isinstance(exc, KeyboardInterrupt) else 2}
    report = build_report(raw, operation='fetch-baseline', preview_only=False, execution_started=started,
                          context={'target_label': '固定资源基线获取', 'machine_id': manager.machine['machine_id'] if manager else None},
                          error=error)
    save_report(report, state)
    return output_report(report, report_format(argv), verbose=report_verbose(argv))
