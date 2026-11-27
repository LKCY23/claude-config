"""Stage 3 architecture tests: catalog_roots: auto and init --sync-selection."""
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
from claude_config.baseline_cmd import inspect_baselines
from claude_config.core.catalog import CatalogResolver
from claude_config.core.plugin_manager import PluginManager
from claude_config.core.schema import SchemaError, resolve_catalog_root, validate_machine
from claude_config.setup import doctor
import test_machine_profiles as machine_profile_fixtures
import test_profile_manager as manager_fixtures
from test_profile_resources import ResourceFixture, run_git
from test_ux_phase3 import _BaselineMixin


class Stage3AutoCatalogRootTests(_BaselineMixin, ResourceFixture):
    def test_schema_accepts_auto_and_rejects_relative_catalog_root(self):
        from copy import deepcopy
        doc = deepcopy(self.machine)
        doc['catalog_roots']['test'] = 'auto'
        self.assertEqual(validate_machine(doc)['catalog_roots']['test'], 'auto')

        bad = dict(doc, catalog_roots={'test': 'relative/path'})
        with self.assertRaisesRegex(SchemaError, 'absolute path'):
            validate_machine(bad)

        resolved = resolve_catalog_root(doc, 'test', {'repo': 'owner/catalog', 'commit': 'a' * 40, 'path': '.'})
        self.assertEqual(resolved, self.state / 'sources' / 'owner--catalog' / ('a' * 40))

    def test_auto_catalog_roots_work_across_resolver_baseline_doctor_and_adopt(self):
        self._prepare_second_baseline()
        # Populate state_root/sources/owner--catalog/<self.old> and switch machine.yaml via init --auto-catalog-roots.
        old_cache = self.state / 'sources' / 'owner--catalog' / self.old
        old_cache.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(['git', 'clone', '-q', str(self.old_checkout), str(old_cache)], check=True, capture_output=True)
        run_git(old_cache, 'remote', 'set-url', 'origin', 'https://github.com/owner/catalog.git')
        run_git(old_cache, 'checkout', '--detach', self.old)

        # Preview first: leaves machine.yaml untouched.
        out_prev = io.StringIO()
        with redirect_stdout(out_prev):
            code = cli_entry.main([
                'init', '--machine', str(self.machine_path), '--auto-catalog-roots', '--format', 'json',
            ])
        self.assertEqual(code, 0, out_prev.getvalue())
        self.assertEqual(yaml.safe_load(self.machine_path.read_text())['catalog_roots']['test'], str(self.old_checkout))

        # Write: converts catalog_roots.test -> 'auto'.
        out_write = io.StringIO()
        with redirect_stdout(out_write):
            code = cli_entry.main([
                'init', '--machine', str(self.machine_path), '--auto-catalog-roots', '--write', '--format', 'json',
            ])
        self.assertEqual(code, 0, out_write.getvalue())
        self.assertEqual(yaml.safe_load(self.machine_path.read_text())['catalog_roots']['test'], 'auto')

        # 1) baseline status, CatalogResolver, and doctor all work seamlessly with 'auto'.
        inspection = inspect_baselines(self.machine_path)
        self.assertTrue(inspection['aligned'])
        self.assertEqual(inspection['catalogs'][0]['catalog_root_mode'], 'auto')
        self.assertEqual(inspection['catalogs'][0]['catalog_root'], str(old_cache))

        pkgs = CatalogResolver(self.manager()).resolve(self.profile)
        self.assertEqual(pkgs['guide@test'].files['guide.md'], b'first')

        doc_report, _ = doctor(self.machine_path)
        self.assertTrue(doc_report['ready'], doc_report)
        cat_checks = [c for c in doc_report['checks'] if c['name'] == 'catalog_directory']
        self.assertEqual(cat_checks[0]['status'], 'passed')
        self.assertIn('auto ->', cat_checks[0]['reason'])

        # 2) Simulate git pull of claude-config-data updating catalogs.yaml to second commit,
        # without editing machine.yaml -> baseline sync --write preserves 'auto' in machine.yaml!
        catalogs_doc = yaml.safe_load((self.data / 'catalogs.yaml').read_text())
        catalogs_doc['catalogs']['test']['commit'] = self.commit
        (self.data / 'catalogs.yaml').write_text(yaml.safe_dump(catalogs_doc, sort_keys=False))

        self.assertFalse(inspect_baselines(self.machine_path)['aligned'])
        p_api, p_git = self._patch_fetch()
        out_bsync = io.StringIO()
        with p_api, p_git, redirect_stdout(out_bsync):
            code = cli_entry.main([
                'baseline', 'sync', '--machine', str(self.machine_path), '--write', '--format', 'json',
            ])
        self.assertEqual(code, 0, out_bsync.getvalue())
        self.assertEqual(yaml.safe_load(self.machine_path.read_text())['catalog_roots']['test'], 'auto')
        self.assertTrue(inspect_baselines(self.machine_path)['aligned'])
        self.assertEqual(CatalogResolver(self.manager()).resolve(self.profile)['guide@test'].files['guide.md'], b'second')

        # 3) PluginManager.ensure_catalog resolves 'auto' cleanly as well.
        pm = PluginManager(self.manager())
        pm.context()
        self.assertEqual(pm.ensure_catalog('test')[0].resolve(), (self.state / 'sources' / 'owner--catalog' / self.commit).resolve())

        # 4) Doctor fails clearly if catalogs.yaml is removed while catalog_roots uses 'auto'.
        (self.data / 'catalogs.yaml').unlink()
        broken_doc, _ = doctor(self.machine_path)
        self.assertFalse(broken_doc['ready'])
        self.assertEqual([c for c in broken_doc['checks'] if c['name'] == 'catalog_directory'][0]['status'], 'failed')


class Stage3SyncSelectionTests(unittest.TestCase):
    def _externalized_fixture(self):
        fixture = manager_fixtures.ManagerTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.addCleanup(fixture.tearDown)
        local, selection, selection_path = machine_profile_fixtures.externalize(fixture)
        return fixture, local, selection, selection_path

    def test_sync_selection_preview_write_autoselect_prune_and_rollback(self):
        fixture, local, selection, selection_path = self._externalized_fixture()

        # Remove codex from local machine.yaml (repository_only -> pruned) and
        # add antigravity with 1 unique profile (local_only -> auto-selected).
        ag_root = fixture.root / '.gemini/config'
        ag_root.mkdir(parents=True)
        ag_app = fixture.root / 'Antigravity.app'
        ag_app.mkdir()
        ag_rel = 'profiles/agents/antigravity/official.yaml'
        ag_prof = fixture.data / ag_rel
        ag_prof.parent.mkdir(parents=True, exist_ok=True)
        ag_prof.write_text(yaml.safe_dump({'schema_version': 1, 'agent_type': 'antigravity'}, sort_keys=False))

        local['agents'].pop('codex')
        local['agents']['antigravity'] = {
            'binding': {'config_root': str(ag_root), 'application': str(ag_app)},
        }
        fixture.machine_path.write_text(yaml.safe_dump(local, sort_keys=False))

        # Status renders init --sync-selection hint when mismatches exist.
        out_status = io.StringIO()
        with patch('claude_config.core.manager.NativeProbe', return_value=fixture.probe), redirect_stdout(out_status):
            cli_entry.main(['status', '--machine', str(fixture.machine_path), '--format', 'text'])
        self.assertIn('claude-config init --sync-selection', out_status.getvalue())

        # Preview by default: does NOT mutate machines/<id>.yaml.
        before_sel_bytes = selection_path.read_bytes()
        before_machine_bytes = fixture.machine_path.read_bytes()
        out_prev = io.StringIO()
        with redirect_stdout(out_prev):
            code = cli_entry.main([
                'init', '--machine', str(fixture.machine_path), '--sync-selection', '--format', 'json',
            ])
        self.assertEqual(code, 0, out_prev.getvalue())
        self.assertEqual(selection_path.read_bytes(), before_sel_bytes)

        # --write applies reconciliation to machines/<id>.yaml and leaves machine.yaml untouched.
        out_write = io.StringIO()
        with redirect_stdout(out_write):
            code = cli_entry.main([
                'init', '--machine', str(fixture.machine_path), '--sync-selection', '--write', '--format', 'json',
            ])
        self.assertEqual(code, 0, out_write.getvalue())
        write_rep = json.loads(out_write.getvalue())
        self.assertEqual(fixture.machine_path.read_bytes(), before_machine_bytes)
        updated = yaml.safe_load(selection_path.read_text())
        self.assertEqual(set(updated['agents']), {'claude-code', 'antigravity'})
        self.assertEqual(updated['agents']['antigravity'], ag_rel)

        # Doctor now reports aligned selection.
        doc_report, _ = doctor(fixture.machine_path)
        self.assertTrue(doc_report['ready'], doc_report)

        # Rollback via `init --run <run_id>` cleanly restores previous machines/<id>.yaml.
        run_id = Path(write_rep['recovery']['journal']).parent.name
        out_rb = io.StringIO()
        with redirect_stdout(out_rb):
            code = cli_entry.main([
                'init', '--machine', str(fixture.machine_path), '--run', run_id, '--write', '--format', 'json',
            ])
        self.assertEqual(code, 0, out_rb.getvalue())
        self.assertEqual(selection_path.read_bytes(), before_sel_bytes)

    def test_sync_selection_requires_explicit_select_when_ambiguous_or_missing_and_validates(self):
        fixture, local, selection, selection_path = self._externalized_fixture()

        ag_root = fixture.root / '.gemini/config'
        ag_root.mkdir(parents=True)
        ag_app = fixture.root / 'Antigravity.app'
        ag_app.mkdir()
        ag_dir = fixture.data / 'profiles/agents/antigravity'
        ag_dir.mkdir(parents=True, exist_ok=True)
        (ag_dir / 'a.yaml').write_text(yaml.safe_dump({'schema_version': 1, 'agent_type': 'antigravity'}, sort_keys=False))
        (ag_dir / 'b.yaml').write_text(yaml.safe_dump({'schema_version': 1, 'agent_type': 'antigravity'}, sort_keys=False))
        local['agents']['antigravity'] = {
            'binding': {'config_root': str(ag_root), 'application': str(ag_app)},
        }
        fixture.machine_path.write_text(yaml.safe_dump(local, sort_keys=False))

        # Ambiguous (>1 profile): fails closed without --select.
        out_amb = io.StringIO()
        with redirect_stdout(out_amb):
            code = cli_entry.main([
                'init', '--machine', str(fixture.machine_path), '--sync-selection', '--format', 'json',
            ])
        self.assertNotEqual(code, 0)
        self.assertIn('--select antigravity=', out_amb.getvalue())

        # Invalid/missing profile in --select is rejected.
        out_bad = io.StringIO()
        with redirect_stdout(out_bad):
            code = cli_entry.main([
                'init', '--machine', str(fixture.machine_path), '--sync-selection',
                '--select', 'antigravity=profiles/agents/antigravity/missing.yaml', '--format', 'json',
            ])
        self.assertNotEqual(code, 0)

        # --select without --sync-selection is rejected.
        out_orphan = io.StringIO()
        with redirect_stdout(out_orphan):
            code = cli_entry.main([
                'init', '--machine', str(fixture.machine_path),
                '--select', 'antigravity=profiles/agents/antigravity/b.yaml', '--format', 'json',
            ])
        self.assertNotEqual(code, 0)

        # Valid explicit --select succeeds.
        out_ok = io.StringIO()
        with redirect_stdout(out_ok):
            code = cli_entry.main([
                'init', '--machine', str(fixture.machine_path), '--sync-selection',
                '--select', 'antigravity=profiles/agents/antigravity/b.yaml', '--write', '--format', 'json',
            ])
        self.assertEqual(code, 0, out_ok.getvalue())
        self.assertEqual(
            yaml.safe_load(selection_path.read_text())['agents']['antigravity'],
            'profiles/agents/antigravity/b.yaml',
        )


if __name__ == '__main__':
    unittest.main()
