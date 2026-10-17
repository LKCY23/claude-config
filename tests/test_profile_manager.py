from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import tomlkit
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from claude_config.core.fields import capability_checks, validate_settings
from claude_config.core.manager import ProfileManager
from claude_config.core.merge import fingerprint
from claude_config.core.schema import SchemaError
from claude_config.core.transaction import PlanStale


class FakeProbe:
    def __init__(self):
        self.calls = []
    def __call__(self, agent, executable, settings, context):
        self.calls.append((agent, executable, deepcopy(settings), deepcopy(context)))
        return capability_checks(agent, settings, '2.1.278' if agent == 'claude-code' else '0.160.0', native_context=context)


class ManagerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.data = self.root / 'data'
        self.state = self.root / 'state'
        self.machine_path = self.root / 'machine.yaml'
        self.bin = self.root / 'claude'
        self.bin.write_text('#!/bin/sh\nprintf "2.1.278\\n"\n')
        self.bin.chmod(0o700)
        self.probe = FakeProbe()
        self.machine = {'schema_version': 1, 'machine_id': 'isolated', 'config_dir': str(self.data), 'catalog_roots': {},
                        'agents': {}, 'apps': {}, 'bindings': {}, 'runtime': {'state_dir': str(self.state)}}
        for agent in ('claude-code', 'codex'):
            name = f'profiles/agents/{agent}/work.yaml'
            path = self.data / name
            path.parent.mkdir(parents=True)
            path.write_text(yaml.safe_dump({'schema_version': 1, 'agent_type': agent, 'settings': {'model': 'selected'}}))
            native = self.root / agent
            native.mkdir()
            self.machine['agents'][agent] = {'profile': name, 'binding': {'executable': str(self.bin), 'config_root': str(native)}}
        self.save_machine()

    def tearDown(self):
        self.temp.cleanup()

    def save_machine(self):
        self.machine_path.write_text(yaml.safe_dump(self.machine, sort_keys=False))

    def manager(self, agent='claude-code'):
        return ProfileManager(self.machine_path, agent, probe=self.probe)

    def profile(self, agent='claude-code', **changes):
        path = self.data / self.machine['agents'][agent]['profile']
        value = yaml.safe_load(path.read_text())
        value.update(changes)
        path.write_text(yaml.safe_dump(value, sort_keys=False))
        return path

    def native(self, agent='claude-code', value=None, text=None):
        path = self.root / agent / ('settings.json' if agent == 'claude-code' else 'config.toml')
        if value is not None:
            text = json.dumps(value) if agent == 'claude-code' else tomlkit.dumps(value)
        if text is not None:
            path.write_text(text)
        return path

    def test_plan_read_only_then_apply_and_noop_preserve_bytes(self):
        path = self.native(value={'model': 'old', 'env': {'ANTHROPIC_AUTH_TOKEN': 'LOCAL_ONLY'}})
        before = path.read_bytes()
        manager = self.manager()
        plan = manager.plan()
        self.assertTrue(plan.report['ready'])
        self.assertFalse(self.state.exists())
        self.assertEqual(path.read_bytes(), before)
        self.assertNotIn('LOCAL_ONLY', json.dumps(plan.report))
        self.assertNotIn('env', json.dumps(self.probe.calls, default=str))
        plan.apply(self.state)
        self.assertEqual(json.loads(path.read_text()), {'model': 'selected', 'env': {'ANTHROPIC_AUTH_TOKEN': 'LOCAL_ONLY'}})
        after = path.read_bytes()
        repeated = self.manager().plan().apply(self.state)
        self.assertEqual(path.read_bytes(), after)
        self.assertTrue(all(row['status'] == 'unchanged' for row in repeated['results']))

    def test_codex_toml_comments_credentials_and_unmanaged_tables_preserved(self):
        self.profile('codex', settings={'model_reasoning_effort': 'high', 'memories': {'use_memories': False}})
        path = self.native('codex', text='# keep comment\nmodel = "unselected" # keep inline\n[model_providers.local]\napi_key = "LOCAL_ONLY"\n[memories]\ngenerate_memories = true\n')
        self.manager('codex').plan().apply(self.state)
        content = path.read_text()
        self.assertIn('# keep comment', content)
        self.assertIn('model = "unselected" # keep inline', content)
        self.assertIn('api_key = "LOCAL_ONLY"', content)
        value = tomlkit.parse(content).unwrap()
        self.assertEqual(value['memories'], {'generate_memories': True, 'use_memories': False})

    def test_first_apply_can_adopt_different_native_setting_with_decision(self):
        self.native(value={'model': 'old'})
        plan = self.manager().plan()
        self.assertEqual(plan.report['decisions'][0]['action'], 'overwrite')
        self.assertEqual(plan.report['decisions'][0]['policy_origin'], 'program_default')

    def test_apply_detects_external_edit_and_global_policy_cannot_hide_it(self):
        path = self.native(value={'model': 'old'})
        self.manager().plan().apply(self.state)
        path.write_text('{"model":"manual"}')
        plan = self.manager().plan(run_policy={'default': 'source_wins'})
        self.assertFalse(plan.report['ready'])
        with self.assertRaises(ValueError):
            plan.apply(self.state)
        self.assertEqual(json.loads(path.read_text())['model'], 'manual')
        current = {'settings': json.loads(path.read_text())}
        resolved = self.manager().plan(resolutions={'/settings/model': {'target_fingerprint': fingerprint(current), 'policy': 'source_wins'}})
        self.assertTrue(resolved.report['ready'])
        resolved.apply(self.state)
        self.assertEqual(json.loads(path.read_text())['model'], 'selected')

    def test_preview_stale_sources_targets_and_executable_are_blocked(self):
        path = self.native(value={'model': 'old'})
        for changed in (path, self.data / self.machine['agents']['claude-code']['profile'], self.bin):
            before = changed.read_bytes()
            plan = self.manager().plan()
            changed.write_bytes(before + b'\n')
            with self.assertRaises(PlanStale):
                plan.apply(self.state)
            changed.write_bytes(before)

    def test_profile_symlink_retarget_after_preview_is_stale(self):
        profile = self.data / self.machine['agents']['claude-code']['profile']
        first = profile.parent / 'first.yaml'
        second = profile.parent / 'second.yaml'
        first.write_bytes(profile.read_bytes())
        second.write_bytes(profile.read_bytes())
        profile.unlink()
        profile.symlink_to(first)
        plan = self.manager().plan()
        profile.unlink()
        profile.symlink_to(second)
        with self.assertRaises(PlanStale):
            plan.apply(self.state)
        self.assertFalse(self.native().exists())

    def test_export_only_explicit_existing_fields_and_no_missing_delete(self):
        profile = self.profile(settings={'model': 'profile', 'language': 'English'})
        self.native(value={'model': 'native', 'effortLevel': 'high', 'env': {'ANTHROPIC_AUTH_TOKEN': 'SECRET'}})
        plan = self.manager().export(['/settings/model', '/settings/language'])
        self.assertEqual(yaml.safe_load(profile.read_text())['settings']['model'], 'profile')
        plan.apply(self.state)
        value = yaml.safe_load(profile.read_text())
        self.assertEqual(value['settings'], {'model': 'native', 'language': 'English'})
        self.assertNotIn('SECRET', profile.read_text())
        self.assertNotIn('SECRET', json.dumps(plan.report))
        self.assertEqual(plan.report['decisions'][1]['action'], 'preserve')

    def test_export_can_create_new_profile_without_changing_machine_binding(self):
        self.native(value={'model': 'native'})
        name = 'profiles/agents/claude-code/created.yaml'
        plan = self.manager().export(['/settings/model'], profile=name)
        self.assertFalse((self.data / name).exists())
        plan.apply(self.state)
        self.assertEqual(yaml.safe_load((self.data / name).read_text())['settings']['model'], 'native')
        self.assertEqual(yaml.safe_load(self.machine_path.read_text())['agents']['claude-code']['profile'], 'profiles/agents/claude-code/work.yaml')

    def test_missing_unselected_config_does_not_create_native_file(self):
        self.profile(settings={})
        path = self.native()
        plan = self.manager().plan()
        self.assertTrue(plan.report['ready'])
        self.assertEqual(plan.changes, ())
        self.assertFalse(path.exists())

    def test_unknown_fields_credentials_paths_and_resources_never_slip_into_settings(self):
        bad = [{'env': {'ANTHROPIC_AUTH_TOKEN': 'private'}}, {'permissions': {'allow': ['Bash(*)']}},
               {'settings': {'source': 'old.json'}}, {'enabledPlugins': {'x': True}}, {'statusLine': {'command': '/tmp/run'}}]
        for settings in bad:
            with self.assertRaises(SchemaError):
                validate_settings('claude-code', settings)
        for settings in ({'projects': {'/tmp': {'trust_level': 'trusted'}}}, {'agents': {'max_threads': 2}}, {'skills': {'config': []}}):
            with self.assertRaises(SchemaError):
                validate_settings('codex', settings)

    def test_selected_type_errors_are_blockers_not_silent_overwrites(self):
        self.native(value={'model': 123})
        plan = self.manager().plan()
        self.assertFalse(plan.report['ready'])
        self.assertTrue(any(row['policy'] == 'type_check' for row in plan.report['decisions']))

    def test_explicit_false_empty_array_and_order_preserved(self):
        self.profile(settings={'autoMemoryEnabled': False, 'fallbackModel': [], 'availableModels': ['second', 'first'],
                               'attribution': {'commit': ''}})
        path = self.native(value={'autoMemoryEnabled': True, 'fallbackModel': ['old'], 'availableModels': ['old'],
                                  'attribution': {'commit': 'signed', 'pr': 'keep'}})
        self.manager().plan().apply(self.state)
        value = json.loads(path.read_text())
        self.assertFalse(value['autoMemoryEnabled'])
        self.assertEqual(value['fallbackModel'], [])
        self.assertEqual(value['availableModels'], ['second', 'first'])
        self.assertEqual(value['attribution'], {'commit': '', 'pr': 'keep'})

    def test_version_unknown_and_v2_max_depth_block(self):
        self.profile(settings={'modelSettings': {'provider/model.with.dots': {'autoCompactWindow': 'auto'}}})
        self.assertFalse(self.manager().plan().report['ready'])
        self.profile('codex', settings={'agents': {'max_depth': 2}})
        self.native('codex', value={'features': {'multi_agent_v2': True}})
        self.assertFalse(self.manager('codex').plan().report['ready'])
        self.native('codex', value={'features': {'multi_agent_v2': False}})
        self.assertTrue(self.manager('codex').plan().report['ready'])

    def test_policy_json_pointer_with_punctuation_and_precedence(self):
        self.profile(settings={'modelSettings': {'provider/model.with.dots': {'effortLevel': 'high'}}},
                     merge_policy={'fields': {'/settings/modelSettings/provider~1model.with.dots/effortLevel': 'keep_target'}})
        path = self.native(value={'modelSettings': {'provider/model.with.dots': {'effortLevel': 'low'}}})
        plan = self.manager().plan(run_policy={'default': 'source_wins'})
        plan.apply(self.state)
        self.assertEqual(json.loads(path.read_text())['modelSettings']['provider/model.with.dots']['effortLevel'], 'low')

    def test_resource_adapter_pending_blocks_entire_apply_but_settings_export_is_scoped(self):
        self.profile(resources={'plugins': {'chosen@cat': {'options': {'enabled': True}}}})
        path = self.native(value={'model': 'old'})
        plan = self.manager().plan()
        self.assertFalse(plan.report['ready'])
        with self.assertRaises(ValueError):
            plan.apply(self.state)
        self.assertEqual(json.loads(path.read_text())['model'], 'old')
        self.assertTrue(self.manager().export(['/settings/model']).report['ready'])

    def test_switch_keeps_residual_updates_binding_and_managed_scope(self):
        self.profile(settings={'model': 'selected', 'language': 'English'})
        self.native(value={'model': 'old'})
        self.manager().plan().apply(self.state)
        name = 'profiles/agents/claude-code/other.yaml'
        (self.data / name).write_text(yaml.safe_dump({'schema_version': 1, 'agent_type': 'claude-code', 'settings': {'model': 'other'}}))
        manager = self.manager()
        plan = manager.plan(profile=name)
        self.assertEqual(yaml.safe_load(self.machine_path.read_text())['agents']['claude-code']['profile'], self.machine['agents']['claude-code']['profile'])
        self.assertTrue(any(row['action'] == 'residual' and row['path'] == ['settings', 'language'] for row in plan.report['decisions']))
        plan.apply(self.state)
        self.assertEqual(json.loads(self.native().read_text()), {'model': 'other', 'language': 'English'})
        self.assertEqual(yaml.safe_load(self.machine_path.read_text())['agents']['claude-code']['profile'], name)
        state = json.loads(manager.state_path.read_text())
        self.assertEqual(state['managed'], {'model': 'other'})

    def test_switch_failure_restores_native_state_and_old_machine_binding(self):
        self.native(value={'model': 'old'})
        self.manager().plan().apply(self.state)
        before_machine = self.machine_path.read_bytes()
        manager = self.manager()
        before_state = manager.state_path.read_bytes()
        name = 'profiles/agents/claude-code/other.yaml'
        (self.data / name).write_text(yaml.safe_dump({'schema_version': 1, 'agent_type': 'claude-code', 'settings': {'model': 'other'}}))
        plan = manager.plan(profile=name)
        from claude_config.core.transaction import atomic_write
        def fail_machine(path, content, mode):
            if path == self.machine_path:
                raise OSError('simulated failure')
            atomic_write(path, content, mode)
        with self.assertRaises(OSError):
            plan.apply(self.state, writer=fail_machine)
        self.assertEqual(self.machine_path.read_bytes(), before_machine)
        self.assertEqual(manager.state_path.read_bytes(), before_state)
        self.assertEqual(json.loads(self.native().read_text())['model'], 'selected')

    def test_bound_codex_app_backend_is_checked_in_addition_to_cli(self):
        backend = self.root / 'app-backend'
        backend.write_text('backend')
        self.machine['apps']['codex-app'] = {'profile': 'profiles/apps/codex-app/daily.yaml',
                                           'binding': {'application': '/Applications/Codex.app', 'data_root': str(self.root / 'app'), 'backend': str(backend)}}
        self.save_machine()
        self.manager('codex').plan()
        self.assertEqual(len(self.probe.calls), 2)
        self.machine['apps']['codex-app']['binding'].pop('backend')
        self.save_machine()
        self.assertFalse(self.manager('codex').plan().report['ready'])

    def test_export_field_selection_and_array_policy_cannot_widen_scope(self):
        self.native(value={'model': 'old'})
        for selection in ([], ['/settings/env'], ['/settings/env/ANTHROPIC_AUTH_TOKEN']):
            with self.assertRaises(SchemaError):
                self.manager().export(selection)
        with self.assertRaises(SchemaError):
            self.manager().plan(run_policy={'fields': {'/settings/model': 'replace'}})

    def test_granular_permission_leaf_incremental_merge_validates_final_object(self):
        self.profile('codex', settings={'approval_policy': {'granular': {'skill_approval': False}}})
        path = self.native('codex', value={'approval_policy': {'granular': {'mcp_elicitations': True, 'rules': True, 'sandbox_approval': False}}})
        plan = self.manager('codex').plan()
        self.assertTrue(plan.report['ready'])
        plan.apply(self.state)
        self.assertEqual(tomlkit.parse(path.read_text()).unwrap()['approval_policy']['granular'],
                         {'mcp_elicitations': True, 'rules': True, 'sandbox_approval': False, 'skill_approval': False})

    def test_malformed_resolution_and_unknown_policy_target_are_rejected(self):
        for resolutions in ({'/settings/model': 'source_wins'}, {'/settings/unselected': {'policy': 'keep_target', 'target_fingerprint': 'a' * 64}},
                            {'/settings/model': {'policy': 'source_wins', 'target_fingerprint': 'main'}}):
            with self.assertRaises(SchemaError):
                self.manager().plan(resolutions=resolutions)
        with self.assertRaises(SchemaError):
            self.manager().plan(run_policy={'fields': {'/settings/provider': 'source_wins'}})

    def test_native_missing_command_is_not_accepted_as_valid_statusline(self):
        self.profile(settings={'statusLine': {'type': 'command'}})
        plan = self.manager().plan()
        self.assertFalse(plan.report['ready'])
        self.assertEqual(plan.report['blockers'][-1]['code'], 'invalid_native_result')
        self.assertTrue(plan.report['decisions'])
        self.assertEqual(plan.changes, ())

    def test_native_number_fields_allow_integer_to_float_but_not_boolean(self):
        self.profile(settings={'statusLine': {'padding': 1.5}})
        path = self.native(value={'statusLine': {'type': 'command', 'command': 'keep-local-command', 'padding': 2}})
        plan = self.manager().plan()
        self.assertTrue(plan.report['ready'])
        plan.apply(self.state)
        self.assertEqual(json.loads(path.read_text())['statusLine']['padding'], 1.5)
        with self.assertRaises(SchemaError):
            validate_settings('claude-code', {'statusLine': {'padding': False}})

    def test_native_probe_failure_is_a_blocker_and_never_writes(self):
        self.native(value={'model': 'old'})
        def unavailable(*args):
            raise subprocess.TimeoutExpired('native-check', 15)
        plan = ProfileManager(self.machine_path, 'claude-code', probe=unavailable).plan()
        self.assertFalse(plan.report['ready'])
        self.assertFalse(self.state.exists())

    def test_native_credential_changes_outside_selected_scope_do_not_trigger_drift(self):
        path = self.native(value={'model': 'old', 'env': {'ANTHROPIC_AUTH_TOKEN': 'FIRST'}})
        self.manager().plan().apply(self.state)
        path.write_text(json.dumps({'model': 'selected', 'env': {'ANTHROPIC_AUTH_TOKEN': 'SECOND'}}))
        self.assertTrue(self.manager().plan().report['ready'])

    def test_cli_plan_validate_dryrun_and_export_are_read_only(self):
        self.native(value={'model': 'old'})
        script = Path(__file__).resolve().parents[1] / 'src/claude_config/profile.py'
        before = self.native().read_bytes()
        profile_before = (self.data / self.machine['agents']['claude-code']['profile']).read_bytes()
        for command in (['plan'], ['validate'], ['apply', '--dry-run'], ['export', '--field', '/settings/model']):
            result = subprocess.run([sys.executable, '-m', 'claude_config.profile', *command, '--machine', str(self.machine_path), '--agent', 'claude-code', '--format', 'json'], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertTrue(json.loads(result.stdout)['ready'])
        self.assertEqual(self.native().read_bytes(), before)
        self.assertEqual((self.data / self.machine['agents']['claude-code']['profile']).read_bytes(), profile_before)
        self.assertEqual(len(list((self.state / 'reports').glob('*.json'))), 4)
        self.assertFalse((self.state / 'runs').exists())
        self.assertFalse(self.manager().state_path.exists())

    def test_cli_reviewed_plan_id_must_match_before_write(self):
        path = self.native(value={'model': 'old'})
        script = Path(__file__).resolve().parents[1] / 'src/claude_config/profile.py'
        common = [sys.executable, '-m', 'claude_config.profile', '--machine', str(self.machine_path), '--agent', 'claude-code', '--format', 'json']
        result = subprocess.run([*common, 'plan'], capture_output=True, text=True, check=True)
        identifier = json.loads(result.stdout)['plan_id']
        path.write_text('{"model":"manual"}')
        result = subprocess.run([*common, 'apply', '--expect-plan-id', identifier], capture_output=True, text=True)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(json.loads(result.stdout)['blockers'][-1]['code'], 'plan_stale')
        self.assertEqual(json.loads(path.read_text())['model'], 'manual')
        self.assertFalse((self.state / 'runs').exists())


if __name__ == '__main__':
    unittest.main()
