"""Framework release ownership, combined shared-file writes, and whole-run recovery."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import shutil
import sys
import unittest
from unittest.mock import patch

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from claude_config.core.cutover import Cutover
from claude_config.core.native import parse_native
from claude_config.core.native_plugins import PluginOperation
from claude_config.core.recovery import rollback_plan
from claude_config.core.schema import SchemaError
from claude_config.core.transaction import FilePlan, PlanStale, atomic_write
import test_profile_plugins as plugin_fixture
from test_profile_resources import ResourceFixture, run_git


class FakeAppProbe:
    running = False

    def check(self, *args):
        return [{'name': 'app_interface', 'status': 'passed'}]

    def assert_stopped(self, *args):
        if self.running:
            raise PlanStale('Isolated App is running')


class CutoverFixture(ResourceFixture):
    def setUp(self):
        super().setUp()
        self.profile['agent_type'] = 'codex'
        self.profile['settings'] = {'model': 'new-model'}
        self.machine['agents'] = {}
        self.save()
        self.save_profile()
        self.tool = self.root / 'installed'
        self.target_data = self.root / 'target-data'
        self.entry = self.root / 'skills/claude-config/SKILL.md'
        self.spec_path = self.root / 'cutover.yaml'
        self.spec = {'schema_version': 2, 'machine': str(self.machine_path), 'machine_target': str(self.tool / 'machine.yaml'),
                     'data_target': str(self.target_data)}
        self.app_probe = FakeAppProbe()
        self.installer = plugin_fixture.FakeInstaller()
        self.patch = patch.object(PluginOperation, '_call', lambda operation, args, work: self.installer(operation, args, work))
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.save_spec()

    def save_spec(self):
        self.spec_path.write_text(yaml.safe_dump(self.spec))

    def coordinator(self):
        return Cutover(self.spec_path, agent_probe=lambda *args: [{'name': 'isolated-native', 'status': 'passed'}], app_probe=self.app_probe)

    def add_app(self):
        self.machine['apps']['codex-app'] = {'profile': 'profiles/apps/codex-app/work.yaml',
                                          'binding': {'application': str(self.root / 'test.app'), 'data_root': str(self.native), 'backend': str(self.binary)}}
        path = self.data / 'profiles/apps/codex-app/work.yaml'
        path.parent.mkdir(parents=True)
        path.write_text(yaml.safe_dump({'schema_version': 1, 'app': 'codex-app', 'settings': {'localeOverride': 'en-US'}}))
        self.save_profile()


class FrameworkCutoverTests(CutoverFixture):
    def test_combined_plan_validates_only_the_final_file_plan(self):
        self.add_app()
        calls = []
        original = FilePlan._validate_all
        def validate(plan):
            calls.append(plan)
            return original(plan)
        with patch.object(FilePlan, '_validate_all', validate):
            plan = self.coordinator().plan()
        self.assertEqual(calls, [plan])
        self.assertTrue(plan.report['ready'], plan.report['blockers'])

    def test_source_drift_before_native_stage_blocks_install_and_restores_prior_files(self):
        package = self.add('tiny', 'plugin', {'.codex-plugin/plugin.json': json.dumps({'name': 'tiny', 'version': '1.0.0'}),
                                             'body.txt': 'reviewed'}, '.codex-plugin/plugin.json')
        self.save()
        plan = self.coordinator().plan()
        self.assertTrue(plan.report['ready'], plan.report['blockers'])
        changed = False
        def writer(path, content, mode):
            nonlocal changed
            atomic_write(path, content, mode)
            if not changed:
                (package / 'body.txt').write_text('later source change')
                changed = True
        with self.assertRaises(PlanStale):
            plan.apply(self.state, writer=writer)
        self.assertFalse(self.installer.calls)
        self.assertFalse((self.target_data / 'catalogs.yaml').exists())
        self.assertFalse((self.native / 'config.toml').exists())
        self.assertEqual((package / 'body.txt').read_text(), 'later source change')

    def test_source_drift_after_native_stage_restores_the_installed_plugin(self):
        package = self.add('tiny', 'plugin', {'.codex-plugin/plugin.json': json.dumps({'name': 'tiny', 'version': '1.0.0'}),
                                             'body.txt': 'reviewed'}, '.codex-plugin/plugin.json')
        self.save()
        plan = self.coordinator().plan()
        self.assertTrue(plan.report['ready'], plan.report['blockers'])
        def change_source(operation, args):
            (package / 'body.txt').write_text('later source change')
        self.installer.after = change_source
        with self.assertRaises(PlanStale):
            plan.apply(self.state)
        native = [row for row in plan.report['results'] if row.get('kind') == 'native-plugin']
        self.assertEqual(len(native), 1)
        self.assertEqual(native[0]['status'], 'rolled_back')
        self.assertFalse((self.native / 'config.toml').exists())
        self.assertEqual((package / 'body.txt').read_text(), 'later source change')

    def test_preview_readonly_install_and_whole_run_rollback(self):
        manager = self.coordinator()
        plan = manager.plan()
        self.assertTrue(plan.report['ready'], plan.report['blockers'])
        self.assertFalse(self.tool.exists())
        self.assertFalse(self.target_data.exists())
        plan.apply(self.state)
        self.assertEqual(parse_native('codex', (self.native / 'config.toml').read_bytes())['model'], 'new-model')
        installed = yaml.safe_load((self.tool / 'machine.yaml').read_text())
        self.assertEqual(installed['config_dir'], str(self.target_data))
        self.assertNotIn('framework_commit',installed.get('runtime',{}))
        self.assertFalse(self.entry.exists())
        rollback = rollback_plan(self.coordinator(), Path(plan.report['recovery']).parent.name)
        self.assertTrue(rollback.report['ready'], rollback.report['blockers'])
        rollback.apply(self.state)
        self.assertFalse((self.tool / 'SKILL.md').exists())
        self.assertFalse(self.entry.exists())
        self.assertFalse((self.native / 'config.toml').exists())

    def test_agent_and_app_native_changes_are_combined_without_losing_fields(self):
        self.add_app()
        self.native.mkdir()
        original = b'model = "old-model"\n# keep comment\n[desktop]\nlocaleOverride = "zh-CN"\nunknownLocal = "preserve"\n'
        (self.native / 'config.toml').write_bytes(original)
        manager = self.coordinator()
        plan = manager.plan()
        self.assertTrue(plan.report['ready'], plan.report['blockers'])
        writes = [change for change in plan.changes if change.path == self.native / 'config.toml']
        self.assertEqual(len(writes), 1)
        plan.apply(self.state)
        native = parse_native('codex', (self.native / 'config.toml').read_bytes())
        self.assertEqual(native['model'], 'new-model')
        self.assertEqual(native['desktop']['localeOverride'], 'en-US')
        self.assertEqual(native['desktop']['unknownLocal'], 'preserve')
        rollback = rollback_plan(self.coordinator(), Path(plan.report['recovery']).parent.name)
        rollback.apply(self.state)
        self.assertEqual((self.native / 'config.toml').read_bytes(), original)

    def test_late_app_open_blocks_all_writes(self):
        self.add_app()
        plan = self.coordinator().plan()
        self.assertTrue(plan.report['ready'])
        self.app_probe.running = True
        with self.assertRaises(PlanStale):
            plan.apply(self.state)
        self.assertFalse(self.tool.exists())
        self.assertFalse((self.native / 'config.toml').exists())

    def test_open_app_preview_is_blocked(self):
        self.add_app()
        self.app_probe.running = True
        # If a child needs a native preference write, it refuses during planning.
        plan = self.coordinator().plan()
        self.assertFalse(plan.report['ready'])
        with self.assertRaises(ValueError):
            plan.apply(self.state)

    def test_existing_new_data_difference_is_not_overwritten(self):
        self.target_data.mkdir()
        (self.target_data / 'catalogs.yaml').write_text('user-owned difference')
        with self.assertRaises(SchemaError):
            self.coordinator().plan()




    def test_legacy_declarations_retired_and_recoverable_without_legacy_parsing(self):
        self.target_data.mkdir()
        old = b'opaque old declarations\n'
        (self.target_data / 'agents.yaml').write_bytes(old)
        self.spec['migrate_existing'] = True
        self.save_spec()
        plan = self.coordinator().plan()
        plan.apply(self.state)
        self.assertFalse((self.target_data / 'agents.yaml').exists())
        rollback = rollback_plan(self.coordinator(), Path(plan.report['recovery']).parent.name)
        rollback.apply(self.state)
        self.assertEqual((self.target_data / 'agents.yaml').read_bytes(), old)

    def test_failure_in_last_bootstrap_write_restores_native_and_data_changes(self):
        plan = self.coordinator().plan()
        def writer(path, content, mode):
            if path == self.tool / 'machine.yaml':
                raise OSError('isolated finalization failure')
            atomic_write(path, content, mode)
        with self.assertRaises(OSError):
            plan.apply(self.state, writer=writer)
        self.assertFalse(self.entry.exists())
        self.assertFalse((self.native / 'config.toml').exists())
        self.assertFalse((self.target_data / 'catalogs.yaml').exists())

    def test_mixed_agent_plugin_recovery_uses_each_exact_binding(self):
        # Add a second host while keeping the original distinct native root.
        entry = '.codex-plugin/plugin.json'
        self.add('tiny', 'plugin', {entry: json.dumps({'name': 'tiny', 'version': '1.0.0'}), 'body': 'isolated'}, entry)
        self.save()
        cc_root = self.root / 'claude-native'
        self.machine['agents']['claude-code'] = {'binding': {'executable': str(self.binary), 'config_root': str(cc_root)},
                                               'profile': 'profiles/agents/claude-code/other.yaml'}
        path = self.data / 'profiles/agents/claude-code/other.yaml'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(yaml.safe_dump({'schema_version': 1, 'agent_type': 'claude-code', 'settings': {'model': 'selected'}}))
        self.machine_path.write_text(yaml.safe_dump(self.machine))
        self.save_spec()
        plan = self.coordinator().plan()
        self.assertTrue(plan.report['ready'], plan.report['blockers'])
        plan.apply(self.state)
        rollback = rollback_plan(self.coordinator(), Path(plan.report['recovery']).parent.name)
        self.assertTrue(rollback.report['ready'], rollback.report['blockers'])
        rollback.apply(self.state)
        self.assertFalse((cc_root / 'settings.json').exists())
        self.assertFalse((self.native / 'config.toml').exists())

    def test_rollback_preserves_post_install_user_changes(self):
        plan = self.coordinator().plan()
        plan.apply(self.state)
        (self.native / 'config.toml').write_text('model = "later-user-choice"\n')
        rollback = rollback_plan(self.coordinator(), Path(plan.report['recovery']).parent.name)
        self.assertFalse(rollback.report['ready'])
        with self.assertRaises(ValueError):
            rollback.apply(self.state)

    def test_app_reopens_during_execution_preserves_changes_until_explicit_recovery(self):
        self.add_app()
        plan = self.coordinator().plan()
        def writer(path, content, mode):
            atomic_write(path, content, mode)
            if path == self.native / 'config.toml':
                self.app_probe.running = True
        with self.assertRaises(PlanStale):
            plan.apply(self.state, writer=writer)
        self.assertTrue((self.native / 'config.toml').exists())
        self.assertFalse(self.tool.exists())
        self.app_probe.running = False
        rollback = rollback_plan(self.coordinator(), Path(plan.report['recovery']).parent.name)
        self.assertTrue(rollback.report['ready'], rollback.report['blockers'])
        rollback.apply(self.state)
        self.assertFalse((self.native / 'config.toml').exists())




if __name__ == '__main__':
    unittest.main()
