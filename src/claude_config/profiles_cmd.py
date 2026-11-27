"""Cross-Profile comparison, matrix dashboard, and explicit selective copy tools."""
from copy import deepcopy
from pathlib import Path
import sys

import yaml

from .core.data_repo import format_dirty_hint
from .core.execution_context import progress
from .core.fields import leaves, validate_settings
from .core.machine_profiles import MachineProfiles
from .core.merge import MISSING, get
from .core.native import parse_yaml, put, snapshot_read
from .core.plugin_locks import empty_locks, merge_locks, validate_local_pins, validate_locks
from .core.report_viewer import terminal_color_enabled
from .core.reporting import encode_json, public_value
from .core.schema import (AGENTS, APPS, PLUGIN_LOCK_SUFFIX, REF, SchemaError, as_pointer,
                          pointer, profile_yaml_files, require, state_directory,
                          validate_machine_profiles, validate_profile, within)
from .core.transaction import FileChange, FilePlan
from .profile import ReportParser, UsageError, report_format
from .status_cmd import effective_file_writes

_ANSI_GREEN = '\033[32m'
_ANSI_YELLOW = '\033[33m'
_ANSI_CYAN = '\033[36m'
_ANSI_RED = '\033[31m'
_ANSI_BOLD = '\033[1m'
_ANSI_RESET = '\033[0m'


def _style(text, code, color):
    return f'{code}{text}{_ANSI_RESET}' if color else text


def _load_local_pins(state_root):
    path = Path(state_root) / 'local-pins.yaml'
    if not path.exists():
        return {'schema_version': 1, 'profiles': {}}
    require(not path.is_symlink(), 'Local pins file must not be a symlink')
    raw, _ = snapshot_read(path)
    value = parse_yaml(raw) if raw else {'schema_version': 1, 'profiles': {}}
    return validate_local_pins(value)


def _machine_bindings(config_dir):
    bindings = {}
    machines_dir = Path(config_dir) / 'machines'
    if not machines_dir.is_dir():
        return bindings
    for path in sorted(machines_dir.glob('*.yaml')):
        if not path.is_file():
            continue
        raw, _ = snapshot_read(path)
        data = validate_machine_profiles(parse_yaml(raw), path.stem)
        for group in ('agents', 'apps'):
            for _, rel in data[group].items():
                bindings.setdefault(rel, []).append(data['machine_id'])
    for rel in bindings:
        bindings[rel].sort()
    return bindings


def _resolve_profile(config_dir, spec):
    """Resolve a user-supplied Profile name/path to (kind, target_type, rel_path, abs_path)."""
    require(isinstance(spec, str) and bool(spec.strip()), 'Profile 名称不能为空')
    root = Path(config_dir).expanduser().absolute()
    token = spec.strip()
    candidates = []
    if token.startswith('profiles/'):
        candidates.append(token if token.endswith('.yaml') else token + '.yaml')
    elif '/' in token:
        parts = Path(token if token.endswith('.yaml') else token + '.yaml').parts
        if len(parts) == 2:
            head, tail = parts
            if head in AGENTS:
                candidates.append(f'profiles/agents/{head}/{tail}')
            elif head in APPS:
                candidates.append(f'profiles/apps/{head}/{tail}')
    else:
        for agent in AGENTS:
            candidates.append(f'profiles/agents/{agent}/{token}.yaml')
        for app in APPS:
            candidates.append(f'profiles/apps/{app}/{token}.yaml')

    matches = []
    for rel in dict.fromkeys(candidates):
        try:
            abs_path = within(root, rel, must_exist=True)
        except SchemaError:
            continue
        parts = Path(rel).parts
        if len(parts) == 4 and parts[0] == 'profiles' and parts[1] in ('agents', 'apps') and parts[3].endswith('.yaml') and not parts[3].endswith(PLUGIN_LOCK_SUFFIX):
            kind = 'agent' if parts[1] == 'agents' else 'app'
            matches.append((kind, parts[2], rel, abs_path))

    if not matches:
        raise SchemaError(f'未找到 Profile：{spec}')
    if len(matches) > 1:
        names = '、'.join(m[2] for m in matches)
        raise SchemaError(f'Profile 名称存在歧义：{spec}（匹配：{names}），请使用 <type>/<name> 格式指定')
    return matches[0]


def _read_profile_and_lock(config_dir, kind, target_type, rel, local_pins):
    root = Path(config_dir).expanduser().absolute()
    path = within(root, rel, must_exist=True)
    raw, expected = snapshot_read(path)
    data = parse_yaml(raw)
    validate_profile(data, kind=kind, expected=target_type)
    if kind == 'agent':
        validate_settings(target_type, data.get('settings', {}))
        lock_file = path.with_suffix('.plugins.lock.yaml')
        require(not lock_file.is_symlink(), 'Plugin lock must not be a symlink')
        lock_raw, lock_expected = snapshot_read(lock_file)
        locks = parse_yaml(lock_raw) if lock_expected['exists'] else empty_locks(target_type, rel)
        validate_locks(locks, target_type, rel)
        effective, origins = merge_locks(locks, local_pins, target_type, rel)
    else:
        lock_file = None
        lock_expected = {'exists': False}
        locks = None
        effective = {'plugins': {}}
        origins = {}
    return {
        'kind': kind,
        'target_type': target_type,
        'profile': rel,
        'short': f'{target_type}/{Path(rel).stem}',
        'path': path,
        'expected': expected,
        'data': data,
        'lock_path': lock_file,
        'lock_expected': lock_expected,
        'locks': locks,
        'effective_locks': effective,
        'lock_origins': origins,
    }


def build_matrix(machine_path, *, agent_filter=None):
    machine_file = Path(machine_path).expanduser().absolute()
    profiles = MachineProfiles(machine_file, strict=False)
    config_dir = Path(profiles.effective['config_dir']).expanduser().absolute()
    state_root = state_directory(profiles.effective)
    local_pins = _load_local_pins(state_root)
    bindings = _machine_bindings(config_dir)

    agents_to_scan = [agent_filter] if agent_filter else list(AGENTS)
    agent_sections = {}
    for agent in agents_to_scan:
        agent_dir = config_dir / 'profiles' / 'agents' / agent
        if not agent_dir.is_dir():
            continue
        items = []
        for path in profile_yaml_files(agent_dir):
            rel = path.relative_to(config_dir).as_posix()
            loaded = _read_profile_and_lock(config_dir, 'agent', agent, rel, local_pins)
            data = loaded['data']
            plugins = {}
            for ref, entry in data.get('resources', {}).get('plugins', {}).items():
                pin = loaded['effective_locks'].get('plugins', {}).get(ref)
                plugins[ref] = {
                    'enabled': entry.get('options', {}).get('enabled'),
                    'pin_source': loaded['lock_origins'].get(ref, 'catalog') if ref in loaded['effective_locks'].get('plugins', {}) else 'catalog',
                    'version': pin['version'] if pin else None,
                    'commit': pin['source']['commit'][:8] if pin else None,
                }
            settings_map = {as_pointer(('settings', *p)): val for p, val in leaves(data.get('settings', {}))}
            resources_map = {
                grp: sorted(entries.keys())
                for grp, entries in data.get('resources', {}).items()
                if grp != 'plugins' and entries
            }
            items.append({
                'profile': rel,
                'short': loaded['short'],
                'machines': bindings.get(rel, []),
                'plugins': plugins,
                'settings': settings_map,
                'resources': resources_map,
            })
        if not items:
            continue

        all_plugins = sorted({ref for it in items for ref in it['plugins']})
        all_setting_keys = sorted({k for it in items for k in it['settings']})
        diff_settings = {}
        for key in all_setting_keys:
            vals = [it['settings'].get(key, MISSING) for it in items]
            if any(v != vals[0] for v in vals[1:]):
                diff_settings[key] = {it['short']: it['settings'].get(key, None) if key in it['settings'] else '<unset>' for it in items}

        all_res_refs = sorted({(grp, ref) for it in items for grp, refs in it['resources'].items() for ref in refs})
        diff_resources = {}
        for grp, ref in all_res_refs:
            presence = [ref in it['resources'].get(grp, ()) for it in items]
            if not all(presence):
                diff_resources[f'{grp}:{ref}'] = {it['short']: (ref in it['resources'].get(grp, ())) for it in items}

        agent_sections[agent] = {
            'profiles': items,
            'all_plugins': all_plugins,
            'diff_settings': diff_settings,
            'diff_resources': diff_resources,
        }

    return {
        'schema_version': 1,
        'operation': 'profiles-matrix',
        'read_only': True,
        'machine_id': profiles.effective['machine_id'],
        'config_dir': str(config_dir),
        'agents': agent_sections,
    }


def render_matrix(report, *, color=False):
    lines = [
        _style(f"Profile 矩阵总览 ({report['machine_id']})", _ANSI_BOLD, color),
        f"  配置目录: {report['config_dir']}",
    ]
    if not report['agents']:
        lines.append('  (未找到匹配的 Agent Profile)')
        return '\n'.join(lines)
    for agent, sec in report['agents'].items():
        lines.append('')
        lines.append(_style(f'[{agent}]', _ANSI_BOLD, color))
        for p in sec['profiles']:
            machines = '、'.join(p['machines']) if p['machines'] else '(未绑定机器)'
            lines.append(f"  · {p['short']}  <- 绑定机器: {machines}")
        if sec['all_plugins']:
            lines.append('  插件对比:')
            for ref in sec['all_plugins']:
                cols = []
                for p in sec['profiles']:
                    info = p['plugins'].get(ref)
                    if not info:
                        cols.append(f"{p['short']}=未选")
                    else:
                        ver = info['version'] or 'catalog'
                        en = '启用' if info['enabled'] is not False else '禁用'
                        cols.append(f"{p['short']}={ver}({info['pin_source']},{en})")
                lines.append(f'    - {ref}: ' + ' | '.join(cols))
        if sec['diff_settings']:
            lines.append('  差异设置:')
            for key, per_prof in sec['diff_settings'].items():
                cols = [f'{k}={v}' for k, v in per_prof.items()]
                lines.append(f'    - {key}: ' + ' | '.join(cols))
        if sec['diff_resources']:
            lines.append('  差异非插件资源:')
            for label, per_prof in sec['diff_resources'].items():
                cols = [f"{k}={'有' if v else '无'}" for k, v in per_prof.items()]
                lines.append(f'    - {label}: ' + ' | '.join(cols))
    return '\n'.join(lines)


def build_diff(machine_path, spec_a, spec_b):
    machine_file = Path(machine_path).expanduser().absolute()
    profiles = MachineProfiles(machine_file, strict=False)
    config_dir = Path(profiles.effective['config_dir']).expanduser().absolute()
    state_root = state_directory(profiles.effective)
    local_pins = _load_local_pins(state_root)
    bindings = _machine_bindings(config_dir)

    ka, ta, rela, _ = _resolve_profile(config_dir, spec_a)
    kb, tb, relb, _ = _resolve_profile(config_dir, spec_b)
    require(ka == kb and ta == tb, f'只能对比同类型 Profile：{rela} ({ta}) 与 {relb} ({tb})')

    pa = _read_profile_and_lock(config_dir, ka, ta, rela, local_pins)
    pb = _read_profile_and_lock(config_dir, kb, tb, relb, local_pins)

    set_a = {as_pointer(('settings', *p)): val for p, val in leaves(pa['data'].get('settings', {}))}
    set_b = {as_pointer(('settings', *p)): val for p, val in leaves(pb['data'].get('settings', {}))}
    settings_diff = []
    for key in sorted(set(set_a) | set(set_b)):
        va = set_a.get(key, MISSING)
        vb = set_b.get(key, MISSING)
        if va != vb:
            settings_diff.append({
                'path': key,
                'a': None if va is MISSING else va,
                'a_present': va is not MISSING,
                'b': None if vb is MISSING else vb,
                'b_present': vb is not MISSING,
            })

    resources_diff = []
    if ka == 'agent':
        res_a = pa['data'].get('resources', {})
        res_b = pb['data'].get('resources', {})
        for group in sorted(set(res_a) | set(res_b)):
            ga = res_a.get(group, {})
            gb = res_b.get(group, {})
            for ref in sorted(set(ga) | set(gb)):
                ea = ga.get(ref, MISSING)
                eb = gb.get(ref, MISSING)
                pin_a = pa['effective_locks']['plugins'].get(ref) if group == 'plugins' else None
                pin_b = pb['effective_locks']['plugins'].get(ref) if group == 'plugins' else None
                orig_a = pa['lock_origins'].get(ref, 'catalog') if group == 'plugins' else None
                orig_b = pb['lock_origins'].get(ref, 'catalog') if group == 'plugins' else None
                lock_diff = group == 'plugins' and (
                    (pin_a or {}).get('source', {}).get('commit') != (pin_b or {}).get('source', {}).get('commit')
                    or orig_a != orig_b
                )
                if ea != eb or lock_diff:
                    resources_diff.append({
                        'group': group,
                        'ref': ref,
                        'a_present': ea is not MISSING,
                        'b_present': eb is not MISSING,
                        'a': None if ea is MISSING else ea,
                        'b': None if eb is MISSING else eb,
                        'a_pin': {'version': pin_a['version'], 'origin': orig_a, 'commit': pin_a['source']['commit'][:8]} if pin_a else ({'version': None, 'origin': 'catalog'} if ea is not MISSING and group == 'plugins' else None),
                        'b_pin': {'version': pin_b['version'], 'origin': orig_b, 'commit': pin_b['source']['commit'][:8]} if pin_b else ({'version': None, 'origin': 'catalog'} if eb is not MISSING and group == 'plugins' else None),
                    })

    return {
        'schema_version': 1,
        'operation': 'profiles-diff',
        'read_only': True,
        'machine_id': profiles.effective['machine_id'],
        'config_dir': str(config_dir),
        'kind': ka,
        'target_type': ta,
        'a': {'profile': rela, 'short': pa['short'], 'machines': bindings.get(rela, [])},
        'b': {'profile': relb, 'short': pb['short'], 'machines': bindings.get(relb, [])},
        'identical': not settings_diff and not resources_diff,
        'settings_diff': settings_diff,
        'resources_diff': resources_diff,
    }


def render_diff(report, *, color=False):
    lines = [
        _style(f"Profile 结构对比 ({report['target_type']})", _ANSI_BOLD, color),
        f"  A: {report['a']['profile']}  (机器: {', '.join(report['a']['machines']) or '无'})",
        f"  B: {report['b']['profile']}  (机器: {', '.join(report['b']['machines']) or '无'})",
    ]
    if report['identical']:
        lines.append(f"  {_style('✓ 完全一致', _ANSI_GREEN, color)}")
        return '\n'.join(lines)
    if report['settings_diff']:
        lines.append('')
        lines.append(_style('设置差异：', _ANSI_BOLD, color))
        for item in report['settings_diff']:
            va = item['a'] if item['a_present'] else '<未声明>'
            vb = item['b'] if item['b_present'] else '<未声明>'
            lines.append(f"  · {item['path']}: A={va}  |  B={vb}")
    if report['resources_diff']:
        lines.append('')
        lines.append(_style('资源差异：', _ANSI_BOLD, color))
        for item in report['resources_diff']:
            sa = '已选' if item['a_present'] else '未选'
            sb = '已选' if item['b_present'] else '未选'
            if item['group'] == 'plugins':
                if item['a_pin']:
                    sa += f"@{item['a_pin'].get('version') or 'catalog'}({item['a_pin']['origin']})"
                if item['b_pin']:
                    sb += f"@{item['b_pin'].get('version') or 'catalog'}({item['b_pin']['origin']})"
            lines.append(f"  · [{item['group']}] {item['ref']}: A={sa}  |  B={sb}")
    return '\n'.join(lines)


def _find_resource_in_profile(profile_data, ref):
    require(REF.fullmatch(ref) is not None, '资源引用格式无效（需为 name@catalog）：' + ref)
    for group, entries in profile_data.get('resources', {}).items():
        if ref in entries:
            return group, entries[ref]
    return None, None


def _plan_copy_item(machine_path, *, src_spec, dst_specs, resource_refs=(), field_selectors=(), include_lock=False):
    require(bool(resource_refs or field_selectors), 'copy-item 至少需要指定一个 --resource 或 --field')
    machine_file = Path(machine_path).expanduser().absolute()
    profiles = MachineProfiles(machine_file, strict=False)
    config_dir = Path(profiles.effective['config_dir']).expanduser().absolute()
    state_root = state_directory(profiles.effective)
    local_pins = _load_local_pins(state_root)

    src_kind, src_type, src_rel, _ = _resolve_profile(config_dir, src_spec)
    if src_kind == 'app':
        require(not resource_refs and not include_lock, 'App Profile 不支持 --resource 或 --include-lock')
    src = _read_profile_and_lock(config_dir, src_kind, src_type, src_rel, local_pins)

    # Pre-validate requested items exist in source.
    resolved_resources = []
    for ref in resource_refs:
        grp, entry = _find_resource_in_profile(src['data'], ref)
        require(grp is not None, f'源 Profile {src_rel} 中未选择资源：{ref}')
        src_pin = src['locks']['plugins'].get(ref) if (include_lock and grp == 'plugins') else None
        if include_lock and grp == 'plugins' and ref not in src['locks']['plugins'] and ref in src['effective_locks']['plugins']:
            src_pin = src['effective_locks']['plugins'][ref]
        resolved_resources.append((grp, ref, deepcopy(entry), deepcopy(src_pin)))

    resolved_fields = []
    for sel in field_selectors:
        parts = pointer(sel)
        require(len(parts) > 1 and parts[0] == 'settings', '--field 必须以 /settings/ 开头：' + sel)
        val = get(src['data'].get('settings', {}), parts[1:])
        require(val is not MISSING, f'源 Profile {src_rel} 中未声明设置：{sel}')
        resolved_fields.append((sel, parts[1:], deepcopy(val)))

    seen_dst = set()
    targets = []
    changes = []
    sources = {}
    scopes = []
    for spec in dst_specs:
        d_kind, d_type, d_rel, d_abs = _resolve_profile(config_dir, spec)
        require(d_rel != src_rel, f'目标 Profile 不能与源相同：{d_rel}')
        require(d_rel not in seen_dst, f'重复的目标 Profile：{d_rel}')
        seen_dst.add(d_rel)
        require(d_kind == src_kind and d_type == src_type, f'跨类型禁止复制：{src_rel} ({src_type}) -> {d_rel} ({d_type})')
        dst = _read_profile_and_lock(config_dir, d_kind, d_type, d_rel, local_pins)
        sources[dst['path']] = dst['expected']
        scopes.append(str(dst['path'].resolve()))

        new_data = deepcopy(dst['data'])
        new_locks = deepcopy(dst['locks']) if d_kind == 'agent' else None
        actions = []

        for grp, ref, entry, src_pin in resolved_resources:
            new_data.setdefault('resources', {}).setdefault(grp, {})[ref] = deepcopy(entry)
            note = f'复制资源 [{grp}] {ref}'
            if include_lock and grp == 'plugins':
                if src_pin is not None:
                    new_locks['plugins'][ref] = deepcopy(src_pin)
                    note += f"（含版本锁 {src_pin['version']}）"
                elif ref in new_locks['plugins']:
                    new_locks['plugins'].pop(ref, None)
                    note += '（同步移除锁，跟随 catalog）'
            actions.append(note)

        for sel, key_path, val in resolved_fields:
            settings_obj = new_data.setdefault('settings', {})
            put(settings_obj, key_path, deepcopy(val))
            actions.append(f'复制设置 {sel} = {val}')

        validate_profile(new_data, kind=d_kind, expected=d_type)
        if d_kind == 'agent':
            validate_settings(d_type, new_data.get('settings', {}))
            validate_locks(new_locks, d_type, d_rel)

        profile_changed = new_data != dst['data']
        lock_changed = d_kind == 'agent' and new_locks != dst['locks']
        if profile_changed:
            raw_bytes = yaml.safe_dump(new_data, allow_unicode=True, sort_keys=False).encode()
            changes.append(FileChange(
                dst['path'], raw_bytes, 'profiles-copy-item',
                lambda raw, k=d_kind, t=d_type: (
                    validate_profile(parse_yaml(raw), kind=k, expected=t),
                    validate_settings(t, parse_yaml(raw).get('settings', {})) if k == 'agent' else None,
                ),
                dst['expected'],
            ))
        if lock_changed:
            sources[dst['lock_path']] = dst['lock_expected']
            scopes.append(str(dst['lock_path'].resolve()))
            lock_bytes = yaml.safe_dump(new_locks, allow_unicode=True, sort_keys=False).encode()
            changes.append(FileChange(
                dst['lock_path'], lock_bytes, 'profiles-copy-lock',
                lambda raw, t=d_type, p=d_rel: validate_locks(parse_yaml(raw), t, p),
                dst['lock_expected'],
            ))
        targets.append({
            'profile': d_rel,
            'profile_changed': profile_changed,
            'lock_changed': lock_changed,
            'actions': actions,
        })

    plan = FilePlan(changes, sources=sources, scopes=scopes, report={'ready': True, 'checks': [], 'decisions': [], 'blockers': []})
    has_writes = effective_file_writes(plan) > 0
    return {
        'schema_version': 1,
        'operation': 'profiles-copy-item',
        'preview_only': True,
        'ready': True,
        'machine_id': profiles.effective['machine_id'],
        'config_dir': str(config_dir),
        'state_root': str(state_root),
        'source_profile': src_rel,
        'has_writes': has_writes,
        'targets': targets,
    }, plan, config_dir, state_root


def _render_copy_item(report, *, color=False):
    header = 'Profile 项复制预览' if report.get('preview_only') else 'Profile 项复制结果'
    lines = [
        _style(f"{header} ({report['machine_id']})", _ANSI_BOLD, color),
        f"  源 Profile: {report['source_profile']}",
    ]
    for tgt in report['targets']:
        changed = tgt['profile_changed'] or tgt['lock_changed']
        if not changed:
            badge = _style('✓ 已一致', _ANSI_GREEN, color)
        elif report.get('preview_only'):
            badge = _style('→ 待复制', _ANSI_CYAN, color)
        else:
            badge = _style('✓ 已写入', _ANSI_GREEN, color)
        lines.append(f"  {badge}  {tgt['profile']}")
        for act in tgt['actions']:
            lines.append(f'    · {act}')
    if not report.get('has_writes'):
        lines.append('')
        lines.append('所有目标均已包含相同声明，无需写入。')
    elif report.get('preview_only'):
        lines.append('')
        lines.append('提示：当前仅预览，未修改任何 Profile；如需执行请追加 --write。')
    elif report.get('run_id'):
        lines.append('')
        lines.append(f"恢复 Run ID：{report['run_id']}")
    return '\n'.join(lines)


def _build_parser():
    parser = ReportParser(
        prog='claude-config profiles',
        description='多 Profile 对比矩阵、结构差异及显式选择性同步（不破坏 Profile 独立性）',
    )
    sub = parser.add_subparsers(dest='subcommand', required=True)

    common = ReportParser(add_help=False)
    common.add_argument('--machine', type=Path, default=Path.home() / '.claude-config-tool/machine.yaml')
    common.add_argument('--format', choices=('auto', 'text', 'json'), default='auto')

    p_mat = sub.add_parser('matrix', parents=[common], help='跨 Profile / 跨机器总览矩阵')
    p_mat.add_argument('--agent', choices=AGENTS, help='仅查看指定 Agent 的 Profile')

    p_diff = sub.add_parser('diff', parents=[common], help='详细对比两个同类型 Profile 的设置、资源与插件锁')
    p_diff.add_argument('profile_a', help='Profile A（如 claude-code/third-party 或完整相对路径）')
    p_diff.add_argument('profile_b', help='Profile B')

    p_copy = sub.add_parser('copy-item', parents=[common], help='显式将指定资源或设置项从源 Profile 复制到一个或多个目标 Profile')
    p_copy.add_argument('--from', dest='source', required=True, help='源 Profile')
    p_copy.add_argument('--to', dest='targets', action='append', required=True, help='目标 Profile（可重复）')
    p_copy.add_argument('--resource', action='append', default=[], help='要复制的资源 ref@catalog（可重复）')
    p_copy.add_argument('--field', action='append', default=[], help='要复制的设置 JSON Pointer，如 /settings/model（可重复）')
    p_copy.add_argument('--include-lock', action='store_true', help='复制插件时同步复制其版本锁（或同步跟随 catalog）')
    exec_group = p_copy.add_mutually_exclusive_group()
    exec_group.add_argument('--dry-run', action='store_true', help='仅预览，不写入文件')
    exec_group.add_argument('--write', '--yes', dest='write', action='store_true', help='跳过确认直接写入文件')
    return parser


def main(argv):
    parser = _build_parser()
    fmt = report_format(argv)
    try:
        args = parser.parse_args(argv)
        machine_file = args.machine.expanduser().absolute()
        if not machine_file.is_file():
            raise SchemaError('未找到 Machine；请运行 claude-config init，或指定 --machine。')
        color = terminal_color_enabled('text' if fmt == 'json' else fmt)

        if args.subcommand == 'matrix':
            progress('汇总 Profile 矩阵')
            report = build_matrix(machine_file, agent_filter=args.agent)
            progress(None)
            print(encode_json(public_value(report)) if fmt == 'json' else render_matrix(report, color=color))
            return 0

        if args.subcommand == 'diff':
            progress('对比 Profile 结构')
            report = build_diff(machine_file, args.profile_a, args.profile_b)
            progress(None)
            print(encode_json(public_value(report)) if fmt == 'json' else render_diff(report, color=color))
            return 0

        # copy-item
        progress('规划 Profile 选择性复制')
        report, plan, config_dir, state_root = _plan_copy_item(
            machine_file,
            src_spec=args.source,
            dst_specs=args.targets,
            resource_refs=args.resource,
            field_selectors=args.field,
            include_lock=args.include_lock,
        )
        execute = bool(args.write)
        interactive = (not args.dry_run and not args.write and fmt != 'json'
                       and sys.stdin.isatty() and sys.stdout.isatty())
        if interactive and report['has_writes']:
            progress(None)
            print(_render_copy_item(report, color=color))
            try:
                execute = input('\n确认执行以上复制写入吗？[y/N]: ').strip().lower() in ('y', 'yes')
            except (EOFError, KeyboardInterrupt):
                execute = False
            if not execute:
                print('已取消，未修改任何文件。')
                return 0

        if execute and not args.dry_run and report['has_writes']:
            progress('写入目标 Profile')
            raw_res = plan.apply(state_root)
            report['preview_only'] = False
            report['run_id'] = raw_res.get('run_id') or (Path(raw_res['recovery']).parent.name if isinstance(raw_res.get('recovery'), str) else None)

        progress(None)
        print(encode_json(public_value(report)) if fmt == 'json' else _render_copy_item(report, color=color))
        if fmt != 'json' and not report['preview_only'] and report['has_writes']:
            hint = format_dirty_hint(config_dir)
            if hint:
                print('\n' + hint)
        return 0
    except (Exception, KeyboardInterrupt) as exc:
        progress(None)
        reason = str(exc) if isinstance(exc, (UsageError, SchemaError, ValueError)) else 'Profile 操作失败；请检查参数与 YAML 格式。'
        code = 'interrupted' if isinstance(exc, KeyboardInterrupt) else 'invalid_arguments' if isinstance(exc, UsageError) else 'profiles_failed'
        exit_status = 130 if isinstance(exc, KeyboardInterrupt) else 2
        err_report = {
            'schema_version': 1,
            'operation': 'profiles',
            'ready': False,
            'error': {'code': code, 'reason': reason, 'exit_code': exit_status},
        }
        if fmt == 'json':
            print(encode_json(public_value(err_report)))
        else:
            print(f'错误：{reason}', file=sys.stderr)
        return exit_status
