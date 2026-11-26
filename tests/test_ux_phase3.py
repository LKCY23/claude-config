"""Phase 3 UX improvements tests: I-10 (baseline & optional --agent), I-11a (status), I-11b (sync)."""
from contextlib import redirect_stdout
import hashlib
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
from claude_config.core.native import parse_native
from claude_config.fetch_baseline import prepare_git
import test_machine_profiles as machine_profile_fixtures
import test_profile_manager as manager_fixtures
from test_profile_resources import ResourceFixture, run_git


class _BaselineMixin:
    def _prepare_second_baseline(self):
        self.add('guide', 'instruction', {'guide.md': 'first'}, 'guide.md')
        self.old = self.save()
        # Isolate the old checkout at self.old so advancing self.repo leaves the cache lagging behind.
        self.old_checkout = self.root / 'stale-checkout'
        subprocess.run(['git', 'clone', '-q', str(self.repo), str(self.old_checkout)], check=True, capture_output=True)
        run_git(self.old_checkout, 'remote', 'set-url', 'origin', 'https://github.com/owner/catalog.git')
        run_git(self.old_checkout, 'checkout', '--detach', self.old)
        self.machine['catalog_roots']['test'] = str(self.old_checkout)
        self.machine_path.write_text(yaml.safe_dump(self.machine, sort_keys=False))

        (self.repo / 'packages/guide/guide.md').write_text('second')
        run_git(self.repo, 'add', '.')
        run_git(self.repo, 'commit', '-qm', 'second baseline')
        self.commit = run_git(self.repo, 'rev-parse', 'HEAD')
        self.release = 'baseline-' + self.commit
        self.record = {
            'schema_version': 1,
            'kind': 'claudespace-baseline',
            'repo': 'owner/catalog',
            'commit': self.commit,
            'catalog_path': '.',
            'catalog_sha256': hashlib.sha256((self.repo / 'catalog.json').read_bytes()).hexdigest(),
            'validation': {'status': 'passed', 'level': 'structural', 'scope': 'catalog-schema'},
        }
        self.calls = []

    def _mock_api(self, path, *, binary=False):
        self.calls.append(('api', path))
        raw = (json.dumps(self.record) + '\n').encode()
        if '/releases/tags/' in path:
            return {
                'tag_name': self.release,
                'draft': False,
                'assets': [{'id': 7, 'name': 'test.baseline.json', 'size': len(raw)}],
            }
        if '/git/ref/tags/' in path:
            return {'object': {'type': 'commit', 'sha': self.commit}}
        if path.endswith('/releases/assets/7') and binary:
            return raw
        self.fail('Unexpected GitHub request: ' + path)

    def _mock_prepare_git(self, root, repo, commit, **_kwargs):
        self.calls.append(('git', repo, commit))
        if root.exists():
            return prepare_git(root, repo, commit)
        root.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(['git', 'clone', '-q', str(self.repo), str(root)], check=True, capture_output=True)
        run_git(root, 'remote', 'set-url', 'origin', 'https://github.com/' + repo + '.git')
        run_git(root, 'checkout', '--detach', commit)
        return True

    def _patch_fetch(self):
        return (
            patch('claude_config.fetch_baseline.api', side_effect=self._mock_api),
            patch('claude_config.fetch_baseline.prepare_git', side_effect=self._mock_prepare_git),
        )


class Phase3BaselineTests(_BaselineMixin, ResourceFixture):
    def test_fetch_baseline_without_agent_defaults_to_bound_agent(self):
        self._prepare_second_baseline()
        out = io.StringIO()
        p_api, p_git = self._patch_fetch()
        with p_api, p_git, redirect_stdout(out):
            code = cli_entry.main([
                'fetch-baseline',
                '--machine', str(self.machine_path),
                '--catalog', 'test',
                '--release', self.release,
                '--format', 'json',
            ])
        self.assertEqual(code, 0, out.getvalue())
        report = json.loads(out.getvalue())
        self.assertTrue(report['ready'])
        self.assertIn('--agent claude-code', report['prepared']['adopt_command'])

    def test_baseline_status_and_sync_dry_run_write_and_idempotency(self):
        self._prepare_second_baseline()
        # Simulate upstream catalogs.yaml already bumped to new commit while machine.yaml still points to old.
        catalogs_path = self.data / 'catalogs.yaml'
        catalogs = yaml.safe_load(catalogs_path.read_text())
        catalogs['catalogs']['test']['commit'] = self.commit
        catalogs_path.write_text(yaml.safe_dump(catalogs, sort_keys=False))

        out_status = io.StringIO()
        with redirect_stdout(out_status):
            code = cli_entry.main(['baseline', 'status', '--machine', str(self.machine_path), '--format', 'json'])
        self.assertEqual(code, 0, out_status.getvalue())
        status_rep = json.loads(out_status.getvalue())
        self.assertFalse(status_rep['aligned'])
        self.assertEqual(status_rep['groups'][0]['status'], 'stale_cache')

        # Dry-run must perform zero network calls and keep machine.yaml unchanged.
        machine_before = self.machine_path.read_text()
        out_dry = io.StringIO()
        p_api, p_git = self._patch_fetch()
        with p_api, p_git, redirect_stdout(out_dry):
            code = cli_entry.main(['baseline', 'sync', '--machine', str(self.machine_path), '--dry-run', '--format', 'json'])
        self.assertEqual(code, 0, out_dry.getvalue())
        dry_rep = json.loads(out_dry.getvalue())
        self.assertTrue(dry_rep['preview_only'])
        self.assertTrue(dry_rep['pending'])
        self.assertTrue(dry_rep['actions'][0]['fetch_needed'])
        self.assertEqual(self.calls, [])
        self.assertEqual(self.machine_path.read_text(), machine_before)

        # Write performs fetch + adopt and updates machine.yaml catalog_roots.
        out_write = io.StringIO()
        p_api, p_git = self._patch_fetch()
        with p_api, p_git, redirect_stdout(out_write):
            code = cli_entry.main(['baseline', 'sync', '--machine', str(self.machine_path), '--write', '--format', 'json'])
        self.assertEqual(code, 0, out_write.getvalue())
        write_rep = json.loads(out_write.getvalue())
        self.assertFalse(write_rep['preview_only'])
        self.assertTrue(write_rep['aligned'])
        self.assertTrue(write_rep['actions'][0]['fetched'])
        self.assertTrue(write_rep['actions'][0]['applied'])
        self.assertTrue(write_rep['actions'][0]['run_id'])
        updated_machine = yaml.safe_load(self.machine_path.read_text())
        self.assertIn(self.commit, updated_machine['catalog_roots']['test'])

        # Idempotent second sync does zero network calls and zero writes.
        self.calls.clear()
        out_again = io.StringIO()
        with redirect_stdout(out_again):
            code = cli_entry.main(['baseline', 'sync', '--machine', str(self.machine_path), '--write', '--format', 'json'])
        self.assertEqual(code, 0)
        again_rep = json.loads(out_again.getvalue())
        self.assertEqual(again_rep['actions'][0]['status'], 'aligned')
        self.assertEqual(self.calls, [])

    def test_baseline_adopt_bare_sha_advances_catalogs_and_machine_and_rejects_bad_sha(self):
        self._prepare_second_baseline()
        out_bad = io.StringIO()
        with redirect_stdout(out_bad):
            code = cli_entry.main([
                'baseline', 'adopt', 'not-a-sha',
                '--machine', str(self.machine_path),
                '--format', 'json',
            ])
        self.assertEqual(code, 2, out_bad.getvalue())
        self.assertFalse(json.loads(out_bad.getvalue())['ready'])

        out = io.StringIO()
        p_api, p_git = self._patch_fetch()
        with p_api, p_git, redirect_stdout(out):
            code = cli_entry.main([
                'baseline', 'adopt', self.commit,
                '--machine', str(self.machine_path),
                '--write',
                '--format', 'json',
            ])
        self.assertEqual(code, 0, out.getvalue())
        rep = json.loads(out.getvalue())
        self.assertEqual(rep['actions'][0]['to_commit'], self.commit)
        self.assertTrue(rep['actions'][0]['run_id'])
        catalogs = yaml.safe_load((self.data / 'catalogs.yaml').read_text())
        self.assertEqual(catalogs['catalogs']['test']['commit'], self.commit)
        machine = yaml.safe_load(self.machine_path.read_text())
        self.assertIn(self.commit, machine['catalog_roots']['test'])


class Phase3StatusTests(unittest.TestCase):
    def _state_snapshot(self, root):
        return {
            p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob('*'))
            if p.is_file()
        }

    def test_status_classifies_targets_degrades_on_mismatch_and_is_strictly_readonly(self):
        fixture = manager_fixtures.ManagerTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.addCleanup(fixture.tearDown)

        # claude-code: synced initially, then profile bumped so claude-code is pending.
        fixture.native('claude-code', value={'model': 'old'})
        fixture.manager('claude-code').plan().apply(fixture.state)

        # codex: conflict (apply, then mutate both profile and native differently).
        fixture.native('codex', value={'model': 'old'})
        fixture.manager('codex').plan().apply(fixture.state)
        fixture.profile('codex', settings={'model': 'new-profile'})
        fixture.native('codex', value={'model': 'local-drift'})

        # Update claude-code profile AFTER codex apply to mark claude-code pending.
        fixture.profile('claude-code', settings={'model': 'updated-model'})

        # Add antigravity before externalizing, then drop it from repo selection so strict=False degrades cleanly.
        ag_root = fixture.root / '.gemini/config'
        ag_root.mkdir(parents=True)
        ag_app = fixture.root / 'Antigravity.app'
        ag_app.mkdir()
        ag_profile_path = fixture.data / 'profiles/agents/antigravity/current.yaml'
        ag_profile_path.parent.mkdir(parents=True, exist_ok=True)
        ag_profile_path.write_text(yaml.safe_dump({'schema_version': 1, 'agent_type': 'antigravity'}, sort_keys=False))
        fixture.machine['agents']['antigravity'] = {
            'profile': 'profiles/agents/antigravity/current.yaml',
            'binding': {'config_root': str(ag_root), 'application': str(ag_app)},
        }
        fixture.machine_path.write_text(yaml.safe_dump(fixture.machine, sort_keys=False))

        _, selection, selection_path = machine_profile_fixtures.externalize(fixture)
        selection['agents'].pop('antigravity')
        selection_path.write_text(yaml.safe_dump(selection, sort_keys=False))

        before = self._state_snapshot(fixture.root)

        out_json = io.StringIO()
        with patch('claude_config.core.manager.NativeProbe', return_value=fixture.probe), \
                redirect_stdout(out_json):
            code = cli_entry.main(['status', '--machine', str(fixture.machine_path), '--format', 'json'])
        self.assertEqual(code, 0, out_json.getvalue())
        report = json.loads(out_json.getvalue())
        self.assertTrue(report['read_only'])
        self.assertTrue(report['mismatches'])

        by_name = {t['name']: t for t in report['targets']}
        self.assertEqual(by_name['claude-code']['state'], 'pending')
        self.assertEqual(by_name['codex']['state'], 'conflict')
        self.assertIn('resolve --agent codex', by_name['codex']['hint'])
        self.assertEqual(by_name['antigravity']['state'], 'error')
        self.assertIn('machines/<id>.yaml', by_name['antigravity']['reason'])
        self.assertEqual(report['summary']['pending'], 1)
        self.assertEqual(report['summary']['conflict'], 1)
        self.assertEqual(report['summary']['error'], 1)

        out_text = io.StringIO()
        with patch('claude_config.core.manager.NativeProbe', return_value=fixture.probe), \
                redirect_stdout(out_text):
            code = cli_entry.main(['status', '--machine', str(fixture.machine_path), '--format', 'text'])
        self.assertEqual(code, 0)
        text = out_text.getvalue()
        self.assertIn('全局配置状态', text)
        self.assertNotIn("{'group':", text)
        self.assertIn('claude-config resolve --agent codex', text)
        self.assertIn('claude-config sync', text)

        after = self._state_snapshot(fixture.root)
        self.assertEqual(before, after)


class Phase3SyncTests(unittest.TestCase):
    def test_sync_dry_run_yes_skips_conflicts_and_applies_ready_targets(self):
        fixture = manager_fixtures.ManagerTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.addCleanup(fixture.tearDown)

        # Create conflict on claude-code and pending update on codex.
        fixture.native('claude-code', value={'model': 'old'})
        fixture.manager('claude-code').plan().apply(fixture.state)
        fixture.profile('claude-code', settings={'model': 'new-profile'})
        fixture.native('claude-code', value={'model': 'local-edit'})

        fixture.native('codex', value={'model': 'old'})

        with patch('claude_config.core.manager.NativeProbe', return_value=fixture.probe):
            # Scoped --agent codex --dry-run exits 0 and makes no writes.
            out_dry = io.StringIO()
            with redirect_stdout(out_dry):
                code = cli_entry.main([
                    'sync', '--machine', str(fixture.machine_path),
                    '--agent', 'codex', '--dry-run', '--format', 'json',
                ])
            self.assertEqual(code, 0, out_dry.getvalue())
            self.assertTrue(json.loads(out_dry.getvalue())['preview_only'])
            self.assertEqual(parse_native('codex', fixture.native('codex').read_bytes())['model'], 'old')

            # Global sync --yes applies codex, skips claude-code (conflict), exits 1.
            out_yes = io.StringIO()
            with redirect_stdout(out_yes):
                code = cli_entry.main(['sync', '--machine', str(fixture.machine_path), '--yes', '--format', 'json'])
            self.assertEqual(code, 1, out_yes.getvalue())
            rep = json.loads(out_yes.getvalue())
            by_name = {t['name']: t for t in rep['targets']}
            self.assertEqual(by_name['codex']['outcome'], 'applied')
            self.assertTrue(by_name['codex']['run_id'])
            self.assertEqual(by_name['claude-code']['outcome'], 'skipped_conflict')
            self.assertEqual(parse_native('codex', fixture.native('codex').read_bytes())['model'], 'selected')
            self.assertEqual(parse_native('claude-code', fixture.native('claude-code').read_bytes())['model'], 'local-edit')

            # Non-TTY without --yes only previews without applying.
            fixture.profile('codex', settings={'model': 'newer'})
            out_nontty = io.StringIO()
            with redirect_stdout(out_nontty):
                code = cli_entry.main(['sync', '--machine', str(fixture.machine_path), '--agent', 'codex'])
            self.assertEqual(code, 0, out_nontty.getvalue())
            self.assertEqual(parse_native('codex', fixture.native('codex').read_bytes())['model'], 'selected')

    def test_sync_interactive_tty_prompt_cancel_and_confirm(self):
        fixture = manager_fixtures.ManagerTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.addCleanup(fixture.tearDown)

        fixture.native('claude-code', value={'model': 'selected'})
        fixture.manager('claude-code').plan().apply(fixture.state)
        fixture.native('codex', value={'model': 'old'})

        with patch('claude_config.core.manager.NativeProbe', return_value=fixture.probe):
            # Cancel on interactive 'n'
            out_no = io.StringIO()
            out_no.isatty = lambda: True
            with patch('sys.stdin.isatty', return_value=True), \
                    patch('builtins.input', return_value='n'), \
                    redirect_stdout(out_no):
                code = cli_entry.main(['sync', '--machine', str(fixture.machine_path), '--agent', 'codex'])
            self.assertEqual(code, 0, out_no.getvalue())
            self.assertIn('已取消', out_no.getvalue())
            self.assertEqual(parse_native('codex', fixture.native('codex').read_bytes())['model'], 'old')

            # Confirm on interactive 'y'
            out_yes = io.StringIO()
            out_yes.isatty = lambda: True
            with patch('sys.stdin.isatty', return_value=True), \
                    patch('builtins.input', return_value='y'), \
                    redirect_stdout(out_yes):
                code = cli_entry.main(['sync', '--machine', str(fixture.machine_path), '--agent', 'codex'])
            self.assertEqual(code, 0, out_yes.getvalue())
            self.assertIn('已写入', out_yes.getvalue())
            self.assertEqual(parse_native('codex', fixture.native('codex').read_bytes())['model'], 'selected')


class Phase3EndToEndSyncWithBaselineTests(_BaselineMixin, ResourceFixture):
    def test_sync_aligns_stale_baseline_and_updates_deployed_resource(self):
        self._prepare_second_baseline()
        # Deploy initial version ('first') using old checkout.
        self.manager().plan().apply(self.state)
        claude_md = self.native / 'CLAUDE.md'
        self.assertIn('first', claude_md.read_text())

        # Now catalogs.yaml advances to self.commit ('second'), while machine.yaml lags at self.old.
        catalogs_path = self.data / 'catalogs.yaml'
        catalogs = yaml.safe_load(catalogs_path.read_text())
        catalogs['catalogs']['test']['commit'] = self.commit
        catalogs_path.write_text(yaml.safe_dump(catalogs, sort_keys=False))

        out = io.StringIO()
        p_api, p_git = self._patch_fetch()
        fake_probe = lambda *args: [{'name': 'isolated-native', 'status': 'passed'}]
        with p_api, p_git, \
                patch('claude_config.core.manager.NativeProbe', return_value=fake_probe), \
                redirect_stdout(out):
            code = cli_entry.main(['sync', '--machine', str(self.machine_path), '--yes', '--format', 'json'])
        self.assertEqual(code, 0, out.getvalue())
        rep = json.loads(out.getvalue())
        self.assertEqual(rep['baseline']['status'], 'synced')
        self.assertTrue(rep['baseline']['actions'][0]['run_id'])
        self.assertEqual(rep['targets'][0]['outcome'], 'applied')
        self.assertTrue(rep['targets'][0]['run_id'])
        self.assertIn('second', claude_md.read_text())
        self.assertNotIn('first', claude_md.read_text())

        # Re-running sync is now a no-op (synced).
        out_noop = io.StringIO()
        with patch('claude_config.core.manager.NativeProbe', return_value=fake_probe), \
                redirect_stdout(out_noop):
            code = cli_entry.main(['sync', '--machine', str(self.machine_path), '--yes', '--format', 'json'])
        self.assertEqual(code, 0)
        noop = json.loads(out_noop.getvalue())
        self.assertEqual(noop['baseline']['status'], 'aligned')
        self.assertEqual(noop['targets'][0]['outcome'], 'synced')


if __name__ == '__main__':
    unittest.main()
