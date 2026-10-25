"""Close obsolete failure evidence only against a later verified managed state."""
from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path
import re

from .journal import read_journal
from .merge import fingerprint
from .native import parse_json, snapshot_read
from .native_plugins import PluginOperation, cache_version, image_fingerprint, validate_native_row
from .schema import require
from .transaction import FileChange, FilePlan, file_fingerprint


def reconcile_plan(manager, run):
    require(not hasattr(manager, 'app'), 'Run reconciliation requires an explicit Agent binding')
    require(isinstance(run, str) and re.fullmatch('[0-9a-f]{32}', run), 'An exact run ID is required')
    path = manager.state_root / 'runs' / run / 'journal.json'
    require(not path.parent.is_symlink(), 'Run directory cannot be a symlink')
    original, expected = snapshot_read(path)
    journal = read_journal(path)
    require(journal['status'] == 'failed', 'Only failed historical runs may be reconciled; running executions require inspection/recovery')
    remaining = [row for row in journal['operations'] if row['status'] in ('started', 'applied', 'recovery_conflict')]
    require(remaining and all(row.get('kind') == 'native-plugin' for row in remaining),
            'Remaining file mutations must be recovered before native history can be reconciled')
    state, state_snapshot = manager._state()
    require(state_snapshot['exists'], 'A successful managed baseline is required')
    sources = {**manager.sources, path: expected, manager.state_path: state_snapshot}
    if journal['schema_version'] == 2:
        sources[path.with_name('events.jsonl')] = file_fingerprint(path.with_name('events.jsonl'))
    provenance = None
    for candidate in sorted((manager.state_root / 'runs').glob('*/journal.json')):
        later = read_journal(candidate)
        if later['status'] != 'applied' or datetime.fromisoformat(later['created_at']) <= datetime.fromisoformat(journal['created_at']):
            continue
        if any(row.get('path') == str(manager.state_path) and row['status'] == 'applied' and row.get('after') == state_snapshot
               for row in later['operations']):
            provenance = candidate
            sources[candidate] = file_fingerprint(candidate)
            if later['schema_version'] == 2:
                sources[candidate.with_name('events.jsonl')] = file_fingerprint(candidate.with_name('events.jsonl'))
            break
    require(provenance is not None, 'Current management must match a recorded successful deployment newer than the failed run')
    proofs, directories = [], []
    for row in remaining:
        old_operation = PluginOperation(row['definition'])
        validate_native_row(row, old_operation)
        require(old_operation.agent == manager.agent and old_operation.root == manager.native_path.parent.resolve(),
                'Unresolved native operation belongs to another Agent binding')
        if manager.agent == 'claude-code':
            from .registrations import claude_user_config
            require(old_operation.user_config == claude_user_config(manager.entry['binding']).resolve(),
                    'Historical user configuration belongs to another binding')
        definition = deepcopy(row['definition'])
        executable = Path(manager.entry['binding']['executable']).resolve()
        definition['executable'], definition['executable_snapshot'] = str(executable), file_fingerprint(executable)
        sources[executable] = definition['executable_snapshot']
        definition['plugins'] = []
        for item in row['definition']['plugins']:
            record = state.get('resources', {}).get(item['ref'], {})
            receipt = record.get('native', {}).get('plugin')
            require(record.get('active') and receipt, 'Every unresolved plugin must have a later active managed receipt')
            require(receipt['selector'] == item['desired']['selector'], 'Plugin identity changed; manual historical review is required')
            definition['plugins'].append({'ref': item['ref'], 'desired': receipt, 'previous': receipt})
        operation = PluginOperation(definition)
        operation.preflight()
        require(operation.unchanged(), 'Current plugin state differs from the later successful management record')
        world = operation.world()
        for item in definition['plugins']:
            receipt = item['desired']
            parent = operation.cache_parent(receipt)
            versions = {name.split('/')[0] for name in world['directories'][str(parent)]['entries']}
            allowed = {cache_version(manager.agent, v) for v in receipt['history']} | set(receipt.get('retained_versions', []))
            require(versions <= allowed, 'Unknown historical image still requires recovery or review')
            for version, image in receipt['history'].items():
                image_path = parent / cache_version(manager.agent, version)
                if image_path.exists():
                    require(image_fingerprint(image_path) == image['image'], 'Historical managed image changed')
        sources.update({Path(p): snapshot for p, snapshot in world['files'].items()})
        directories.extend(operation.directories)
        proofs.append({'resources': [x['ref'] for x in definition['plugins']], 'native_fingerprint': fingerprint(world)})
    receipt = {'schema_version': 1, 'kind': 'superseded-run', 'run_id': run,
               'journal_sha256': expected['sha256'], 'events_sha256': sources.get(path.with_name('events.jsonl'), {}).get('sha256'),
               'managed_snapshot': state_snapshot, 'superseding_run': provenance.parent.name,
               'superseding_journal_sha256': sources[provenance]['sha256'], 'proofs': proofs,
               'outcome': 'preserved_later_verified_management'}
    target = manager.state_root / 'run-reconciliations' / (run + '.json')
    require(not target.parent.is_symlink(), 'Reconciliation directory cannot be a symlink')
    report = {'operation': 'reconcile-run', 'agent_type': manager.agent, 'ready': True,
              'plan_id': fingerprint(receipt), 'checks': [{'name': 'later_management', 'status': 'passed',
              'reason': 'Later successful deployment and current plugin images match; original failure evidence is retained'}],
              'decisions': [], 'blockers': [], 'reconciliation': receipt, 'reconciled_journal': str(path)}
    return FilePlan([FileChange(target, (json.dumps(receipt, indent=2) + '\n').encode(), 'run-reconciliation', parse_json)],
                    sources=sources, directories=directories, scopes=[str(manager.native_path.resolve())], report=report)
