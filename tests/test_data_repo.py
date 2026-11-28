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

    def test_publish_blocks_staged_state_files_and_unmerged_conflicts(self):
        fixture, _, peer = self._setup_git_data()

        # 1) Force-staged .state/ file must block publish even with another valid config change.
        secret_state = fixture.data / '.state' / 'private-journal.json'
        secret_state.parent.mkdir(parents=True, exist_ok=True)
        secret_state.write_text('{"secret":"local"}', encoding='utf-8')
        run_git(fixture.data, 'add', '-f', '.state/private-journal.json')
        local_prof = fixture.data / 'profiles/agents/codex/work.yaml'
        good = yaml.safe_load(local_prof.read_text())
        good['settings']['model'] = 'with-staged-state'
        local_prof.write_text(yaml.safe_dump(good, sort_keys=False))

        blocked_state = publish_repo(fixture.data, dry_run=False)
        self.assertFalse(blocked_state['ready'])
        self.assertEqual(blocked_state['status'], 'staged_state_files')
        self.assertIn('.state/private-journal.json', blocked_state['staged_state_files'])

        # Clean index & reset working tree for conflict test.
        run_git(fixture.data, 'reset', '-q', 'HEAD', '--', '.state')
        good['settings']['model'] = 'initial'
        local_prof.write_text(yaml.safe_dump(good, sort_keys=False))

        # 2) Unmerged conflict path (e.g., README.md not schema-validated) must block publish.
        (peer / 'README.md').write_text('peer line\n', encoding='utf-8')
        run_git(peer, 'add', 'README.md')
        run_git(peer, 'commit', '-qm', 'peer readme')
        run_git(peer, 'push', '-q')

        (fixture.data / 'README.md').write_text('local line\n', encoding='utf-8')
        run_git(fixture.data, 'add', 'README.md')
        run_git(fixture.data, 'commit', '-qm', 'local readme')
        run_git(fixture.data, 'fetch', '-q')
        merge_proc = subprocess.run(['git', '-C', str(fixture.data), 'merge', '--no-edit', 'origin/master'], capture_output=True, text=True)
        self.assertNotEqual(merge_proc.returncode, 0)

        blocked_conflict = publish_repo(fixture.data, dry_run=False)
        self.assertFalse(blocked_conflict['ready'])
        self.assertEqual(blocked_conflict['status'], 'unmerged_conflicts')
        self.assertIn('README.md', blocked_conflict['unmerged_files'])

    def test_publish_dirty_and_behind_identical_reconciles_without_deadlock(self):
        fixture, _, peer = self._setup_git_data()

        # Peer advances codex/work.yaml to 'shared-model' and pushes.
        peer_prof = peer / 'profiles/agents/codex/work.yaml'
        peer_data = yaml.safe_load(peer_prof.read_text())
        peer_data['settings']['model'] = 'shared-model'
        peer_prof.write_text(yaml.safe_dump(peer_data, sort_keys=False))
        run_git(peer, 'commit', '-qam', 'peer updates codex model')
        run_git(peer, 'push', '-q')

        # Local makes identical uncommitted edit without having fetched yet.
        local_prof = fixture.data / 'profiles/agents/codex/work.yaml'
        local_prof.write_text(peer_prof.read_text())

        # Preview defaults to fetch=True and detects identical remote reconciliation.
        code_prev, out_prev = self._run_cli([
            'data', 'publish', '--machine', str(fixture.machine_path), '--dry-run', '--format', 'json',
        ])
        self.assertEqual(code_prev, 0, out_prev)
        prev_rep = json.loads(out_prev)
        self.assertEqual(prev_rep['status'], 'pending_reconcile_identical')
        self.assertTrue(prev_rep['ready'])

        code_exec, out_exec = self._run_cli([
            'data', 'publish', '--machine', str(fixture.machine_path), '--yes', '--format', 'json',
        ])
        self.assertEqual(code_exec, 0, out_exec)
        exec_rep = json.loads(out_exec)
        self.assertEqual(exec_rep['status'], 'reconciled_with_remote')
        self.assertEqual(exec_rep['pulled_commits'], 1)
        self.assertFalse(inspect_repo(fixture.data, fetch=False)['dirty'])
        self.assertEqual(inspect_repo(fixture.data, fetch=False)['behind'], 0)

    def test_publish_dirty_and_behind_disjoint_auto_rebases_and_pushes(self):
        fixture, _, peer = self._setup_git_data()

        # Peer edits claude-code profile and pushes.
        peer_cc = peer / 'profiles/agents/claude-code/work.yaml'
        peer_cc_data = yaml.safe_load(peer_cc.read_text())
        peer_cc_data['settings']['model'] = 'peer-claude'
        peer_cc.write_text(yaml.safe_dump(peer_cc_data, sort_keys=False))
        run_git(peer, 'commit', '-qam', 'peer updates claude-code')
        run_git(peer, 'push', '-q')

        # Local edits a disjoint file (codex/work.yaml).
        local_codex = fixture.data / 'profiles/agents/codex/work.yaml'
        local_data = yaml.safe_load(local_codex.read_text())
        local_data['settings']['model'] = 'local-rebased-codex'
        local_codex.write_text(yaml.safe_dump(local_data, sort_keys=False))

        code_prev, out_prev = self._run_cli([
            'data', 'publish', '--machine', str(fixture.machine_path), '--dry-run', '--format', 'json',
        ])
        self.assertEqual(code_prev, 0, out_prev)
        prev_rep = json.loads(out_prev)
        self.assertEqual(prev_rep['status'], 'pending')
        self.assertTrue(prev_rep['will_rebase'])

        code_exec, out_exec = self._run_cli([
            'data', 'publish', '--machine', str(fixture.machine_path), '--yes', '--format', 'json',
        ])
        self.assertEqual(code_exec, 0, out_exec)
        exec_rep = json.loads(out_exec)
        self.assertEqual(exec_rep['status'], 'published')
        self.assertTrue(exec_rep['rebased'])

        run_git(peer, 'pull', '--ff-only', '-q')
        self.assertEqual(yaml.safe_load((peer / 'profiles/agents/codex/work.yaml').read_text())['settings']['model'], 'local-rebased-codex')
        self.assertEqual(yaml.safe_load((fixture.data / 'profiles/agents/claude-code/work.yaml').read_text())['settings']['model'], 'peer-claude')

    def test_publish_dirty_and_behind_overlap_blocks_without_circular_advice(self):
        fixture, _, peer = self._setup_git_data()

        peer_prof = peer / 'profiles/agents/codex/work.yaml'
        peer_data = yaml.safe_load(peer_prof.read_text())
        peer_data['settings']['model'] = 'remote-other'
        peer_prof.write_text(yaml.safe_dump(peer_data, sort_keys=False))
        run_git(peer, 'commit', '-qam', 'remote edit codex')
        run_git(peer, 'push', '-q')

        local_prof = fixture.data / 'profiles/agents/codex/work.yaml'
        local_data = yaml.safe_load(local_prof.read_text())
        local_data['settings']['model'] = 'local-conflict'
        local_prof.write_text(yaml.safe_dump(local_data, sort_keys=False))

        blocked = publish_repo(fixture.data, dry_run=True)
        self.assertFalse(blocked['ready'])
        self.assertEqual(blocked['status'], 'behind_conflict')
        self.assertIn('profiles/agents/codex/work.yaml', blocked['overlapping_files'])
        self.assertNotIn('claude-config data pull', blocked['reason'])
        self.assertIn('--no-pager', blocked['reason'])
        self.assertIn('--prefer-local', blocked['reason'])
        self.assertIn('--pr', blocked['reason'])
        self.assertTrue(blocked['can_prefer_local'])
        self.assertFalse(blocked['metadata_only_lock_diff'])
        self.assertIn('-  model: remote-other', blocked['diff_vs_upstream'])
        self.assertIn('+  model: local-conflict', blocked['diff_vs_upstream'])

        pull_blocked = pull_repo(fixture.data, dry_run=False, fetch=True)
        self.assertFalse(pull_blocked['ready'])
        self.assertEqual(pull_blocked['status'], 'dirty')
        self.assertIn('自动对齐/安全变基', pull_blocked['reason'])

        # Interactive confirmation hides redundant `--yes` tip.
        from claude_config.data_cmd import _confirm_publish
        sample = {
            'ready': True, 'preview_only': True, 'status': 'pending',
            'machine_id': 'm1', 'config_dir': str(fixture.data),
            'proposed_message': 'chore: test', 'files': ['a.yaml'],
        }
        buf = io.StringIO()
        with patch('builtins.input', return_value='n'), redirect_stdout(buf):
            self.assertFalse(_confirm_publish(sample, False))
        self.assertNotIn('追加 --yes', buf.getvalue())

    def test_publish_behind_conflict_detects_lockfile_metadata_only_diff_and_renders_inline(self):
        fixture, _, peer = self._setup_git_data()
        lock_rel = 'profiles/agents/claude-code/work.plugins.lock.yaml'
        base_pin = {
            'catalog': {'repo': 'example/catalog', 'commit': 'a' * 40, 'path': '.'},
            'source': {'repo': 'example/clarity', 'commit': 'b' * 40, 'path': 'release/plugin'},
            'version': '0.8.0',
            'source_fingerprint': 'c' * 64,
            'resolution': {'kind': 'commit', 'requested': 'b' * 40},
        }
        remote_lock = {
            'schema_version': 1,
            'agent_type': 'claude-code',
            'profile': 'profiles/agents/claude-code/work.yaml',
            'plugins': {'clarity@personal': base_pin},
        }
        (peer / lock_rel).write_text(yaml.safe_dump(remote_lock, sort_keys=False), encoding='utf-8')
        run_git(peer, 'add', lock_rel)
        run_git(peer, 'commit', '-qm', 'peer locks clarity by commit')
        run_git(peer, 'push', '-q')

        local_lock = yaml.safe_load(yaml.safe_dump(remote_lock, sort_keys=False))
        local_lock['plugins']['clarity@personal']['resolution'] = {'kind': 'release', 'requested': '0.8.0'}
        (fixture.data / lock_rel).write_text(yaml.safe_dump(local_lock, sort_keys=False), encoding='utf-8')

        code, out = self._run_cli([
            'data', 'publish', '--machine', str(fixture.machine_path), '--dry-run', '--format', 'text',
        ])
        self.assertEqual(code, 1, out)
        self.assertIn('git --no-pager -C', out)
        self.assertIn('仅来源元数据（resolution）不同', out)
        self.assertIn('kind: commit', out)
        self.assertIn('kind: release', out)

        # Changing version/fingerprint must NOT be classified as metadata-only.
        local_lock['plugins']['clarity@personal']['version'] = '0.8.1'
        local_lock['plugins']['clarity@personal']['resolution'] = {'kind': 'release', 'requested': '0.8.1'}
        (fixture.data / lock_rel).write_text(yaml.safe_dump(local_lock, sort_keys=False), encoding='utf-8')
        blocked = publish_repo(fixture.data, dry_run=True)
        self.assertEqual(blocked['status'], 'behind_conflict')
        self.assertFalse(blocked['metadata_only_lock_diff'])

    def test_publish_prefer_local_previews_net_diff_preserves_disjoint_remote_and_pushes(self):
        fixture, _, peer = self._setup_git_data()

        # Peer edits both codex/work.yaml (overlapping) and claude-code/work.yaml (disjoint).
        peer_codex = peer / 'profiles/agents/codex/work.yaml'
        pc = yaml.safe_load(peer_codex.read_text())
        pc['settings']['model'] = 'remote-codex'
        peer_codex.write_text(yaml.safe_dump(pc, sort_keys=False))
        peer_cc = peer / 'profiles/agents/claude-code/work.yaml'
        pcc = yaml.safe_load(peer_cc.read_text())
        pcc['settings']['model'] = 'peer-claude-must-survive'
        peer_cc.write_text(yaml.safe_dump(pcc, sort_keys=False))
        run_git(peer, 'commit', '-qam', 'peer updates codex and claude-code')
        run_git(peer, 'push', '-q')

        # Local (behind by 1, ahead == 0) edits only codex/work.yaml.
        local_codex = fixture.data / 'profiles/agents/codex/work.yaml'
        lc = yaml.safe_load(local_codex.read_text())
        lc['settings']['model'] = 'local-preferred-codex'
        local_codex.write_text(yaml.safe_dump(lc, sort_keys=False))

        code_prev, out_prev = self._run_cli([
            'data', 'publish', '--machine', str(fixture.machine_path), '--prefer-local', '--dry-run', '--format', 'json',
        ])
        self.assertEqual(code_prev, 0, out_prev)
        prev_rep = json.loads(out_prev)
        self.assertTrue(prev_rep['ready'])
        self.assertTrue(prev_rep['prefer_local'])
        self.assertEqual(prev_rep['status'], 'pending')
        self.assertEqual(prev_rep['overlapping_files'], ['profiles/agents/codex/work.yaml'])
        self.assertIn('-  model: remote-codex', prev_rep['diff_vs_upstream'])
        self.assertIn('+  model: local-preferred-codex', prev_rep['diff_vs_upstream'])

        code_exec, out_exec = self._run_cli([
            'data', 'publish', '--machine', str(fixture.machine_path), '--prefer-local', '--yes', '--format', 'json',
        ])
        self.assertEqual(code_exec, 0, out_exec)
        exec_rep = json.loads(out_exec)
        self.assertTrue(exec_rep['ready'])
        self.assertEqual(exec_rep['status'], 'published')
        self.assertTrue(exec_rep['prefer_local'])
        self.assertTrue(exec_rep['rebased'])
        self.assertEqual(exec_rep['pulled_commits'], 1)

        run_git(peer, 'pull', '--ff-only', '-q')
        self.assertEqual(yaml.safe_load((peer / 'profiles/agents/codex/work.yaml').read_text())['settings']['model'], 'local-preferred-codex')
        self.assertEqual(yaml.safe_load((peer / 'profiles/agents/claude-code/work.yaml').read_text())['settings']['model'], 'peer-claude-must-survive')
        self.assertEqual(yaml.safe_load((fixture.data / 'profiles/agents/claude-code/work.yaml').read_text())['settings']['model'], 'peer-claude-must-survive')
        self.assertFalse(inspect_repo(fixture.data, fetch=False)['dirty'])

    def test_publish_prefer_local_blocks_when_ahead_or_with_pr_and_rolls_back_on_validation_error(self):
        fixture, _, peer = self._setup_git_data()

        # Peer edits codex/work.yaml and pushes.
        peer_codex = peer / 'profiles/agents/codex/work.yaml'
        pc = yaml.safe_load(peer_codex.read_text())
        pc['settings']['model'] = 'remote-v1'
        peer_codex.write_text(yaml.safe_dump(pc, sort_keys=False))
        run_git(peer, 'commit', '-qam', 'peer codex')
        run_git(peer, 'push', '-q')

        # Combining --prefer-local with --pr is rejected.
        code_bad, out_bad = self._run_cli([
            'data', 'publish', '--machine', str(fixture.machine_path), '--prefer-local', '--pr', '--format', 'json',
        ])
        self.assertEqual(code_bad, 2, out_bad)

        # Local commits an overlapping change (ahead == 1) -> --prefer-local must refuse.
        local_codex = fixture.data / 'profiles/agents/codex/work.yaml'
        lc = yaml.safe_load(local_codex.read_text())
        lc['settings']['model'] = 'local-committed'
        local_codex.write_text(yaml.safe_dump(lc, sort_keys=False))
        run_git(fixture.data, 'commit', '-qam', 'local ahead commit')

        blocked = publish_repo(fixture.data, dry_run=False, prefer_local=True)
        self.assertFalse(blocked['ready'])
        self.assertEqual(blocked['status'], 'behind_conflict')
        self.assertFalse(blocked['can_prefer_local'])
        self.assertIn('未推送提交', blocked['reason'])

        # Rollback guard: reset local commit back to base, have peer remove a profile referenced by a new local machine edit.
        run_git(fixture.data, 'reset', '--hard', 'HEAD~1', '-q')
        extra_prof = peer / 'profiles/agents/codex/temp.yaml'
        extra_prof.write_text("schema_version: 1\nagent_type: codex\nsettings:\n  model: temp\n", encoding='utf-8')
        run_git(peer, 'add', '.')
        run_git(peer, 'commit', '-qm', 'peer add temp')
        run_git(peer, 'push', '-q')
        run_git(fixture.data, 'pull', '--ff-only', '-q')

        # Peer deletes temp.yaml and updates machine profile selection; local points machine file to temp.yaml.
        machine_rel = f"machines/{fixture.machine['machine_id']}.yaml"
        extra_prof.unlink()
        peer_machine = peer / machine_rel
        pm = yaml.safe_load(peer_machine.read_text())
        pm['agents']['codex'] = 'profiles/agents/codex/work.yaml'
        peer_machine.write_text(yaml.safe_dump(pm, sort_keys=False))
        run_git(peer, 'add', '-A')
        run_git(peer, 'commit', '-qm', 'peer remove temp')
        run_git(peer, 'push', '-q')

        local_machine = fixture.data / machine_rel
        lm = yaml.safe_load(local_machine.read_text())
        lm['agents']['codex'] = 'profiles/agents/codex/temp.yaml'
        local_machine_text = yaml.safe_dump(lm, sort_keys=False)
        local_machine.write_text(local_machine_text)
        orig_head = inspect_repo(fixture.data, fetch=False)['head_full']

        rolled_back = publish_repo(fixture.data, dry_run=False, prefer_local=True)
        self.assertFalse(rolled_back['ready'])
        self.assertEqual(rolled_back['status'], 'validation_failed')
        self.assertIn('已完整还原现场', rolled_back['reason'])
        self.assertEqual(inspect_repo(fixture.data, fetch=False)['head_full'], orig_head)
        self.assertEqual(local_machine.read_text(), local_machine_text)
        self.assertTrue((fixture.data / 'profiles/agents/codex/temp.yaml').is_file())

    def test_publish_dirty_chmod_not_misclassified_as_identical(self):
        fixture, _, peer = self._setup_git_data()
        run_git(fixture.data, 'config', 'core.filemode', 'true')

        peer_prof = peer / 'profiles/agents/codex/work.yaml'
        peer_data = yaml.safe_load(peer_prof.read_text())
        peer_data['settings']['model'] = 'shared-model'
        peer_prof.write_text(yaml.safe_dump(peer_data, sort_keys=False))
        run_git(peer, 'commit', '-qam', 'peer updates codex model')
        run_git(peer, 'push', '-q')

        local_prof = fixture.data / 'profiles/agents/codex/work.yaml'
        local_prof.write_text(peer_prof.read_text())
        local_prof.chmod(0o755)

        blocked = publish_repo(fixture.data, dry_run=True)
        self.assertFalse(blocked['ready'])
        self.assertEqual(blocked['status'], 'behind_conflict')


if __name__ == '__main__':
    unittest.main()
