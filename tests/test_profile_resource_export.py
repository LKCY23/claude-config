import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

import tomlkit
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from claude_config.core.native_plugins import PluginOperation
from claude_config.core.transaction import PlanStale
from test_profile_resources import ResourceFixture
from test_profile_plugins import FakeInstaller


class ResourceExportTests(ResourceFixture):
    def deploy(self):
        plan = self.manager().plan()
        self.assertTrue(plan.report['ready'], plan.report['blockers'])
        plan.apply(self.state)
        return plan

    def export(self, ref, fields=(), policy=None):
        return self.manager().export(fields, resource_refs=[ref], run_policy=policy)

    def profile_path(self):
        return self.data / self.machine['agents'][self.profile['agent_type']]['profile']

    def skill(self):
        self.profile['agent_type'] = 'codex'
        self.machine['bindings']['paths'] = {'skills': str(self.root / 'home/.agents/skills')}
        self.add('tiny', 'skill', {'SKILL.md': '---\nname: tiny\ndescription: test\n---\nbody\n'}, 'SKILL.md',
                 options={'control': {'enabled': False, 'allow_implicit_invocation': False}},
                 requires={'bindings': {'install_root': {'kind': 'path', 'required': True}}}, bindings={'install_root': 'paths.skills'})
        self.save()

    def mcp(self, **kwargs):
        self.profile['agent_type'] = 'codex'
        self.add('server', 'mcp', {'server.json': json.dumps({'name': 'remote', 'server': {'url': 'https://example.invalid/mcp'}})}, 'server.json',
                 options={'enabled': False, 'tool_timeout_sec': 5}, **kwargs)
        self.save()

    def test_skill_usage_export_updates_profile_without_native_or_state_changes(self):
        self.skill()
        self.deploy()
        native_path = self.manager().native_path
        value = tomlkit.parse(native_path.read_text())
        value['skills']['config'][0]['enabled'] = True
        native_path.write_text(tomlkit.dumps(value))
        yaml_path = self.root / 'home/.agents/skills/tiny/agents/openai.yaml'
        yaml_path.write_text(yaml_path.read_text().replace('false', 'true'))
        before, state = native_path.read_bytes(), self.manager().state_path.read_bytes()
        plan = self.export('tiny@test', fields=['/settings/model'])
        self.assertTrue(plan.report['ready'], plan.report['blockers'])
        self.assertFalse(yaml.safe_load(self.profile_path().read_text())['resources']['skills']['tiny@test']['options']['control']['enabled'])
        plan.apply(self.state)
        exported = yaml.safe_load(self.profile_path().read_text())['resources']['skills']['tiny@test']
        self.assertEqual(exported['options']['control'], {'enabled': True, 'allow_implicit_invocation': True})
        self.assertEqual(exported['bindings'], {'install_root': 'paths.skills'})
        self.assertNotIn(str(self.root), self.profile_path().read_text())
        self.assertEqual(native_path.read_bytes(), before)
        self.assertEqual(self.manager().state_path.read_bytes(), state)
        # Export did not silently adopt the externally changed activation.
        self.assertFalse(self.manager().plan().report['ready'])

    def test_export_missing_resource_preserves_declaration_and_existing_options(self):
        self.skill()
        before = self.profile_path().read_bytes()
        plan = self.export('tiny@test')
        self.assertTrue(plan.report['ready'], plan.report['blockers'])
        plan.apply(self.state)
        self.assertEqual(self.profile_path().read_bytes(), before)
        self.assertTrue(any(row['action'] == 'preserve_missing' for row in plan.report['decisions']))

    def test_skill_changed_business_body_and_extra_file_block_export(self):
        self.skill()
        self.deploy()
        root = self.root / 'home/.agents/skills/tiny'
        (root / 'extra.txt').write_text('local body')
        self.assertFalse(self.export('tiny@test').report['ready'])
        (root / 'extra.txt').unlink()
        (root / 'SKILL.md').write_text((root / 'SKILL.md').read_text() + 'manual body change')
        self.assertFalse(self.export('tiny@test').report['ready'])

    def test_skill_ambiguous_activation_blocks(self):
        self.skill()
        self.deploy()
        path = self.manager().native_path
        data = tomlkit.parse(path.read_text())
        data['skills']['config'].append(data['skills']['config'][0])
        path.write_text(tomlkit.dumps(data))
        self.assertFalse(self.export('tiny@test').report['ready'])

    def test_native_option_absence_preserves_profile_option(self):
        self.mcp()
        self.deploy()
        path = self.manager().native_path
        value = tomlkit.parse(path.read_text())
        del value['mcp_servers']['remote']['tool_timeout_sec']
        path.write_text(tomlkit.dumps(value))
        plan = self.export('server@test')
        self.assertTrue(plan.report['ready'], plan.report['blockers'])
        plan.apply(self.state)
        self.assertEqual(yaml.safe_load(self.profile_path().read_text())['resources']['mcp_servers']['server@test']['options']['tool_timeout_sec'], 5)

    def test_mcp_float_usage_export_and_keep_target_policy(self):
        self.mcp()
        self.deploy()
        path = self.manager().native_path
        value = tomlkit.parse(path.read_text())
        value['mcp_servers']['remote']['enabled'] = True
        value['mcp_servers']['remote']['tool_timeout_sec'] = 7.5
        path.write_text(tomlkit.dumps(value))
        plan = self.export('server@test', policy={'fields': {'/resources/mcp_servers/server@test/options/enabled': 'keep_target'}})
        self.assertTrue(plan.report['ready'], plan.report['blockers'])
        plan.apply(self.state)
        options = yaml.safe_load(self.profile_path().read_text())['resources']['mcp_servers']['server@test']['options']
        self.assertFalse(options['enabled'])
        self.assertEqual(options['tool_timeout_sec'], 7.5)

    def test_mcp_local_connection_or_literal_secret_not_exported(self):
        self.mcp()
        self.deploy()
        path = self.manager().native_path
        value = tomlkit.parse(path.read_text())
        value['mcp_servers']['remote']['url'] = 'https://example.invalid/private?token=DO_NOT_PRINT'
        path.write_text(tomlkit.dumps(value))
        plan = self.export('server@test')
        self.assertFalse(plan.report['ready'])
        self.assertNotIn('DO_NOT_PRINT', json.dumps(plan.report))
        with self.assertRaises(ValueError):
            plan.apply(self.state)

    def test_plugin_export_verifies_native_fixed_content_and_does_not_install(self):
        self.add('tiny', 'plugin', {'.claude-plugin/plugin.json': json.dumps({'name': 'tiny', 'version': '1.0.0'})}, '.claude-plugin/plugin.json', options={'enabled': False})
        self.save()
        installer = FakeInstaller()
        with patch.object(PluginOperation, '_call', lambda operation, args, work: installer(operation, args, work)):
            original = self.deploy()
        desired = original.native_operations[0].plugins[0]['desired']
        value = json.loads(self.manager().native_path.read_text())
        value['enabledPlugins'][desired['selector']] = True
        self.manager().native_path.write_text(json.dumps(value))
        with patch.object(PluginOperation, '_call', side_effect=AssertionError('Export is not an install')):
            plan = self.export('tiny@test')
            self.assertTrue(plan.report['ready'], plan.report['blockers'])
            plan.apply(self.state)
        self.assertTrue(yaml.safe_load(self.profile_path().read_text())['resources']['plugins']['tiny@test']['options']['enabled'])

    def test_hook_uniform_controls_export_and_command_drift_block(self):
        self.add('check', 'hook', {'hook.json': json.dumps({'hooks': {'PreToolUse': [{'matcher': 'Bash', 'hooks': [{'type': 'command',
                  'command': {'executable': {'binding': 'program'}, 'args': [{'resource_file': 'run.sh'}]}}]}]}}), 'run.sh': 'exit 0\n'}, 'hook.json', mode='lifecycle',
                 options={'timeout': 5}, requires={'bindings': {'program': {'kind': 'executable', 'required': True}}}, bindings={'program': 'executables.bash'})
        self.save()
        self.deploy()
        value = json.loads(self.manager().native_path.read_text())
        value['hooks']['PreToolUse'][0]['hooks'][0]['timeout'] = 8
        self.manager().native_path.write_text(json.dumps(value))
        plan = self.export('check@test')
        self.assertTrue(plan.report['ready'], plan.report['blockers'])
        plan.apply(self.state)
        self.assertEqual(yaml.safe_load(self.profile_path().read_text())['resources']['hooks']['check@test']['options']['timeout'], 8)
        value['hooks']['PreToolUse'][0]['hooks'][0]['command'] = 'LOCAL_PRIVATE_COMMAND'
        self.manager().native_path.write_text(json.dumps(value))
        plan = self.export('check@test')
        self.assertFalse(plan.report['ready'])
        self.assertNotIn('LOCAL_PRIVATE_COMMAND', json.dumps(plan.report))

    def test_instruction_surrounding_text_is_not_exported(self):
        self.add('guide', 'instruction', {'guide.md': 'fixed guide\n'}, 'guide.md')
        self.save()
        self.deploy()
        path = self.native / 'CLAUDE.md'
        path.write_text('UNOWNED_PRIVATE_TEXT\n' + path.read_text())
        plan = self.export('guide@test')
        self.assertTrue(plan.report['ready'], plan.report['blockers'])
        plan.apply(self.state)
        self.assertNotIn('UNOWNED_PRIVATE_TEXT', self.profile_path().read_text())
        self.assertNotIn('UNOWNED_PRIVATE_TEXT', json.dumps(plan.report))

    def test_rule_and_agent_source_reference_only(self):
        self.add('rule', 'rule', {'rule.md': 'fixed rule\n'}, 'rule.md')
        self.add('review', 'agent', {'role.md': '---\nname: reviewer\ndescription: test\nmodel: inherit\n---\nfixed role\n'}, 'role.md', options={'model': 'sonnet'})
        self.save()
        self.deploy()
        for ref in ('rule@test', 'review@test'):
            plan = self.export(ref)
            self.assertTrue(plan.report['ready'], plan.report['blockers'])
            plan.apply(self.state)
        self.assertNotIn('fixed role', self.profile_path().read_text())

    def test_export_explicit_scope_unrecognized_and_duplicate_refs_rejected(self):
        self.skill()
        with self.assertRaises(ValueError):
            self.manager().export([])
        plan = self.manager().export([], resource_refs=['unknown@test'])
        self.assertFalse(plan.report['ready'])
        self.assertFalse(self.manager().export([], resource_refs=['tiny@test', 'tiny@test']).report['ready'])

    def test_resource_changes_after_preview_make_export_stale(self):
        self.skill()
        self.deploy()
        plan = self.export('tiny@test')
        (self.root / 'home/.agents/skills/tiny/extra.txt').write_text('late file')
        with self.assertRaises(PlanStale):
            plan.apply(self.state)

    def test_cli_resource_export_previews_by_default(self):
        from claude_config import profile as profile_config
        self.skill()
        self.deploy()
        with patch('claude_config.core.manager.NativeProbe', return_value=lambda *args: [{'name': 'fake', 'status': 'passed'}]), patch('builtins.print') as output:
            result = profile_config.main(['export', '--machine', str(self.machine_path), '--agent', 'codex', '--resource', 'tiny@test', '--format', 'json'])
            self.assertEqual(result, 0)
            self.assertTrue(json.loads(output.call_args.args[0])['preview_only'])


if __name__ == '__main__':
    unittest.main()
