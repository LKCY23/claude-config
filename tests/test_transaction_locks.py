"""Bounded file descriptors and one shared execution lock across real processes."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

SCRIPTS = Path(__file__).resolve().parents[1] / 'src'

BULK_WORKER = r'''
import json
from pathlib import Path
import resource
import sys
from types import SimpleNamespace
sys.path.insert(0, sys.argv[1])
from claude_config.core.recovery import rollback_plan
from claude_config.core.transaction import FileChange, FilePlan
root = Path(sys.argv[2])
_, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
resource.setrlimit(resource.RLIMIT_NOFILE, (256, hard))
state = root / 'state'
outputs = root / 'outputs'
outputs.mkdir()
changes = []
for index in range(600):
    target = outputs / str(index)
    target.write_bytes(('original ' + str(index)).encode())
    target.chmod(0o640)
    changes.append(FileChange(target, ('new ' + str(index)).encode(), 'test', lambda data: None))
plan = FilePlan(changes, scopes=['isolated-bulk'])
result = plan.apply(state)
assert all(row['status'] == 'applied' for row in result['results'])
run = Path(result['recovery']).parent.name
manager = SimpleNamespace(state_root=state, sources={}, recovery_scopes=['isolated-bulk'])
restoration = rollback_plan(manager, run)
assert restoration.report['ready']
restoration.apply(state)
assert all((outputs / str(index)).read_bytes() == ('original ' + str(index)).encode() for index in range(600))
assert all((outputs / str(index)).stat().st_mode & 0o777 == 0o640 for index in range(600))
print(json.dumps({'limit': resource.getrlimit(resource.RLIMIT_NOFILE)[0],
                  'outputs': len(list(outputs.iterdir())),
                  'locks': sorted(path.name for path in (state / 'locks').iterdir()),
                  'runs': len(list((state / 'runs').iterdir())),
                  'original_run_status': json.loads(Path(result['recovery']).read_text())['status']}))
'''

CONTENDING_WORKER = r'''
import json
from pathlib import Path
import sys
sys.path.insert(0, sys.argv[1])
from claude_config.core.execution_context import progress_output
from claude_config.core.transaction import FileChange, FilePlan, PlanStale, atomic_write
root, state, role = Path(sys.argv[2]), Path(sys.argv[3]), sys.argv[4]
target = root / (role + '-target')
plan = FilePlan([FileChange(target, b'new', 'test', lambda data: None)], scopes=[role])
(root / (role + '-planned')).touch()
class ProgressMarker:
    def write(self, message):
        if '等待配置写入锁' in message:
            (root / (role + '-waiting')).touch()
    def flush(self):
        pass
def writer(path, data, mode):
    atomic_write(path, data, mode)
    if role == 'first':
        (root / 'first-holding').touch()
        if sys.stdin.readline().strip() != 'release':
            raise RuntimeError('Test lock holder did not receive release')
try:
    with progress_output(ProgressMarker()):
        plan.apply(state, writer=writer)
    status = 'applied'
except PlanStale:
    status = 'stale'
print(json.dumps({'status': status}))
'''


@unittest.skipUnless(os.name == 'posix', 'POSIX file locks and resource limits')
class TransactionLockTests(unittest.TestCase):
    def start_worker(self, root, state, role):
        process = subprocess.Popen([sys.executable, '-c', CONTENDING_WORKER, str(SCRIPTS), str(root), str(state), role],
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        def cleanup():
            if process.poll() is None:
                process.kill()
            process.communicate()
        self.addCleanup(cleanup)
        return process

    def wait_for_marker(self, process, marker):
        deadline = time.monotonic() + 10
        while not marker.exists():
            if process.poll() is not None:
                stdout, stderr = process.communicate()
                self.fail('Worker exited before marker: ' + stdout + stderr)
            if time.monotonic() > deadline:
                self.fail('Worker did not reach ' + marker.name)
            time.sleep(0.01)

    def finish(self, process, input=None):
        stdout, stderr = process.communicate(input=input, timeout=20)
        self.assertEqual(process.returncode, 0, stderr)
        return json.loads(stdout)

    def test_600_existing_outputs_and_whole_run_rollback_under_256_descriptor_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run([sys.executable, '-c', BULK_WORKER, str(SCRIPTS), directory],
                                    capture_output=True, text=True, timeout=45)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout), {'limit': 256, 'outputs': 600, 'locks': ['execution.lock'],
                                                        'runs': 2, 'original_run_status': 'rolled_back'})

    def test_disjoint_scopes_and_targets_wait_for_same_canonical_state_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'real').mkdir()
            (root / 'alias').symlink_to(root / 'real', target_is_directory=True)
            first = self.start_worker(root, root / 'real/state', 'first')
            self.wait_for_marker(first, root / 'first-holding')
            second = self.start_worker(root, root / 'alias/state', 'second')
            self.wait_for_marker(second, root / 'second-waiting')
            time.sleep(0.25)
            self.assertIsNone(second.poll())
            self.assertFalse((root / 'second-target').exists())
            self.assertEqual(self.finish(first, 'release\n'), {'status': 'applied'})
            self.assertEqual(self.finish(second), {'status': 'applied'})
            self.assertEqual((root / 'second-target').read_bytes(), b'new')
            self.assertEqual([path.name for path in (root / 'real/state/locks').iterdir()], ['execution.lock'])

    def test_target_is_rechecked_after_waiting_and_new_user_edit_is_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / 'state'
            target = root / 'second-target'
            target.write_bytes(b'original')
            first = self.start_worker(root, state, 'first')
            self.wait_for_marker(first, root / 'first-holding')
            second = self.start_worker(root, state, 'second')
            self.wait_for_marker(second, root / 'second-waiting')
            time.sleep(0.25)
            self.assertIsNone(second.poll())
            target.write_bytes(b'user edit while waiting')
            self.assertEqual(self.finish(first, 'release\n'), {'status': 'applied'})
            self.assertEqual(self.finish(second), {'status': 'stale'})
            self.assertEqual(target.read_bytes(), b'user edit while waiting')
            self.assertEqual(len(list((state / 'runs').iterdir())), 1)


if __name__ == '__main__':
    unittest.main()
