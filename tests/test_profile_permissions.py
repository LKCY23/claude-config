import json
import os
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from claude_config.core.fields import selected_paths, validate_settings
from claude_config.core.manager import ProfileManager
from claude_config.core.permissions import domain_rule, enforcement_checks, permission_dependencies
from claude_config.core.recovery import rollback_plan
from claude_config.core.schema import SchemaError
import test_profile_manager as fixtures


class PermissionTests(unittest.TestCase):
    def test_portable_named_leaves_are_registered_and_exportable(self):
        settings = {'default_permissions': 'review', 'features': {'network_proxy': False},
                    'permissions': {'review': {'description': 'Review policy', 'extends': ':read-only',
                                               'network': {'enabled': False, 'allow_local_binding': False,
                                                           'domains': {'**.example.com': 'allow', 'api.example.com': 'deny'}}}}}
        self.assertEqual(validate_settings('codex', settings), settings)
        self.assertEqual(selected_paths('codex', ['/settings/permissions/review/network/domains/**.example.com']),
                         [('settings', 'permissions', 'review', 'network', 'domains', '**.example.com')])

    def test_local_and_unregistered_permission_fields_stay_excluded(self):
        for policy in ({'workspace_roots': {'/tmp/project': True}}, {'filesystem': {'/tmp/project': 'write'}},
                       {'network': {'unix_sockets': {'/var/run/docker.sock': 'allow'}}},
                       {'network': {'proxy_url': 'http://localhost:3128'}}, {'unregistered': True}):
            with self.subTest(policy=policy), self.assertRaises(SchemaError):
                validate_settings('codex', {'permissions': {'review': policy}})

    def test_boolean_types_and_permission_reference_values_are_strict(self):
        for value in (0, 'false', None):
            with self.assertRaises(SchemaError):
                validate_settings('codex', {'permissions': {'review': {'network': {'enabled': value}}}})
        for value in (':unknown', '', '../profile'):
            with self.assertRaises(SchemaError):
                validate_settings('codex', {'default_permissions': value})
        with self.assertRaises(SchemaError):
            validate_settings('codex', {'permissions': {'review': {'extends': ':danger-full-access'}}})

    def test_domain_rules_reject_urls_ports_paths_and_invalid_wildcards(self):
        for value in ('https://api.example.com', 'api.example.com:443', '/tmp/socket', '*.127.0.0.1', 'user@host', '*.*.example.com'):
            with self.subTest(value=value), self.assertRaises(SchemaError):
                domain_rule(value, 'allow')
        with self.assertRaises(SchemaError): domain_rule('*', 'deny')
        for value in ('api.example.com', '*.example.com', '**.example.com', '*', 'localhost', '127.0.0.1', '::1'):
            domain_rule(value, 'allow')

    def test_dependencies_follow_parents_without_copying_local_roots_or_endpoints(self):
        native = {'default_permissions': 'child', 'permissions': {
            'base': {'extends': ':read-only', 'filesystem': {'/private/project': 'read'},
                     'network': {'enabled': True, 'proxy_url': 'http://user:PRIVATE@localhost:1234',
                                 'unix_sockets': {'/private/socket': 'allow'}}},
            'child': {'extends': 'base', 'network': {'domains': {'example.com': 'allow'}}}}}
        result = permission_dependencies({'default_permissions': 'child'}, native)
        self.assertEqual(set(result), {'base', 'child'})
        self.assertNotIn('PRIVATE', json.dumps(result))
        self.assertNotIn('filesystem', result['base'])
        self.assertNotIn('unix_sockets', result['base']['network'])
        self.assertIn('filesystem', native['permissions']['base'])

    def test_missing_parent_and_cycles_are_blocked(self):
        for policies in ({'child': {'extends': 'missing'}}, {'child': {'extends': 'other'}, 'other': {'extends': 'child'}}):
            with self.assertRaises(SchemaError):
                permission_dependencies({'default_permissions': 'child'}, {'default_permissions': 'child', 'permissions': policies})

    def test_builtin_selection_does_not_validate_unrelated_local_policies(self):
        self.assertEqual(permission_dependencies({'default_permissions': ':workspace'}, {'default_permissions': ':workspace',
                                                                                       'permissions': {'unrelated': None}}), {})

    def test_domain_enforcement_warning_does_not_enable_proxy(self):
        settings = {'permissions': {'review': {'network': {'domains': {'example.com': 'allow'}}}}}
        rows = enforcement_checks(settings, {})
        self.assertEqual(rows[0]['code'], 'network_proxy_inactive')
        self.assertNotIn('features', settings)
        self.assertEqual(enforcement_checks(settings, {'features': {'network_proxy': True}}), [])


class PermissionManagerTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.ManagerTests()
        self.f.setUp()
    def tearDown(self): self.f.tearDown()

    def test_apply_export_and_rollback_preserve_local_policy_fields(self):
        self.f.profile('codex', settings={'default_permissions': 'review', 'permissions': {'review': {'extends': ':read-only', 'network': {'enabled': False}}}})
        path = self.f.native('codex', value={'permissions': {'review': {'filesystem': {'/local/project': 'read'},
                                                                     'network': {'proxy_url': 'http://localhost:3128'}}},
                                           'model_providers': {'local': {'api_key': 'PRIVATE'}}})
        before = path.read_bytes()
        manager = self.f.manager('codex')
        plan = manager.plan()
        self.assertTrue(plan.report['ready'], plan.report['blockers'])
        self.assertNotIn('/local/project', json.dumps(self.f.probe.calls, default=str))
        plan.apply(self.f.state)
        self.assertIn('/local/project', path.read_text())
        self.assertIn('PRIVATE', path.read_text())
        exported = self.f.manager('codex').export(['/settings/permissions/review/network/enabled'])
        self.assertTrue(exported.report['ready'])
        self.assertNotIn('PRIVATE', json.dumps(exported.report))
        rollback_plan(self.f.manager('codex'), Path(plan.report['recovery']).parent.name).apply(self.f.state)
        self.assertEqual(path.read_bytes(), before)

    def test_missing_dependency_and_existing_legacy_sandbox_block_all_writes(self):
        self.f.profile('codex', settings={'default_permissions': 'missing'})
        plan = self.f.manager('codex').plan()
        self.assertFalse(plan.report['ready'])
        self.f.profile('codex', settings={'default_permissions': ':read-only'})
        path = self.f.native('codex', value={'sandbox_mode': 'workspace-write'})
        before = path.read_bytes()
        plan = self.f.manager('codex').plan()
        self.assertFalse(plan.report['ready'])
        self.assertEqual(path.read_bytes(), before)

    def test_final_policy_selection_and_proxy_context_respect_keep_target(self):
        self.f.profile('codex', settings={'default_permissions': 'unused-source-name', 'features': {'network_proxy': False}},
                       merge_policy={'fields': {'/settings/default_permissions': 'keep_target', '/settings/features/network_proxy': 'keep_target'}})
        self.f.native('codex', value={'default_permissions': 'existing', 'features': {'network_proxy': True},
                                    'permissions': {'existing': {'extends': ':read-only'}}})
        plan = self.f.manager('codex').plan()
        self.assertTrue(plan.report['ready'], plan.report['blockers'])
        _, _, settings, context = self.f.probe.calls[-1]
        self.assertEqual(settings['default_permissions'], 'existing')
        self.assertTrue(context['features']['network_proxy'])


@unittest.skipUnless(os.environ.get('CLAUDE_CONFIG_RUN_NATIVE_SMOKE') == '1', 'Opt-in named permission parser checks')
class NativePermissionSmoke(unittest.TestCase):
    def exercise(self, variable):
        binary = os.environ.get(variable)
        if not binary: self.skipTest(variable + ' is required')
        f = fixtures.ManagerTests()
        f.setUp()
        try:
            f.machine['agents']['codex']['binding']['executable'] = binary
            f.save_machine()
            f.profile('codex', settings={'default_permissions': 'review', 'permissions': {'review': {'extends': ':read-only',
                            'network': {'enabled': False, 'domains': {'example.com': 'allow'}}}}})
            manager = ProfileManager(f.machine_path, 'codex')
            plan = manager.plan()
            self.assertTrue(plan.report['ready'], plan.report['blockers'])
            self.assertTrue(any(row.get('name') == 'native_field_readback' for row in plan.report['checks']))
            plan.apply(f.state)
            self.assertTrue(ProfileManager(f.machine_path, 'codex').plan().report['ready'])
        finally: f.tearDown()
    def test_cli(self): self.exercise('CLAUDE_CONFIG_NATIVE_CODEX')
    def test_app_backend(self): self.exercise('CLAUDE_CONFIG_NATIVE_CODEX_APP')


if __name__ == '__main__': unittest.main()
