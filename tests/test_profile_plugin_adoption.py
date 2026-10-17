"""Exact existing plugin adoption, collision prevention and guarded recovery."""
import json
import io
from pathlib import Path
import shutil
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from claude_config.core.native import parse_native, render_native, snapshot_read
from claude_config.core.native_plugins import cache_version, PluginOperation
from claude_config.core.recovery import rollback_plan
from claude_config.core.transaction import PlanStale, atomic_write
import test_profile_plugins as fixtures
from claude_config import profile as profile_config
from test_profile_resources import ResourceFixture


class PluginAdoptionTests(ResourceFixture):
    plugin = fixtures.PluginTests.plugin
    deploy = fixtures.PluginTests.deploy

    def setUp(self):
        super().setUp()
        self.installer = fixtures.FakeInstaller()
        self.patch = patch.object(PluginOperation, '_call', lambda operation, args, work: self.installer(operation, args, work))
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def seed(self, source, entry, market='legacy', enabled=False, scope='user'):
        name = json.loads((source / entry).read_text())['name']
        version = json.loads((source / entry).read_text())['version']
        selector = name + '@' + market
        legacy = self.root / 'existing-source' / market
        legacy.mkdir(parents=True, exist_ok=True)
        cache = self.native / 'plugins/cache' / market / name / cache_version(self.profile['agent_type'], version)
        shutil.copytree(source, cache)
        path = self.native / ('settings.json' if self.profile['agent_type'] == 'claude-code' else 'config.toml')
        content, _ = snapshot_read(path)
        config = parse_native(self.profile['agent_type'], content)
        if self.profile['agent_type'] == 'claude-code':
            known_path = self.native / 'plugins/known_marketplaces.json'
            known = json.loads(known_path.read_text()) if known_path.exists() else {}
            known[market] = {'source': {'source': 'directory', 'path': str(legacy)}, 'installLocation': str(legacy)}
            atomic_write(known_path, json.dumps(known).encode())
            inventory_path = self.native / 'plugins/installed_plugins.json'
            inventory = json.loads(inventory_path.read_text()) if inventory_path.exists() else {'version': 2, 'plugins': {}}
            inventory['plugins'][selector] = [{'scope': scope, 'version': version, 'installPath': str(cache)}]
            atomic_write(inventory_path, json.dumps(inventory).encode())
            config.setdefault('extraKnownMarketplaces', {})[market] = {'source': {'source': 'directory', 'path': str(legacy)}}
            config.setdefault('enabledPlugins', {})[selector] = enabled
        else:
            config.setdefault('marketplaces', {})[market] = {'source_type': 'local', 'source': str(legacy)}
            config.setdefault('plugins', {})[selector] = {'enabled': enabled}
        config['unrelated'] = 'retained'
        atomic_write(path, render_native(self.profile['agent_type'], content, config))
        return selector, cache

    def exact(self, agent='claude-code', version='1.0.0'):
        root, entry = self.plugin(agent=agent)
        manifest = json.loads((root / entry).read_text())
        manifest['version'] = version
        (root / entry).write_text(json.dumps(manifest))
        self.save()
        selector, cache = self.seed(root, entry)
        return root, entry, selector, cache

    def assert_exact_adoption(self, agent):
        _, _, selector, cache = self.exact(agent)
        before = {path.relative_to(cache): path.read_bytes() for path in cache.rglob('*') if path.is_file()}
        plan = self.manager().plan(adopt_plugins={'tiny@test': selector})
        self.assertTrue(plan.report['ready'], plan.report['blockers'])
        self.assertEqual(self.installer.calls, [])
        self.assertTrue(any(row['action'] == 'adopt_existing' for row in plan.report['decisions']))
        plan.apply(self.state)
        expected = ['marketplace'] if agent == 'claude-code' else ['marketplace', 'marketplace']
        self.assertEqual([args[1] for args in self.installer.calls], expected)
        receipt = plan.native_operations[0].plugins[0]['desired']
        self.assertEqual(receipt['selector'], selector)
        self.assertFalse(receipt['enabled'])
        self.assertEqual(before, {path.relative_to(cache): path.read_bytes() for path in cache.rglob('*') if path.is_file()})
        self.installer.calls.clear()
        self.deploy()
        self.assertEqual(self.installer.calls, [])
        self.assertEqual(parse_native(agent, self.manager().native_path.read_bytes())['unrelated'], 'retained')

    def test_claude_exact_adoption(self):
        self.assert_exact_adoption('claude-code')

    def test_codex_exact_adoption(self):
        self.assert_exact_adoption('codex')

    def test_duplicate_without_explicit_adoption_is_blocked(self):
        self.exact()
        plan = self.manager().plan()
        self.assertFalse(plan.report['ready'])
        self.assertTrue(any('duplicate' in row.get('reason', '') for row in plan.report['blockers']))

    def test_different_files_or_version_cannot_be_adopted_as_an_upgrade(self):
        root, entry, selector, cache = self.exact()
        (cache / 'body.txt').write_text('local modification')
        plan = self.manager().plan(adopt_plugins={'tiny@test': selector})
        self.assertFalse(plan.report['ready'])
        self.assertTrue(any(row.get('code') == 'adoption_content_mismatch' for row in plan.report['blockers']))
        with self.assertRaises(ValueError):
            plan.apply(self.state)
        self.assertEqual(self.installer.calls, [])

    def test_partial_marketplace_is_blocked(self):
        root, entry, selector, _ = self.exact()
        other, other_entry = self.plugin('other')
        self.save()
        self.seed(other, other_entry)
        self.profile['resources']['plugins'].pop('other@test')
        self.save_profile()
        plan = self.manager().plan(adopt_plugins={'tiny@test': selector})
        self.assertFalse(plan.report['ready'])
        self.assertTrue(any('unselected' in row.get('reason', '') for row in plan.report['blockers']))

    def test_project_scope_is_not_adopted(self):
        _, _, selector, _ = self.exact()
        path = self.native / 'plugins/installed_plugins.json'
        inventory = json.loads(path.read_text())
        inventory['plugins'][selector].append({**inventory['plugins'][selector][0], 'scope': 'project'})
        path.write_text(json.dumps(inventory))
        plan = self.manager().plan(adopt_plugins={'tiny@test': selector})
        self.assertFalse(plan.report['ready'])
        self.assertTrue(any('project-scope' in row.get('reason', '') for row in plan.report['blockers']))

    def test_multiple_plugins_can_adopt_a_shared_marketplace(self):
        _, _, selector, _ = self.exact()
        source, entry = self.plugin('other')
        self.save()
        second, _ = self.seed(source, entry)
        plan = self.manager().plan(adopt_plugins={'tiny@test': selector, 'other@test': second})
        self.assertTrue(plan.report['ready'], plan.report['blockers'])
        plan.apply(self.state)
        self.assertEqual([args[1] for args in self.installer.calls], ['marketplace'])

    def test_unrelated_same_name_marketplace_is_preserved_after_explicit_adoption(self):
        source, entry, selector, _ = self.exact()
        self.seed(source, entry, market='unmanaged')
        plan = self.manager().plan(adopt_plugins={'tiny@test': selector})
        self.assertTrue(plan.report['ready'], plan.report['blockers'])
        plan.apply(self.state)
        self.assertTrue((self.native / 'plugins/cache/unmanaged/tiny/1.0.0').exists())
        self.assertTrue(self.manager().plan().report['ready'])

    def test_late_cache_change_invalidates_reviewed_adoption(self):
        _, _, selector, cache = self.exact()
        plan = self.manager().plan(adopt_plugins={'tiny@test': selector})
        (cache / 'body.txt').write_text('late edit')
        with self.assertRaises(PlanStale):
            plan.apply(self.state)
        self.assertEqual(self.installer.calls, [])

    def test_historical_unowned_image_is_retained_and_not_repackaged(self):
        root, entry, selector, cache = self.exact()
        historical = cache.parent / '0.3.0'
        shutil.copytree(cache, historical)
        (historical / 'body.txt').write_text('unowned old body')
        plan = self.manager().plan(adopt_plugins={'tiny@test': selector})
        self.assertTrue(plan.report['ready'], plan.report['blockers'])
        before = {path.relative_to(historical): path.read_bytes() for path in historical.rglob('*') if path.is_file()}
        plan.apply(self.state)
        self.assertEqual(before, {path.relative_to(historical): path.read_bytes() for path in historical.rglob('*') if path.is_file()})
        receipt = plan.native_operations[0].plugins[0]['desired']
        self.assertEqual(receipt['retained_versions'], ['0.3.0'])
        self.assertNotIn('0.3.0', receipt['history'])
        self.assertTrue(self.manager().plan().report['ready'])
        document = json.loads((root / entry).read_text())
        document['version'] = '1.1.0'
        (root / entry).write_text(json.dumps(document))
        self.save()
        self.assertFalse(self.manager().plan().report['ready'])
        shutil.rmtree(historical)  # Simulates separately reviewed native cleanup.
        self.assertTrue(self.manager().plan().report['ready'])

    def test_retained_symlinks_survive_adoption_and_explicit_rollback(self):
        _, _, selector, cache = self.exact()
        historical = cache.parent / '0.3.0'
        historical.mkdir()
        outside = self.root / 'unowned-script'
        outside.write_text('leave untouched')
        (historical / 'command').symlink_to(outside)
        (historical / 'dangling').symlink_to('../missing')
        plan = self.manager().plan(adopt_plugins={'tiny@test': selector})
        self.assertTrue(plan.report['ready'], plan.report['blockers'])
        before = plan.native_operations[0].world()
        plan.apply(self.state)
        recovery = rollback_plan(self.manager(), Path(plan.report['recovery']).parent.name)
        self.assertTrue(recovery.report['ready'], recovery.report['blockers'])
        recovery.apply(self.state)
        self.assertEqual(plan.native_operations[0].world(), before)
        self.assertEqual(outside.read_text(), 'leave untouched')
        self.assertTrue((historical / 'dangling').is_symlink())

    def test_installer_mutating_retained_symlink_is_still_blocked(self):
        _, _, selector, cache = self.exact()
        historical = cache.parent / '0.3.0'
        historical.mkdir()
        link = historical / 'command'
        link.symlink_to('original')
        plan = self.manager().plan(adopt_plugins={'tiny@test': selector})
        def mutate(operation, args):
            link.unlink()
            link.symlink_to('changed')
        self.installer.after = mutate
        with self.assertRaises(ValueError):
            plan.apply(self.state)
        journal = json.loads(Path(plan.report['recovery']).read_text())
        native = next(row for row in journal['operations'] if row.get('kind') == 'native-plugin')
        self.assertFalse(native['safe_recovery'])
        self.assertEqual(link.readlink(), Path('changed'))
        self.assertTrue(Path(plan.native_operations[0].plugins[0]['desired']['manifest']).exists())

    def test_source_removal_failure_restores_codex_without_reinstall(self):
        _, _, selector, _ = self.exact('codex')
        plan = self.manager().plan(adopt_plugins={'tiny@test': selector})
        before = plan.native_operations[0].world()
        def fail(operation, args):
            if args[:3] == ['plugin', 'marketplace', 'remove']:
                raise RuntimeError('isolated failure between source removal and addition')
        self.installer.after = fail
        with self.assertRaises(RuntimeError):
            plan.apply(self.state)
        self.assertEqual(plan.native_operations[0].world(), before)
        self.assertEqual([args[1] for args in self.installer.calls], ['marketplace'])

    def test_plus_version_adoption_rollback_restores_registration_without_reinstall(self):
        _, _, selector, _ = self.exact(version='0.1.0+packaging.2')
        plan = self.manager().plan(adopt_plugins={'tiny@test': selector})
        self.assertTrue(plan.report['ready'], plan.report['blockers'])
        before = plan.native_operations[0].world()
        plan.apply(self.state)
        self.installer.calls.clear()
        recovery = rollback_plan(self.manager(), Path(plan.report['recovery']).parent.name)
        self.assertTrue(recovery.report['ready'], recovery.report['blockers'])
        recovery.apply(self.state)
        self.assertEqual(plan.native_operations[0].world(), before)
        self.assertEqual(self.installer.calls, [])

    def test_adoption_failure_restores_original_metadata(self):
        _, _, selector, _ = self.exact()
        plan = self.manager().plan(adopt_plugins={'tiny@test': selector})
        before = plan.native_operations[0].world()
        def writer(path, content, mode):
            if path == self.manager().state_path:
                raise OSError('isolated finalization failure')
            atomic_write(path, content, mode)
        with self.assertRaises(OSError):
            plan.apply(self.state, writer=writer)
        self.assertEqual(plan.native_operations[0].world(), before)
        self.assertFalse(self.manager().state_path.exists())

    def test_export_uses_adopted_identity_instead_of_creating_a_second_selector(self):
        _, _, selector, _ = self.exact()
        plan = self.manager().plan(adopt_plugins={'tiny@test': selector})
        plan.apply(self.state)
        path = self.manager().native_path
        config = json.loads(path.read_text())
        config['enabledPlugins'][selector] = True
        path.write_text(json.dumps(config))
        export = self.manager().export([], resource_refs=['tiny@test'])
        self.assertTrue(export.report['ready'], export.report['blockers'])
        export.apply(self.state)
        _, profile, *_ = self.manager()._profile()
        self.assertTrue(profile['resources']['plugins']['tiny@test']['options']['enabled'])

    def test_cli_adoption_preview_saves_report_without_native_changes(self):
        _, _, selector, _ = self.exact()
        original = self.manager().native_path.read_bytes()
        with patch('claude_config.core.manager.NativeProbe', return_value=lambda *args: [{'name': 'isolated-native', 'status': 'passed'}]), patch('sys.stdout', new=io.StringIO()) as output:
            result = profile_config.main(['plan', '--machine', str(self.machine_path), '--agent', 'claude-code',
                                          '--adopt-plugin', 'tiny@test=' + selector, '--format', 'json'])
        self.assertEqual(result, 0, output.getvalue())
        report = json.loads(output.getvalue())
        self.assertTrue(report['artifacts']['saved'])
        self.assertEqual(original, self.manager().native_path.read_bytes())
        self.assertFalse(self.manager().state_path.exists())
        self.assertEqual(self.installer.calls, [])


if __name__ == '__main__':
    unittest.main()
