"""Stage 2 architecture tests: batched Git resolution, scan/hash deduplication, concurrent status/sync, and smart re-plan."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
from contextvars import copy_context
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import unittest
from unittest.mock import patch

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from claude_config import cli as cli_entry
from claude_config.core import catalog as catalog_mod
from claude_config.core.catalog import CatalogResolver
from claude_config.core.execution_context import progress, progress_output
from claude_config.core.manager import ProfileManager
from claude_config.core.native import parse_native
from claude_config.core.schema import SchemaError
import test_profile_manager as manager_fixtures
from test_profile_resources import ResourceFixture, run_git


class Stage2CatalogBatchTests(ResourceFixture):
    def test_multi_package_resolution_deduplicates_verify_ls_files_and_cat_file(self):
        self.add('skill-a', 'skill', {
            'SKILL.md': '---\nname: skill-a\ndescription: first\n---\nbody\n',
            'references/a.txt': 'alpha',
        }, 'SKILL.md')
        self.add('skill-b', 'skill', {
            'SKILL.md': '---\nname: skill-b\ndescription: second\n---\nbody\n',
            'references/b.txt': 'beta',
        }, 'SKILL.md')
        self.add('guide', 'instruction', {'guide.md': 'hello'}, 'guide.md')
        self.save()

        with patch('claude_config.core.catalog.git', wraps=catalog_mod.git) as calls:
            packages = CatalogResolver(self.manager()).resolve(self.profile)

        self.assertEqual(set(packages), {'skill-a@test', 'skill-b@test', 'guide@test'})
        ls_calls = [c for c in calls.call_args_list if 'ls-files' in c.args]
        cat_calls = [c for c in calls.call_args_list if 'cat-file' in c.args]
        status_calls = [c for c in calls.call_args_list if 'status' in c.args]
        self.assertEqual(len(ls_calls), 1)
        self.assertEqual(len(cat_calls), 1)
        self.assertEqual(len(status_calls), 1)

    def test_batched_blob_check_still_blocks_tampered_assume_unchanged_file(self):
        self.add('skill-a', 'skill', {
            'SKILL.md': '---\nname: skill-a\ndescription: first\n---\nbody\n',
        }, 'SKILL.md')
        self.add('guide', 'instruction', {'guide.md': 'original'}, 'guide.md')
        self.save()

        guide_path = self.repo / 'packages/guide/guide.md'
        before = guide_path.read_bytes()
        run_git(self.repo, 'update-index', '--assume-unchanged', 'packages/guide/guide.md')
        try:
            guide_path.write_bytes(b'tampered')
            with self.assertRaisesRegex(SchemaError, 'pinned Git object'):
                CatalogResolver(self.manager()).resolve(self.profile)
        finally:
            guide_path.write_bytes(before)
            run_git(self.repo, 'update-index', '--no-assume-unchanged', 'packages/guide/guide.md')


class Stage2ResourceScanTests(ResourceFixture):
    def test_lightweight_scan_still_detects_extra_files_symlinks_and_drift(self):
        self.add('skill-a', 'skill', {
            'SKILL.md': '---\nname: skill-a\ndescription: first\n---\nbody\n',
            'references/note.txt': 'ok',
        }, 'SKILL.md')
        self.save()

        self.manager().plan().apply(self.state)
        self.assertTrue(self.manager().plan(prepare=False).report['ready'])

        deployed = self.native / 'skills/skill-a'
        extra = deployed / 'unmanaged.txt'
        extra.write_text('extra')
        blocked = self.manager().plan(prepare=False)
        self.assertFalse(blocked.report['ready'])
        self.assertIn('unmanaged_resource_collision', blocked.report['resource_conflicts'][0]['issues'])
        extra.unlink()

        link = deployed / 'escape.txt'
        link.symlink_to(self.native)
        blocked_link = self.manager().plan(prepare=False)
        self.assertFalse(blocked_link.report['ready'])
        self.assertIn('unmanaged_resource_collision', blocked_link.report['resource_conflicts'][0]['issues'])
        link.unlink()

        (deployed / 'references/note.txt').write_text('edited')
        drifted = self.manager().plan(prepare=False)
        self.assertFalse(drifted.report['ready'])
        self.assertEqual(drifted.report['blockers'][0]['code'], 'resource_drift')


class Stage2ConcurrencyAndSmartSyncTests(unittest.TestCase):
    def test_progress_output_is_thread_safe(self):
        buf = io.StringIO()
        with progress_output(buf):
            def worker(idx):
                for j in range(15):
                    progress(f'worker-{idx}-{j}')
            with ThreadPoolExecutor(max_workers=4) as pool:
                futures = [pool.submit(copy_context().run, worker, i) for i in range(4)]
                for future in futures:
                    future.result()
        lines = buf.getvalue().splitlines()
        self.assertEqual(len(lines), 60)
        for line in lines:
            self.assertRegex(line, r'^\[\d+\.\ds\] worker-\d+-\d+$')

    def test_concurrent_status_and_sync_preserve_order_and_skip_synced_replan(self):
        fixture = manager_fixtures.ManagerTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.addCleanup(fixture.tearDown)

        # Make claude-code already synced, and codex pending update.
        fixture.native('claude-code', value={'model': 'selected'})
        fixture.manager('claude-code').plan().apply(fixture.state)
        fixture.native('codex', value={'model': 'old'})

        # Add antigravity as pending too so we have 3 agents running concurrently.
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

        orig_plan = ProfileManager.plan
        recorded_calls = []

        def tracked_plan(self_mgr, *args, **kwargs):
            # Force claude-code to finish slower than codex/antigravity to test deterministic ordering.
            if not kwargs.get('prepare', True) and self_mgr.agent == 'claude-code':
                time.sleep(0.05)
            recorded_calls.append((self_mgr.agent, kwargs.get('prepare', True)))
            return orig_plan(self_mgr, *args, **kwargs)

        out_status = io.StringIO()
        with patch('claude_config.core.manager.NativeProbe', return_value=fixture.probe), \
                patch.object(ProfileManager, 'plan', tracked_plan), \
                redirect_stdout(out_status):
            code = cli_entry.main(['status', '--machine', str(fixture.machine_path), '--format', 'json'])
        self.assertEqual(code, 0, out_status.getvalue())
        status_rep = json.loads(out_status.getvalue())
        self.assertEqual([t['name'] for t in status_rep['targets']], ['claude-code', 'codex', 'antigravity'])
        self.assertEqual(status_rep['targets'][0]['state'], 'synced')
        self.assertEqual(status_rep['targets'][1]['state'], 'pending')

        recorded_calls.clear()
        out_sync = io.StringIO()
        with patch('claude_config.core.manager.NativeProbe', return_value=fixture.probe), \
                patch.object(ProfileManager, 'plan', tracked_plan), \
                redirect_stdout(out_sync):
            code = cli_entry.main(['sync', '--machine', str(fixture.machine_path), '--yes', '--format', 'json'])
        self.assertEqual(code, 0, out_sync.getvalue())
        sync_rep = json.loads(out_sync.getvalue())
        self.assertEqual([t['name'] for t in sync_rep['targets']], ['claude-code', 'codex', 'antigravity'])
        by_name = {t['name']: t for t in sync_rep['targets']}
        self.assertEqual(by_name['claude-code']['outcome'], 'synced')
        self.assertEqual(by_name['codex']['outcome'], 'applied')
        self.assertEqual(parse_native('codex', fixture.native('codex').read_bytes())['model'], 'selected')

        # claude-code was already synced and baseline did not change -> NO prepare=True call for claude-code!
        prepare_true_agents = [agent for agent, prep in recorded_calls if prep is True]
        self.assertNotIn('claude-code', prepare_true_agents)
        self.assertIn('codex', prepare_true_agents)

    def test_sync_replans_all_targets_when_baseline_updates(self):
        from test_ux_phase3 import _BaselineMixin

        class _SyncBaselineCase(_BaselineMixin, ResourceFixture):
            pass

        case = _SyncBaselineCase()
        case.setUp()
        self.addCleanup(case.doCleanups)
        self.addCleanup(case.tearDown)
        case._prepare_second_baseline()
        # Apply old baseline to claude-code first so preview is already 'synced' on the old baseline.
        case.manager().plan().apply(case.state)
        # Update catalogs.yaml to second commit while machine still points to old_checkout -> baseline pending.
        catalogs_doc = yaml.safe_load((case.data / 'catalogs.yaml').read_text())
        catalogs_doc['catalogs']['test']['commit'] = case.commit
        (case.data / 'catalogs.yaml').write_text(yaml.safe_dump(catalogs_doc, sort_keys=False))

        orig_plan = ProfileManager.plan
        prepare_true_calls = []

        def tracked_plan(self_mgr, *args, **kwargs):
            if kwargs.get('prepare', True):
                prepare_true_calls.append(self_mgr.agent)
            return orig_plan(self_mgr, *args, **kwargs)

        out = io.StringIO()
        p_api, p_git = case._patch_fetch()
        with p_api, p_git, \
                patch('claude_config.core.manager.NativeProbe', return_value=lambda *a: []), \
                patch.object(ProfileManager, 'plan', tracked_plan), \
                redirect_stdout(out):
            code = cli_entry.main(['sync', '--machine', str(case.machine_path), '--yes', '--format', 'json'])
        self.assertEqual(code, 0, out.getvalue())
        self.assertEqual(prepare_true_calls, ['claude-code'])


if __name__ == '__main__':
    unittest.main()
