import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from claude_config.core.recovery import rollback_plan
from claude_config.core.transaction import PlanStale, atomic_write
from test_profile_resources import ResourceFixture


class RecoveryTests(ResourceFixture):
    def deployed(self):
        self.hook()
        self.save()
        report = self.manager().plan().apply(self.state)
        return Path(report['recovery']).parent.name

    def test_preview_does_not_write_and_rollback_removes_only_run_created_files(self):
        run = self.deployed()
        unrelated = self.native / 'unowned.txt'
        unrelated.write_text('retain')
        plan = rollback_plan(self.manager(), run)
        self.assertTrue(plan.report['ready'], plan.report['blockers'])
        self.assertTrue(self.manager().native_path.exists())
        self.assertTrue(self.manager().state_path.exists())
        plan.apply(self.state)
        self.assertFalse(self.manager().native_path.exists())
        self.assertFalse(self.manager().state_path.exists())
        self.assertEqual(unrelated.read_text(), 'retain')
        journal = json.loads((self.state / 'runs' / run / 'journal.json').read_text())
        self.assertEqual(journal['status'], 'rolled_back')
        repeated = rollback_plan(self.manager(), run).apply(self.state)
        self.assertTrue(all(row['status'] == 'unchanged' for row in repeated['results']))

    def test_existing_native_bytes_comments_and_mode_are_restored(self):
        self.hook()
        self.save()
        self.native.mkdir()
        path = self.manager().native_path
        original = b'{ "model": "before", "unowned": [1, 2] }\n'
        path.write_bytes(original)
        path.chmod(0o640)
        run = Path(self.manager().plan().apply(self.state)['recovery']).parent.name
        plan = rollback_plan(self.manager(), run)
        self.assertTrue(plan.report['ready'], plan.report['blockers'])
        plan.apply(self.state)
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(path.stat().st_mode & 0o777, 0o640)

    def test_later_user_edit_blocks_every_rollback_write(self):
        run = self.deployed()
        path = self.manager().native_path
        path.write_text('{"manual": "preserve"}')
        plan = rollback_plan(self.manager(), run)
        self.assertFalse(plan.report['ready'])
        self.assertTrue(any(row['code'] == 'recovery_conflict' for row in plan.report['blockers']))
        with self.assertRaises(ValueError):
            plan.apply(self.state)
        self.assertTrue(self.manager().state_path.exists())
        self.assertEqual(path.read_text(), '{"manual": "preserve"}')

    def test_backup_tampering_is_rejected_before_any_restore(self):
        self.hook()
        self.save()
        self.native.mkdir()
        self.manager().native_path.write_text('{"model":"before"}')
        run = Path(self.manager().plan().apply(self.state)['recovery']).parent.name
        journal = json.loads((self.state / 'runs' / run / 'journal.json').read_text())
        Path(next(row['backup'] for row in journal['operations'] if 'backup' in row)).write_text('modified')
        with self.assertRaises(ValueError):
            rollback_plan(self.manager(), run)
        self.assertEqual(json.loads(self.manager().native_path.read_text())['model'], 'selected')

    def test_rollback_preview_stale_on_current_file_edit(self):
        run = self.deployed()
        plan = rollback_plan(self.manager(), run)
        self.manager().native_path.write_text('{"manual":true}')
        with self.assertRaises(PlanStale):
            plan.apply(self.state)
        self.assertTrue(self.manager().state_path.exists())

    def test_rollback_itself_is_recoverable_when_a_later_restore_fails(self):
        run = self.deployed()
        plan = rollback_plan(self.manager(), run)
        before = self.manager().native_path.read_bytes()
        def writer(path, content, mode):
            if path == self.state / 'runs' / run / 'journal.json':
                raise OSError('isolated failure')
            atomic_write(path, content, mode)
        with self.assertRaises(OSError):
            plan.apply(self.state, writer=writer)
        self.assertEqual(self.manager().native_path.read_bytes(), before)
        self.assertTrue(self.manager().state_path.exists())
        self.assertEqual(json.loads((self.state / 'runs' / run / 'journal.json').read_text())['status'], 'applied')

    def test_failed_run_already_auto_restored_is_safe_to_mark_recovered(self):
        self.hook()
        self.save()
        plan = self.manager().plan()
        def writer(path, content, mode):
            if path == self.manager().native_path:
                raise OSError('isolated failure')
            atomic_write(path, content, mode)
        with self.assertRaises(OSError):
            plan.apply(self.state, writer=writer)
        run = Path(plan.report['recovery']).parent.name
        recovery = rollback_plan(self.manager(), run)
        self.assertTrue(recovery.report['ready'], recovery.report['blockers'])
        recovery.apply(self.state)
        self.assertFalse(self.manager().native_path.exists())

    def test_other_binding_legacy_journal_and_run_path_escape_are_rejected(self):
        run = self.deployed()
        path = self.state / 'runs' / run / 'journal.json'
        original = json.loads(path.read_text())
        for change in ({'scopes': ['/another/native.json']}, {'kind': 'legacy-run'}):
            path.write_text(json.dumps({**original, **change}))
            with self.assertRaises(ValueError):
                rollback_plan(self.manager(), run)
        for value in ('../elsewhere', 'f' * 31, str(path)):
            with self.assertRaises(ValueError):
                rollback_plan(self.manager(), value)

    def test_cli_rollback_is_preview_only_until_write_is_explicit(self):
        from claude_config import profile as profile_config
        run = self.deployed()
        args = ['rollback', '--machine', str(self.machine_path), '--agent', 'claude-code', '--run', run, '--format', 'json']
        with patch('builtins.print') as output:
            self.assertEqual(profile_config.main(args), 0)
            report = json.loads(output.call_args.args[0])
            self.assertTrue(report['preview_only'])
            self.assertTrue(self.manager().native_path.exists())
            self.assertEqual(profile_config.main([*args, '--write', '--expect-plan-id', report['plan_id']]), 0)
        self.assertFalse(self.manager().native_path.exists())


if __name__ == '__main__':
    unittest.main()
