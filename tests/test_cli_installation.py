"""Package entrypoint independent of source checkout and active shell interpreter."""
from pathlib import Path
import os
import json
import sys
import subprocess
import tempfile
import unittest


@unittest.skipUnless(os.environ.get('CLAUDE_CONFIG_TEST_COMMAND'), 'Requires an explicitly installed wheel command')
class InstalledCommandTests(unittest.TestCase):
    def test_arbitrary_cwd_and_fresh_home_with_minimal_path(self):
        command=os.environ['CLAUDE_CONFIG_TEST_COMMAND']
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);home=root/'home';home.mkdir();cwd=root/'任意 工作目录';cwd.mkdir()
            env={**os.environ,'HOME':str(home),'PATH':'/usr/bin:/bin'}
            env.pop('PYTHONPATH',None);env.pop('VIRTUAL_ENV',None);env.pop('CONDA_PREFIX',None)
            for args in (['--version'],['--help'],['plan','--help']):
                result=subprocess.run([command,*args],cwd=cwd,env=env,text=True,capture_output=True,timeout=10)
                self.assertEqual(result.returncode,0,result.stderr)
            self.assertEqual(list(home.iterdir()),[])
            result=subprocess.run([command,'init','--write','--format','json'],cwd=cwd,env=env,text=True,capture_output=True,timeout=10)
            self.assertEqual(result.returncode,0,result.stdout+result.stderr)
            self.assertTrue((home/'.claude-config-tool/machine.yaml').is_file())
            self.assertFalse((cwd/'.claude-config-tool').exists())
            result=subprocess.run([command,'doctor','--format','json'],cwd='/',env=env,text=True,capture_output=True,timeout=10)
            self.assertEqual(result.returncode,0,result.stdout+result.stderr)
            self.assertFalse((home/'.codex').exists());self.assertFalse((home/'.claude').exists())

    def test_skill_records_invoked_command_without_command_directory_on_path(self):
        command = os.environ['CLAUDE_CONFIG_TEST_COMMAND']
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / 'home'
            home.mkdir()
            machine = root / 'reviewed-machine.yaml'
            machine.write_text(json.dumps({
                'schema_version': 1, 'machine_id': 'isolated-install',
                'config_dir': str(root / 'data'), 'catalog_roots': {},
                'agents': {'codex': {'profile': 'profiles/agents/codex/current.yaml',
                                    'binding': {'executable': sys.executable,
                                                'config_root': str(root / 'native')}}},
                'apps': {}, 'bindings': {}}))
            skill = root / 'skills/claude-config/SKILL.md'
            env = {**os.environ, 'HOME': str(home), 'PATH': '/usr/bin:/bin'}
            for key in ('PYTHONPATH', 'VIRTUAL_ENV', 'CONDA_PREFIX'):
                env.pop(key, None)
            result = subprocess.run(
                [command, 'init', '--from-machine', str(machine),
                 '--machine', str(root / 'installed/machine.yaml'),
                 '--skill', 'codex=' + str(skill), '--write', '--format', 'json'],
                cwd='/', env=env, text=True, capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn('Command: `' + str(Path(command).absolute()) + '`', skill.read_text())
            self.assertFalse((root / 'native').exists())
            self.assertFalse((home / '.codex').exists())
            self.assertFalse((home / '.claude').exists())


if __name__=='__main__':unittest.main()
