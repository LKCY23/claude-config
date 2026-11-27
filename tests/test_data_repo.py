"""Tests for config_dir Git sync loop (`data status/pull/publish`, `status`, `sync --pull`)."""
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from claude_config import cli as cli_entry
from claude_config.core.data_repo import format_dirty_hint, inspect_repo, publish_repo, pull_repo, validate_data_repo
from claude_config.core.native import parse_native
import test_machine_profiles as machine_profile_fixtures
import test_profile_manager as manager_fixtures
from test_profile_resources import run_git


class DataRepoTests(unittest.TestCase):
    def _setup_git_data(self):
        fixture = manager_fixtures.ManagerTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.addCleanup(fixture.tearDown)

        fixture.profile('codex', settings={'model': 'initial'})
        machine_profile_fixtures.externalize(fixture)

        (fixture.data / '.gitignore').write_text('/.state/\n', encoding='utf-8')
        run_git(fixture.data, 'init', '-q', '-b', 'master')
        run_git(fixture.data, 'config', 'user.name', 'Test User')
        run_git(fixture.data, 'config', 'user.email', 'test@example.com')
        run_git(fixture.data, 'add', '.')
        run_git(fixture.data, 'commit', '-qm', 'initial config data')

        bare = fixture.root / 'remote-data.git'
        subprocess.run(['git', 'init', '--bare', '-q', '-b', 'master', str(bare)], check=True)
        run_git(fixture.data, 'remote', 'add', 'origin', str(bare))
        run_git(fixture.data, 'push', '-u', 'origin', 'master', '-q')

        peer = fixture.root / 'peer-clone'
        subprocess.run(['git', 'clone', '-q', str(bare), str(peer)], check=True)
        run_git(peer, 'config', 'user.name', 'Peer User')
        run_git(peer, 'config', 'user.email', 'peer@example.com')

        return fixture, bare, peer

    def _run_cli(self, argv):
        out = io.StringIO()
        with redirect_stdout(out):
            code = cli_entry.main(argv)
        return code, out.getvalue()

    def test_inspect_repo_states_and_state_dir_exclusion(self):
        fixture, _, peer = self._setup_git_data()

        non_git = fixture.root / 'not-a-repo'
        non_git.mkdir()
        self.assertEqual(inspect_repo(non_git)['state'], 'not_git')
        self.assertIsNone(format_dirty_hint(non_git))

        state_file = fixture.data / '.state' / 'tmp-report.json'
        state_file.parent.mkdir(parents=True, exist_ok=True)
        state_file.write_text('{}', encoding='utf-8')
        clean = inspect_repo(fixture.data, fetch=False)
        self.assertEqual(clean['state'], 'clean')
        self.assertFalse(clean['dirty'])
        self.assertEqual(clean['branch'], 'master')

        codex_prof = fixture.data / 'profiles/agents/codex/work.yaml'
        orig_prof = codex_prof.read_text()
        codex_prof.write_text(orig_prof + '\n# edit\n')
        extra = fixture.data / 'profiles/agents/codex/extra.yaml'
        extra.write_text("schema_version: 1\nagent_type: codex\nsettings:\n  model: extra\n")
        dirty = inspect_repo(fixture.data, fetch=False)
        self.assertEqual(dirty['state'], 'dirty')
        self.assertIn('profiles/agents/codex/work.yaml', dirty['dirty_files'])
        self.assertIn('profiles/agents/codex/extra.yaml', dirty['untracked_files'])
        self.assertIn('claude-config data publish', format_dirty_hint(fixture.data))

        codex_prof.write_text(orig_prof)
        extra.unlink()
        peer_prof = peer / 'profiles/agents/codex/work.yaml'
        p_data = yaml.safe_load(peer_prof.read_text())
        p_data['settings']['model'] = 'from-peer'
        peer_prof.write_text(yaml.safe_dump(p_data, sort_keys=False))
        run_git(peer, 'add', '.')
        run_git(peer, 'commit', '-qm', 'peer bump')
        run_git(peer, 'push', '-q')

        behind = inspect_repo(fixture.data, fetch=True)
        self.assertEqual(behind['state'], 'behind')
        self.assertEqual(behind['behind'], 1)
        self.assertEqual(behind['ahead'], 0)

        c_data = yaml.safe_load(codex_prof.read_text())
        c_data['settings']['model'] = 'local-commit'
        codex_prof.write_text(yaml.safe_dump(c_data, sort_keys=False))
        run_git(fixture.data, 'add', '.')
        run_git(fixture.data, 'commit', '-qm', 'local diverge')
        diverged = inspect_repo(fixture.data, fetch=False)
        self.assertEqual(diverged['state'], 'diverged')
        self.assertTrue(diverged['diverged'])

    def test_pull_repo_preview_ff_only_and_refusals(self):
        fixture, _, peer = self._setup_git_data()

        up = pull_repo(fixture.data, dry_run=False, fetch=True)
        self.assertTrue(up['ready'])
        self.assertEqual(up['status'], 'up_to_date')

        peer_prof = peer / 'profiles/agents/codex/work.yaml'
        p_data = yaml.safe_load(peer_prof.read_text())
        p_data['settings']['model'] = 'remote-v2'
        peer_prof.write_text(yaml.safe_dump(p_data, sort_keys=False))
        run_git(peer, 'add', '.')
        run_git(peer, 'commit', '-qm', 'remote v2')
        run_git(peer, 'push', '-q')

        prev = pull_repo(fixture.data, dry_run=True, fetch=True)
        self.assertTrue(prev['ready'])
        self.assertEqual(prev['status'], 'pending')
        self.assertIn('profiles/agents/codex/work.yaml', prev['updated_files'])
        self.assertEqual(yaml.safe_load((fixture.data / 'profiles/agents/codex/work.yaml').read_text())['settings']['model'], 'initial')

        local_prof = fixture.data / 'profiles/agents/codex/work.yaml'
        saved = local_prof.read_text()
        local_prof.write_text(saved + '\n# dirty\n')
        blocked_dirty = pull_repo(fixture.data, dry_run=False, fetch=False)
        self.assertFalse(blocked_dirty['ready'])
        self.assertEqual(blocked_dirty['status'], 'dirty')
        local_prof.write_text(saved)

        pulled = pull_repo(fixture.data, dry_run=False, fetch=True)
        self.assertTrue(pulled['ready'])
        self.assertTrue(pulled['pulled'])
        self.assertEqual(pulled['status'], 'pulled')
        self.assertEqual(yaml.safe_load(local_prof.read_text())['settings']['model'], 'remote-v2')

    def test_validate_and_publish_repo_end_to_end(self):
        fixture, _, peer = self._setup_git_data()

        noop = publish_repo(fixture.data, dry_run=True)
        self.assertTrue(noop['ready'])
        self.assertEqual(noop['status'], 'nothing_to_publish')

        local_prof = fixture.data / 'profiles/agents/codex/work.yaml'
        bad = yaml.safe_load(local_prof.read_text())
        bad['settings']['unknown_forbidden_key'] = 123
        local_prof.write_text(yaml.safe_dump(bad, sort_keys=False))
        val_fail = validate_data_repo(fixture.data)
        self.assertFalse(val_fail['ok'])
        pub_fail = publish_repo(fixture.data, dry_run=False)
        self.assertFalse(pub_fail['ready'])
        self.assertEqual(pub_fail['status'], 'validation_failed')

        good = yaml.safe_load(local_prof.read_text())
        good['settings'].pop('unknown_forbidden_key')
        good['settings']['model'] = 'published-model'
        local_prof.write_text(yaml.safe_dump(good, sort_keys=False))

        code_prev, out_prev = self._run_cli([
            'data', 'publish', '--machine', str(fixture.machine_path), '--dry-run', '--format', 'json',
        ])
        self.assertEqual(code_prev, 0, out_prev)
        prev_rep = json.loads(out_prev)
        self.assertTrue(prev_rep['preview_only'])
        self.assertEqual(prev_rep['status'], 'pending')

        code_pub, out_pub = self._run_cli([
            'data', 'publish', '--machine', str(fixture.machine_path), '--yes',
            '-m', 'update codex model', '--format', 'json',
        ])
        self.assertEqual(code_pub, 0, out_pub)
        pub_rep = json.loads(out_pub)
        self.assertEqual(pub_rep['status'], 'published')
        self.assertTrue(pub_rep['pushed'])

        run_git(peer, 'pull', '--ff-only', '-q')
        peer_data = yaml.safe_load((peer / 'profiles/agents/codex/work.yaml').read_text())
        self.assertEqual(peer_data['settings']['model'], 'published-model')

    def test_status_data_repo_and_sync_pull_end_to_end(self):
        fixture, _, peer = self._setup_git_data()
        fixture.native('claude-code', value={'model': 'selected'})
        fixture.native('codex', value={'model': 'initial'})
        with patch('claude_config.core.manager.NativeProbe', return_value=fixture.probe):
            fixture.manager('claude-code').plan().apply(fixture.state)
            fixture.manager('codex').plan().apply(fixture.state)

        peer_prof = peer / 'profiles/agents/codex/work.yaml'
        p_data = yaml.safe_load(peer_prof.read_text())
        p_data['settings']['model'] = 'peer-updated'
        peer_prof.write_text(yaml.safe_dump(p_data, sort_keys=False))
        run_git(peer, 'add', '.')
        run_git(peer, 'commit', '-qm', 'peer updates model')
        run_git(peer, 'push', '-q')

        code_st, out_st = self._run_cli([
            'status', '--machine', str(fixture.machine_path), '--fetch', '--format', 'text',
        ])
        self.assertEqual(code_st, 0, out_st)
        self.assertIn('配置仓库状态', out_st)
        self.assertIn('落后 1 个提交', out_st)
        self.assertIn('claude-config sync --pull', out_st)

        with patch('claude_config.core.manager.NativeProbe', return_value=fixture.probe):
            code_dry, out_dry = self._run_cli([
                'sync', '--machine', str(fixture.machine_path), '--agent', 'codex',
                '--pull', '--dry-run', '--format', 'json',
            ])
        self.assertEqual(code_dry, 0, out_dry)
        dry_rep = json.loads(out_dry)
        self.assertTrue(dry_rep['preview_only'])
        self.assertEqual(dry_rep['data_pull']['status'], 'pending')
        self.assertEqual(parse_native('codex', fixture.native('codex').read_bytes())['model'], 'initial')

        with patch('claude_config.core.manager.NativeProbe', return_value=fixture.probe):
            code_sync, out_sync = self._run_cli([
                'sync', '--machine', str(fixture.machine_path), '--agent', 'codex',
                '--pull', '--yes', '--format', 'json',
            ])
        self.assertEqual(code_sync, 0, out_sync)
        sync_rep = json.loads(out_sync)
        self.assertEqual(sync_rep['data_pull']['status'], 'pulled')
        self.assertEqual(sync_rep['targets'][0]['outcome'], 'applied')
        self.assertEqual(parse_native('codex', fixture.native('codex').read_bytes())['model'], 'peer-updated')


if __name__ == '__main__':
    unittest.main()
