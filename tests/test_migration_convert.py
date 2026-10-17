"""Conversion writes new local candidates without importing credentials/state."""
import json
from pathlib import Path
import sys
import tempfile
import unittest

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from claude_config.core.catalog import CatalogResolver
from claude_config.core.manager import ProfileManager
from claude_config.core.migration_convert import Conversion, conversion
from claude_config.core.schema import validate_machine, validate_profile


class ConversionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.data, self.repo, self.output = (self.root / x for x in ('data','legacy-source','candidate'))
        self.data.mkdir();(self.repo/'catalog/packages/review').mkdir(parents=True)
        package=self.repo/'catalog/packages/review'
        (package/'.claude-plugin').mkdir()
        (package/'.claude-plugin/plugin.json').write_text(json.dumps({'name':'review','version':'1.0.0','skills':'./skills/'}))
        (package/'skills/test').mkdir(parents=True)
        (package/'skills/test/SKILL.md').write_text('---\nname: test\ndescription: test\n---\nbody\n')
        (self.repo/'catalog/catalog.json').write_text(json.dumps({'version':1,'name':'team','plugins':[{'name':'review','source':'packages/review','author':'third party'}]}))
        (self.data/'prefs.json').write_text('{"model":"active","api_key":"PRIVATE_INPUT"}')
        self.agents={'version':1,'catalogs':{'team':{'repo':'owner/repo','path':'catalog'}},'agents':{'claude':{'plugins':['review@team'],'config':{'source':'prefs.json'}}}}
        self.manifest={'version':1}
        self.save()

    def tearDown(self):self.temp.cleanup()

    def save(self):
        (self.data/'agents.yaml').write_text(yaml.safe_dump(self.agents))
        (self.data/'manifest.yaml').write_text(yaml.safe_dump(self.manifest))

    def candidate(self):
        return Conversion(self.data,self.output,{'owner/repo':self.repo},executables={'claude-code':'/usr/bin/false','codex':'/usr/bin/false'}).build()

    def test_preview_has_no_output_and_does_not_export_secret_values(self):
        before={p:p.read_bytes() for p in self.data.iterdir()}
        candidate=self.candidate()
        self.assertFalse(self.output.exists())
        self.assertNotIn('PRIVATE_INPUT',json.dumps(candidate.report))
        self.assertNotIn('PRIVATE_INPUT',b'\n'.join(candidate.files.values()).decode())
        self.assertEqual(before,{p:p.read_bytes() for p in self.data.iterdir()})

    def test_written_candidate_is_a_fixed_resolvable_new_catalog(self):
        candidate=self.candidate();report=candidate.write()
        machine=self.output/'machine.proposed.yaml'
        validate_machine(yaml.safe_load(machine.read_text()))
        manager=ProfileManager(machine,'claude-code',probe=lambda *a:[])
        profile=yaml.safe_load((self.output/'data/profiles/agents/claude-code/current.yaml').read_text())
        validate_profile(profile)
        packages=CatalogResolver(manager).resolve(profile)
        self.assertEqual(set(packages),{'review@team'})
        self.assertFalse(report['deployment_ready'])
        self.assertEqual(profile['settings'],{'model':'active'})
        self.assertFalse((self.output/'data/agents.yaml').exists())

    def test_published_snapshot_files_are_pinned_despite_development_gitignore(self):
        package=self.repo/'catalog/packages/review'
        (package/'.gitignore').write_text('dist/\n')
        (package/'dist').mkdir();(package/'dist/entry.js').write_text('published output')
        self.candidate().write()
        machine=self.output/'machine.proposed.yaml'
        manager=ProfileManager(machine,'claude-code',probe=lambda *a:[])
        profile=yaml.safe_load((self.output/'data/profiles/agents/claude-code/current.yaml').read_text())
        packages=CatalogResolver(manager).resolve(profile)
        self.assertIn('dist/entry.js',packages['review@team'].files)

    def test_current_values_win_and_disabled_attribution_is_mapped(self):
        (self.data/'base.json').write_text('{"model":"old","includeCoAuthoredBy":false,"hasCompletedOnboarding":true}')
        self.manifest['settings']={'base':{'source':'base.json'}};self.save()
        c=self.candidate();profile=c.agents['claude-code']
        self.assertEqual(profile['settings'],{'model':'active','attribution':{'commit':'','pr':''}})
        self.assertTrue(any(r['action']=='preserve' for r in c.report['decisions']))
        self.assertNotIn('hasCompletedOnboarding',profile['settings'])

    def test_missing_codex_manifest_is_a_reviewed_version_change(self):
        (self.data/'codex.toml').write_text('model = "active"\n')
        self.agents['agents']['codex']={'plugins':['review@team'],'config':{'source':'codex.toml'}};self.save()
        c=self.candidate()
        prefix='source/catalogs/team/packages/review/'
        codex=json.loads(c.files[prefix+'.codex-plugin/plugin.json'])
        self.assertEqual(codex['version'],'1.0.0+claudespace.migration.1')
        self.assertEqual(c.report['migration_conversion']['review_required'][0]['code'],'native_manifest_adapter')
        self.assertEqual(json.loads((self.repo/'catalog/packages/review/.claude-plugin/plugin.json').read_text())['version'],'1.0.0')

    def test_native_memory_import_keeps_declared_order_and_one_selection(self):
        for name in ('second','first'):(self.data/(name+'.md')).write_text(name+' body\n')
        self.manifest['memory']={'items':{'second':{'source':'second.md'},'first':{'source':'first.md'}}}
        # Preserve the explicit declaration order rather than safe_dump sorting.
        (self.data/'manifest.yaml').write_text(yaml.safe_dump(self.manifest,sort_keys=False))
        c=self.candidate()
        self.assertEqual(c.files['source/catalogs/configuration-resources/packages/global-instructions/global.md'],b'@./second.md\n@./first.md\n')
        self.assertEqual(len(c.agents['claude-code']['resources']['instructions']),1)

    def test_unknown_sources_and_external_symlinks_block_without_output(self):
        self.agents['agents']['claude']['files']=[{'source':'prefs.json'}];self.save()
        with self.assertRaises(ValueError):self.candidate()
        self.assertFalse(self.output.exists())
        self.agents['agents']['claude'].pop('files');self.save()
        target=self.root/'private';target.write_text('PRIVATE')
        (self.repo/'catalog/packages/review/escape').symlink_to(target)
        with self.assertRaises(ValueError):self.candidate()

    def test_stale_input_and_existing_output_cannot_be_written(self):
        c=self.candidate();(self.data/'prefs.json').write_text('{"model":"late"}')
        with self.assertRaises(ValueError):c.write()
        self.assertFalse(self.output.exists())
        self.output.mkdir()
        with self.assertRaises(ValueError):self.candidate()

    def test_added_package_file_and_changed_plan_identity_block(self):
        c=self.candidate()
        (self.repo/'catalog/packages/review/late.txt').write_text('late')
        with self.assertRaises(ValueError):c.write()
        with self.assertRaises(ValueError):
            conversion(self.data,self.output,{'owner/repo':self.repo},write=True,expect_plan_id=c.report['plan_id'])
        self.assertFalse(self.output.exists())

    def test_explicit_app_profile_is_separate_and_exclusions_are_enforced(self):
        path=self.root/'app.yaml';path.write_text('schema_version: 1\napp: codex-app\nsettings:\n  preventSleepWhileRunning: false\n')
        c=Conversion(self.data,self.output,{'owner/repo':self.repo},executables={'claude-code':'/usr/bin/false'},app_profiles={'codex-app':path}).build()
        self.assertEqual(c.apps['codex-app']['settings'],{'preventSleepWhileRunning':False})
        self.assertNotIn('preventSleepWhileRunning',c.agents['claude-code']['settings'])
        path.write_text('schema_version: 1\napp: codex-app\nsettings:\n  git-worktree-root: /private\n')
        with self.assertRaises(ValueError):
            Conversion(self.data,self.output,{'owner/repo':self.repo},app_profiles={'codex-app':path}).build()


if __name__=='__main__':unittest.main()
