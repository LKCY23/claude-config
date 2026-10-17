"""Agent resource usage policies, omission, body updates and drift boundaries."""
from pathlib import Path
import sys
import unittest

import tomlkit
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from claude_config.core.native import parse_yaml
from claude_config.core.recovery import rollback_plan
from claude_config.core.resources import frontmatter
from claude_config.core.schema import SchemaError
from claude_config.core.transaction import PlanStale
from test_profile_resources import ResourceFixture


class AgentOptionTests(ResourceFixture):
    def role(self, agent='claude-code', *, source_options=None, options=None):
        self.profile['agent_type'] = agent
        source_options = source_options or {}
        if agent == 'claude-code':
            entry = 'role.md'
            content = '---\n' + yaml.safe_dump({'name': 'reviewer', 'description': 'Review', **source_options}, sort_keys=False) + '---\nReview carefully\n'
        else:
            entry = 'role.toml'
            content = '# source comment\n' + tomlkit.dumps({'name': 'reviewer', 'description': 'Review',
                                                          'developer_instructions': 'Review carefully', **source_options})
        root = self.add('review', 'agent', {entry: content}, entry, options=options)
        self.save()
        return root / entry

    def deployed(self):
        suffix = '.md' if self.profile['agent_type'] == 'claude-code' else '.toml'
        return self.native / 'agents' / ('reviewer' + suffix)

    def value(self):
        content = self.deployed().read_bytes()
        return frontmatter(content)[0] if self.profile['agent_type'] == 'claude-code' else tomlkit.parse(content.decode()).unwrap()

    def deploy(self, **kwargs):
        plan = self.manager().plan(**kwargs)
        self.assertTrue(plan.report['ready'], plan.report['blockers'])
        plan.apply(self.state)
        return plan

    def change_options(self, options):
        self.profile['resources']['agents']['review@test']['options'] = options
        self.save_profile()

    def test_claude_model_policy_and_run_field_precedence(self):
        self.role(options={'model': 'sonnet'})
        self.deploy()
        self.change_options({'model': 'opus'})
        key = '/resources/agents/review@test/options/model'
        self.profile['merge_policy'] = {'fields': {key: 'keep_target'}}
        self.save_profile()
        plan = self.deploy()
        self.assertEqual(self.value()['model'], 'sonnet')
        decision = next(row for row in plan.report['decisions'] if row['path'] == ['resources', 'agents', 'review@test', 'options', 'model'])
        self.assertEqual(decision['policy_origin'], 'profile_field')
        self.assertEqual(decision['source']['kind'], 'profile-resource-options')
        self.assertIn('target_fingerprint', decision)
        self.deploy(run_policy={'fields': {key: 'source_wins'}})
        self.assertEqual(self.value()['model'], 'opus')

    def test_codex_model_and_reasoning_have_independent_policies(self):
        self.role('codex', options={'model': 'first', 'model_reasoning_effort': 'high'})
        self.deploy()
        self.change_options({'model': 'second', 'model_reasoning_effort': 'low'})
        key = '/resources/agents/review@test/options/model_reasoning_effort'
        self.profile['merge_policy'] = {'fields': {key: 'keep_target'}}
        self.save_profile()
        self.deploy()
        self.assertEqual(self.value()['model'], 'second')
        self.assertEqual(self.value()['model_reasoning_effort'], 'high')
        self.assertIn('# source comment', self.deployed().read_text())

    def test_omitted_options_preserve_current_without_writing_defaults(self):
        self.role('codex', options={'model': 'chosen', 'model_reasoning_effort': 'high'})
        self.deploy()
        before = self.deployed().read_bytes()
        self.change_options({})
        plan = self.deploy()
        self.assertEqual(self.deployed().read_bytes(), before)
        self.assertEqual(self.value()['model_reasoning_effort'], 'high')
        self.assertTrue(any(row['action'] == 'preserve' for row in plan.report['decisions']))

    def test_new_definition_without_options_has_no_model_defaults(self):
        self.role('codex')
        self.deploy()
        self.assertNotIn('model', self.value())
        self.assertNotIn('model_reasoning_effort', self.value())

    def test_source_defined_model_is_explicit_and_has_catalog_provenance(self):
        self.role(source_options={'model': 'inherit'}, options={'model': 'sonnet'})
        self.deploy()
        self.change_options({})
        plan = self.deploy()
        self.assertEqual(self.value()['model'], 'inherit')
        decision = next(row for row in plan.report['decisions'] if row['path'][-1] == 'model' and 'options' in row['path'])
        self.assertEqual(decision['source']['kind'], 'resource-usage')

    def test_new_fixed_body_updates_while_policy_keeps_model(self):
        source = self.role(options={'model': 'sonnet'})
        self.deploy()
        source.write_text(source.read_text().replace('Review carefully', 'Review updated code'))
        self.profile['resources']['agents']['review@test']['options'] = {'model': 'opus'}
        self.profile['merge_policy'] = {'fields': {'/resources/agents/review@test/options/model': 'keep_target'}}
        self.save()
        self.deploy()
        self.assertIn('Review updated code', self.deployed().read_text())
        self.assertEqual(self.value()['model'], 'sonnet')
        self.assertNotIn('model: sonnet', source.read_text())

    def test_fail_if_different_blocks_all_resource_and_settings_writes(self):
        self.role(options={'model': 'sonnet'})
        self.deploy()
        before = self.deployed().read_bytes()
        native_before = self.manager().native_path.read_bytes()
        self.change_options({'model': 'opus'})
        self.profile['settings']['model'] = 'another-main'
        self.save_profile()
        plan = self.manager().plan(run_policy={'fields': {'/resources/agents/review@test/options/model': 'fail_if_different'}})
        self.assertFalse(plan.report['ready'])
        self.assertTrue(any(row.get('code') == 'resource_option_conflict' for row in plan.report['blockers']))
        with self.assertRaises(ValueError):
            plan.apply(self.state)
        self.assertEqual(self.deployed().read_bytes(), before)
        self.assertEqual(self.manager().native_path.read_bytes(), native_before)

    def test_body_or_model_external_drift_is_not_bypassed_by_source_wins(self):
        self.role(options={'model': 'sonnet'})
        self.deploy()
        before = self.deployed().read_text()
        for text in (before.replace('Review carefully', 'Hand-edited body'), before.replace('sonnet', 'opus')):
            with self.subTest(text=text):
                self.deployed().write_text(text)
                plan = self.manager().plan(run_policy={'default': 'source_wins'})
                self.assertFalse(plan.report['ready'])
                self.assertTrue(any(row.get('code') == 'resource_drift' for row in plan.report['blockers']))
                self.assertEqual(self.deployed().read_text(), text)

    def test_deleted_definition_blocks_before_parsing_or_recreating(self):
        self.role(options={'model': 'sonnet'})
        self.deploy()
        self.deployed().unlink()
        plan = self.manager().plan()
        self.assertFalse(plan.report['ready'])
        self.assertFalse(self.deployed().exists())

    def test_unknown_native_definition_is_never_adopted_by_an_option_policy(self):
        self.role(options={'model': 'sonnet'})
        path = self.deployed()
        path.parent.mkdir(parents=True)
        path.write_text('---\nname: reviewer\ndescription: local\nmodel: sonnet\n---\nlocal\n')
        self.assertFalse(self.manager().plan(run_policy={'default': 'keep_target'}).report['ready'])
        self.assertIn('local', path.read_text())

    def test_resource_policy_scope_and_list_policies_are_strict(self):
        self.role(options={'model': 'sonnet'})
        for path, action in (('/resources/agents/review@test/options/tools', 'keep_target'),
                             ('/resources/agents/review@test/options/model', 'replace'),
                             ('/resources/agents/review@test/options/model_reasoning_effort', 'source_wins'),
                             ('/resources/agents/unknown@test/options/model', 'keep_target')):
            with self.subTest(path=path, action=action), self.assertRaises(SchemaError):
                self.manager().plan(run_policy={'fields': {path: action}})

    def test_invalid_model_and_reasoning_types_block(self):
        self.role('codex', options={'model': 'chosen'})
        for options in ({'model': ''}, {'model': True}, {'model_reasoning_effort': ''}, {'model_reasoning_effort': False}):
            self.change_options(options)
            self.assertFalse(self.manager().plan().report['ready'])

    def test_removed_selection_retains_and_reselection_preserves_options(self):
        self.role(options={'model': 'sonnet'})
        self.deploy()
        selection = self.profile['resources'].pop('agents')
        self.save_profile()
        plan = self.deploy()
        self.assertTrue(any(row['action'] == 'residual' for row in plan.report['decisions']))
        selection['review@test']['options'] = {}
        self.profile['resources']['agents'] = selection
        self.save_profile()
        self.deploy()
        self.assertEqual(self.value()['model'], 'sonnet')

    def test_identity_update_moves_owned_definition_and_keeps_model(self):
        source = self.role(options={'model': 'sonnet'})
        self.deploy()
        old = self.deployed()
        source.write_text(source.read_text().replace('name: reviewer', 'name: renamed'))
        self.profile['resources']['agents']['review@test']['options'] = {}
        self.save()
        self.deploy()
        self.assertFalse(old.exists())
        new = self.native / 'agents/renamed.md'
        self.assertEqual(frontmatter(new.read_bytes())[0]['model'], 'sonnet')

    def test_export_roundtrip_preserves_usage_and_native_state(self):
        self.role('codex', options={'model': 'chosen', 'model_reasoning_effort': 'high'})
        self.deploy()
        self.change_options({})
        before = self.deployed().read_bytes()
        state = self.manager().state_path.read_bytes()
        plan = self.manager().export([], resource_refs=['review@test'])
        self.assertTrue(plan.report['ready'], plan.report['blockers'])
        plan.apply(self.state)
        profile = parse_yaml((self.data / self.machine['agents']['codex']['profile']).read_bytes())
        self.assertEqual(profile['resources']['agents']['review@test']['options'], {'model': 'chosen', 'model_reasoning_effort': 'high'})
        self.assertEqual(self.deployed().read_bytes(), before)
        self.assertEqual(self.manager().state_path.read_bytes(), state)

    def test_stale_native_agent_after_plan_cannot_be_written(self):
        self.role(options={'model': 'sonnet'})
        self.deploy()
        self.change_options({'model': 'opus'})
        plan = self.manager().plan()
        self.deployed().write_text(self.deployed().read_text() + '\nlate edit\n')
        with self.assertRaises(PlanStale):
            plan.apply(self.state)
        self.assertIn('late edit', self.deployed().read_text())

    def test_independent_rollback_restores_previous_model_and_body(self):
        self.role('codex', options={'model': 'before', 'model_reasoning_effort': 'high'})
        self.deploy()
        before = self.deployed().read_bytes()
        self.change_options({'model': 'after', 'model_reasoning_effort': 'low'})
        result = self.deploy().report
        plan = rollback_plan(self.manager(), Path(result['recovery']).parent.name)
        self.assertTrue(plan.report['ready'])
        plan.apply(self.state)
        self.assertEqual(self.deployed().read_bytes(), before)


if __name__ == '__main__':
    unittest.main()
