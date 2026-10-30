"""Observe selected skill deployments without adopting files or executing skills."""
from collections import Counter
from pathlib import Path
import hashlib
import sqlite3

from .merge import fingerprint
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
    if agent == 'claude-code':
        value = native.get('skillOverrides', {}).get(identity)
        return {'on': True, 'off': False}.get(value)
    rows = native.get('skills', {}).get('config', [])
    matches = [row.get('enabled') for row in rows if isinstance(row, dict)
               and isinstance(row.get('path'), str)
               and Path(row['path']).resolve() == entry.resolve()]
    return matches[0] if len(matches) == 1 else None


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
    def guard():
        if external_claims(cc_root, manager.agent, identities) != claims:
            raise PlanStale('External skill ownership changed after planning')
        if any(skill_entry(path) != expected for path, expected in legacy_snapshots.items()):
            raise PlanStale('Legacy skill entry changed after planning')
    return {'agent_type': manager.agent, 'skills': rows, 'external_manager_status': claims['status'],
            'ownership_fingerprint': fingerprint(claims), 'checks': checks}, guard
