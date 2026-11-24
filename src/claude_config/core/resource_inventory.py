"""Observe selected skill deployments without adopting files or executing skills."""
from collections import Counter
from pathlib import Path
import hashlib
import json
import re
import sqlite3

from .merge import fingerprint
from .native import parse_json, parse_yaml, snapshot_read
from .transaction import PlanStale, file_fingerprint


def change_kind(baseline, local):
    if local.get('inspection'):
        return 'unreadable_or_unsafe'
    if not local.get('exists'):
        return 'missing'
    if not baseline.get('exists'):
        return 'added'
    if baseline.get('sha256') != local.get('sha256'):
        return 'content_changed'
    return 'mode_changed'


def skill_entry(path):
    """An entry is evidence of a file, never proof of native discovery."""
    path = Path(path)
    result = {'path': str(path), 'exists': path.is_file(),
              'symlink': path.is_symlink() or path.parent.is_symlink()}
    try:
        result['resolved'] = str(path.resolve())
        if result['exists']:
            # Native skill entries are text. Do not read arbitrary large targets.
            if path.stat().st_size > 2 * 1024 * 1024:
                result['inspection'] = 'entry_too_large'
            else:
                result.update(sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                              mode=path.stat().st_mode & 0o777)
    except (OSError, RuntimeError):
        result['inspection'] = 'unreadable'
    return result


def cc_switch_root(manager):
    if manager.agent == 'antigravity':
        return None
    explicit = manager.machine.get('bindings', {}).get('paths', {}).get('cc-switch-data')
    if explicit:
        return Path(explicit)
    standard = Path.home() / ('.claude' if manager.agent == 'claude-code' else '.codex')
    # A separate test/configuration instance is not owned by the default CC Switch.
    return Path.home() / '.cc-switch' if manager.native_path.parent == standard else None


def external_claims(root, agent, identities):
    if root is None or not identities:
        return {'status': 'not_applicable', 'claims': []}
    database = Path(root) / 'cc-switch.db'
    if not database.exists():
        return {'status': 'absent', 'claims': []}
    column = 'enabled_claude' if agent == 'claude-code' else 'enabled_codex'
    try:
        with sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True, timeout=2) as connection:
            # Never select settings, provider credentials or request history.
            rows = connection.execute('SELECT name, directory FROM skills WHERE ' + column + ' != 0').fetchall()
        claims = sorted({identity for name, directory in rows for identity in identities
                         if identity in (name, directory)})
        return {'status': 'read', 'claims': claims}
    except (sqlite3.Error, OSError):
        return {'status': 'unreadable', 'claims': []}


def native_enabled(agent, native, identity, entry):
    if agent == 'antigravity':
        return None
    if agent == 'claude-code':
        value = native.get('skillOverrides', {}).get(identity)
        return {'on': True, 'off': False}.get(value)
    rows = native.get('skills', {}).get('config', [])
    matches = [row.get('enabled') for row in rows if isinstance(row, dict)
               and isinstance(row.get('path'), str)
               and Path(row['path']).resolve() == entry.resolve()]
    return matches[0] if len(matches) == 1 else None


def _read_json_readonly(path):
    raw, expected = snapshot_read(path)
    if not expected['exists']:
        return None, expected
    return parse_json(raw), expected


def _antigravity_skill_name(skill_md, fallback):
    try:
        if skill_md.is_symlink() or not skill_md.is_file() or skill_md.stat().st_size > 2 * 1024 * 1024:
            return fallback
        text = snapshot_read(skill_md)[0].decode()
        match = re.match(r'\A---\r?\n(.*?)\r?\n---(?:\r?\n|$)', text, re.S)
        if match:
            meta = parse_yaml(match[1].encode())
            name = meta.get('name')
            if isinstance(name, str) and name:
                return name
    except (OSError, ValueError, UnicodeError):
        pass
    return fallback


def _antigravity_mcp_servers(path, checks, subject):
    try:
        doc, expected = _read_json_readonly(path)
    except (OSError, ValueError) as exc:
        checks.append({'name': 'antigravity_mcp_config', 'status': 'blocked', 'code': 'antigravity_mcp_unreadable',
                       'subject': subject, 'reason': f'Cannot read Antigravity MCP config ({type(exc).__name__})'})
        return []
    if not expected['exists'] or doc is None:
        return []
    servers = doc.get('mcpServers', {})
    if not isinstance(servers, dict):
        checks.append({'name': 'antigravity_mcp_config', 'status': 'blocked', 'code': 'antigravity_mcp_invalid',
                       'subject': subject, 'reason': 'Antigravity mcpServers must be an object'})
        return []
    return sorted(name for name in servers if isinstance(name, str) and name)


def inspect_antigravity(config_root):
    """Read-only Antigravity discovery; redacts/never retains userSettings, globalPermissionGrants, sidecars, or tokens."""
    root = Path(config_root)
    checks = []
    plugins_enabled = {}
    config_status = 'absent'
    config_path = root / 'config.json'
    try:
        config_doc, expected = _read_json_readonly(config_path)
        if expected['exists'] and config_doc is not None:
            config_status = 'read'
            raw_plugins = config_doc.get('plugins', {})
            if isinstance(raw_plugins, dict):
                for directory, entry in sorted(raw_plugins.items()):
                    if isinstance(directory, str) and isinstance(entry, dict) and type(entry.get('enabled')) is bool:
                        plugins_enabled[directory] = entry['enabled']
            del config_doc
    except (OSError, ValueError) as exc:
        config_status = 'unreadable'
        checks.append({'name': 'antigravity_config', 'status': 'blocked', 'code': 'antigravity_config_unreadable',
                       'reason': f'Cannot read Antigravity config.json ({type(exc).__name__})'})

    plugins = []
    plugins_dir = root / 'plugins'
    if plugins_dir.is_dir() and not plugins_dir.is_symlink():
        for entry_dir in sorted(plugins_dir.iterdir(), key=lambda p: p.name):
            if entry_dir.is_symlink() or not entry_dir.is_dir():
                continue
            manifest_path = None
            for candidate in (entry_dir / 'plugin.json', entry_dir / '.antigravity-plugin' / 'plugin.json'):
                if candidate.exists() or candidate.is_symlink():
                    manifest_path = candidate
                    break
            if manifest_path is None:
                continue
            try:
                manifest, expected = _read_json_readonly(manifest_path)
                if not expected['exists'] or manifest is None:
                    continue
            except (OSError, ValueError) as exc:
                checks.append({'name': 'antigravity_plugin_manifest', 'status': 'blocked',
                               'code': 'invalid_plugin_manifest', 'plugin_dir': entry_dir.name,
                               'reason': f"Cannot read plugin manifest in '{entry_dir.name}' ({type(exc).__name__})"})
                continue
            raw_name = manifest.get('name')
            manifest_name = raw_name if isinstance(raw_name, str) and raw_name else None
            effective_name = manifest_name or entry_dir.name
            manifest_disabled = manifest.get('disabled') if type(manifest.get('disabled')) is bool else None
            config_enabled = plugins_enabled.get(entry_dir.name)
            enabled = config_enabled if config_enabled is not None else (not manifest_disabled if manifest_disabled is not None else True)

            plugin_skills = []
            plugin_skills_dir = entry_dir / 'skills'
            if plugin_skills_dir.is_dir() and not plugin_skills_dir.is_symlink():
                for sub in sorted(plugin_skills_dir.iterdir(), key=lambda p: p.name):
                    skill_md = sub / 'SKILL.md'
                    if not sub.is_symlink() and sub.is_dir() and skill_md.is_file():
                        skill_name = _antigravity_skill_name(skill_md, sub.name)
                        plugin_skills.append({'dir': sub.name, 'name': skill_name, 'path': str(skill_md)})

            plugin_mcp = _antigravity_mcp_servers(entry_dir / 'mcp_config.json', checks, 'plugin:' + entry_dir.name)
            has_hooks = (entry_dir / 'hooks.json').is_file() and not (entry_dir / 'hooks.json').is_symlink()
            plugins.append({
                'dir': entry_dir.name,
                'name': effective_name,
                'manifest_name': manifest_name,
                'dir_name_mismatch': manifest_name is not None and manifest_name != entry_dir.name,
                'manifest_path': str(manifest_path),
                'manifest_kind': 'antigravity-plugin' if manifest_path.parent.name == '.antigravity-plugin' else 'plugin.json',
                'config_enabled': config_enabled,
                'manifest_disabled': manifest_disabled,
                'enabled': enabled,
                'skills': [s['name'] for s in plugin_skills],
                'skill_entries': plugin_skills,
                'mcp_servers': plugin_mcp,
                'has_hooks': has_hooks,
            })

    standalone_skills = []
    skills_dir = root / 'skills'
    if skills_dir.is_dir() and not skills_dir.is_symlink():
        for skill_dir in sorted(skills_dir.iterdir(), key=lambda p: p.name):
            skill_md = skill_dir / 'SKILL.md'
            if not skill_dir.is_symlink() and skill_dir.is_dir() and skill_md.is_file():
                skill_name = _antigravity_skill_name(skill_md, skill_dir.name)
                standalone_skills.append({
                    'dir': skill_dir.name,
                    'name': skill_name,
                    'path': str(skill_md),
                    'entry': skill_entry(skill_md),
                })

    global_mcp = _antigravity_mcp_servers(root / 'mcp_config.json', checks, 'global')

    # Collision & hygiene checks
    by_name = {}
    by_dir = {p['dir']: p for p in plugins}
    for plugin in plugins:
        by_name.setdefault(plugin['name'], []).append(plugin)
    for plugin_name, group in sorted(by_name.items()):
        if len(group) > 1:
            dirs = [p['dir'] for p in group]
            checks.append({
                'name': 'duplicate_plugin_name',
                'status': 'blocked',
                'code': 'duplicate_plugin_name',
                'plugin_name': plugin_name,
                'directories': dirs,
                'reason': f"Duplicate Antigravity plugin name '{plugin_name}' across directories: {', '.join(dirs)}",
            })
    for plugin in plugins:
        if plugin['dir_name_mismatch'] and plugin['manifest_name'] in by_dir:
            target = by_dir[plugin['manifest_name']]
            if target['dir'] != plugin['dir'] and target['name'] != plugin['manifest_name']:
                checks.append({
                    'name': 'plugin_dir_name_collision',
                    'status': 'blocked',
                    'code': 'plugin_dir_name_collision',
                    'plugin_name': plugin['manifest_name'],
                    'directories': sorted({plugin['dir'], target['dir']}),
                    'reason': f"Plugin directory '{plugin['dir']}' declares name '{plugin['manifest_name']}', colliding with plugin directory '{target['dir']}'",
                })

    for standalone in standalone_skills:
        standalone_keys = {standalone['dir'], standalone['name']}
        for plugin in plugins:
            for ps in plugin['skill_entries']:
                if standalone_keys & {ps['dir'], ps['name']}:
                    checks.append({
                        'name': 'skill_plugin_overlap',
                        'status': 'blocked',
                        'code': 'skill_plugin_overlap',
                        'skill': standalone['dir'],
                        'plugin_dir': plugin['dir'],
                        'plugin_skill': ps['dir'],
                        'reason': f"Standalone skill '{standalone['dir']}' overlaps plugin '{plugin['dir']}' skill '{ps['dir']}'",
                    })

    global_mcp_set = set(global_mcp)
    for plugin in plugins:
        for server_name in sorted(global_mcp_set & set(plugin['mcp_servers'])):
            checks.append({
                'name': 'mcp_server_collision',
                'status': 'blocked',
                'code': 'mcp_server_collision',
                'server': server_name,
                'plugin_dir': plugin['dir'],
                'reason': f"Global MCP server '{server_name}' in mcp_config.json collides with plugin '{plugin['dir']}' mcp_config.json",
            })

    if not any(row['status'] in ('blocked', 'failed') for row in checks):
        checks.append({'name': 'antigravity_discovery', 'status': 'passed',
                       'reason': 'Antigravity read-only discovery completed with no collisions'})

    payload = {
        'config_root': str(root),
        'config_status': config_status,
        'plugins_enabled': plugins_enabled,
        'plugins': plugins,
        'skills': standalone_skills,
        'mcp_servers': global_mcp,
        'checks': checks,
    }
    payload['fingerprint'] = fingerprint({
        'config_status': config_status,
        'plugins_enabled': plugins_enabled,
        'plugins': plugins,
        'skills': standalone_skills,
        'mcp_servers': global_mcp,
    })
    return payload


def inspect_skills(manager, previous, rendered, native, *, selected_refs=None):
    """Return metadata/checks and a semantic guard, independent of busy DB logs."""
    if selected_refs is not None:
        previous = {ref: row for ref, row in previous.items() if ref in selected_refs}
    selected = {ref: item for ref, item in rendered.items() if item['package'].item['kind'] == 'skill'}
    identities = {item['identity'] for item in selected.values()}
    cc_root = cc_switch_root(manager)
    claims = external_claims(cc_root, manager.agent, identities)
    checks, rows, legacy_snapshots = [], [], {}
    if claims['status'] == 'unreadable':
        checks.append({'name': 'skill_external_manager', 'status': 'blocked',
                       'code': 'external_manager_unreadable',
                       'reason': 'Cannot inspect CC Switch skill ownership; no provider data was read'})
    for ref in sorted(set(selected) | {r for r, old in previous.items() if old['kind'] == 'skill'}):
        old = previous.get(ref, {})
        item = selected.get(ref)
        identity = item['identity'] if item else old['native']['identity']
        if item:
            install_root = (Path(item['slots']['install_root']) if manager.agent == 'codex'
                            else manager.native_path.parent / 'skills')
            root = install_root / identity
        else:
            root = Path(old['native']['root'])
        entry = root / 'SKILL.md'
        changes = []
        for location, expected in old.get('files', {}).items():
            if not Path(location).is_relative_to(Path(old['native']['root'])):
                continue
            try:
                actual = file_fingerprint(location)
            except (ValueError, OSError):
                actual = {'exists': True, 'inspection': 'unsafe_or_unreadable'}
            if expected != actual:
                changes.append({'path': location, 'kind': change_kind(expected, actual)})
        candidates = []
        other_roots = [Path.home() / '.agents/skills', Path.home() / '.claude/skills']
        if cc_root:
            other_roots.append(cc_root / 'skills')
        for alternate in other_roots:
            path = alternate / identity / 'SKILL.md'
            if path != entry and path.is_file():
                candidates.append(skill_entry(path))
        legacy = manager.native_path.parent / 'skills' / identity / 'SKILL.md'
        duplicate = manager.agent == 'codex' and legacy != entry and legacy.is_file()
        if manager.agent == 'codex' and item and legacy != entry:
            legacy_snapshots[str(legacy)] = skill_entry(legacy)
        row = {'agent_type': manager.agent, 'ref': ref, 'identity': identity,
               'selected': item is not None, 'managed': bool(old),
               'lifecycle': 'selected' if item else 'residual',
               'catalog_commit': item['package'].commit if item else old.get('catalog_commit'),
               'source_fingerprint': item['package'].source_fingerprint if item else old.get('source_fingerprint'),
               'desired_enabled': item['package'].selection.get('options', {}).get('control', {}).get('enabled') if item else None,
               'native_enabled': native_enabled(manager.agent, native, identity, entry),
               'entry': skill_entry(entry), 'file_changes': changes,
               'change_counts': dict(Counter(change['kind'] for change in changes)),
               'alternate_entries': candidates,
               'external_managers': ['cc-switch'] if identity in claims['claims'] else [],
               'legacy_entry': skill_entry(legacy) if duplicate else None,
               'native_discovery': {'status': 'not_verified', 'reason': 'File presence and settings are not a native discovery test'}}
        rows.append(row)
        if not item:
            continue
        path = ['resources', 'skills', ref]
        if identity in claims['claims']:
            checks.append({'name': 'skill_external_manager', 'path': path, 'status': 'blocked',
                           'code': 'external_skill_manager_conflict',
                           'reason': 'CC Switch also enables this skill for this client; hand off ownership before deployment'})
        if duplicate:
            checks.append({'name': 'skill_legacy_entry', 'path': path, 'status': 'blocked',
                           'code': 'duplicate_skill_entry',
                           'reason': 'A same-name legacy Codex entry remains; explicitly review and retire that entry'})
    ag_inventory = None
    if manager.agent == 'antigravity':
        ag_inventory = inspect_antigravity(manager.native_path.parent)
        checks.extend(ag_inventory['checks'])
    def guard():
        if external_claims(cc_root, manager.agent, identities) != claims:
            raise PlanStale('External skill ownership changed after planning')
        if any(skill_entry(path) != expected for path, expected in legacy_snapshots.items()):
            raise PlanStale('Legacy skill entry changed after planning')
        if ag_inventory is not None and inspect_antigravity(manager.native_path.parent)['fingerprint'] != ag_inventory['fingerprint']:
            raise PlanStale('Antigravity native discovery changed after planning')
    result = {'agent_type': manager.agent, 'skills': rows, 'external_manager_status': claims['status'],
              'ownership_fingerprint': fingerprint({'claims': claims, 'antigravity': ag_inventory['fingerprint']}) if ag_inventory is not None else fingerprint(claims),
              'checks': checks}
    if ag_inventory is not None:
        result['antigravity'] = ag_inventory
    return result, guard
