"""Read-only execution inventory and exact file-state inspection."""
import argparse
from collections import Counter
import json
from pathlib import Path
import re

from .core.journal import read_journal, owner_status, reconciliation
from .core.schema import load_yaml, state_directory, validate_machine
from .core.transaction import file_fingerprint


def main(argv=None):
    parser = argparse.ArgumentParser(prog='claude-config runs', description=__doc__)
    parser.add_argument('--machine', type=Path, default=Path.home() / '.claude-config-tool/machine.yaml')
    parser.add_argument('--run', help='Inspect one exact run ID')
    parser.add_argument('--format', choices=('json', 'text'), default='text')
    args = parser.parse_args(argv)
    if args.run and not re.fullmatch('[0-9a-f]{32}', args.run):
        parser.error('--run requires a 32-character run ID')
    machine = validate_machine(load_yaml(args.machine))
    state = state_directory(machine)
    paths = [state / 'runs' / args.run / 'journal.json'] if args.run else sorted((state / 'runs').glob('*/journal.json'))
    rows = []
    for path in paths:
        journal = read_journal(path)
        row = {'run_id': path.parent.name, 'journal': str(path), 'status': journal['status'],
               'process': owner_status(journal.get('owner')), 'created_at': journal.get('created_at'),
               'updated_at': journal.get('updated_at'), 'scopes': journal['scopes'],
               'operations': dict(Counter(op['status'] for op in journal['operations']))}
        row['interrupted'] = row['status'] == 'running' and row['process'] == 'exited'
        receipt = reconciliation(path)
        if receipt:
            row['reconciliation'] = {'outcome': receipt['outcome'], 'superseding_run': receipt['superseding_run']}
        if journal.get('unchanged_count'):
            row['operations']['unchanged'] = journal['unchanged_count']
        if args.run:
            matches = Counter()
            differences = []
            for op in journal['operations']:
                if op.get('kind') == 'native-plugin':
                    from .core.native_plugins import PluginOperation
                    current = PluginOperation(op['definition']).world()
                    before, after = op['before'], op.get('after')
                elif op.get('action') == 'native_plugin' or op['status'] in ('planned', 'unchanged'):
                    continue
                else:
                    current = file_fingerprint(op['path'])
                    before, after = op['before'], op.get('after', op.get('expected_after'))
                match = 'before' if current == before else 'after' if current == after else 'different'
                matches[match] += 1
                if match == 'different':
                    differences.append({'path': op['path'], 'owner': op.get('owner')})
            row['current_matches'] = dict(matches)
            row['differences'] = differences
        rows.append(row)
    result = {'machine_id': machine['machine_id'], 'state_dir': str(state), 'runs': rows}
    if args.format == 'json':
        print(json.dumps(result, ensure_ascii=False))
    else:
        for row in rows:
            print(f"{row['run_id']}  {row['status']}  process={row['process']}  {row['operations']}")
            if row.get('reconciliation'):
                print('已核对：保留后续已验证配置；依据运行 ' + row['reconciliation']['superseding_run'] + '；上方 failed 为保留的历史记录。')
            if args.run:
                print('当前内容：' + json.dumps(row['current_matches'], ensure_ascii=False))
        if args.run and rows[0].get('reconciliation'):
            print('原失败证据已保留；无需为此历史记录回滚当前配置。')
        else:
            print('查看不会恢复或认领内容；使用 rollback 预览恢复，或用 reconcile-run 核对已被后续部署取代的旧记录。')
    return 0
