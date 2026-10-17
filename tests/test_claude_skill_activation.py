"""Installed-but-disabled Claude skills, ownership, export and recovery."""
from pathlib import Path
import json,sys,unittest,yaml
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from claude_config.core.recovery import rollback_plan
from test_profile_resources import ResourceFixture

class ClaudeActivationTests(ResourceFixture):
    def setUp(self):
        super().setUp()
        self.body='---\nname: tiny\ndescription: Native activation fixture\n---\nBody stays unchanged.\n'
        self.add('tiny','skill',{'SKILL.md':self.body},'SKILL.md',options={'control':{'enabled':False}})
        self.profile.pop('settings',None);self.save()
    def deploy(self):
        p=self.manager().plan();self.assertTrue(p.report['ready'],p.report['blockers']);p.apply(self.state);return p
    def native_value(self):return json.loads(self.manager().native_path.read_text())
    def write_native(self,v):
        p=self.manager().native_path;p.parent.mkdir(parents=True,exist_ok=True);p.write_text(json.dumps(v))
    def test_disabled_keeps_exact_body_and_repeat_is_ready(self):
        self.deploy();self.assertEqual(self.native_value()['skillOverrides'],{'tiny':'off'})
        self.assertEqual((self.native/'skills/tiny/SKILL.md').read_text(),self.body)
        self.assertTrue(self.manager().plan().report['ready'])
    def test_enable_preserves_other_overrides_and_fields(self):
        self.write_native({'skillOverrides':{'unrelated':'name-only'},'localSetting':'preserve'})
        self.deploy();self.profile['resources']['skills']['tiny@test']['options']['control']['enabled']=True;self.save_profile();self.deploy()
        self.assertEqual(self.native_value(),{'skillOverrides':{'unrelated':'name-only','tiny':'on'},'localSetting':'preserve'})
    def test_omission_preserves_disabled_state(self):
        self.deploy();self.profile['resources']['skills']['tiny@test']['options']={};self.save_profile();self.deploy()
        self.assertEqual(self.native_value()['skillOverrides']['tiny'],'off')
    def test_export_maps_off(self):
        self.deploy();p=self.manager().export([],resource_refs=['tiny@test']);self.assertTrue(p.report['ready'],p.report['blockers']);p.apply(self.state)
        value=yaml.safe_load((self.data/self.machine['agents']['claude-code']['profile']).read_text())
        self.assertFalse(value['resources']['skills']['tiny@test']['options']['control']['enabled'])
    def test_unknown_override_blocks_adoption(self):
        for value in ('off','on','name-only'):
            self.write_native({'skillOverrides':{'tiny':value}});self.assertFalse(self.manager().plan().report['ready'])
    def test_external_change_and_explicit_restore(self):
        self.deploy();v=self.native_value();v['skillOverrides']['tiny']='on';self.write_native(v)
        plan=self.manager().plan();self.assertFalse(plan.report['ready']);conflict=plan.report['resource_conflicts'][0]
        resolution={'/resources/skills/tiny@test':{'conflict_id':conflict['conflict_id'],'action':'adopt_source'}}
        fixed=self.manager().plan(resolutions=resolution);self.assertTrue(fixed.report['ready'],fixed.report['blockers']);fixed.apply(self.state)
        self.assertEqual(self.native_value()['skillOverrides']['tiny'],'off')
    def test_intermediate_mode_not_exported_as_boolean(self):
        self.deploy();v=self.native_value();v['skillOverrides']['tiny']='name-only';self.write_native(v)
        self.assertFalse(self.manager().plan().report['ready'])
        self.assertFalse(self.manager().export([],resource_refs=['tiny@test']).report['ready'])
    def test_rollback_restores_original_settings(self):
        self.write_native({'localSetting':'keep'});before=self.manager().native_path.read_bytes();p=self.deploy()
        rollback=rollback_plan(self.manager(),Path(p.report['recovery']).parent.name);self.assertTrue(rollback.report['ready']);rollback.apply(self.state)
        self.assertEqual(self.manager().native_path.read_bytes(),before)
        self.assertFalse((self.native/'skills/tiny/SKILL.md').exists())

if __name__=='__main__':unittest.main()
