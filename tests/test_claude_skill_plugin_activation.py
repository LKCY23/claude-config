import json
from pathlib import Path
import sys
import os
import subprocess
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from claude_config.core.recovery import rollback_plan
from test_claude_skill_activation import ClaudeActivationTests


class RegistrationOrderTests(unittest.TestCase):
    def test_new_native_fields_have_identical_bytes_across_hash_seeds(self):
        code = '''
import json
from pathlib import Path
from claude_config.core.registrations import Registrations
r = Registrations.__new__(Registrations)
r.main_path = Path('/native')
r.main_roots = {'hooks', 'other'}
r.main_fields = {('enabledPlugins', name) for name in ['analyze@skills-dir', 'practice@skills-dir', 'evaluate@skills-dir']}
r.documents = {r.main_path: {'value': {'hooks': {}, 'other': {}, 'enabledPlugins': {name: False for _, name in r.main_fields}}}}
print(json.dumps(r.native_result({})))
'''
        results = []
        for seed in ('1', '2', '3', '4'):
            env = {**os.environ, 'PYTHONHASHSEED': seed, 'PYTHONPATH': str(Path(__file__).resolve().parents[1] / 'src')}
            results.append(subprocess.check_output([sys.executable, '-c', code], env=env))
        self.assertEqual(len(set(results)), 1)


class ClaudeSkillPluginTests(ClaudeActivationTests):
    def setUp(self):
        super().setUp()
        self.manifest = self.repo / 'packages/tiny/.claude-plugin/plugin.json'
        self.manifest.parent.mkdir()
        self.manifest.write_text(json.dumps({'name': 'tiny', 'skills': ['./']}))
        self.save()

    def test_plugin_gate_follows_disabled_enabled_and_omitted_selection(self):
        self.deploy()
        self.assertFalse(self.native_value()['enabledPlugins']['tiny@skills-dir'])
        self.profile['resources']['skills']['tiny@test']['options']['control']['enabled'] = True
        self.save_profile()
        self.deploy()
        self.assertTrue(self.native_value()['enabledPlugins']['tiny@skills-dir'])
        self.profile['resources']['skills']['tiny@test']['options'] = {}
        self.save_profile()
        self.deploy()
        self.assertTrue(self.native_value()['enabledPlugins']['tiny@skills-dir'])

    def test_upgrade_old_standalone_control_adds_owned_plugin_gate(self):
        self.manifest.unlink()
        self.save()
        self.deploy()
        self.manifest.write_text(json.dumps({'name': 'tiny', 'skills': ['./']}))
        self.save()
        self.deploy()
        self.assertFalse(self.native_value()['enabledPlugins']['tiny@skills-dir'])

    def test_plugin_drift_blocks_plan_and_inconsistent_export(self):
        self.deploy()
        value = self.native_value()
        value['enabledPlugins']['tiny@skills-dir'] = True
        self.write_native(value)
        plan = self.manager().plan()
        self.assertFalse(plan.report['ready'])
        self.assertFalse(self.manager().export([], resource_refs=['tiny@test']).report['ready'])
        conflict = plan.report['resource_conflicts'][0]
        fixed = self.manager().plan(resolutions={'/resources/skills/tiny@test': {
            'conflict_id': conflict['conflict_id'], 'action': 'adopt_source'}})
        self.assertTrue(fixed.report['ready'], fixed.report['blockers'])
        fixed.apply(self.state)
        self.assertFalse(self.native_value()['enabledPlugins']['tiny@skills-dir'])

    def test_unowned_explicit_plugin_gate_is_not_overwritten(self):
        self.write_native({'enabledPlugins': {'tiny@skills-dir': True}})
        self.assertFalse(self.manager().plan().report['ready'])

    def test_missing_override_does_not_hide_later_plugin_change_from_conflict_id(self):
        self.deploy()
        value = self.native_value()
        del value['skillOverrides']['tiny']
        self.write_native(value)
        first = self.manager().plan().report['resource_conflicts'][0]
        value['enabledPlugins']['tiny@skills-dir'] = True
        self.write_native(value)
        second = self.manager().plan().report['resource_conflicts'][0]
        self.assertNotEqual(first['conflict_id'], second['conflict_id'])
        stale = self.manager().plan(resolutions={'/resources/skills/tiny@test': {
            'conflict_id': first['conflict_id'], 'action': 'adopt_source'}})
        self.assertFalse(stale.report['ready'])

    def test_manifest_name_mismatch_blocks_before_writes(self):
        self.manifest.write_text(json.dumps({'name': 'other'}))
        self.save()
        self.assertFalse(self.manager().plan().report['ready'])

    def test_rollback_preserves_unrelated_plugin_gate(self):
        self.write_native({'enabledPlugins': {'unrelated@market': True}})
        before = self.manager().native_path.read_bytes()
        plan = self.deploy()
        rollback = rollback_plan(self.manager(), Path(plan.report['recovery']).parent.name)
        self.assertTrue(rollback.report['ready'], rollback.report['blockers'])
        rollback.apply(self.state)
        self.assertEqual(self.manager().native_path.read_bytes(), before)

    def test_enable_preserves_other_overrides_and_fields(self):
        # Extend the base assertion to include the additional owned plugin gate.
        self.write_native({'skillOverrides': {'unrelated': 'name-only'}, 'localSetting': 'preserve'})
        self.deploy()
        self.profile['resources']['skills']['tiny@test']['options']['control']['enabled'] = True
        self.save_profile()
        self.deploy()
        self.assertEqual(self.native_value(), {
            'skillOverrides': {'unrelated': 'name-only', 'tiny': 'on'},
            'localSetting': 'preserve', 'enabledPlugins': {'tiny@skills-dir': True}})
