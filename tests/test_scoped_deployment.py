"""Partial deployment keeps unrelated runtime writes and managed state intact."""
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

from test_profile_resources import ResourceFixture
from claude_config.core.deployment_scope import DeploymentScope
from claude_config.core.native_plugins import PluginOperation
from claude_config.core.transaction import PlanStale, atomic_write
from claude_config.core.schema import SchemaError


class ScopedResourceTests(ResourceFixture):
    def skill(self, name, body='old', **kwargs):
        return self.add(name, 'skill', {'SKILL.md': f'---\nname: {name}\ndescription: test\n---\n{body}'}, 'SKILL.md', **kwargs)

    def deploy(self, selection=None):
        plan = self.manager().plan(selection=selection)
        self.assertTrue(plan.report['ready'], plan.report['blockers'])
        plan.apply(self.state)
        return plan

    def test_single_skill_preserves_unrelated_drift_and_management(self):
        one = self.skill('one')
        self.skill('two')
        self.save(); self.deploy()
        before = json.loads(self.manager().state_path.read_text())
        native = self.native / 'settings.json'
        native.write_text('{"model":"local-change", "runtime":1}')
        two = self.native / 'skills/two/SKILL.md'
        two.write_text('user-edited unrelated skill')
        (one / 'SKILL.md').write_text('---\nname: one\ndescription: test\n---\nnew')
        self.save()
        plan = self.manager().plan(selection=DeploymentScope(resources=('one@test',)))
        self.assertTrue(plan.report['ready'], plan.report['blockers'])
        self.assertNotIn(native, plan.paths)
        native.write_text('{"model":"another-local-change", "runtime":2}')
        plan.apply(self.state)
        after = json.loads(self.manager().state_path.read_text())
        self.assertEqual(after['resources']['two@test'], before['resources']['two@test'])
        self.assertEqual(after['managed'], before['managed'])
        self.assertEqual(after['declared'], before['declared'])
        self.assertEqual(json.loads(native.read_text())['runtime'], 2)
        self.assertEqual(two.read_text(), 'user-edited unrelated skill')
        self.assertTrue((self.native / 'skills/one/SKILL.md').read_text().endswith('new'))
        self.assertFalse(self.manager().plan().report['ready'])

    def test_settings_scope_preserves_resource_state_and_files(self):
        self.skill('one'); self.save(); self.deploy()
        before = json.loads(self.manager().state_path.read_text())
        skill = self.native / 'skills/one/SKILL.md'; skill.write_text('local body')
        self.profile['settings']['model'] = 'updated'; self.save_profile()
        self.deploy(DeploymentScope(mode='settings'))
        after = json.loads(self.manager().state_path.read_text())
        self.assertEqual(before['resources'], after['resources'])
        self.assertEqual(skill.read_text(), 'local body')
        self.assertEqual(json.loads((self.native / 'settings.json').read_text())['model'], 'updated')

    def test_one_field_preserves_other_settings_baseline(self):
        self.profile['settings']['effortLevel'] = 'high'; self.save(); self.deploy()
        before = json.loads(self.manager().state_path.read_text())
        self.profile['settings'].update(model='new-model', effortLevel='low'); self.save_profile()
        self.deploy(DeploymentScope(fields=('/settings/effortLevel',)))
        after = json.loads(self.manager().state_path.read_text())
        self.assertEqual(after['declared']['model'], before['declared']['model'])
        self.assertEqual(after['managed']['model'], before['managed']['model'])
        native = json.loads((self.native / 'settings.json').read_text())
        self.assertEqual(native['model'], 'selected'); self.assertEqual(native['effortLevel'], 'low')

    def test_dependency_closure_expands_only_already_selected_resources(self):
        self.skill('dep'); self.skill('one', requires={'resources':['dep@test']}); self.skill('other'); self.save()
        plan = self.deploy(DeploymentScope(resources=('one@test',)))
        self.assertEqual(plan.report['deployment_scope']['expanded']['resources'], ['dep@test'])
        self.assertFalse((self.native / 'skills/other').exists())
        self.assertFalse((self.native / 'settings.json').exists())
        self.assertEqual(set(json.loads(self.manager().state_path.read_text())['resources']), {'one@test','dep@test'})

    def test_settings_binding_brings_resource_and_resource_update_refreshes_binding(self):
        self.hook(); self.skill('other'); self.save()
        self.deploy(DeploymentScope(fields=('/settings/statusLine/command',)))
        first = json.loads((self.native / 'settings.json').read_text())['statusLine']['command']
        (self.repo / 'packages/status/run.sh').write_text('#!/bin/sh\nprintf new\n'); self.save()
        plan = self.deploy(DeploymentScope(resources=('status@test',)))
        second = json.loads((self.native / 'settings.json').read_text())['statusLine']['command']
        self.assertNotEqual(first, second)
        self.assertIn('/settings/statusLine/command', plan.report['deployment_scope']['expanded']['fields'])
        self.assertFalse((self.native / 'skills/other').exists())

    def test_resource_controls_are_part_of_effective_scope(self):
        self.skill('one',options={'control':{'enabled':False}});self.save()
        plan=self.deploy(DeploymentScope(resources=('one@test',)))
        self.assertEqual(json.loads((self.native/'settings.json').read_text())['skillOverrides']['one'],'off')
        self.assertIn('/settings/skillOverrides/one',plan.report['deployment_scope']['effective']['fields'])

    def test_scope_and_selectors_are_part_of_plan_identity(self):
        self.skill('one');self.save()
        one=self.manager().plan(selection=DeploymentScope(resources=('one@test',)))
        group=self.manager().plan(selection=DeploymentScope(groups=('skills',)))
        self.assertNotEqual(one.report['plan_id'],group.report['plan_id'])
        with self.assertRaises(SchemaError):self.manager().plan(selection=DeploymentScope(resources=('missing@test',)))
        with self.assertRaises(SchemaError):self.manager().plan(selection=DeploymentScope(fields=('/settings/absent',)))

    def test_partial_update_refuses_uncompleted_profile_switch(self):
        self.skill('one');self.save();self.deploy()
        state=json.loads(self.manager().state_path.read_text());state['profile']='profiles/agents/claude-code/other.yaml'
        self.manager().state_path.write_text(json.dumps(state))
        with self.assertRaisesRegex(SchemaError,'Profile switch'):
            self.manager().plan(selection=DeploymentScope(resources=('one@test',)))

    def test_partial_update_still_guards_target_edits(self):
        self.skill('one');self.save();self.deploy()
        plan=self.manager().plan(selection=DeploymentScope(resources=('one@test',)))
        target=self.native/'skills/one/SKILL.md';target.write_text('manual')
        with self.assertRaises(PlanStale):plan.apply(self.state)
        self.assertEqual(target.read_text(),'manual')

    def test_read_sibling_from_installed_version_not_new_unapplied_source(self):
        self.skill('two');self.skill('one','[two](../two/SKILL.md)');self.save();self.deploy()
        before=(self.native/'skills/two/SKILL.md').read_bytes()
        (self.repo/'packages/two/SKILL.md').write_text('---\nname: renamed\ndescription: test\n---\nnew')
        self.save()
        self.deploy(DeploymentScope(resources=('one@test',)))
        self.assertEqual((self.native/'skills/two/SKILL.md').read_bytes(),before)
        self.assertFalse((self.native/'skills/renamed').exists())

    def test_scoped_sibling_dependency_change_invalidates_plan(self):
        self.skill('two');self.skill('one','[two](../two/SKILL.md)');self.save();self.deploy()
        plan=self.manager().plan(selection=DeploymentScope(resources=('one@test',)))
        (self.native/'skills/two/SKILL.md').write_text('modified linked dependency')
        with self.assertRaises(PlanStale):plan.apply(self.state)

    def test_cli_plan_apply_and_scope_change_rejection(self):
        from contextlib import redirect_stdout, redirect_stderr
        from io import StringIO
        from claude_config.profile import _main
        self.skill('one'); self.save()
        base=['--agent','claude-code','--machine',str(self.machine_path),'--format','json']
        def run(args):
            out=StringIO()
            with patch('claude_config.core.manager.NativeProbe', return_value=lambda *a:[{'name':'fixture','status':'passed'}]),redirect_stdout(out),redirect_stderr(StringIO()):
                code=_main(args+base)
            return code,json.loads(out.getvalue())
        code,plan=run(['plan','--resource','one@test'])
        self.assertEqual(code,0)
        code,rejected=run(['apply','--resource-group','skills','--expect-plan-id',plan['plan_id']])
        self.assertEqual(code,1);self.assertEqual(rejected['errors'][0]['code'],'plan_stale')
        self.assertFalse((self.native/'skills/one').exists())
        code,result=run(['apply','--resource','one@test','--expect-plan-id',plan['plan_id']])
        self.assertEqual(code,0,result.get('errors'))
        self.assertEqual(result['activation'][0]['mode'],'reinvoke_skill')
        self.assertFalse(result['activation'][0]['verified'])

    def test_codex_scoped_skill_and_control_preserve_agent_settings(self):
        import tomlkit
        self.profile['agent_type']='codex'
        self.machine['bindings']['paths']={'skills':str(self.root/'.agents/skills')}
        self.skill('one',bindings={'install_root':'paths.skills'},options={'control':{'enabled':False}},
                   requires={'bindings':{'install_root':{'kind':'path','required':True}}})
        self.profile['settings']={'model':'selected','model_reasoning_effort':'high'}
        self.save()
        self.native.mkdir(exist_ok=True)
        (self.native/'config.toml').write_text('model = "keep"\n')
        self.deploy(DeploymentScope(resources=('one@test',)))
        native=tomlkit.parse((self.native/'config.toml').read_text())
        self.assertEqual(native['model'],'keep')
        self.assertFalse(native['skills']['config'][0]['enabled'])
        state=json.loads(self.manager().state_path.read_text())
        self.assertEqual(state['managed'],{})

    def test_retained_consumer_blocks_destructive_partial_identity_change(self):
        two = self.skill('two'); self.skill('one','[two](../two/SKILL.md)')
        self.save(); self.deploy()
        (two/'SKILL.md').write_text('---\nname: renamed\ndescription: test\n---\nnew')
        self.save()
        plan = self.manager().plan(selection=DeploymentScope(resources=('two@test',)))
        self.assertFalse(plan.report['ready'])
        self.assertTrue(any(c.get('code')=='scope_expansion_required' for c in plan.report['checks']))
        self.assertTrue((self.native/'skills/two/SKILL.md').exists())

    def test_partial_failure_rolls_back_only_selected_files(self):
        one=self.skill('one');self.skill('two');self.save();self.deploy()
        outside=self.native/'skills/two/SKILL.md'
        (one/'SKILL.md').write_text('---\nname: one\ndescription: test\n---\nnew');self.save()
        plan=self.manager().plan(selection=DeploymentScope(resources=('one@test',)))
        before=(self.native/'skills/one/SKILL.md').read_bytes()
        def writer(path,content,mode):
            if path.resolve()==self.manager().state_path.resolve():
                outside.write_text('concurrent external edit')
                raise OSError('failure before baseline')
            atomic_write(path,content,mode)
        with self.assertRaises(OSError):plan.apply(self.state,writer=writer)
        self.assertEqual((self.native/'skills/one/SKILL.md').read_bytes(),before)
        self.assertEqual(outside.read_text(),'concurrent external edit')

    def test_unselected_plugin_is_not_observed_by_skill_only_plan(self):
        self.skill('one')
        self.add('tiny','plugin',{'.claude-plugin/plugin.json':json.dumps({'name':'tiny','version':'1.0.0'})},'.claude-plugin/plugin.json')
        self.save()
        with patch.object(PluginOperation,'__init__',side_effect=AssertionError('unrelated plugin observed')):
            plan=self.deploy(DeploymentScope(resources=('one@test',)))
        self.assertFalse(plan.native_operations)

    def test_resource_group_does_not_update_other_kinds(self):
        self.skill('one');self.hook();self.save()
        self.deploy(DeploymentScope(groups=('skills',)))
        self.assertFalse((self.native/'settings.json').exists())
        self.assertEqual(set(json.loads(self.manager().state_path.read_text())['resources']),{'one@test'})

    def test_repeated_partial_deployment_has_no_changes(self):
        self.skill('one');self.save()
        selector=DeploymentScope(resources=('one@test',))
        self.deploy(selector);plan=self.deploy(selector)
        self.assertTrue(all(r['status']=='unchanged' for r in plan.report['results']))

    def test_new_reference_precedes_entry_and_old_reference_removal(self):
        root=self.skill('one','[old](old.md)');(root/'old.md').write_text('old');self.save();self.deploy()
        (root/'new.md').write_text('new');(root/'SKILL.md').write_text('---\nname: one\ndescription: test\n---\n[new](new.md)');(root/'old.md').unlink();self.save()
        plan=self.manager().plan(selection=DeploymentScope(resources=('one@test',)))
        changes=[c for c in plan.changes if c.path.parent==(self.native/'skills/one').resolve()]
        names=[c.path.name for c in changes]
        self.assertLess(names.index('new.md'),names.index('SKILL.md'))
        self.assertLess(names.index('SKILL.md'),names.index('old.md'))
        plan.apply(self.state)
        self.assertFalse((self.native/'skills/one/old.md').exists())


class ScopeArgumentTests(unittest.TestCase):
    def test_conflicting_and_duplicate_selectors(self):
        for kwargs in [dict(mode='settings',resources=('x@a',)),dict(resources=('x@a','x@a')),
                       dict(groups=('bad',)),dict(resources=('x@a',),fields=('/settings/model',))]:
            with self.subTest(kwargs=kwargs),self.assertRaises(SchemaError):DeploymentScope(**kwargs)

    def test_partial_merge_does_not_replace_an_unrelated_parent_value(self):
        from claude_config.core.deployment_scope import update_leaves
        target = {'env': 'user-value'}
        with self.assertRaises(SchemaError):update_leaves(target, {'env': {'one': 'new'}})
        self.assertEqual(target, {'env': 'user-value'})

    def test_cli_rejects_partial_switch_and_unreviewed_apply(self):
        from claude_config.profile import _main
        from claude_config.profile import UsageError
        for args in [['switch','--agent','codex','--profile','x','--scope','settings'],
                     ['apply','--agent','codex','--resource','x@a'],
                     ['plan','--app','codex-app','--scope','resources'],
                     ['plan','--agent','codex','--scope','settings','--field','/settings/model']]:
            with self.subTest(args=args),self.assertRaises(UsageError):_main(args)
