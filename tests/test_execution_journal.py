import json
import os
from pathlib import Path
import subprocess
import sys
import socket
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from claude_config.core.journal import JournalWriter, read_journal, owner_status
from claude_config.core.recovery import rollback_plan
from claude_config.core.transaction import FileChange, FilePlan, UnfinishedRun, atomic_write


CRASH_WORKER = '''
import os,sys
from pathlib import Path
from claude_config.core.transaction import FileChange,FilePlan,atomic_write
root=Path(sys.argv[1])
target=root/'target'
def crash(path,data,mode):
    atomic_write(path,data,mode)
    os._exit(19)
FilePlan([FileChange(target,b'new','fixture',lambda _:None)],scopes=[str(target)]).apply(root/'state',writer=crash)
'''


class ExecutionJournalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.state = self.root / 'state'
        self.target = self.root / 'target'

    def test_hard_exit_after_write_replays_intent_and_blocks_new_apply(self):
        self.target.write_bytes(b'old')
        process = subprocess.run([sys.executable, '-c', CRASH_WORKER, str(self.root)], timeout=15)
        self.assertEqual(process.returncode, 19)
        path = next((self.state / 'runs').glob('*/journal.json'))
        # Initial checkpoint has no operations; durable intent lives in the log.
        self.assertEqual(json.loads(path.read_bytes())['operations'], [])
        journal = read_journal(path)
        self.assertEqual(journal['operations'][0]['status'], 'started')
        self.assertEqual(owner_status(journal['owner']), 'exited')
        other = FilePlan([FileChange(self.target, b'again', 'fixture', lambda _: None)], scopes=[str(self.target)])
        with self.assertRaises(UnfinishedRun):
            other.apply(self.state)
        with self.assertRaises(UnfinishedRun):
            other.apply(self.root / 'another-state')
        manager = SimpleNamespace(state_root=self.state, sources={}, recovery_scopes=[str(self.target)])
        plan = rollback_plan(manager, path.parent.name)
        self.assertTrue(plan.report['ready'])
        plan.apply(self.state)
        self.assertEqual(self.target.read_bytes(), b'old')
        self.assertEqual(read_journal(path)['status'], 'rolled_back')

    def test_incomplete_tail_is_ignored_but_complete_corruption_is_rejected(self):
        self.state.mkdir()
        path = self.state / 'journal.json'
        value = {'schema_version': 2, 'kind': 'profile-file-run', 'operations': [], 'status': 'running'}
        writer = JournalWriter(path, value, atomic_write)
        writer.operation({'step': 2, 'status': 'started'})
        log = path.with_name('events.jsonl')
        good = log.read_bytes()
        log.write_bytes(good + b'{"sequence":2')
        self.assertEqual(read_journal(path)['operations'], [{'step': 2, 'status': 'started'}])
        log.write_bytes(good.replace(b'started', b'changed'))
        with self.assertRaises(ValueError):
            read_journal(path)

    def test_sigterm_restores_completed_write_before_exit(self):
        self.target.write_bytes(b'old')
        script = CRASH_WORKER.replace('import os,sys', 'import os,sys,signal').replace('os._exit(19)', 'os.kill(os.getpid(), signal.SIGTERM)')
        head, call = script.split('FilePlan([', 1)
        script = head + 'try:\n    FilePlan([' + call.strip() + '\nexcept KeyboardInterrupt:\n    sys.exit(130)\n'
        result = subprocess.run([sys.executable, '-c', script, str(self.root)], capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 130, result.stderr)
        self.assertEqual(self.target.read_bytes(), b'old')
        journal = read_journal(next((self.state / 'runs').glob('*/journal.json')))
        self.assertEqual(journal['status'], 'failed')
        self.assertEqual(journal['operations'][0]['status'], 'rolled_back')

    def test_pid_reuse_does_not_claim_original_owner_is_alive(self):
        owner = {'pid': 123, 'start': 'earlier', 'host': socket.gethostname()}
        with patch('claude_config.core.journal.process_identity', return_value={'pid': 123, 'start': 'later'}):
            self.assertEqual(owner_status(owner), 'exited')

    def test_checkpoint_requires_its_event_chain(self):
        plan = FilePlan([FileChange(self.target, b'new', 'fixture', lambda _: None)])
        report = plan.apply(self.state)
        path = Path(report['recovery'])
        path.with_name('events.jsonl').write_bytes(b'')
        with self.assertRaises(ValueError):
            read_journal(path)

    def test_unchanged_files_have_no_per_file_events(self):
        self.target.write_bytes(b'same')
        report = FilePlan([FileChange(self.target, b'same', 'fixture', lambda _: None)]).apply(self.state)
        path = Path(report['recovery'])
        events = [json.loads(line) for line in path.with_name('events.jsonl').read_text().splitlines()]
        self.assertEqual([row['kind'] for row in events], ['status'])
        self.assertEqual(read_journal(path)['operations'], [])
        self.assertEqual(read_journal(path)['unchanged_count'], 1)

    def test_legacy_v1_remains_readable(self):
        path = self.root / 'old.json'
        value = {'schema_version': 1, 'kind': 'profile-file-run', 'status': 'running', 'operations': []}
        path.write_text(json.dumps(value))
        self.assertEqual(read_journal(path), value)

    def test_checkpoint_bytes_grow_linearly(self):
        sizes = []
        for count in (500, 1000):
            directory = self.root / str(count)
            writes = []
            def measured(path, data, mode=0o600):
                if Path(path).name == 'journal.json':
                    writes.append(len(data))
                atomic_write(path, data, mode)
            plan = FilePlan([FileChange(directory / str(n), b'x', 'fixture', lambda _: None) for n in range(count)])
            with patch('claude_config.core.transaction.atomic_write', side_effect=measured):
                report = plan.apply(directory / 'state')
            sizes.append(sum(writes))
            self.assertLess(len(writes), 8)
            self.assertEqual(len(read_journal(report['recovery'])['operations']), count)
        self.assertLess(sizes[1] / sizes[0], 3)


if __name__ == '__main__':
    unittest.main()
