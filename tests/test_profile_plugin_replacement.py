"""Explicit legacy plugin upgrades require a matching reconstructable source."""
import json
from pathlib import Path
import shutil
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from claude_config.core.native_plugins import image_fingerprint, PluginOperation
from claude_config.core.recovery import rollback_plan
from claude_config.core.schema import SchemaError
from claude_config.core.transaction import PlanStale, atomic_write
import test_profile_plugin_adoption as fixtures
import test_profile_plugins as plugins
from test_profile_resources import ResourceFixture


class PluginReplacementTests(ResourceFixture):
    def setUp(self):
        super().setUp()
        self.installer = plugins.FakeInstaller()
        self.patch = patch.object(PluginOperation, '_call', lambda operation, args, work: self.installer(operation, args, work))
        self.patch.start()
        self.addCleanup(self.patch.stop)

    plugin = fixtures.PluginAdoptionTests.plugin
    seed = fixtures.PluginAdoptionTests.seed

    def setup_replacement(self, agent='claude-code'):
        root, entry = self.plugin(agent=agent, enabled=None)
        recovery = self.root / 'reviewed-recovery-source'
        shutil.copytree(root, recovery)
        selector, cache = self.seed(root, entry)
        spec = self.root / 'replacement.json'
        spec.write_text(json.dumps({'schema_version': 1, 'selector': selector, 'version': '1.0.0',
                                    'image': image_fingerprint(recovery), 'source': str(recovery)}))
        manifest = json.loads((root / entry).read_text())
        manifest['version'] = '1.0.0+adapter.1'
        (root / entry).write_text(json.dumps(manifest))
        self.save()
        return root, entry, recovery, spec, cache

    def assert_replacement(self, agent):
        _, _, _, spec, _ = self.setup_replacement(agent)
        plan = self.manager().plan(replace_plugins={'tiny@test': str(spec)})
        self.assertTrue(plan.report['ready'], plan.report['blockers'])
        self.assertTrue(any(row['action'] == 'replace_existing' for row in plan.report['decisions']))
        before = plan.native_operations[0].world()
        plan.apply(self.state)
        operation = plan.native_operations[0]
        receipt = operation.plugins[0]['desired']
        self.assertEqual(receipt['selector'], 'tiny@legacy')
        self.assertEqual(set(receipt['history']), {'1.0.0', '1.0.0+adapter.1'})
        self.assertFalse(operation.native(receipt)['enabled'])
        self.assertTrue(self.manager().plan().report['ready'])
        recovery = rollback_plan(self.manager(), Path(plan.report['recovery']).parent.name)
        self.assertTrue(recovery.report['ready'], recovery.report['blockers'])
        recovery.apply(self.state)
        self.assertEqual(operation.world(), before)

    def test_claude_explicit_replacement_and_recovery(self):
        self.assert_replacement('claude-code')

    def test_codex_explicit_replacement_and_recovery(self):
        self.assert_replacement('codex')

    def test_changed_installed_files_do_not_match_old_source_pin(self):
        _, _, _, spec, cache = self.setup_replacement()
        (cache / 'body.txt').write_text('manual edit')
        plan = self.manager().plan(replace_plugins={'tiny@test': str(spec)})
        self.assertFalse(plan.report['ready'])
        self.assertTrue(any(row.get('code') == 'replacement_target_mismatch' for row in plan.report['blockers']))
        self.assertEqual(self.installer.calls, [])

    def test_same_version_replacement_is_blocked(self):
        root, entry, _, spec, _ = self.setup_replacement()
        manifest = json.loads((root / entry).read_text())
        manifest['version'] = '1.0.0'
        (root / entry).write_text(json.dumps(manifest))
        (root / 'body.txt').write_text('unversioned new body')
        self.save()
        plan = self.manager().plan(replace_plugins={'tiny@test': str(spec)})
        self.assertFalse(plan.report['ready'])

    def test_different_file_modes_cannot_claim_exact_reconstruction(self):
        _, _, recovery, spec, _ = self.setup_replacement()
        (recovery / 'body.txt').chmod(0o600)
        plan = self.manager().plan(replace_plugins={'tiny@test': str(spec)})
        self.assertFalse(plan.report['ready'])
        self.assertTrue(any('file modes' in row.get('reason', '') for row in plan.report['blockers']))

    def test_modified_source_pin_and_native_cache_source_are_rejected(self):
        _, _, recovery, spec, cache = self.setup_replacement()
        (recovery / 'body.txt').write_text('changed')
        with self.assertRaises(SchemaError):
            self.manager().plan(replace_plugins={'tiny@test': str(spec)})
        value = json.loads(spec.read_text())
        value['source'] = str(cache)
        value['image'] = image_fingerprint(cache)
        spec.write_text(json.dumps(value))
        with self.assertRaises(SchemaError):
            self.manager().plan(replace_plugins={'tiny@test': str(spec)})

    def test_adoption_and_replacement_cannot_be_combined_for_the_same_resource(self):
        _, _, _, spec, _ = self.setup_replacement()
        with self.assertRaises(SchemaError):
            self.manager().plan(adopt_plugins={'tiny@test': 'tiny@legacy'}, replace_plugins={'tiny@test': str(spec)})

    def test_late_recovery_source_member_invalidates_the_plan(self):
        _, _, recovery, spec, _ = self.setup_replacement()
        plan = self.manager().plan(replace_plugins={'tiny@test': str(spec)})
        (recovery / 'added.txt').write_text('late member')
        with self.assertRaises(PlanStale):
            plan.apply(self.state)
        self.assertEqual(self.installer.calls, [])

    def test_failure_after_native_upgrade_restores_old_version_and_metadata(self):
        _, _, _, spec, _ = self.setup_replacement('codex')
        plan = self.manager().plan(replace_plugins={'tiny@test': str(spec)})
        before = plan.native_operations[0].world()
        def writer(path, content, mode):
            if path == self.manager().state_path:
                raise OSError('isolated finalization failure')
            atomic_write(path, content, mode)
        with self.assertRaises(OSError):
            plan.apply(self.state, writer=writer)
        self.assertEqual(plan.native_operations[0].world(), before)

    def test_failure_between_codex_registration_steps_does_not_reinstall_old_image(self):
        _, _, _, spec, _ = self.setup_replacement('codex')
        plan = self.manager().plan(replace_plugins={'tiny@test': str(spec)})
        before = plan.native_operations[0].world()
        def fail(operation, args):
            if args[:3] == ['plugin', 'marketplace', 'remove']:
                raise RuntimeError('isolated interrupted source replacement')
        self.installer.after = fail
        with self.assertRaises(RuntimeError):
            plan.apply(self.state)
        self.assertEqual(plan.native_operations[0].world(), before)
        self.assertEqual([args[1] for args in self.installer.calls], ['marketplace'])

    def test_restore_does_not_enable_an_already_enabled_old_installation(self):
        _, _, _, spec, _ = self.setup_replacement()
        path = self.manager().native_path
        native = json.loads(path.read_text())
        native['enabledPlugins']['tiny@legacy'] = True
        path.write_text(json.dumps(native))
        plan = self.manager().plan(replace_plugins={'tiny@test': str(spec)})
        self.assertTrue(plan.report['ready'], plan.report['blockers'])
        plan.apply(self.state)
        self.installer.calls.clear()
        rollback = rollback_plan(self.manager(), Path(plan.report['recovery']).parent.name)
        rollback.apply(self.state)
        self.assertFalse(any(args[1] == 'enable' for args in self.installer.calls))

    def test_existing_installation_in_use_blocks_upgrade_even_after_preview(self):
        _, _, _, spec, cache = self.setup_replacement()
        plan = self.manager().plan(replace_plugins={'tiny@test': str(spec)})
        (cache / '.in_use').mkdir()
        with self.assertRaises(ValueError):
            plan.apply(self.state)
        self.assertFalse(any(args[1] in ('install', 'update', 'add') for args in self.installer.calls))


if __name__ == '__main__':
    unittest.main()
