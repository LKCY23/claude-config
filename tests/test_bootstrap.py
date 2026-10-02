import importlib.util
from pathlib import Path
import json
import shutil
import subprocess
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('bootstrap', Path(__file__).resolve().parents[1] / 'scripts/bootstrap.py')
bootstrap = importlib.util.module_from_spec(spec);spec.loader.exec_module(bootstrap)


class BootstrapTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory();self.root = Path(self.temp.name)
        self.source=self.root/'source';self.source.mkdir()
        (self.source/'SKILL.md').write_text('Canonical rules\n')
        (self.source/'.gitignore').write_text('config.yaml\nstate/\nbackups/\n')
        self.git(self.source,'init','-q');self.commit('initial')
        self.old_head=self.git(self.source,'rev-parse','HEAD')
        self.tool=self.root/'tool'
        subprocess.run(['git','clone','--quiet',str(self.source),str(self.tool)],check=True)
        (self.source/'SKILL.md').write_text('Reviewed new canonical rules\n');self.commit('updated')
        self.data=self.root/'data';self.data.mkdir();(self.data/'agents.yaml').write_text('version: 1\nagents: {}\n')
        self.catalog=self.root/'catalog';self.catalog.mkdir()
        self.home=self.root/'home';self.home.mkdir()
        self.entry=self.home/'.claude/skills/claude-config/SKILL.md';self.entry.parent.mkdir(parents=True)
        self.entry.write_text('Prior user entry\n')
        self.oauth=self.home/'.codex/auth.json';self.oauth.parent.mkdir(parents=True);self.oauth.write_text('synthetic fixture, never touched\n')

    def tearDown(self):self.temp.cleanup()

    def git(self,root,*args):
        return subprocess.check_output(['git','-C',str(root),*args],text=True).strip()

    def commit(self,message):
        self.git(self.source,'add','.')
        self.git(self.source,'-c','user.name=Test','-c','user.email=test@example.invalid','commit','-qm',message)

    def deploy(self,apply):
        return bootstrap.deploy(self.source,self.tool,self.data,self.catalog,('claude','codex'),home=self.home,apply=apply)

    def test_plan_has_no_mutations(self):
        self.deploy(False)
        self.assertEqual(self.git(self.tool,'rev-parse','HEAD'),self.old_head)
        self.assertEqual(self.entry.read_text(),'Prior user entry\n')
        self.assertFalse((self.tool/'config.yaml').exists())

    def test_apply_and_rollback_preserve_prior_entry_and_auth_fixture(self):
        result=self.deploy(True)
        self.assertEqual(self.git(self.tool,'rev-parse','HEAD'),result['source_commit'])
        self.assertIn('default host for this entry is **claude**',self.entry.read_text())
        native=self.home/'.agents/skills/claude-config/SKILL.md'
        self.assertIn('default host for this entry is **codex**',native.read_text())
        self.assertEqual(self.oauth.read_text(),'synthetic fixture, never touched\n')
        bootstrap.rollback(result['receipt'])
        self.assertEqual(self.git(self.tool,'rev-parse','HEAD'),self.old_head)
        self.assertEqual(self.entry.read_text(),'Prior user entry\n')
        self.assertFalse(native.exists())
        self.assertFalse((self.tool/'config.yaml').exists())

    def test_installed_dirty_framework_is_preserved(self):
        (self.tool/'SKILL.md').write_text('local user change')
        with self.assertRaisesRegex(ValueError,'tracked changes'):self.deploy(True)
        self.assertEqual(self.entry.read_text(),'Prior user entry\n')

    def test_duplicate_native_and_legacy_entries_are_reported(self):
        for folder in ['.agents/skills/claude-config','.codex/skills/claude-config']:
            (self.home/folder).mkdir(parents=True)
        with self.assertRaisesRegex(ValueError,'Duplicate Codex'):self.deploy(True)

    def test_second_bootstrap_rollback_restores_same_branch_previous_commit(self):
        first=self.deploy(True)
        (self.source/'SKILL.md').write_text('Another reviewed version\n');self.commit('third')
        second=self.deploy(True)
        bootstrap.rollback(second['receipt'])
        self.assertEqual(self.git(self.tool,'rev-parse','HEAD'),first['source_commit'])


if __name__=='__main__':unittest.main()
