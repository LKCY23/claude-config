"""Tests for `--local-only`, `--promote`, `--also-profiles`, `plugin unlock`, and dirty repo hints."""
import contextlib
import io
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from claude_config.core.native_plugins import PluginOperation
from claude_config.core.plugin_manager import PluginManager
from claude_config.plugin import main as plugin_main
from test_profile_plugins import FakeInstaller
from test_profile_resources import ResourceFixture, run_git


class PluginLockWorkflowTests(ResourceFixture):
    def setUp(self):
        super().setUp()
        self.installer = FakeInstaller()
        installer = self.installer
        self.mock_native = patch.object(PluginOperation, '_call', lambda op, args, work: installer(op, args, work))
        self.mock_native.start()
        self.addCleanup(self.mock_native.stop)

        self.plugin_root = self.add('tiny', 'plugin', {
            '.claude-plugin/plugin.json': json.dumps({'name': 'tiny', 'version': '1.0.0'}), 'body.txt': 'one',
        }, '.claude-plugin/plugin.json')
        self.commit = self.save()
        self.profile_path = self.data / self.machine['agents']['claude-code']['profile']
        self.lock = self.profile_path.with_suffix('.plugins.lock.yaml')

        # Second independent claude-code Profile
        self.other_profile = self.profile_path.parent / 'official.yaml'
        other_data = {
            'schema_version': 1,
            'agent_type': 'claude-code',
            'settings': {'model': 'official-selected'},
            'resources': {'plugins': {'tiny@test': {'options': {'enabled': True}}}},
        }
        self.other_profile.write_text(yaml.safe_dump(other_data, sort_keys=False))
        self.other_lock = self.other_profile.with_suffix('.plugins.lock.yaml')

        (self.data / '.gitignore').write_text('/.state/\n', encoding='utf-8')
        run_git(self.data, 'init', '-q', '-b', 'master')
        run_git(self.data, 'config', 'user.name', 'Test User')
        run_git(self.data, 'config', 'user.email', 'test@example.com')
        run_git(self.data, 'add', '.')
        run_git(self.data, 'commit', '-qm', 'initial')

    def fetch(self, root, repo, commit):
        if root.exists():
            return False
        root.parent.mkdir(parents=True, exist_ok=True)
        run_git(self.root, 'clone', '--no-hardlinks', str(self.repo), str(root))
        run_git(root, 'remote', 'set-url', 'origin', 'https://github.com/' + repo + '.git')
        run_git(root, 'checkout', '--detach', commit)
        return True

    def make_side_commit(self, version='2.0.0'):
        """Create a commit on a side branch in self.repo while keeping catalog HEAD pinned cleanly at self.commit."""
        base_branch = run_git(self.repo, 'rev-parse', '--abbrev-ref', 'HEAD')
        branch = f'dev-{version}'
        run_git(self.repo, 'checkout', '-qb', branch)
        (self.plugin_root / '.claude-plugin/plugin.json').write_text(json.dumps({'name': 'tiny', 'version': version}))
        (self.plugin_root / 'body.txt').write_text(f'body-{version}')
        run_git(self.repo, 'add', '.')
        run_git(self.repo, 'commit', '-qm', f'tiny {version}')
        new_commit = run_git(self.repo, 'rev-parse', 'HEAD')
        run_git(self.repo, 'checkout', '-q', base_branch)
        return new_commit

    def receipt(self):
        return json.loads(self.manager().state_path.read_text())['resources']['tiny@test']['native']['plugin']

    def cli(self, *args):
        out = io.StringIO()
        with patch('claude_config.plugin.ProfileManager', side_effect=lambda *a: self.manager()), \
                patch('claude_config.fetch_baseline.prepare_git', side_effect=self.fetch), \
                contextlib.redirect_stdout(out):
            code = plugin_main([*args, '--agent', 'claude-code', '--machine', str(self.machine_path), '--format', 'json'])
        return code, json.loads(out.getvalue())

    def _git_clean(self):
        return run_git(self.data, 'status', '--porcelain').strip() == ''

    def test_local_only_upgrade_leaves_git_clean_then_promote_updates_lock(self):
        code_in, rep_in = self.cli('install', 'tiny', '--write')
        self.assertEqual(code_in, 0, rep_in)
        run_git(self.data, 'add', '.')
        run_git(self.data, 'commit', '-qm', 'lock tiny 1.0.0')
        self.assertTrue(self._git_clean())

        commit_v2 = self.make_side_commit('2.0.0')
        lock_before = self.lock.read_text()

        # Upgrade with --local-only -> native updated, lock & Git tree untouched
        code, rep = self.cli('upgrade', 'tiny', '--commit', commit_v2, '--local-only', '--write')
        self.assertEqual(code, 0, rep)
        self.assertEqual(rep['plugin_management']['pin_scope'], 'local')
        self.assertEqual(self.receipt()['version'], '2.0.0')
        self.assertEqual(self.lock.read_text(), lock_before)
        self.assertTrue(self._git_clean(), f'local-only dirtied git: {run_git(self.data, "status", "--porcelain")}')
        local_pins_path = self.state / 'local-pins.yaml'
        self.assertTrue(local_pins_path.is_file())

        # Ordinary manager.plan() honors local pin and keeps native at 2.0.0
        ordinary = self.manager().plan()
        self.assertTrue(ordinary.report['ready'], ordinary.report['blockers'])
        self.assertFalse(ordinary.native_operations)

        # plugin status reports pin_source == 'local'
        code_st, st_rep = self.cli('status', 'tiny')
        self.assertEqual(code_st, 0, st_rep)
        row = st_rep['plugins'][0]
        self.assertEqual(row['pin_source'], 'local')
        self.assertEqual(row['local_pin_version'], '2.0.0')
        self.assertEqual(row['profile_locked_version'], '1.0.0')
        self.assertEqual(row['selected_version'], '2.0.0')

        # Promote without --commit -> writes Profile lock and cleans local-pins.yaml
        code_pr, pr_rep = self.cli('upgrade', 'tiny', '--promote', '--write')
        self.assertEqual(code_pr, 0, pr_rep)
        self.assertEqual(pr_rep['plugin_management']['pin_scope'], 'lock')
        self.assertEqual(yaml.safe_load(self.lock.read_text())['plugins']['tiny@test']['version'], '2.0.0')
        self.assertFalse(local_pins_path.exists())
        self.assertFalse(self._git_clean())
        code_st2, st_rep2 = self.cli('status', 'tiny')
        self.assertEqual(st_rep2['plugins'][0]['pin_source'], 'lock')

    def test_also_profiles_updates_multiple_locks_and_validates_target(self):
        self.cli('install', 'tiny', '--write')
        commit_v2 = self.make_side_commit('2.0.0')

        code, rep = self.cli(
            'upgrade', 'tiny', '--commit', commit_v2, '--also-profiles', 'official', '--write',
        )
        self.assertEqual(code, 0, rep)
        self.assertEqual(yaml.safe_load(self.lock.read_text())['plugins']['tiny@test']['version'], '2.0.0')
        self.assertEqual(yaml.safe_load(self.other_lock.read_text())['plugins']['tiny@test']['version'], '2.0.0')

        # Reject --also-profiles if plugin not selected in target Profile
        unselected = self.profile_path.parent / 'minimal.yaml'
        unselected.write_text(yaml.safe_dump({
            'schema_version': 1, 'agent_type': 'claude-code', 'settings': {'model': 'minimal'},
        }, sort_keys=False))
        commit_v3 = self.make_side_commit('3.0.0')
        code_bad, rep_bad = self.cli(
            'upgrade', 'tiny', '--commit', commit_v3, '--also-profiles', 'minimal', '--write',
        )
        self.assertNotEqual(code_bad, 0)
        self.assertIn('not selected', rep_bad['errors'][0]['reason'])
        self.assertEqual(yaml.safe_load(self.lock.read_text())['plugins']['tiny@test']['version'], '2.0.0')

        # Reject combining --local-only and --also-profiles
        code_mix, _ = self.cli(
            'upgrade', 'tiny', '--commit', commit_v3, '--local-only', '--also-profiles', 'official', '--write',
        )
        self.assertNotEqual(code_mix, 0)

    def test_unlock_removes_pin_and_returns_plugin_to_catalog_version(self):
        self.cli('install', 'tiny', '--write')
        v2_commit = self.make_side_commit('2.0.0')

        code_up, rep_up = self.cli('upgrade', 'tiny', '--commit', v2_commit, '--write')
        self.assertEqual(code_up, 0, rep_up)
        self.assertEqual(self.receipt()['version'], '2.0.0')

        # Unlock returns tiny back to catalog commit (1.0.0) and unpins lock
        code_un, rep_un = self.cli('unlock', 'tiny', '--write')
        self.assertEqual(code_un, 0, rep_un)
        self.assertEqual(rep_un['plugin_management']['pin_scope'], 'unlocked')
        self.assertEqual(self.receipt()['version'], '1.0.0')
        self.assertNotIn('tiny@test', yaml.safe_load(self.lock.read_text())['plugins'])
        code_st, st_rep = self.cli('status', 'tiny')
        self.assertEqual(st_rep['plugins'][0]['pin_source'], 'catalog')
        self.assertEqual(st_rep['plugins'][0]['selected_version'], '1.0.0')

    def test_text_mode_emits_dirty_hint_while_json_mode_stays_pure(self):
        out_text = io.StringIO()
        with patch('claude_config.plugin.ProfileManager', side_effect=lambda *a: self.manager()), \
                patch('claude_config.fetch_baseline.prepare_git', side_effect=self.fetch), \
                contextlib.redirect_stdout(out_text):
            code = plugin_main([
                'install', 'tiny', '--agent', 'claude-code',
                '--machine', str(self.machine_path), '--write', '--format', 'text',
            ])
        self.assertEqual(code, 0, out_text.getvalue())
        self.assertIn('claude-config data publish', out_text.getvalue())


if __name__ == '__main__':
    unittest.main()
