"""Previewable rollback of one new-format file run, preserving later edits."""
from copy import deepcopy
import hashlib
from pathlib import Path
import json
import re

from .merge import fingerprint
from .native import parse_json, snapshot_read
from .schema import closed, require, version
from .transaction import FileChange, FilePlan, file_fingerprint
from .journal import read_journal, owner_status


def snapshot(value):
    require(isinstance(value, dict) and type(value.get('exists')) is bool, 'Invalid recovery file snapshot')
    if value['exists']:
        closed(value, ('exists', 'sha256', 'mode'), ('exists', 'sha256', 'mode'), 'recovery snapshot')
        require(isinstance(value['sha256'], str) and re.fullmatch(r'[0-9a-f]{64}', value['sha256'])
                and type(value['mode']) is int and 0 <= value['mode'] <= 0o777, 'Invalid recovery fingerprint or mode')
    else:
        require(value == {'exists': False}, 'Missing-file snapshot cannot contain other fields')


def rollback_plan(manager, run):
    require(isinstance(run, str) and re.fullmatch(r'[0-9a-f]{32}', run), 'Rollback requires a new-format run ID')
    root = manager.state_root / 'runs' / run
    path = root / 'journal.json'
    require(not (manager.state_root / 'runs').is_symlink() and root.resolve().is_relative_to(manager.state_root.resolve())
            and root.resolve().parent == (manager.state_root / 'runs').resolve() and not root.is_symlink(), 'Recovery run escapes its state root')
    original, expected = snapshot_read(path)
    journal = read_journal(path)
    require(journal['schema_version'] in (1, 2), 'Unsupported execution journal version')
    if journal['schema_version'] == 2 and journal['status'] == 'running':
        require(owner_status(journal.get('owner')) == 'exited', 'Execution owner is still active or cannot be verified')
    closed(journal, ('schema_version', 'kind', 'plan_id', 'created_at', 'status', 'scopes', 'operations', 'error_type', 'events', 'owner', 'updated_at', 'unchanged_count'),
           ('schema_version', 'kind', 'plan_id', 'created_at', 'status', 'scopes', 'operations'), 'recovery journal')
    require(journal['kind'] == 'profile-file-run', 'Recovery supports only the new file executor journal')
    scopes = manager.recovery_scopes if hasattr(manager, 'recovery_scopes') else [str(manager.native_path.resolve())]
    require(journal['scopes'] == scopes, 'Run does not belong to this native configuration binding')
    require(journal['status'] in ('applied', 'failed', 'running', 'interrupted', 'rolled_back'), 'Unknown recovery run state')
    require(isinstance(journal['operations'], list), 'Invalid recovery operation list')
    sources = {**manager.sources, path: expected}
    if journal['schema_version'] == 2:
        sources[path.with_name('events.jsonl')] = file_fingerprint(path.with_name('events.jsonl'))
    changes, decisions, blockers = [], [], []
    native_operations, steps, virtual = [], [], {}
    seen = set()
    for row in reversed(journal['operations']):
        if row.get('action') == 'native_plugin' and row.get('status') == 'unchanged':
            continue
        if row.get('kind') == 'native-plugin':
            require(not hasattr(manager, 'app'), 'App recovery cannot operate native Agent plugins')
            from .native_plugins import PluginOperation, PluginRestoreOperation, validate_native_row, backup_snapshot
            operation = PluginOperation(row['definition'])
            validate_native_row(row, operation)
            if hasattr(manager, 'check_native_operation'):
                manager.check_native_operation(operation)
            else:
                require(operation.agent == manager.agent and operation.root == manager.native_path.parent.resolve()
                        and operation.definition['executable'] == str(Path(manager.entry['binding']['executable']).resolve()), 'Native run belongs to another runtime binding')
            if not hasattr(manager, 'check_native_operation') and manager.agent == 'claude-code':
                from .registrations import claude_user_config
                require(operation.user_config == claude_user_config(manager.entry['binding']).resolve(), 'Native user file binding differs from the recorded run')
            if row['status'] not in ('started', 'applied', 'recovery_conflict'):
                continue
            current = operation.world()
            if 'after' not in row and row['status'] == 'started' and current != row['before']:
                # Only v2 per-plugin intent can support reconstruction of a
                # missing completion observation. Legacy ambiguity stays blocked.
                if journal['schema_version'] == 2 and row.get('plugin_actions'):
                    try:
                        operation.inspect_interrupted(row)
                    except (ValueError, OSError):
                        pass
            matched = (row.get('safe_recovery') or row.get('cleanup_safe')) and current == row.get('after')
            restored = current == row['before']
            if not (matched or restored):
                blockers.append({'code': 'recovery_conflict', 'path': ['recovery', str(operation.root)],
                                 'reason': 'Native plugin metadata or image changed after this operation'})
            decisions.append({'path': ['recovery', str(operation.root), 'plugins'], 'source': {'kind': 'native-run', 'run': run},
                              'target': {'kind': 'native-plugin'}, 'before_exists': True, 'before': {'fingerprint': fingerprint(current)},
                              'incoming_exists': True, 'incoming': {'fingerprint': fingerprint(row['before'])},
                              'after_exists': True, 'after': {'fingerprint': fingerprint(row['before'] if matched or restored else current)},
                              'action': 'native_restore' if matched and not restored else 'unchanged' if restored else 'conflict',
                              'category': 'unresolved_conflict' if not (matched or restored) else 'policy_merge',
                              'policy': 'exact-native-run-result', 'policy_origin': 'recovery_adapter',
                              'reason': 'Reconstruct recorded images through native installers; later state changes block restoration',
                              'alternatives': ['retain_current', 'manual_reconcile_and_replan'], 'check_status': 'passed' if matched or restored else 'blocked'})
            for native_path, native_expected in current['files'].items():
                sources[Path(native_path)] = native_expected
            for native_path, backup in row['backups'].items():
                backup_path = Path(backup)
                require(backup_path.parent.resolve() == root.resolve(), 'Native recovery backup must belong to this run')
                actual = file_fingerprint(backup_path)
                require(actual['sha256'] == backup_snapshot(row, native_path)['sha256'], 'Native recovery backup fingerprint changed')
                sources[backup_path] = actual
            if matched and not restored:
                restoration = PluginRestoreOperation(row, root)
                restoration.preflight()
                native_operations.append(restoration)
                steps.append(restoration)
            virtual.update({str(Path(path).resolve()): value for path, value in row['before']['files'].items()})
            continue
        closed(row, ('path', 'owner', 'before', 'action', 'after_sha256', 'status', 'backup', 'expected_after', 'after', 'step'),
               ('path', 'owner', 'before', 'action', 'after_sha256', 'status'), 'recovery file operation')
        require(isinstance(row['path'], str) and Path(row['path']).is_absolute() and isinstance(row['owner'], str), 'Invalid recovery target identity')
        target = Path(row['path'])
        actual = target.resolve()
        require(actual not in seen and not actual.is_relative_to(root.resolve()), 'Duplicate target or run metadata used as a recovery target')
        seen.add(actual)
        require(row['action'] in ('write', 'delete') and row['status'] in ('planned', 'started', 'applied', 'unchanged', 'failed', 'rolled_back', 'recovery_conflict'), 'Unknown recovery operation state')
        snapshot(row['before'])
        if row['status'] in ('planned', 'unchanged'):
            continue
        snapshot(row.get('expected_after'))
        require(row['action'] == 'write' and row['expected_after']['exists'] and row['after_sha256'] == row['expected_after']['sha256']
                or row['action'] == 'delete' and row['expected_after'] == {'exists': False} and row['after_sha256'] is None,
                'Recovery action and expected result disagree')
        if 'after' in row:
            snapshot(row['after'])
            require(row['after'] == row['expected_after'], 'Recorded write differs from planned result')
        observed = file_fingerprint(target)
        current = virtual.get(str(actual), observed)
        sources[target] = observed
        desired = row['before']
        matched = current == row['expected_after']
        restored = current == desired
        conflict = not (matched or restored)
        decisions.append({'path': ['recovery', str(target)], 'source': {'kind': 'run-backup', 'run': run},
                          'target': {'kind': 'native-file', 'owner': row['owner']},
                          'before_exists': current['exists'], 'before': current, 'incoming_exists': desired['exists'], 'incoming': desired,
                          'after_exists': (current if conflict else desired)['exists'], 'after': current if conflict else desired,
                          'action': 'conflict' if conflict else 'unchanged' if restored else 'restore_file' if desired['exists'] else 'remove_run_created_file',
                          'category': 'unresolved_conflict' if conflict else 'unchanged' if restored else 'policy_merge',
                          'policy': 'exact-run-result', 'policy_origin': 'recovery_adapter',
                          'reason': 'Later changes are preserved; this run cannot overwrite them' if conflict else 'Restore only when current state equals the recorded run result; already restored files are retained',
                          'alternatives': ['retain_current', 'manual_reconcile_and_replan'], 'check_status': 'blocked' if conflict else 'passed'})
        if conflict:
            blockers.append({'code': 'recovery_conflict', 'path': ['recovery', str(target)], 'reason': 'Current file differs from both recorded before and after snapshots'})
            continue
        if restored:
            continue
        content = b''
        if desired['exists']:
            require('backup' in row and isinstance(row['backup'], str), 'Recovery operation has no backup')
            backup = Path(row['backup'])
            require(backup.is_absolute() and backup.parent.resolve() == root.resolve(), 'Recovery backup must belong to this run')
            content, backup_expected = snapshot_read(backup)
            require(backup_expected['exists'] and backup_expected['sha256'] == desired['sha256'], 'Recovery backup fingerprint changed')
            sources[backup] = backup_expected
        def validate(data, desired=desired, target=target):
            require(not desired['exists'] and not data or desired['exists'] and hashlib.sha256(data).hexdigest() == desired['sha256'], 'Recovery content fingerprint differs')
        change = FileChange(target, content, 'rollback:' + row['owner'], validate, observed,
                            desired.get('mode'), delete=not desired['exists'])
        changes.append(change)
        steps.append(change)
        virtual[str(actual)] = desired
    updated = deepcopy(journal)
    updated['status'] = 'rolled_back'
    content = original if journal['status'] == 'rolled_back' else (json.dumps(updated, indent=2) + '\n').encode()
    change = FileChange(path, content, 'recovery-run-status', parse_json, expected)
    changes.append(change)
    steps.append(change)
    identity = {'app': manager.app} if hasattr(manager, 'app') else {'agent_type': manager.agent} if hasattr(manager, 'agent') else {'kind': 'combined-cutover'}
    report = {'operation': 'rollback', **identity, 'ready': not blockers,
              'plan_id': fingerprint({'run': run, 'journal': expected, 'decisions': decisions}),
              'checks': [{'name': 'recovery_scope', 'status': 'passed', 'reason': 'New journal belongs to the selected native binding'}],
              'decisions': decisions, 'blockers': blockers, 'original_run': run}
    def lifecycle_guard():
        if hasattr(manager, 'recovery_phase_check'):
            manager.recovery_phase_check([change.path for change in changes])
        elif hasattr(manager, 'recovery_check'):
            for change in changes:
                manager.recovery_check(change.path)
    def phase_guard():
        if report['ready']:
            lifecycle_guard()
    def step_guard(step):
        if hasattr(manager, 'recovery_check'):
            manager.recovery_check(step.path if isinstance(step, FileChange) else None)
    if hasattr(manager, 'recovery_check'):
        try:
            lifecycle_guard()
        except (OSError, ValueError, RuntimeError):
            report['blockers'].append({'code': 'app_running', 'reason': 'Close the App normally and replan native preference recovery'})
            report['ready'] = False
    return FilePlan(changes, sources=sources, scopes=journal['scopes'], report=report, native_operations=native_operations, steps=steps,
                    guards=[phase_guard], step_guard=step_guard, recovery_guard=lifecycle_guard)
