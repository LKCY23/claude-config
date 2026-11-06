"""App declaration API tests; all files are synthetic and native access is forbidden."""
from contextlib import redirect_stdout, redirect_stderr
from copy import deepcopy
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from claude_config.app_profile import APP, SelectedDeclaration, declarations
from claude_config.cli import main
from claude_config.core.app_fields import declaration_schema, registry, validate_settings
from claude_config.core.app_manager import AppProfileManager
from claude_config.core.native import snapshot_read
from claude_config.core.recovery import rollback_plan
from claude_config.core.schema import SchemaError, within
from claude_config.core.transaction import PlanStale, atomic_write


class AppProfileApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.data, self.state = self.root / 'data', self.root / 'state'
        self.native = self.root / 'native'
        self.native.mkdir()
        self.native_file = self.native / 'config.json'
        self.native_file.write_text('{"credential":"NATIVE_SECRET_SENTINEL"}')
        self.name = 'profiles/apps/claude-desktop/work.yaml'
        self.profile = self.data / self.name
        self.profile.parent.mkdir(parents=True)
        self.value = {'schema_version': 1, 'app': APP, 'description': 'PROFILE_DESCRIPTION_PRIVATE',
                      'settings': {'userThemeMode': 'light', 'locale': 'en-US', 'autoModeEnabled': True}}
        self.profile.write_text(yaml.safe_dump(self.value, sort_keys=False))
        self.machine_file = self.root / 'machine.yaml'
        self.machine = {'schema_version': 1, 'machine_id': 'fixture', 'config_dir': str(self.data),
                        'catalog_roots': {}, 'agents': {}, 'apps': {APP: {'profile': self.name,
                        'binding': {'application': str(self.root / 'Claude.app'), 'data_root': str(self.native)}}},
                        'bindings': {'secrets': {'fixture': {'provider': 'native', 'ref': 'PRIVATE_SECRET_REFERENCE'}}},
                        'runtime': {'state_dir': str(self.state)}}
        self.save_machine()
        self.patch_file = self.root / 'patch.json'

    def save_machine(self):
        self.machine_file.write_text(yaml.safe_dump(self.machine, sort_keys=False))

    def patch_file_for(self, set_values=None, unset=None):
        value = {'schema_version': 1}
        if set_values is not None:
            value['set'] = set_values
        if unset is not None:
            value['unset'] = unset
        self.patch_file.write_text(json.dumps(value))
        return self.patch_file

    def invoke(self, action, *options):
        args = ['app-profile', action, '--app', APP, '--format', 'json']
        if action != 'schema':
            args += ['--machine', str(self.machine_file)]
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(args + list(options))
        return code, json.loads(out.getvalue()), err.getvalue()

    def preview(self, set_values=None, unset=None):
        self.patch_file_for(set_values, unset)
        code, report, _ = self.invoke('edit', '--patch', str(self.patch_file))
        self.assertEqual(code, 0, report)
        return report

    def write(self, plan_id):
        return self.invoke('edit', '--patch', str(self.patch_file), '--write', '--expect-plan-id', plan_id)

    def externalize(self):
        selection = {'schema_version': 1, 'machine_id': 'fixture', 'agents': {}, 'apps': {APP: self.name}}
        del self.machine['apps'][APP]['profile']
        self.machine['profile_selection'] = 'machines/fixture.yaml'
        path = self.data / 'machines/fixture.yaml'
        path.parent.mkdir(); path.write_text(yaml.safe_dump(selection, sort_keys=False)); self.save_machine()
        return path, selection

    def test_schema_requires_no_machine_and_uses_exact_field_authority(self):
        with patch('claude_config.app_profile.AppProfileManager', side_effect=AssertionError('must not read Machine')):
            code, report, _ = self.invoke('schema')
        self.assertEqual(code, 0)
        self.assertEqual(report['capability'], 'app-profile-declaration-v1')
        self.assertIsNone(report['target'])
        self.assertEqual(set(report['settings_schema']['fields']), set(registry(APP)))
        self.assertEqual(len(report['settings_schema']['fields']), 6)
        self.assertEqual(report['settings_schema']['fields']['desktopHome']['enum'], ['standard', 'off'])
        self.assertEqual(report['settings_schema'], declaration_schema(APP))
        self.assertFalse(self.state.exists())

    def test_inspect_projects_only_selected_five_fields_and_reads_no_native(self):
        before = self.native_file.read_bytes()
        with (patch.object(AppProfileManager, '_native', side_effect=AssertionError('native access')),
              patch.object(AppProfileManager, '_state', side_effect=AssertionError('native state access'))):
            code, report, stderr = self.invoke('inspect')
        self.assertEqual(code, 0)
        self.assertEqual(report['declarations']['autoModeEnabled'], {'declared': True, 'value': True})
        self.assertEqual(report['declarations']['chatTabEnabled'], {'declared': False})
        self.assertEqual(report['target']['profile'], self.name)
        self.assertEqual(report['target']['source_kind'], 'embedded')
        self.assertRegex(report['revision'], '^[0-9a-f]{64}$')
        output = json.dumps(report) + stderr
        for secret in ['NATIVE_SECRET_SENTINEL', 'PROFILE_DESCRIPTION_PRIVATE', 'PRIVATE_SECRET_REFERENCE']:
            self.assertNotIn(secret, output)
        self.assertEqual(self.native_file.read_bytes(), before)
        self.assertFalse(self.state.exists())

    def test_repository_selection_is_authoritative_and_never_switched(self):
        selection_path, selection = self.externalize()
        other = self.profile.parent / 'other.yaml'
        other.write_text(yaml.safe_dump({'schema_version': 1, 'app': APP, 'settings': {'locale': 'zh-CN'}}))
        selection['apps'][APP] = str(other.relative_to(self.data))
        selection_path.write_text(yaml.safe_dump(selection))
        before_machine, before_selection = self.machine_file.read_bytes(), selection_path.read_bytes()
        code, inspected, _ = self.invoke('inspect')
        self.assertEqual(code, 0)
        self.assertEqual(inspected['target']['profile_file'], str(other.resolve()))
        self.assertEqual(inspected['target']['source_kind'], 'repository')
        self.assertEqual(inspected['target']['profile_selection_file'], str(selection_path))
        preview = self.preview({'userThemeMode': 'dark'})
        code, saved, _ = self.write(preview['plan_id'])
        self.assertEqual(code, 0, saved)
        self.assertEqual(self.machine_file.read_bytes(), before_machine)
        self.assertEqual(selection_path.read_bytes(), before_selection)
        self.assertEqual(yaml.safe_load(other.read_text())['settings'], {'locale': 'zh-CN', 'userThemeMode': 'dark'})

    def test_preview_then_write_has_same_plan_changes_backup_and_separate_native_apply(self):
        original, native = self.profile.read_bytes(), self.native_file.read_bytes()
        code, inspected, _ = self.invoke('inspect'); self.assertEqual(code, 0)
        preview = self.preview({'userThemeMode': 'dark', 'autoModeEnabled': False}, ['locale'])
        self.assertEqual(preview['revision'], inspected['revision'])
        self.assertEqual(self.profile.read_bytes(), original)
        self.assertFalse(self.state.exists())
        self.assertEqual(preview['execution']['status'], 'preview')
        code, saved, _ = self.write(preview['plan_id'])
        self.assertEqual(code, 0, saved)
        self.assertEqual(saved['plan_id'], preview['plan_id'])
        self.assertEqual(saved['changes'], preview['changes'])
        self.assertEqual(saved['declarations'], preview['proposed_declarations'])
        self.assertNotEqual(saved['revision'], preview['revision'])
        self.assertTrue(saved['execution']['profile_saved']); self.assertFalse(saved['execution']['native_write'])
        self.assertTrue(saved['recovery']['available'])
        self.assertEqual(self.native_file.read_bytes(), native)
        actual = yaml.safe_load(self.profile.read_text())
        self.assertEqual(actual['description'], self.value['description'])
        self.assertEqual(actual['settings'], {'userThemeMode': 'dark', 'autoModeEnabled': False})
        run = self.state / 'runs' / saved['recovery']['run_id']
        self.assertEqual((run / '0.before').read_bytes(), original)
        rollback_plan(AppProfileManager(self.machine_file, APP), saved['recovery']['run_id']).apply(self.state)
        self.assertEqual(self.profile.read_bytes(), original)
        self.assertEqual(self.native_file.read_bytes(), native)

    def test_noop_preserves_yaml_comments_and_does_not_create_execution_history(self):
        self.profile.write_bytes(b'# Keep this comment\n' + self.profile.read_bytes())
        original = self.profile.read_bytes()
        preview = self.preview({'userThemeMode': 'light'}, ['builtinBrowserEnabled'])
        self.assertEqual(preview['changes'], [])
        code, saved, _ = self.write(preview['plan_id'])
        self.assertEqual(code, 0)
        self.assertEqual(saved['execution'], {'started': False, 'status': 'unchanged', 'native_write': False, 'profile_saved': False})
        self.assertEqual(saved['revision'], preview['revision'])
        self.assertFalse(saved['recovery']['available']); self.assertFalse(self.state.exists())
        self.assertEqual(self.profile.read_bytes(), original)

    def test_unknown_wrong_typed_duplicate_and_overlapping_patch_rejected_without_leaks(self):
        cases = [{'schema_version': 2, 'set': {'locale': 'en'}},
                 {'schema_version': True, 'set': {'locale': 'en'}},
                 {'schema_version': 1, 'set': {'PRIVATE_PATCH_KEY': 'SECRET_PATCH_VALUE'}},
                 {'schema_version': 1, 'set': {'autoModeEnabled': 1}},
                 {'schema_version': 1, 'set': {'locale': None}},
                 {'schema_version': 1, 'set': {'locale': 'en'}, 'unset': ['locale']},
                 {'schema_version': 1, 'unset': ['locale', 'locale']},
                 {'schema_version': 1, 'unset': ['unknown']}, {'schema_version': 1},
                 {'schema_version': 1, 'set': {}, 'extra': 'SECRET_PATCH_VALUE'}]
        original = self.profile.read_bytes()
        for value in cases:
            with self.subTest(value=value):
                self.patch_file.write_text(json.dumps(value))
                code, report, stderr = self.invoke('edit', '--patch', str(self.patch_file))
                self.assertEqual(code, 1)
                self.assertEqual(report['errors'], [{'code': 'invalid_patch'}])
                self.assertNotIn('SECRET_PATCH_VALUE', json.dumps(report) + stderr)
                self.assertEqual(self.profile.read_bytes(), original)
        self.patch_file.write_text('{"schema_version":1,"set":{"locale":"en","locale":"fr"}}')
        self.assertEqual(self.invoke('edit', '--patch', str(self.patch_file))[1]['errors'], [{'code': 'invalid_patch'}])
        self.assertFalse(self.state.exists())

    def test_write_requires_exact_reviewed_plan_and_explicit_absolute_inputs(self):
        preview = self.preview({'locale': 'zh-CN'})
        original = self.profile.read_bytes()
        for flags in [('--write',), ('--expect-plan-id', preview['plan_id']), ('--write', '--expect-plan-id', 'x')]:
            code, report, _ = self.invoke('edit', '--patch', str(self.patch_file), *flags)
            self.assertEqual(code, 1); self.assertEqual(report['errors'], [{'code': 'invalid_arguments'}])
        code, report, _ = self.write('0' * 64)
        self.assertEqual(code, 1); self.assertEqual(report['errors'], [{'code': 'plan_stale'}])
        self.assertEqual(self.profile.read_bytes(), original); self.assertFalse(self.state.exists())

    def test_external_profile_patch_machine_and_selection_edits_make_review_stale(self):
        selection_path, selection = self.externalize()
        originals = {path: path.read_bytes() for path in [self.profile, self.machine_file, selection_path]}
        for changed in ['profile', 'patch', 'machine', 'selection']:
            with self.subTest(changed=changed):
                for path, data in originals.items(): path.write_bytes(data)
                preview = self.preview({'userThemeMode': 'dark'})
                if changed == 'profile': self.profile.write_bytes(originals[self.profile] + b'\n# concurrent edit\n')
                elif changed == 'patch': self.patch_file_for({'locale': 'fr'})
                elif changed == 'machine': self.machine_file.write_bytes(originals[self.machine_file] + b'\n# concurrent edit\n')
                else: selection_path.write_bytes(originals[selection_path] + b'\n# concurrent edit\n')
                before = self.profile.read_bytes()
                code, report, _ = self.write(preview['plan_id'])
                self.assertEqual(code, 1); self.assertEqual(report['errors'], [{'code': 'plan_stale'}])
                self.assertEqual(self.profile.read_bytes(), before)
        self.assertFalse(self.state.exists())

    def test_pending_fileplan_rechecks_sources_before_any_write(self):
        self.patch_file_for({'userThemeMode': 'dark'})
        selected = SelectedDeclaration(self.machine_file); plan, _, _ = selected.plan(self.patch_file)
        original = self.profile.read_bytes()
        self.patch_file_for({'userThemeMode': 'system'})
        with self.assertRaises(PlanStale): plan.apply(self.state)
        self.assertEqual(self.profile.read_bytes(), original)

    def test_changing_selected_profile_cannot_reuse_the_old_reviewed_plan(self):
        selection_path, selection = self.externalize()
        preview = self.preview({'userThemeMode': 'dark'})
        other = self.profile.parent / 'other.yaml'
        other.write_text(yaml.safe_dump({'schema_version': 1, 'app': APP, 'settings': {'locale': 'fr'}}))
        selection['apps'][APP] = str(other.relative_to(self.data))
        selection_path.write_text(yaml.safe_dump(selection))
        original, other_original = self.profile.read_bytes(), other.read_bytes()
        code, result, _ = self.write(preview['plan_id'])
        self.assertEqual(code, 1); self.assertEqual(result['errors'], [{'code': 'plan_stale'}])
        self.assertEqual(self.profile.read_bytes(), original); self.assertEqual(other.read_bytes(), other_original)
        self.assertFalse(self.state.exists())

    def test_outside_profile_and_native_data_alias_fail_before_profile_read(self):
        outside = self.root / 'outside.yaml'; outside.write_text(yaml.safe_dump(self.value))
        self.profile.unlink(); self.profile.symlink_to(outside)
        code, report, _ = self.invoke('inspect')
        self.assertEqual(code, 1); self.assertEqual(report['errors'], [{'code': 'invalid_configuration'}])
        self.profile.unlink(); self.profile.write_text(yaml.safe_dump(self.value))
        self.machine['apps'][APP]['binding']['data_root'] = str(self.profile.parent)
        self.save_machine()
        with patch('claude_config.app_profile.snapshot_read', side_effect=AssertionError('must reject before Profile read')):
            code, report, _ = self.invoke('inspect')
        self.assertEqual(code, 1); self.assertEqual(report['errors'], [{'code': 'invalid_configuration'}])

    def test_profile_reference_swap_after_path_approval_is_rejected_before_read(self):
        other = self.data / 'private-native.yaml'
        other.write_text(yaml.safe_dump({'schema_version': 1, 'app': APP,
                                       'description': 'SWAPPED_PRIVATE_SENTINEL',
                                       'settings': {'userThemeMode': 'dark'}}))
        calls = 0
        def swap_after_first_resolution(root, name, must_exist=True):
            nonlocal calls
            candidate = within(root, name, must_exist=must_exist)
            calls += 1
            if calls == 1:
                self.profile.unlink()
                self.profile.symlink_to(other)
            return candidate
        with (patch('claude_config.app_profile.within', side_effect=swap_after_first_resolution),
              patch('claude_config.app_profile.snapshot_read', wraps=snapshot_read) as read):
            code, result, stderr = self.invoke('inspect')
        self.assertEqual(code, 1)
        self.assertEqual(result['errors'], [{'code': 'plan_stale'}])
        self.assertIsNone(result['declarations'])
        self.assertIsNone(result['target'])
        read.assert_not_called()
        self.assertNotIn('SWAPPED_PRIVATE_SENTINEL', json.dumps(result) + stderr)
        self.assertFalse(self.state.exists())

    def test_fault_during_profile_commit_is_not_reported_as_saved_and_restores_original(self):
        preview = self.preview({'locale': 'fr'})
        original = self.profile.read_bytes()
        selected = SelectedDeclaration(self.machine_file); plan, _, _ = selected.plan(self.patch_file)
        def fail_after_write(path, content, mode):
            atomic_write(path, content, mode)
            raise OSError('PRIVATE_WRITE_FAILURE_CONTENT')
        with self.assertRaises(OSError): plan.apply(self.state, writer=fail_after_write)
        self.assertEqual(self.profile.read_bytes(), original)
        self.assertTrue(plan.report['recovery'])
        with patch('claude_config.app_profile.FilePlan.apply', side_effect=OSError('PRIVATE_WRITE_FAILURE_CONTENT')):
            code, result, stderr = self.write(preview['plan_id'])
        self.assertEqual(code, 2); self.assertFalse(result['execution']['profile_saved'])
        self.assertNotIn('PRIVATE_WRITE_FAILURE_CONTENT', json.dumps(result) + stderr)


if __name__ == '__main__':
    unittest.main()
