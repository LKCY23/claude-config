"""Default runtime state follows final configuration data, with explicit overrides."""
import json
from pathlib import Path
import sys
import unittest

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from claude_config.core.app_manager import AppProfileManager
from claude_config.core.manager import ProfileManager
from claude_config.core.schema import state_directory
from test_framework_cutover import CutoverFixture, FakeAppProbe
from test_profile_resources import ResourceFixture


class StandaloneStateDirectoryTests(ResourceFixture):
    def setUp(self):
        super().setUp()
        self.machine['apps']['claude-desktop'] = {'profile': 'profiles/apps/claude-desktop/work.yaml',
                                                'binding': {'application': str(self.root / 'test.app'),
                                                            'data_root': str(self.root / 'desktop')}}
        path = self.data / 'profiles/apps/claude-desktop/work.yaml'
        path.parent.mkdir(parents=True)
        path.write_text(yaml.safe_dump({'schema_version': 1, 'app': 'claude-desktop',
                                       'settings': {'userThemeMode': 'system'}}))
        self.save()

    def test_agent_and_app_default_without_runtime_declaration_are_readonly(self):
        self.machine.pop('runtime')
        self.save_profile()
        agent = self.manager()
        app = AppProfileManager(self.machine_path, 'claude-desktop', probe=FakeAppProbe())
        for manager in (agent, app):
            self.assertEqual(manager.state_root, self.data / '.state')
            self.assertTrue(manager.state_path.is_relative_to(self.data / '.state'))
            plan = manager.plan()
            self.assertTrue(plan.report['ready'], plan.report['blockers'])
        self.assertFalse((self.data / '.state').exists())
        self.assertFalse((self.root / 'desktop').exists())
        self.assertFalse(self.native.exists())

    def test_explicit_runtime_state_directory_retains_precedence(self):
        override = self.root / 'explicit-state'
        self.machine['runtime']['state_dir'] = str(override)
        self.save_profile()
        agent = self.manager()
        app = AppProfileManager(self.machine_path, 'claude-desktop', probe=FakeAppProbe())
        self.assertEqual(agent.state_root, override)
        self.assertEqual(app.state_root, override)
        self.assertEqual(state_directory(self.machine, config_dir=self.root / 'other-data'), override)
        self.assertFalse(override.exists())


class CutoverStateDirectoryTests(CutoverFixture):
    def setUp(self):
        super().setUp()
        self.add_app()
        self.machine['agents']['claude-code'] = {'profile': 'profiles/agents/claude-code/work.yaml',
                                                'binding': {'executable': str(self.binary),
                                                            'config_root': str(self.root / 'claude-native')}}
        self.machine['apps']['claude-desktop'] = {'profile': 'profiles/apps/claude-desktop/work.yaml',
                                                'binding': {'application': str(self.root / 'claude.app'),
                                                            'data_root': str(self.root / 'desktop')}}
        for kind, client, setting in (('agents', 'claude-code', {'model': 'selected'}),
                                      ('apps', 'claude-desktop', {'userThemeMode': 'system'})):
            path = self.data / f'profiles/{kind}/{client}/work.yaml'
            path.parent.mkdir(parents=True)
            identity = {'agent_type': client} if kind == 'agents' else {'app': client}
            path.write_text(yaml.safe_dump({'schema_version': 1, **identity, 'settings': setting}))
        self.machine['runtime'].pop('state_dir')
        self.save_profile()
        self.save_spec()

    def test_all_four_clients_use_final_data_state_without_writing_during_plan(self):
        manager = self.coordinator()
        expected = self.target_data / '.state'
        self.assertNotEqual(expected, self.data / '.state')
        self.assertEqual(manager.state_root, expected)
        self.assertEqual(set(manager.agents), {'claude-code', 'codex'})
        self.assertEqual(set(manager.apps), {'claude-desktop', 'codex-app'})
        for child in (*manager.agents.values(), *manager.apps.values()):
            self.assertEqual(child.root, self.data)
            self.assertEqual(child.state_root, expected)
            self.assertTrue(child.state_path.is_relative_to(expected))
            self.assertNotIn('state_dir', child.machine['runtime'])
        plan = manager.plan()
        self.assertTrue(plan.report['ready'], plan.report['blockers'])
        self.assertFalse(self.target_data.exists())
        self.assertFalse((self.data / '.state').exists())
        self.assertFalse(self.tool.exists())

    def test_installed_machine_resolves_same_journal_and_baselines_without_override(self):
        manager = self.coordinator()
        plan = manager.plan()
        self.assertTrue(plan.report['ready'], plan.report['blockers'])
        plan.apply(manager.state_root)
        machine_path = self.tool / 'machine.yaml'
        installed = yaml.safe_load(machine_path.read_text())
        self.assertNotIn('state_dir', installed['runtime'])
        self.assertEqual(installed['config_dir'], str(self.target_data))
        journal = Path(plan.report['recovery'])
        self.assertEqual(journal.parent.parent, (manager.state_root / 'runs').resolve())
        self.assertEqual(json.loads(journal.read_text())['status'], 'applied')
        for agent in manager.agents:
            reloaded = ProfileManager(machine_path, agent, probe=lambda *args: [])
            self.assertEqual(reloaded.state_root, manager.state_root)
            self.assertEqual(reloaded.state_path, manager.agents[agent].state_path)
            self.assertTrue(reloaded.state_path.exists())
        for app in manager.apps:
            reloaded = AppProfileManager(machine_path, app, probe=FakeAppProbe())
            self.assertEqual(reloaded.state_root, manager.state_root)
            self.assertEqual(reloaded.state_path, manager.apps[app].state_path)
            self.assertTrue(reloaded.state_path.exists())
        self.assertFalse((self.data / '.state').exists())

    def test_cutover_explicit_override_wins_and_is_preserved_in_installed_machine(self):
        override = self.root / 'explicit-state'
        self.machine['runtime']['state_dir'] = str(override)
        self.save_profile()
        manager = self.coordinator()
        self.assertEqual(manager.state_root, override)
        for child in (*manager.agents.values(), *manager.apps.values()):
            self.assertEqual(child.state_root, override)
        installed = yaml.safe_load(next(c.content for c in manager.plan().changes if c.owner == 'machine-binding'))
        self.assertEqual(installed['runtime']['state_dir'], str(override))
        self.assertEqual(installed['config_dir'], str(self.target_data))
        plan = manager.plan()
        self.assertTrue(plan.report['ready'], plan.report['blockers'])
        self.assertFalse(override.exists())
        self.assertFalse(self.target_data.exists())


if __name__ == '__main__':
    unittest.main()
