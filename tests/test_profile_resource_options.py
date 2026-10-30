"""Native skill selectors and portable resource policy decisions."""
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

import tomlkit
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from claude_config.core.recovery import rollback_plan
from claude_config.core.transaction import PlanStale, atomic_write
from test_profile_resources import ResourceFixture


class ResourceOptionTests(ResourceFixture):
    def skill(self, *, control=None, agent='codex', name='tiny', files=None):
        self.profile['agent_type'] = agent
        self.machine['bindings']['paths'] = {'skills': str(self.root / 'home/.agents/skills')}
        return self.add('tiny', 'skill', files or {'SKILL.md': f'---\nname: {name}\ndescription: Test\n---\nDo not execute\n'}, 'SKILL.md',
                        options={'control': control or {}},
                        requires={'bindings': {'install_root': {'kind': 'path', 'required': True}}} if agent == 'codex' else {},
                        bindings={'install_root': 'paths.skills'} if agent == 'codex' else {})

    def deploy(self, **kwargs):
        plan = self.manager().plan(**kwargs)
        self.assertTrue(plan.report['ready'], plan.report['blockers'])
        plan.apply(self.state)
        return plan

    def read_native(self):
        return tomlkit.parse(self.manager().native_path.read_text()).unwrap()

    def set_native(self, value):
        self.native_path = self.manager().native_path
        self.native_path.parent.mkdir(parents=True, exist_ok=True)
        self.native_path.write_text(tomlkit.dumps(value))

    def test_disabled_skill_is_installed_with_separate_native_activation(self):
        self.skill(control={'enabled': False, 'allow_implicit_invocation': False})
        self.save()
        plan = self.manager().plan()
        self.assertTrue(plan.report['ready'], plan.report['blockers'])
        self.assertFalse(self.native.exists())
        plan.apply(self.state)
        path = self.root / 'home/.agents/skills/tiny/SKILL.md'
        self.assertTrue(path.exists())
        self.assertEqual(self.read_native()['skills']['config'], [{'path': str(path.resolve()), 'enabled': False}])
        policy = yaml.safe_load((path.parent / 'agents/openai.yaml').read_text())['policy']
        self.assertEqual(policy, {'allow_implicit_invocation': False})
        profile = self.data / self.machine['agents']['codex']['profile']
        self.assertNotIn(str(path), profile.read_text())

    def test_activation_alone_creates_native_config_without_ordinary_settings(self):
        self.skill(control={'enabled': False})
        self.profile.pop('settings')
        self.save()
        self.deploy()
        self.assertEqual(set(self.read_native()), {'skills'})

    def test_omitted_activation_does_not_write_a_native_default(self):
        self.skill(control={'allow_implicit_invocation': False})
        self.profile.pop('settings')
        self.save()
        self.deploy()
        self.assertFalse(self.manager().native_path.exists())

    def test_shared_skills_table_preserves_globals_and_other_rows_order(self):
        self.skill(control={'enabled': False})
        self.profile['settings']['skills'] = {'include_instructions': True, 'max_context_tokens': 600}
        self.save()
        rows = [{'name': 'unrelated', 'enabled': False}, {'path': str(self.root / 'other/SKILL.md'), 'enabled': True}]
        self.set_native({'skills': {'include_instructions': False, 'bundled': {'opaque': True}, 'config': rows}})
        plan = self.deploy()
        value = self.read_native()['skills']
        self.assertEqual(value['config'][:2], rows)
        self.assertTrue(value['include_instructions'])
        self.assertEqual(value['max_context_tokens'], 600)
        self.assertEqual(value['bundled'], {'opaque': True})
        self.assertEqual(sum(change.path == self.manager().native_path for change in plan.changes), 1)

    def test_existing_unknown_path_row_and_name_selector_block(self):
        self.skill(control={'enabled': False})
        self.save()
        path = str((self.root / 'home/.agents/skills/tiny/SKILL.md').resolve())
        for row in ({'path': path, 'enabled': False}, {'name': 'tiny', 'enabled': True}):
            self.set_native({'skills': {'config': [row]}})
            plan = self.manager().plan()
            self.assertFalse(plan.report['ready'])
            with self.assertRaises(ValueError):
                plan.apply(self.state)

    def test_duplicate_managed_row_and_activation_drift_block(self):
        self.skill(control={'enabled': False})
        self.save()
        self.deploy()
        value = self.read_native()
        value['skills']['config'] *= 2
        self.set_native(value)
        self.assertFalse(self.manager().plan().report['ready'])
        value['skills']['config'] = value['skills']['config'][:1]
        value['skills']['config'][0]['enabled'] = True
        self.set_native(value)
        self.assertFalse(self.manager().plan(run_policy={'default': 'source_wins'}).report['ready'])

    def test_omitted_controls_preserve_activation_and_invocation(self):
        self.skill(control={'enabled': False, 'allow_implicit_invocation': False})
        self.save()
        self.deploy()
        self.profile['resources']['skills']['tiny@test']['options'] = {}
        self.save_profile()
        plan = self.deploy()
        self.assertFalse(self.read_native()['skills']['config'][0]['enabled'])
        path = self.root / 'home/.agents/skills/tiny/agents/openai.yaml'
        self.assertFalse(yaml.safe_load(path.read_text())['policy']['allow_implicit_invocation'])
        self.assertTrue(any(row['action'] == 'preserve' and row['path'][-1] == 'enabled' for row in plan.report['decisions']))

    def test_selection_removal_retains_control_and_reselect_checks_it(self):
        self.skill(control={'enabled': False})
        self.save()
        self.deploy()
        selection = self.profile['resources'].pop('skills')
        self.save_profile()
        plan = self.deploy()
        self.assertTrue(any(row['action'] == 'residual' for row in plan.report['decisions']))
        self.assertFalse(self.read_native()['skills']['config'][0]['enabled'])
        self.profile['resources']['skills'] = selection
        self.save_profile()
        self.deploy()

    def test_skill_rename_moves_only_the_owned_selector(self):
        root = self.skill(control={'enabled': False})
        self.save()
        self.deploy()
        (root / 'SKILL.md').write_text('---\nname: renamed\ndescription: Test\n---\nbody\n')
        self.profile['resources']['skills']['tiny@test']['options'] = {}
        self.save()
        self.deploy()
        row = self.read_native()['skills']['config'][0]
        self.assertTrue(row['path'].endswith('/renamed/SKILL.md'))
        self.assertFalse(row['enabled'])
        self.assertFalse((self.root / 'home/.agents/skills/tiny/SKILL.md').exists())

    def test_install_root_relocation_with_existing_control_blocks(self):
        self.skill(control={'enabled': False})
        self.save()
        self.deploy()
        self.machine['bindings']['paths']['skills'] = str(self.root / 'other-home/.agents/skills')
        self.save_profile()
        self.assertFalse(self.manager().plan().report['ready'])

    def test_policy_keep_target_and_specific_run_policy_precedence(self):
        self.skill(control={'enabled': False})
        self.save()
        self.deploy()
        self.profile['resources']['skills']['tiny@test']['options']['control']['enabled'] = True
        key = '/resources/skills/tiny@test/options/control/enabled'
        self.profile['merge_policy'] = {'fields': {key: 'keep_target'}}
        self.save_profile()
        plan = self.deploy()
        self.assertFalse(self.read_native()['skills']['config'][0]['enabled'])
        self.assertTrue(any(row['path'][-1] == 'enabled' and row['policy_origin'] == 'profile_field' for row in plan.report['decisions']))
        self.deploy(run_policy={'fields': {key: 'source_wins'}})
        self.assertTrue(self.read_native()['skills']['config'][0]['enabled'])

    def test_fail_if_different_blocks_body_and_native_config(self):
        self.skill(control={'enabled': False})
        self.save()
        self.deploy()
        before = self.manager().native_path.read_bytes()
        self.profile['resources']['skills']['tiny@test']['options']['control']['enabled'] = True
        self.save_profile()
        plan = self.manager().plan(run_policy={'default': 'fail_if_different'})
        self.assertFalse(plan.report['ready'])
        self.assertTrue(any(row.get('code') == 'resource_option_conflict' for row in plan.report['blockers']))
        with self.assertRaises(ValueError):
            plan.apply(self.state)
        self.assertEqual(self.manager().native_path.read_bytes(), before)

    def test_invocation_field_policy_preserves_current_rendered_body(self):
        self.skill(control={'allow_implicit_invocation': False})
        self.save()
        self.deploy()
        path = self.root / 'home/.agents/skills/tiny/agents/openai.yaml'
        before = path.read_bytes()
        self.profile['resources']['skills']['tiny@test']['options']['control']['allow_implicit_invocation'] = True
        self.profile['merge_policy'] = {'fields': {'/resources/skills/tiny@test/options/control/allow_implicit_invocation': 'keep_target'}}
        self.save_profile()
        self.deploy()
        self.assertEqual(path.read_bytes(), before)

    def test_claude_invocation_omission_and_policy_work_with_native_activation(self):
        self.skill(agent='claude-code', control={'disable-model-invocation': True})
        self.save()
        self.deploy()
        self.profile['resources']['skills']['tiny@test']['options'] = {}
        self.save_profile()
        self.deploy()
        self.assertIn('disable-model-invocation: true', (self.native / 'skills/tiny/SKILL.md').read_text())
        self.profile['resources']['skills']['tiny@test']['options'] = {'control': {'enabled': False}}
        self.save_profile()
        self.deploy()
        self.assertEqual(json.loads(self.manager().native_path.read_text())['skillOverrides']['tiny'], 'off')

    def test_deleted_owned_invocation_file_is_not_silently_adopted(self):
        self.skill(control={'allow_implicit_invocation': False})
        self.save()
        self.deploy()
        (self.root / 'home/.agents/skills/tiny/agents/openai.yaml').unlink()
        self.profile['resources']['skills']['tiny@test']['options'] = {}
        self.save_profile()
        self.assertFalse(self.manager().plan().report['ready'])

    def test_preview_stale_on_unrelated_native_list_change(self):
        self.skill(control={'enabled': False})
        self.save()
        plan = self.manager().plan()
        self.set_native({'skills': {'config': [{'name': 'other', 'enabled': False}]}})
        with self.assertRaises(PlanStale):
            plan.apply(self.state)

    def test_activation_and_body_restore_on_failure_and_independent_rollback(self):
        self.skill(control={'enabled': False})
        self.save()
        plan = self.manager().plan()
        def fail(path, content, mode):
            if path == self.manager().state_path:
                raise OSError('isolated failure')
            atomic_write(path, content, mode)
        with self.assertRaises(OSError):
            plan.apply(self.state, writer=fail)
        self.assertFalse(self.manager().native_path.exists())
        result = self.deploy()
        rollback = rollback_plan(self.manager(), Path(result.report['recovery']).parent.name)
        self.assertTrue(rollback.report['ready'], rollback.report['blockers'])
        rollback.apply(self.state)
        self.assertFalse(self.manager().native_path.exists())

    def test_invalid_resource_policy_paths_and_list_policies_rejected(self):
        self.skill(control={'enabled': False})
        self.save()
        for key, action in (('/resources/skills/tiny@test/options/control/unknown', 'keep_target'),
                            ('/resources/skills/not-selected@test/options/control/enabled', 'keep_target'),
                            ('/resources/skills/tiny@test/options/control/enabled', 'replace')):
            with self.assertRaises(ValueError):
                self.manager().plan(run_policy={'fields': {key: action}})

    def test_mcp_options_policy_preserves_order_and_reports_overrides(self):
        self.profile['agent_type'] = 'codex'
        self.add('server', 'mcp', {'server.json': json.dumps({'name': 'remote', 'server': {'url': 'https://example.invalid/mcp'}})}, 'server.json',
                 options={'enabled_tools': ['one', 'two'], 'tools': {'one': {'approval_mode': 'prompt'}}})
        self.save()
        self.deploy()
        self.profile['resources']['mcp_servers']['server@test']['options'] = {'enabled_tools': ['two', 'one'], 'tools': {'one': {'approval_mode': 'auto'}}}
        self.profile['merge_policy'] = {'fields': {'/resources/mcp_servers/server@test/options/enabled_tools': 'keep_target'}}
        self.save_profile()
        plan = self.deploy()
        self.assertEqual(self.read_native()['mcp_servers']['remote']['enabled_tools'], ['one', 'two'])
        self.assertEqual(self.read_native()['mcp_servers']['remote']['tools']['one']['approval_mode'], 'auto')
        self.assertTrue(any(row['policy_origin'] == 'profile_field' for row in plan.report['decisions']))

    def test_plugin_enabled_uses_resource_field_policy(self):
        from claude_config.core.native_plugins import PluginOperation
        from test_profile_plugins import FakeInstaller
        installer = FakeInstaller()
        self.add('tiny', 'plugin', {'.claude-plugin/plugin.json': json.dumps({'name': 'tiny', 'version': '1.0.0'})}, '.claude-plugin/plugin.json', options={'enabled': False})
        self.save()
        with patch.object(PluginOperation, '_call', lambda operation, args, work: installer(operation, args, work)):
            self.deploy()
            self.profile['resources']['plugins']['tiny@test']['options']['enabled'] = True
            self.profile['merge_policy'] = {'fields': {'/resources/plugins/tiny@test/options/enabled': 'keep_target'}}
            self.save_profile()
            plan = self.deploy()
            desired = json.loads(self.manager().state_path.read_text())['resources']['tiny@test']['native']['plugin']
            self.assertFalse(desired['enabled'])
            self.assertFalse(json.loads(self.manager().native_path.read_text())['enabledPlugins'][desired['selector']])
            self.assertFalse(plan.native_operations)
            self.assertTrue(any(row['path'][-1] == 'enabled' and row['policy_origin'] == 'profile_field' for row in plan.report['decisions']))

    def test_hook_timeout_policy_and_conflict_report_target_each_handler(self):
        self.add('check', 'hook', {'hook.json': json.dumps({'hooks': {'PreToolUse': [{'matcher': 'Bash', 'hooks': [{'type': 'command',
                  'command': {'executable': {'binding': 'program'}, 'args': ['public literal']}}]}]}})}, 'hook.json', mode='lifecycle',
                 options={'timeout': 5}, requires={'bindings': {'program': {'kind': 'executable', 'required': True}}}, bindings={'program': 'executables.bash'})
        self.save()
        self.deploy()
        self.profile['resources']['hooks']['check@test']['options']['timeout'] = 8
        key = '/resources/hooks/check@test/options/timeout'
        self.profile['merge_policy'] = {'fields': {key: 'keep_target'}}
        self.save_profile()
        plan = self.deploy()
        value = json.loads(self.manager().native_path.read_text())
        self.assertEqual(value['hooks']['PreToolUse'][0]['hooks'][0]['timeout'], 5)
        self.assertTrue(any(row['path'][-1] == 'timeout' and row.get('native_path') for row in plan.report['decisions']))
        plan = self.manager().plan(run_policy={'fields': {key: 'fail_if_different'}})
        self.assertFalse(plan.report['ready'])

    def test_profile_resource_policy_does_not_block_ordinary_export(self):
        self.skill(control={'enabled': False})
        self.profile['merge_policy'] = {'fields': {'/resources/skills/tiny@test/options/control/enabled': 'keep_target'}}
        self.save()
        self.deploy()
        plan = self.manager().export(['/settings/model'])
        self.assertTrue(plan.report['ready'], plan.report['blockers'])

    def test_resource_list_replace_is_allowed_but_append_unique_is_not(self):
        self.profile['agent_type'] = 'codex'
        self.add('server', 'mcp', {'server.json': json.dumps({'name': 'remote', 'server': {'url': 'https://example.invalid/mcp'}})}, 'server.json', options={'enabled_tools': ['one']})
        self.save()
        self.deploy()
        self.profile['resources']['mcp_servers']['server@test']['options']['enabled_tools'] = []
        self.save_profile()
        key = '/resources/mcp_servers/server@test/options/enabled_tools'
        self.deploy(run_policy={'fields': {key: 'replace'}})
        self.assertEqual(self.read_native()['mcp_servers']['remote']['enabled_tools'], [])
        with self.assertRaises(ValueError):
            self.manager().plan(run_policy={'fields': {key: 'append_unique'}})


if __name__ == '__main__':
    unittest.main()
