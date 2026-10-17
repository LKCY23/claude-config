"""Explicit phase checks, target checks, and scoped CLI progress."""
from io import StringIO
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from claude_config.core.execution_context import progress, progress_output
from claude_config.core.transaction import FileChange, FilePlan, PlanStale, atomic_write, file_fingerprint


class ValidationBoundaryTests(unittest.TestCase):
    def test_global_guards_do_not_run_for_each_file(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            calls = []
            changes = [FileChange(root / str(i), b'reviewed', 'test', lambda data: None) for i in range(20)]
            plan = FilePlan(changes, guards=[lambda: calls.append('checked')])
            self.assertEqual(calls, ['checked'])
            plan.apply(root / 'state')
            self.assertEqual(calls, ['checked'] * 3)  # plan, apply start, completion

    def test_source_bytes_are_not_rehashed_for_every_output(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / 'source'
            source.write_bytes(b'fixed input')
            reads = []
            def observe(path):
                if Path(path) == source:
                    reads.append(path)
                return file_fingerprint(path)
            with patch('claude_config.core.transaction.file_fingerprint', side_effect=observe):
                plan = FilePlan([FileChange(root / str(i), b'new', 'test', lambda data: None) for i in range(20)],
                                sources={source: file_fingerprint(source)})
                reads.clear()
                plan.apply(root / 'state')
            self.assertEqual(len(reads), 2)  # apply start and completion, independent of output count

    def test_supplied_target_and_source_pins_are_checked_once_during_planning(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            target = root / 'target'
            target.write_bytes(b'original')
            expected = file_fingerprint(target)
            reads = []
            def observe(path):
                if Path(path) == target:
                    reads.append(path)
                return file_fingerprint(path)
            with patch('claude_config.core.transaction.file_fingerprint', side_effect=observe):
                FilePlan([FileChange(target, b'new', 'test', lambda data: None, expected)],
                         sources={target: expected})
            self.assertEqual(len(reads), 1)

    def test_mutating_supplied_pin_does_not_hide_a_late_source_change(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / 'source'
            source.write_bytes(b'reviewed')
            expected = file_fingerprint(source)
            def validate(data):
                source.write_bytes(b'changed')
                expected.update(file_fingerprint(source))
            with self.assertRaises(PlanStale):
                FilePlan([FileChange(root / 'target', b'new', 'test', validate)], sources={source: expected})

    def test_change_during_content_validation_invalidates_plan(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / 'source'
            source.write_bytes(b'reviewed')
            def validate(data):
                source.write_bytes(b'changed')
            with self.assertRaises(PlanStale):
                FilePlan([FileChange(root / 'target', b'new', 'test', validate)],
                         sources={source: file_fingerprint(source)})
            self.assertFalse((root / 'target').exists())

    def test_late_source_change_is_detected_at_completion_and_restored(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source, target = root / 'source', root / 'target'
            source.write_bytes(b'reviewed')
            target.write_bytes(b'original')
            plan = FilePlan([FileChange(target, b'new', 'test', lambda data: None)],
                            sources={source: file_fingerprint(source)})
            def writer(path, data, mode):
                atomic_write(path, data, mode)
                source.write_bytes(b'later user change')
            with self.assertRaises(PlanStale):
                plan.apply(root / 'state', writer=writer)
            self.assertEqual(target.read_bytes(), b'original')
            self.assertEqual(source.read_bytes(), b'later user change')
            self.assertEqual(plan.report['results'][0]['status'], 'rolled_back')

    def test_next_target_edit_blocks_overwrite_and_preserves_user_change(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            first, second = root / 'first', root / 'second'
            first.write_bytes(b'original first')
            second.write_bytes(b'original second')
            plan = FilePlan([FileChange(first, b'new first', 'test', lambda data: None),
                             FileChange(second, b'new second', 'test', lambda data: None)])
            def writer(path, data, mode):
                atomic_write(path, data, mode)
                if path == first:
                    second.write_bytes(b'user edit')
            with self.assertRaises(PlanStale):
                plan.apply(root / 'state', writer=writer)
            self.assertEqual(first.read_bytes(), b'original first')
            self.assertEqual(second.read_bytes(), b'user edit')

    def test_deferred_child_plan_does_not_validate_or_authorize_execution(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            target = root / 'target'
            checks = []
            plan = FilePlan([FileChange(target, b'new', 'test', lambda data: checks.append('content'),
                                        {'exists': False})], guards=[lambda: checks.append('source')], prepare=False)
            self.assertFalse(checks)
            self.assertFalse(any(row['name'] == 'file_preflight' for row in plan.report['checks']))
            with self.assertRaises(ValueError):
                plan.apply(root / 'state')
            self.assertFalse(target.exists())

    def test_progress_is_opt_in_and_writes_only_to_given_stream(self):
        stream = StringIO()
        progress('hidden')
        with progress_output(stream):
            progress('检查来源；尚未写入')
        progress('hidden again')
        self.assertIn('检查来源；尚未写入', stream.getvalue())
        self.assertNotIn('hidden', stream.getvalue())


if __name__ == '__main__':
    unittest.main()
