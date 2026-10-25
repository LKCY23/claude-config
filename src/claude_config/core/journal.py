"""Durable append events with bounded checkpoints; v1 journals stay readable."""
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()


def process_identity(pid=None):
    pid = os.getpid() if pid is None else pid
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return None
    except PermissionError:
        return {'pid': pid, 'start': None}
    try:
        proc = Path(f'/proc/{pid}/stat')
        if proc.exists():
            start = proc.read_text().rsplit(')', 1)[1].split()[19]
        else:
            result = subprocess.run(['/bin/ps', '-o', 'lstart=', '-p', str(pid)], capture_output=True, text=True, timeout=5)
            start = result.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        start = None
    return {'pid': pid, 'start': start}


def owner_status(owner):
    if not owner or owner.get('host') != socket.gethostname():
        return 'unknown'
    current = process_identity(owner['pid'])
    if current is None:
        return 'exited'
    if owner.get('start') is None or current['start'] is None:
        return 'unknown'
    return 'active' if current['start'] == owner['start'] else 'exited'


def read_journal(path):
    path = Path(path)
    if path.is_symlink():
        raise ValueError('Journal cannot be a symlink')
    value = json.loads(path.read_bytes())
    if value.get('schema_version') == 1:
        return value
    if value.get('schema_version') != 2 or value.get('kind') != 'profile-file-run':
        raise ValueError('Unsupported execution journal version')
    checkpoint = value.get('events', {})
    expected_count = checkpoint.get('count')
    if type(expected_count) is not int or expected_count < 0:
        raise ValueError('Invalid journal checkpoint')
    log = path.with_name('events.jsonl')
    if log.is_symlink() or not log.is_file():
        raise ValueError('Execution event log is missing or unsafe')
    operations = {row['step']: row for row in value['operations']}
    if len(operations) != len(value['operations']):
        raise ValueError('Duplicate journal step')
    previous, count = '0' * 64, 0
    anchored = expected_count == 0 and checkpoint.get('digest') == previous
    with log.open('rb') as stream:
        for line in stream:
            if not line.endswith(b'\n'):
                # An incomplete final append has no committed event. Earlier
                # complete intent remains sufficient to inspect the target.
                break
            event = json.loads(line)
            digest = event.pop('digest')
            count += 1
            if event.get('sequence') != count or event.get('previous') != previous or hashlib.sha256(encoded(event)).hexdigest() != digest:
                raise ValueError('Execution event chain is corrupt')
            previous = digest
            if count == expected_count:
                anchored = digest == checkpoint.get('digest')
            if count > expected_count:
                if event['kind'] == 'operation':
                    row = event['row']
                    if type(row.get('step')) is not int or row['step'] < 0:
                        raise ValueError('Invalid execution step')
                    operations[row['step']] = row
                elif event['kind'] == 'status':
                    value.update(event['fields'])
                else:
                    raise ValueError('Unknown execution event')
                value['updated_at'] = event['at']
    if count < expected_count or not anchored:
        raise ValueError('Execution checkpoint does not match its log')
    value['operations'] = [operations[key] for key in sorted(operations)]
    value['events'] = {'count': count, 'digest': previous}
    return value


class JournalWriter:
    def __init__(self, path, value, atomic_writer):
        self.path, self.value, self.atomic_writer = Path(path), value, atomic_writer
        self.sequence, self.digest, self.next_checkpoint = 0, '0' * 64, 256
        self.value.update(schema_version=2, events={'count': 0, 'digest': self.digest},
                          owner={**process_identity(), 'host': socket.gethostname()})
        self.atomic_writer(self.path.with_name('events.jsonl'), b'')
        self.checkpoint()

    def checkpoint(self):
        self.value['events'] = {'count': self.sequence, 'digest': self.digest}
        snapshot = dict(self.value)
        snapshot['operations'] = [row for row in self.value['operations'] if row['status'] != 'unchanged']
        snapshot['unchanged_count'] = len(self.value['operations']) - len(snapshot['operations'])
        self.atomic_writer(self.path, encoded(snapshot) + b'\n')

    def append(self, kind, **fields):
        event = {'sequence': self.sequence + 1, 'previous': self.digest, 'kind': kind,
                 'at': datetime.now(timezone.utc).isoformat(), **fields}
        digest = hashlib.sha256(encoded(event)).hexdigest()
        data = encoded({**event, 'digest': digest}) + b'\n'
        fd = os.open(self.path.with_name('events.jsonl'), os.O_WRONLY | os.O_APPEND | getattr(os, 'O_NOFOLLOW', 0))
        try:
            view = memoryview(data)
            while view:
                written = os.write(fd, view)
                if written <= 0:
                    raise OSError('Execution event append did not progress')
                view = view[written:]
            os.fsync(fd)
        finally:
            os.close(fd)
        self.sequence, self.digest = event['sequence'], digest
        self.value['updated_at'] = event['at']
        # Doubling checkpoints keep total serialization linear, rather than
        # rewriting a growing snapshot every fixed number of operations.
        if self.sequence >= self.next_checkpoint:
            self.checkpoint()
            self.next_checkpoint *= 2

    def operation(self, row):
        self.append('operation', row=deepcopy(row))

    def status(self, status, **fields):
        self.append('status', fields={'status': status, **fields})
        self.value.update(status=status, **fields)


def unfinished(state_root, scopes=None):
    result = []
    for path in sorted((Path(state_root) / 'runs').glob('*/journal.json')):
        journal = read_journal(path)
        if scopes is not None and not set(journal.get('scopes', ())).intersection(scopes):
            continue
        if journal.get('status') not in ('running', 'interrupted', 'failed'):
            continue
        remaining = [row for row in journal.get('operations', []) if row.get('status') in ('started', 'applied', 'recovery_conflict')]
        if journal.get('status') == 'failed' and not remaining:
            continue
        state = owner_status(journal.get('owner'))
        result.append({'run_id': path.parent.name, 'journal': str(path), 'status': journal['status'],
                       'process': state, 'interrupted': journal['status'] == 'running' and state == 'exited',
                       'remaining': len(remaining), 'scopes': journal.get('scopes', []),
                       'updated_at': journal.get('updated_at', journal.get('created_at'))})
    return result


def registered_pending(directory, scopes):
    path = Path(directory) / 'pending.json'
    if path.is_symlink():
        raise ValueError('Execution registry cannot be a symlink')
    registry = json.loads(path.read_bytes()) if path.exists() else {}
    rows = []
    for location in sorted({registry[scope] for scope in scopes if scope in registry}):
        journal_path = Path(location)
        try:
            value = read_journal(journal_path)
            pending = value['status'] in ('running', 'interrupted') or value['status'] == 'failed' and any(
                row['status'] in ('started', 'applied', 'recovery_conflict') for row in value['operations'])
            if not pending:
                continue
            status = value['status']
        except (ValueError, OSError, KeyError):
            status = 'unreadable'
        rows.append({'run_id': journal_path.parent.name, 'journal': location, 'status': status})
    return rows


def register_run(directory, scopes, journal_path, writer, *, remove=False):
    if not scopes:
        return
    path = Path(directory) / 'pending.json'
    if path.is_symlink():
        raise ValueError('Execution registry cannot be a symlink')
    registry = json.loads(path.read_bytes()) if path.exists() else {}
    for scope in scopes:
        if remove:
            if registry.get(scope) == str(journal_path):
                del registry[scope]
        else:
            registry[scope] = str(journal_path)
    writer(path, encoded(registry) + b'\n')
