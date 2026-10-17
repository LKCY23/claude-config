import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from claude_config.core.merge import MergePlan, fingerprint
from claude_config.core.schema import (SchemaError, StrictLoader, load_json, pointer,
                                 validate_catalogs, validate_machine, validate_profile, within)
from claude_config.core.transaction import FileChange, FilePlan, PlanStale, atomic_write, check_ownership
import yaml


class SchemaTests(unittest.TestCase):
    def profile(self, **extras):
        return {'schema_version': 1, 'agent_type': 'claude-code', **extras}

    def test_no_legacy_outer_structure_or_boolean_version(self):
        for value in ({'version': 1, 'agents': {}}, self.profile(schema_version=True), self.profile(config={'source': 'x'})):
            with self.assertRaises(SchemaError):
                validate_profile(value)

    def test_duplicate_yaml_keys_and_aliases_rejected(self):
        for text in ('x: 1\nx: 2\n', 'x: &a [1]\ny: *a\n', '1: value\n'):
            with self.assertRaises(SchemaError):
                yaml.load(text, Loader=StrictLoader)

    def test_json_yaml_types(self):
        value = yaml.load('a: true\nb: false\nc: on\nd: 2026-10-09\ne: "1"\n', Loader=StrictLoader)
        self.assertEqual(value, {'a': True, 'b': False, 'c': 'on', 'd': '2026-10-09', 'e': '1'})

    def test_duplicate_json_keys_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'bad.json'
            path.write_text('{"a":1,"a":2}')
            with self.assertRaises(SchemaError):
                load_json(path)

    def test_resources_are_keyed_and_app_cannot_own_resources(self):
        validate_profile(self.profile(resources={'plugins': {'chosen@catalog': {'options': {'enabled': False}}}}))
        for resources in ({'plugins': ['chosen@catalog']}, {'rules': {'no-catalog': {}}},
                          {'instructions': {'one@cat': {}, 'two@cat': {}}}):
            with self.assertRaises(SchemaError):
                validate_profile(self.profile(resources=resources))
        with self.assertRaises(SchemaError):
            validate_profile({'schema_version': 1, 'app': 'codex-app', 'resources': {}}, 'app')

    def test_bindings_cannot_duplicate_settings_or_use_unselected_resource(self):
        command = {'kind': 'command', 'executable': 'executables.bash', 'args': ['-l']}
        with self.assertRaises(SchemaError):
            validate_profile(self.profile(settings={'statusLine': {'command': 'raw'}},
                                          bindings={'/settings/statusLine/command': command}))
        command['args'] = [{'resource': 'unselected@cat', 'path': 'script.sh'}]
        with self.assertRaises(SchemaError):
            validate_profile(self.profile(bindings={'/settings/statusLine/command': command}))

    def test_json_pointer_keeps_model_punctuation(self):
        self.assertEqual(pointer('/settings/modelSettings/vendor.model~1v2/effortLevel'),
                         ('settings', 'modelSettings', 'vendor.model/v2', 'effortLevel'))
        with self.assertRaises(SchemaError):
            pointer('/settings/~2bad')

    def test_catalog_pin_and_unknown_fields(self):
        value = {'schema_version': 1, 'catalogs': {'cat': {'repo': 'owner/repo', 'commit': 'a' * 40, 'path': 'domains/candidates'}}}
        validate_catalogs(value)
        value['catalogs']['cat']['commit'] = 'main'
        with self.assertRaises(SchemaError):
            validate_catalogs(value)

    def test_source_symlink_and_traversal_cannot_escape(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'root'
            root.mkdir()
            outside = Path(directory) / 'outside'
            outside.write_text('x')
            (root / 'link').symlink_to(outside)
            for value in ('../outside', 'link'):
                with self.assertRaises(SchemaError):
                    within(root, value)

    def machine(self):
        return {'schema_version': 1, 'machine_id': 'test', 'config_dir': '/tmp/data', 'catalog_roots': {},
                'agents': {'codex': {'profile': 'profiles/agents/codex/work.yaml',
                                     'binding': {'executable': '/tmp/codex', 'config_root': '/tmp/native'}}},
                'apps': {}, 'bindings': {'secrets': {'github-token': {'provider': 'env', 'variable': 'GITHUB_TOKEN'}}}}

    def test_machine_uses_typed_binding_references(self):
        validate_machine(self.machine())
        value = self.machine()
        value['bindings']['secrets']['github-token'] = {'provider': 'env', 'variable': 'GITHUB_TOKEN', 'value': 'private'}
        with self.assertRaises(SchemaError):
            validate_machine(value)

    def test_machine_profile_directory_must_match(self):
        value = self.machine()
        value['agents']['codex']['profile'] = 'profiles/agents/claude-code/work.yaml'
        with self.assertRaises(SchemaError):
            validate_machine(value)


class MergeTests(unittest.TestCase):
    def plan(self, incoming, target, **kwargs):
        return MergePlan(incoming, target, source={'kind': 'profile'}, destination={'kind': 'native'}, **kwargs)

    def test_add_overwrite_and_repeat_noop_have_decisions(self):
        first = self.plan({'settings': {'model': 'selected', 'enabled': False}}, {'settings': {'model': 'old'}})
        self.assertEqual(first.result, {'settings': {'model': 'selected', 'enabled': False}})
        self.assertEqual([d['action'] for d in first.decisions], ['add', 'overwrite', 'object_merge'])
        repeated = self.plan(first.incoming, first.result)
        self.assertEqual(repeated.result, first.result)
        self.assertIn('unchanged', [d['action'] for d in repeated.decisions])

    def test_missing_export_and_unselected_fields_preserve_profile(self):
        target = {'settings': {'model': 'profile', 'effort': 'high'}}
        plan = self.plan({'settings': {'effort': 'low'}}, target, scope=['/settings/model'])
        self.assertEqual(plan.result, target)
        self.assertEqual(plan.decisions[0]['action'], 'preserve')

    def test_selected_objects_report_retained_children_but_not_other_runtime(self):
        plan = self.plan({'settings': {'x': 1}}, {'settings': {'x': 0, 'keep': 'yes'}, 'credentials': {'password': 'private'}})
        self.assertEqual(plan.result['settings']['keep'], 'yes')
        self.assertIn(['settings', 'keep'], [row['path'] for row in plan.decisions])
        self.assertNotIn(['credentials'], [row['path'] for row in plan.decisions])

    def test_false_zero_empty_string_empty_array_and_null_are_values(self):
        incoming = {'a': False, 'b': 0, 'c': '', 'd': [], 'e': None}
        plan = self.plan(incoming, {})
        self.assertEqual(plan.result, incoming)
        self.assertTrue(all(row['incoming_exists'] for row in plan.decisions))
        self.assertTrue(plan.decisions[-1]['after_exists'])

    def test_null_is_distinct_from_missing(self):
        plan = self.plan({'x': None}, {'y': None}, scope=['/x', '/y'])
        self.assertTrue(plan.decisions[0]['incoming_exists'])
        self.assertFalse(plan.decisions[1]['incoming_exists'])

    def test_arrays_replace_in_order_and_resource_maps_merge_by_id(self):
        plan = self.plan({'ordered': [2, 1], 'resources': {'skills': {'a@cat': {'options': {'enabled': False}}}}},
                         {'ordered': [1, 3], 'resources': {'skills': {'b@cat': {}, 'a@cat': {'options': {'other': 1}}}}})
        self.assertEqual(plan.result['ordered'], [2, 1])
        self.assertEqual(set(plan.result['resources']['skills']), {'a@cat', 'b@cat'})
        self.assertEqual(plan.result['resources']['skills']['a@cat']['options'], {'enabled': False, 'other': 1})

    def test_policy_precedence_profile_field_beats_run_default(self):
        plan = self.plan({'x': 1}, {'x': 2}, profile_policy={'fields': {'/x': 'keep_target'}},
                         run_policy={'default': 'source_wins'})
        self.assertEqual(plan.result['x'], 2)
        self.assertEqual(plan.decisions[0]['policy_origin'], 'profile_field')
        plan = self.plan({'x': 1}, {'x': 2}, profile_policy={'fields': {'/x': 'keep_target'}},
                         run_policy={'fields': {'/x': 'source_wins'}})
        self.assertEqual(plan.result['x'], 1)

    def test_fail_if_different_blocks_and_retains(self):
        plan = self.plan({'x': 1}, {'x': 2}, profile_policy={'default': 'fail_if_different'})
        self.assertEqual(plan.result['x'], 2)
        self.assertTrue(plan.blockers)

    def test_append_unique_requires_registered_set_list(self):
        args = {'profile_policy': {'fields': {'/array': 'append_unique'}}}
        blocked = self.plan({'array': [2, 3]}, {'array': [1, 2]}, **args)
        self.assertTrue(blocked.blockers)
        allowed = self.plan({'array': [2, 3]}, {'array': [1, 2]}, set_lists=['/array'], **args)
        self.assertEqual(allowed.result['array'], [1, 2, 3])

    def test_type_mismatch_cannot_be_bypassed(self):
        for target in ({'x': 0}, {'x': {'value': 1}}):
            plan = self.plan({'x': False}, target)
            self.assertTrue(plan.blockers)
        self.assertTrue(self.plan({'x': {'value': 1}}, {'x': 'scalar'}).blockers)

    def test_global_source_wins_does_not_bypass_external_drift(self):
        plan = self.plan({'x': 1}, {'x': 3}, baseline={'x': 2}, run_policy={'default': 'source_wins'})
        self.assertTrue(plan.blockers)
        self.assertEqual(plan.result['x'], 3)
        resolved = self.plan({'x': 1}, {'x': 3}, baseline={'x': 2},
                             resolutions={'/x': {'target_fingerprint': fingerprint({'x': 3}), 'policy': 'source_wins'}})
        self.assertFalse(resolved.blockers)
        self.assertEqual(resolved.result['x'], 1)
        stale = self.plan({'x': 1}, {'x': 4}, baseline={'x': 2},
                          resolutions={'/x': {'target_fingerprint': fingerprint({'x': 3}), 'policy': 'source_wins'}})
        self.assertTrue(stale.blockers)

    def test_external_deletion_requires_resolution(self):
        plan = self.plan({'x': 1}, {}, baseline={'x': 2})
        self.assertTrue(plan.blockers)
        self.assertEqual(plan.result, {})

    def test_switch_reports_residual_and_keeps_value(self):
        plan = self.plan({'x': 1}, {'x': 2, 'old': 3}, previous={'x': 2, 'old': 3})
        self.assertEqual(plan.result['old'], 3)
        self.assertEqual(plan.decisions[-1]['action'], 'residual')

    def test_secret_decisions_redacted_and_inputs_unchanged(self):
        incoming, target = {'env': {'MY_API_KEY': 'secret-new'}}, {'env': {'MY_API_KEY': 'secret-old'}}
        plan = self.plan(incoming, target)
        report = json.dumps(plan.report())
        self.assertNotIn('secret-new', report)
        self.assertNotIn('secret-old', report)
        self.assertEqual(incoming['env']['MY_API_KEY'], 'secret-new')
        self.assertEqual(target['env']['MY_API_KEY'], 'secret-old')

    def test_empty_scope_means_export_nothing_and_overlaps_rejected(self):
        self.assertEqual(self.plan({'x': 1}, {'x': 2}, scope=[]).result, {'x': 2})
        with self.assertRaises(ValueError):
            self.plan({}, {}, scope=['/x', '/x/y'])
        with self.assertRaises(ValueError):
            self.plan({}, {}, scope=['/x', '/x'])


class TransactionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.target, self.source, self.state = (self.root / name for name in ('target.json', 'profile.yaml', 'state'))
        self.target.write_text('{"before": true}\n')
        self.source.write_text('schema_version: 1\n')

    def tearDown(self):
        self.temp.cleanup()

    def change(self, target=None, content=b'{"after": true}\n'):
        return FileChange(target or self.target, content, 'test', lambda content: json.loads(content))

    def test_plan_is_read_only_and_validate_all_before_writes(self):
        before = self.target.read_bytes()
        FilePlan([self.change()], sources=[self.source])
        self.assertEqual(self.target.read_bytes(), before)
        self.assertFalse(self.state.exists())
        with self.assertRaises(json.JSONDecodeError):
            FilePlan([self.change(), self.change(self.root / 'bad.json', b'bad')])
        self.assertEqual(self.target.read_bytes(), before)

    def test_source_or_target_change_invalidates_preview(self):
        for changed in (self.source, self.target):
            plan = FilePlan([self.change()], sources=[self.source])
            changed.write_text('changed')
            with self.assertRaises(PlanStale):
                plan.apply(self.state)
            self.assertFalse((self.state / 'runs').exists())

    def test_apply_private_backup_and_second_noop_result(self):
        before = self.target.read_bytes()
        report = FilePlan([self.change()], sources=[self.source]).apply(self.state)
        self.assertEqual(report['results'][0]['status'], 'applied')
        backup = Path(report['results'][0]['backup'])
        self.assertEqual(backup.read_bytes(), before)
        self.assertEqual(backup.stat().st_mode & 0o777, 0o600)
        self.assertEqual(backup.parent.stat().st_mode & 0o777, 0o700)
        repeated = FilePlan([self.change()]).apply(self.state)
        self.assertEqual(repeated['results'][0]['status'], 'unchanged')

    def test_mid_run_failure_restores_only_this_runs_writes(self):
        before = self.target.read_bytes()
        second = self.root / 'second.json'
        plan = FilePlan([self.change(), self.change(second)])
        def fail_second(path, content, mode):
            if path == second:
                raise OSError('simulated failure')
            atomic_write(path, content, mode)
        with self.assertRaises(OSError):
            plan.apply(self.state, writer=fail_second)
        self.assertEqual(self.target.read_bytes(), before)
        self.assertFalse(second.exists())
        self.assertEqual(plan.report['results'][0]['status'], 'rolled_back')

    def test_recovery_never_clobbers_later_manual_edit(self):
        second = self.root / 'second.json'
        plan = FilePlan([self.change(), self.change(second)])
        def fail_with_manual_edit(path, content, mode):
            if path == second:
                self.target.write_text('{"manual": true}')
                raise OSError('failure')
            atomic_write(path, content, mode)
        with self.assertRaises(OSError):
            plan.apply(self.state, writer=fail_with_manual_edit)
        self.assertEqual(json.loads(self.target.read_text()), {'manual': True})
        self.assertEqual(plan.report['results'][0]['status'], 'recovery_conflict')

    def test_duplicate_file_and_overlapping_leaf_ownership_block(self):
        with self.assertRaises(ValueError):
            FilePlan([self.change(), self.change()])
        check_ownership([(self.target, ['model'], 'agent'), (self.target, ['desktop', 'setting'], 'app')])
        with self.assertRaises(ValueError):
            check_ownership([(self.target, ['desktop'], 'app'), (self.target, ['desktop', 'setting'], 'agent')])

    def test_symlink_target_blocks_without_mutation(self):
        symlink = self.root / 'link.json'
        symlink.symlink_to(self.target)
        with self.assertRaises(ValueError):
            FilePlan([self.change(symlink)])

    def test_merge_blockers_prevent_any_target_write(self):
        report = MergePlan({'x': 1}, {'x': 2}, source={}, destination={},
                           profile_policy={'default': 'fail_if_different'}).report()
        plan = FilePlan([self.change()], report=report)
        with self.assertRaises(ValueError):
            plan.apply(self.state)
        self.assertFalse(self.state.exists())

    def test_planning_input_snapshot_is_not_taken_after_validation(self):
        def mutate_during_validation(content):
            json.loads(content)
            self.target.write_text('{"manual": true}')
        with self.assertRaises(PlanStale):
            FilePlan([FileChange(self.target, b'{}', 'test', mutate_during_validation)])

    def test_caller_can_supply_snapshot_of_native_input_used_to_merge(self):
        from claude_config.core.transaction import file_fingerprint
        expected = file_fingerprint(self.target)
        self.target.write_text('{"changed_after_read": true}')
        change = FileChange(self.target, b'{}', 'test', lambda data: json.loads(data), expected)
        with self.assertRaises(PlanStale):
            FilePlan([change])


if __name__ == '__main__':
    unittest.main()
