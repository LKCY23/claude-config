"""A fresh data target must ignore local state without losing existing rules."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from claude_config.core.recovery import rollback_plan
from test_framework_cutover import CutoverFixture
from test_profile_resources import run_git


class StateIgnoreTests(CutoverFixture):
    def test_cutover_keeps_existing_rules_and_ignores_default_state(self):
        self.machine['runtime'].pop('state_dir')
        self.save_profile()
        self.target_data.mkdir()
        before = b'.worktrees/\n# preserve comment\n'
        path = self.target_data / '.gitignore'
        path.write_bytes(before)
        manager = self.coordinator()
        plan = manager.plan()
        self.assertTrue(plan.report['ready'], plan.report['blockers'])
        self.assertEqual(path.read_bytes(), before)
        plan.apply(manager.state_root)
        self.assertEqual(path.read_bytes(), before + b'/.state/\n')
        run_git(self.target_data, 'init', '-q')
        ignored = run_git(self.target_data, 'check-ignore', '.state/reports/sample.json', '.state/runs/backup')
        self.assertIn('.state/reports/sample.json', ignored)
        self.assertIn('.state/runs/backup', ignored)
        restoration = rollback_plan(self.coordinator(), Path(plan.report['recovery']).parent.name)
        restoration.apply(manager.state_root)
        self.assertEqual(path.read_bytes(), before)
        # Backups remain local even though the original root rules are restored.
        retained = Path(plan.report['recovery']).relative_to(self.target_data.resolve()).as_posix()
        self.assertIn(retained, run_git(self.target_data, 'check-ignore', retained))
        self.assertEqual((manager.state_root / '.gitignore').read_bytes(), b'*\n')

    def test_new_data_target_gets_ignore_rule(self):
        manager = self.coordinator()
        plan = manager.plan()
        self.assertFalse(self.target_data.exists())
        plan.apply(manager.state_root)
        self.assertEqual((self.target_data / '.gitignore').read_bytes(), b'/.state/\n')


if __name__ == '__main__':
    unittest.main()
