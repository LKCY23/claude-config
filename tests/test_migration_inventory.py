from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from claude_config import migration as migration_config
from claude_config.core.migration_inventory import inventory


class InventoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.data = self.root / 'data'
        self.data.mkdir()
        (self.data / 'prefs.json').write_text('{"model":"selected", "effortLevel":"high", "api_key":"PRIVATE"}')
        self.agents = {'version': 1, 'catalogs': {'team': {'repo': 'owner/repo', 'path': 'catalog.json'}},
                       'agents': {'claude': {'plugins': ['review@team'], 'config': {'source': 'prefs.json'}}}}
        self.save()
    def tearDown(self): self.temp.cleanup()
    def save(self): (self.data / 'agents.yaml').write_text(yaml.safe_dump(self.agents))

    def test_active_sources_inventory_is_read_only_and_never_exports_values(self):
        before = {path: path.read_bytes() for path in self.data.iterdir()}
        report = inventory(self.data)
        self.assertTrue(report['ready'])
        self.assertFalse(report['deployment_ready'])
        rows = report['migration_inventory']['agent_settings']
        self.assertEqual({tuple(row['path']): row['status'] for row in rows}, {('model',): 'supported', ('effortLevel',): 'supported', ('api_key',): 'excluded'})
        self.assertNotIn('PRIVATE', json.dumps(report))
        for path, content in before.items(): self.assertEqual(path.read_bytes(), content)

    def test_legacy_base_does_not_override_active_agent_selection(self):
        (self.data / 'base.json').write_text('{"model":"older", "includeCoAuthoredBy":false}')
        (self.data / 'manifest.yaml').write_text(yaml.safe_dump({'version': 1, 'settings': {'base': {'source': 'base.json'}}}))
        report = inventory(self.data)
        rows = report['migration_inventory']['agent_settings']
        self.assertTrue(any(row['status'] == 'shadowed_legacy_setting' for row in rows))
        self.assertTrue(any(row['status'] == 'needs_semantic_conversion' for row in rows))

    def test_archived_plugin_list_and_old_v2_export_are_not_loaded(self):
        (self.data / 'plugins.yaml').write_text('secret: PRIVATE\ninvalid: [')
        (self.data / 'agents.v2.candidate.yaml').write_text('version: 2\n')
        report = inventory(self.data)
        self.assertTrue(report['ready'])
        self.assertEqual(len(report['migration_inventory']['plugins']), 1)
        self.assertNotIn('PRIVATE', json.dumps(report))

    def test_missing_source_and_unknown_agent_fields_are_blockers(self):
        self.agents['agents']['claude']['config']['source'] = 'missing.json'
        self.agents['agents']['claude']['unknown'] = 'PRIVATE'
        self.save()
        report = inventory(self.data)
        self.assertFalse(report['ready'])
        self.assertNotIn('PRIVATE', json.dumps(report))
        self.assertEqual(len(report['blockers']), 2)

    def test_external_symlink_is_recorded_without_following_private_content(self):
        external = self.root / 'private.json'
        external.write_text('PRIVATE CONTENT')
        (self.data / 'outside.json').symlink_to(external)
        self.agents['agents']['claude']['config']['source'] = 'outside.json'
        self.save()
        report = inventory(self.data)
        self.assertEqual(report['migration_inventory']['assets'][0]['status'], 'external_symlink_needs_binding')
        self.assertNotIn('PRIVATE', json.dumps(report))

    def test_duplicate_or_undeclared_plugin_catalog_is_blocked(self):
        for values in (['review@missing'], ['review@team', 'review@team'], 'review@team'):
            self.agents['agents']['claude']['plugins'] = values
            self.save()
            self.assertFalse(inventory(self.data)['ready'])

    def test_unknown_manifest_section_cannot_be_silently_discarded(self):
        (self.data / 'manifest.yaml').write_text('version: 1\nunknown: PRIVATE\n')
        report = inventory(self.data)
        self.assertFalse(report['ready'])
        self.assertNotIn('PRIVATE', json.dumps(report))

    def test_excluded_app_font_is_recorded_without_becoming_a_pending_adapter(self):
        (self.data / 'desktop.json').write_text('{"userThemeMode":"dark","chatFont":"PRIVATE"}')
        (self.data / 'manifest.yaml').write_text(yaml.safe_dump({'version': 1, 'archives': {'claude_desktop': {'mac': {'source': 'desktop.json'}}}}))
        report = inventory(self.data)
        self.assertTrue(report['ready'])
        rows = {tuple(row['path']):row['status'] for row in report['migration_inventory']['app_settings']}
        self.assertEqual(rows[('userThemeMode',)], 'supported')
        self.assertEqual(rows[('chatFont',)], 'excluded')
        self.assertNotIn('PRIVATE', json.dumps(report))

    def test_cli_saves_standard_json_and_does_not_create_native_or_profiles(self):
        output = io.StringIO()
        with redirect_stdout(output):
            code = migration_config.main(['inventory', '--data-dir', str(self.data), '--state-dir', str(self.root / 'state'), '--format', 'json'])
        report = json.loads(output.getvalue())
        self.assertEqual(code, 0)
        self.assertTrue(report['artifacts']['saved'])
        self.assertEqual(json.loads(Path(report['artifacts']['json_path']).read_text()), report)
        self.assertFalse((self.data / 'profiles').exists())
        self.assertNotIn('PRIVATE', output.getvalue())

    def test_cli_argument_failure_uses_json_and_private_fallback(self):
        output = io.StringIO()
        with patch('claude_config.migration.fallback_state_root', return_value=self.root / 'state'), redirect_stdout(output):
            code = migration_config.main(['inventory', '--format=json', '--unknown=PRIVATE'])
        report = json.loads(output.getvalue())
        self.assertEqual(code, 2)
        self.assertTrue(report['artifacts']['saved'])
        self.assertNotIn('PRIVATE', output.getvalue())

    def test_cli_human_summary_uses_inventory_counts_and_standard_check_counts(self):
        output = io.StringIO()
        with redirect_stdout(output):
            code = migration_config.main(['inventory', '--data-dir', str(self.data), '--state-dir', str(self.root / 'state'), '--format', 'text'])
        self.assertEqual(code, 0)
        self.assertIn('对象：配置数据仓库', output.getvalue())
        self.assertIn('1 个插件选择', output.getvalue())
        self.assertIn('类型可转换', output.getvalue())
        self.assertIn('完整 JSON：', output.getvalue())
        self.assertNotIn('PRIVATE', output.getvalue())


if __name__ == '__main__': unittest.main()
