"""Opt-in whole-run native operations and actual installed helper loading."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import unittest

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from claude_config.core.cutover import Cutover
from claude_config.core.recovery import rollback_plan
from claude_config.core.transaction import atomic_write
import test_framework_cutover as fixtures


@unittest.skipUnless(os.environ.get('CLAUDE_CONFIG_RUN_NATIVE_SMOKE') == '1', 'Opt-in combined native cutover checks')
class NativeCutoverSmokeTests(fixtures.CutoverFixture):
    def setUp(self):
        super().setUp()
        self.patch.stop()
        cli = os.environ.get('CLAUDE_CONFIG_NATIVE_CODEX')
        backend = os.environ.get('CLAUDE_CONFIG_NATIVE_CODEX_APP')
        if not cli or not backend:
            self.skipTest('Actual Codex CLI and App backend are required')
        self.machine['agents']['codex']['binding']['executable'] = cli
        self.profile['settings'] = {'model': 'gpt-6.1-sol'}
        self.add_app()
        self.machine['apps']['codex-app']['binding']['backend'] = backend
        self.save_profile()

    def coordinator(self):
        # The synthetic App never uses these temporary roots. Native Agent and
        # App-backend parsers/installers remain the actual selected binaries.
        return Cutover(self.spec_path, app_probe=self.app_probe)

    def plugin(self):
        import json
        self.add('tiny', 'plugin', {'.codex-plugin/plugin.json': json.dumps({'name': 'tiny', 'version': '1.0.0'}), 'body.txt': 'isolated'}, '.codex-plugin/plugin.json')
        self.save()

    def test_actual_native_install_shared_preferences_and_complete_rollback(self):
        self.plugin()
        manager = self.coordinator()
        plan = manager.plan()
        self.assertTrue(plan.report['ready'], plan.report['blockers'])
        plan.apply(self.state)
        rollback = rollback_plan(self.coordinator(), Path(plan.report['recovery']).parent.name)
        self.assertTrue(rollback.report['ready'], rollback.report['blockers'])
        rollback.apply(self.state)
        self.assertFalse((self.native / 'config.toml').exists())
        self.assertFalse(self.entry.exists())

    def test_final_framework_failure_recovers_actual_native_plugin_and_preferences(self):
        self.plugin()
        plan = self.coordinator().plan()
        self.assertTrue(plan.report['ready'], plan.report['blockers'])
        before = plan.native_operations[0].world()
        def writer(path, content, mode):
            if path == self.tool / 'machine.yaml':
                raise OSError('isolated finalization failure')
            atomic_write(path, content, mode)
        with self.assertRaises(OSError):
            plan.apply(self.state, writer=writer)
        self.assertFalse((self.native / 'config.toml').exists())
        self.assertFalse(self.entry.exists())
        # Before native operations the combined Agent/App file has been written;
        # overall recovery must also restore the earlier missing-file snapshot.
        self.assertEqual(plan.native_operations[0].world(), before)

    def test_two_actual_agents_share_one_recoverable_run(self):
        binary = os.environ.get('CLAUDE_CONFIG_NATIVE_CLAUDE')
        if not binary:
            self.skipTest('Actual Claude CLI is required')
        native = self.root / 'claude-native'
        self.machine['agents']['claude-code'] = {'profile': 'profiles/agents/claude-code/work.yaml',
                                               'binding': {'executable': binary, 'config_root': str(native)}}
        path = self.data / 'profiles/agents/claude-code/work.yaml'
        path.parent.mkdir(parents=True)
        path.write_text(yaml.safe_dump({'schema_version': 1, 'agent_type': 'claude-code', 'settings': {'model': 'sonnet'}}))
        self.machine_path.write_text(yaml.safe_dump(self.machine))
        self.save_spec()
        self.plugin()
        import json
        package = self.repo / 'packages/tiny'
        (package / '.claude-plugin').mkdir()
        (package / '.claude-plugin/plugin.json').write_text(json.dumps({'name': 'tiny', 'version': '1.0.0'}))
        self.catalog['resources']['tiny']['compatibility']['claude-code']['entry'] = '.claude-plugin/plugin.json'
        profile = yaml.safe_load(path.read_text())
        profile['resources'] = {'plugins': {'tiny@test': {}}}
        path.write_text(yaml.safe_dump(profile))
        self.save()
        plan = self.coordinator().plan()
        self.assertTrue(plan.report['ready'], plan.report['blockers'])
        self.assertEqual(len(plan.native_operations), 2)
        plan.apply(self.state)
        rollback = rollback_plan(self.coordinator(), Path(plan.report['recovery']).parent.name)
        rollback.apply(self.state)
        self.assertFalse((native / 'settings.json').exists())
        self.assertFalse((self.native / 'config.toml').exists())


if __name__ == '__main__':
    unittest.main()
