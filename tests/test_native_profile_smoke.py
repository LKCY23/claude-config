"""Opt-in, isolated checks with installed binaries; never call a model."""
import os
from pathlib import Path
import sys
import tempfile
import unittest

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from claude_config.core.manager import ProfileManager


@unittest.skipUnless(os.environ.get('CLAUDE_CONFIG_RUN_NATIVE_SMOKE') == '1', 'Opt-in native executable checks')
class NativeSmokeTests(unittest.TestCase):
    def run_agent(self, agent, variable, settings):
        binary = os.environ.get(variable)
        if not binary:
            self.skipTest(variable + ' is required')
        with tempfile.TemporaryDirectory(prefix='profile-native-smoke-') as directory:
            root = Path(directory)
            name = f'profiles/agents/{agent}/work.yaml'
            profile = root / 'data' / name
            profile.parent.mkdir(parents=True)
            profile.write_text(yaml.safe_dump({'schema_version': 1, 'agent_type': agent, 'settings': settings}))
            machine = {'schema_version': 1, 'machine_id': 'isolated-smoke', 'config_dir': str(root / 'data'),
                       'catalog_roots': {}, 'agents': {agent: {'profile': name, 'binding': {'executable': binary, 'config_root': str(root / 'native')}}},
                       'apps': {}, 'bindings': {}, 'runtime': {'state_dir': str(root / 'state')}}
            machine_file = root / 'machine.yaml'
            machine_file.write_text(yaml.safe_dump(machine))
            manager = ProfileManager(machine_file, agent)
            plan = manager.plan()
            self.assertTrue(plan.report['ready'], plan.report['blockers'])
            self.assertFalse(manager.native_path.exists())
            plan.apply(root / 'state')
            self.assertTrue(manager.native_path.exists())
            repeated = ProfileManager(machine_file, agent).plan().apply(root / 'state')
            self.assertTrue(all(row['status'] == 'unchanged' for row in repeated['results']))

    def test_claude_cli(self):
        self.run_agent('claude-code', 'CLAUDE_CONFIG_NATIVE_CLAUDE', {'model': 'example', 'effortLevel': 'high', 'autoMemoryEnabled': False})

    def test_codex_cli(self):
        self.run_agent('codex', 'CLAUDE_CONFIG_NATIVE_CODEX', {'model': 'example', 'model_reasoning_effort': 'high', 'memories': {'use_memories': False}})

    def test_codex_app_backend(self):
        self.run_agent('codex', 'CLAUDE_CONFIG_NATIVE_CODEX_APP', {'model': 'example', 'model_reasoning_effort': 'high', 'skills': {'include_instructions': False}})
