import json
from pathlib import Path
import sqlite3
import unittest
import tomlkit

from test_profile_resources import ResourceFixture
from claude_config.core.recovery import rollback_plan
from claude_config.core.schema import SchemaError
from claude_config.core.skill_retirement import retirement_plan
from claude_config.core.transaction import PlanStale


class RetirementTests(ResourceFixture):
    def setUp(self):
        super().setUp()
        self.body = '---\nname: tiny\ndescription: fixture\n---\nOriginal skill\n'
        self.add('tiny', 'skill', {'SKILL.md': self.body, 'helper.txt': 'required'}, 'SKILL.md',
                 options={'control': {'enabled': False}})
        self.profile.pop('settings', None)
        self.save()
        self.native.mkdir()
        (self.native / 'settings.json').write_text(json.dumps({'localSetting': 'preserve', 'env': {'ANTHROPIC_AUTH_TOKEN': 'LOCAL_ONLY'}}))
        plan = self.manager().plan(); self.assertTrue(plan.report['ready']); plan.apply(self.state)
        self.entry = self.native / 'skills/tiny/SKILL.md'

    def deselect(self):
        self.profile['resources']['skills'].clear(); self.save_profile()

    def test_selected_even_disabled_must_not_be_uninstalled(self):
        with self.assertRaisesRegex(SchemaError, 'Remove the resource selection'):
            retirement_plan(self.manager(), ['tiny@test'])
        self.assertTrue(self.entry.exists())

    def test_preview_retirement_and_rollback_preserve_other_files(self):
        self.deselect()
        other = self.native / 'vm_bundles/large.img'; other.parent.mkdir(); other.write_bytes(b'not part of this change')
        settings = self.native / 'settings.json'; before = settings.read_bytes()
        manager = self.manager(); state_before = manager.state_path.read_bytes()
        plan = retirement_plan(manager, ['tiny@test'])
        self.assertTrue(self.entry.exists())
        self.assertNotIn(other, [change.path for change in plan.changes])
        self.assertNotIn('LOCAL_ONLY', json.dumps(plan.report))
        plan.apply(self.state)
        self.assertFalse(self.entry.exists())
        self.assertEqual(json.loads(settings.read_text()), {'localSetting': 'preserve', 'env': {'ANTHROPIC_AUTH_TOKEN': 'LOCAL_ONLY'}, 'skillOverrides': {}})
        self.assertNotIn('tiny@test', json.loads(manager.state_path.read_text())['resources'])
        self.assertTrue(other.exists())
        self.assertFalse(self.manager().plan().report['blockers'])
        restored = rollback_plan(self.manager(), Path(plan.report['recovery']).parent.name)
        self.assertTrue(restored.report['ready']); restored.apply(self.state)
        self.assertEqual(self.entry.read_text(), self.body)
        self.assertEqual(settings.read_bytes(), before)
        self.assertEqual(manager.state_path.read_bytes(), state_before)

    def test_changed_body_mode_or_unknown_file_block(self):
        self.deselect()
        before = self.entry.read_bytes()
        self.entry.write_bytes(before + b'local change')
        with self.assertRaisesRegex(SchemaError, 'changed since deployment'):
            retirement_plan(self.manager(), ['tiny@test'])
        self.entry.write_bytes(before); self.entry.chmod(0o644)
        with self.assertRaisesRegex(SchemaError, 'changed since deployment'):
            retirement_plan(self.manager(), ['tiny@test'])
        self.entry.chmod(0o600)
        (self.entry.parent / 'user-note.txt').write_text('keep')
        with self.assertRaisesRegex(SchemaError, 'Unknown files'):
            retirement_plan(self.manager(), ['tiny@test'])

    def test_activation_change_is_not_silently_deleted(self):
        self.deselect()
        p = self.native / 'settings.json'; value = json.loads(p.read_text())
        value['skillOverrides']['tiny'] = 'on'; p.write_text(json.dumps(value))
        with self.assertRaisesRegex(SchemaError, 'activation changed'):
            retirement_plan(self.manager(), ['tiny@test'])

    def test_late_edit_blocks_retirement_before_any_deletion(self):
        self.deselect(); plan = retirement_plan(self.manager(), ['tiny@test'])
        self.entry.write_text(self.body + 'edited later')
        with self.assertRaises(PlanStale):
            plan.apply(self.state)
        self.assertTrue(self.entry.exists())

    def test_retargeted_profile_link_blocks_retirement(self):
        self.deselect()
        profile = self.data / self.machine['agents']['claude-code']['profile']
        first, second = profile.with_name('first.yaml'), profile.with_name('second.yaml')
        first.write_bytes(profile.read_bytes()); second.write_bytes(profile.read_bytes())
        profile.unlink(); profile.symlink_to(first.name)
        plan = retirement_plan(self.manager(), ['tiny@test'])
        profile.unlink(); profile.symlink_to(second.name)
        with self.assertRaises(PlanStale):
            plan.apply(self.state)
        self.assertTrue(self.entry.exists())

    def test_foreign_claim_blocks_until_handoff(self):
        self.deselect()
        cc = self.root / 'cc-switch'; cc.mkdir()
        self.machine['bindings']['paths'] = {'cc-switch-data': str(cc)}; self.save_profile()
        with sqlite3.connect(cc / 'cc-switch.db') as db:
            db.execute('CREATE TABLE skills (name,directory,enabled_claude,enabled_codex)')
            db.execute('INSERT INTO skills VALUES (?,?,?,?)', ('tiny', 'tiny', 1, 0))
        with self.assertRaisesRegex(SchemaError, 'Hand off external'):
            retirement_plan(self.manager(), ['tiny@test'])

    def test_unrelated_pending_setting_is_not_applied_by_retirement(self):
        self.deselect(); self.profile['settings'] = {'model': 'new-unapplied'}; self.save_profile()
        plan = retirement_plan(self.manager(), ['tiny@test']); plan.apply(self.state)
        self.assertNotIn('model', json.loads((self.native / 'settings.json').read_text()))

    def test_rollback_protects_edits_after_retirement(self):
        self.deselect(); plan = retirement_plan(self.manager(), ['tiny@test']); plan.apply(self.state)
        self.entry.write_text('new owner content')
        rollback = rollback_plan(self.manager(), Path(plan.report['recovery']).parent.name)
        self.assertFalse(rollback.report['ready'])
        self.assertEqual(self.entry.read_text(), 'new owner content')


class CodexRetirementTests(ResourceFixture):
    def setUp(self):
        super().setUp()
        self.profile['agent_type'] = 'codex'
        self.machine['agents'] = {'codex': {'profile': 'profiles/agents/codex/work.yaml',
                                          'binding': {'executable': str(self.binary), 'config_root': str(self.native)}}}
        self.machine['bindings']['paths'] = {'skill-root': str(self.root / '.agents/skills')}
        self.add('tiny', 'skill', {'SKILL.md': '---\nname: tiny\ndescription: test\n---\nbody\n'}, 'SKILL.md',
                 requires={'bindings': {'install_root': {'kind': 'path', 'required': True}}},
                 bindings={'install_root': 'paths.skill-root'}, options={'control': {'enabled': False}})
        self.profile.pop('settings', None); self.save()
        self.native.mkdir(); (self.native / 'config.toml').write_text('# preserve\nmodel = "existing"\n')
        p = self.manager().plan(); self.assertTrue(p.report['ready']); p.apply(self.state)
        self.profile['resources']['skills'].clear(); self.save_profile()

    def test_codex_control_retirement_and_rollback_preserve_comments(self):
        native = self.native / 'config.toml'; before = native.read_bytes()
        plan = retirement_plan(self.manager(), ['tiny@test']); plan.apply(self.state)
        self.assertIn('# preserve', native.read_text())
        data = tomlkit.parse(native.read_text()); self.assertEqual(data['model'], 'existing')
        self.assertEqual(list(data['skills']['config']), [])
        rollback = rollback_plan(self.manager(), Path(plan.report['recovery']).parent.name)
        self.assertTrue(rollback.report['ready']); rollback.apply(self.state)
        self.assertEqual(native.read_bytes(), before)

    def test_changed_codex_activation_blocks_retirement(self):
        native = self.native / 'config.toml'; data = tomlkit.parse(native.read_text())
        data['skills']['config'][0]['enabled'] = True; native.write_text(tomlkit.dumps(data))
        with self.assertRaisesRegex(SchemaError, 'activation differs'):
            retirement_plan(self.manager(), ['tiny@test'])


if __name__ == '__main__':
    unittest.main()
