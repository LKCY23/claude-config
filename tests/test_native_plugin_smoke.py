"""Opt-in native installs, updates and recovery using only temporary roots."""
import json
import os
from pathlib import Path
import shutil
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from claude_config.core.manager import ProfileManager
from claude_config.core.native_plugins import PluginOperation
from claude_config.core.recovery import rollback_plan
from claude_config.core.transaction import atomic_write
from test_profile_resources import ResourceFixture


@unittest.skipUnless(os.environ.get('CLAUDE_CONFIG_RUN_NATIVE_SMOKE') == '1', 'Opt-in native plugin checks')
class NativePluginSmokeTests(ResourceFixture):
    def setup_plugin(self, agent, binary_variable):
        binary = os.environ.get(binary_variable)
        if not binary:
            self.skipTest(binary_variable + ' is required')
        self.profile['agent_type'] = agent
        path = '.claude-plugin/plugin.json' if agent == 'claude-code' else '.codex-plugin/plugin.json'
        root = self.add('tiny', 'plugin', {path: json.dumps({'name': 'tiny', 'version': '1.0.0', 'description': 'isolated'}),
                                          'body.txt': 'version one'}, path, options={'enabled': False})
        self.save()
        self.machine['agents'][agent]['binding']['executable'] = binary
        self.save_profile()
        return root, path

    def real_manager(self):
        return ProfileManager(self.machine_path, self.profile['agent_type'])

    def assert_install_update(self, agent, variable):
        root, entry = self.setup_plugin(agent, variable)
        plan = self.real_manager().plan()
        self.assertTrue(plan.report['ready'], plan.report['blockers'])
        self.assertFalse(self.native.exists())
        self.assertEqual(len(plan.native_operations), 1)
        plan.apply(self.state)
        row = json.loads(Path(plan.report['recovery']).read_text())['operations']
        native = next(row for row in row if row.get('kind') == 'native-plugin')
        self.assertTrue(native['safe_recovery'])
        operation = plan.native_operations[0]
        desired = operation.plugins[0]['desired']
        self.assertFalse(operation.native(desired)['enabled'])
        # No-op source apply does not cause native registration/install calls.
        repeated = self.real_manager().plan()
        self.assertFalse(repeated.native_operations)
        with patch.object(PluginOperation, '_call', side_effect=AssertionError('No-op must not call native installer')):
            repeated.apply(self.state)
        before_update = operation.world()
        document = json.loads((root / entry).read_text())
        document['version'] = '1.1.0'
        (root / entry).write_text(json.dumps(document))
        (root / 'body.txt').write_text('version two')
        self.save()
        update = self.real_manager().plan()
        self.assertTrue(update.report['ready'], update.report['blockers'])
        update.apply(self.state)
        actual = update.native_operations[0].native(update.native_operations[0].plugins[0]['desired'])
        self.assertEqual(actual['version'], '1.1.0')
        self.assertFalse(actual['enabled'])
        run = Path(update.report['recovery']).parent.name
        recovery = rollback_plan(self.real_manager(), run)
        self.assertTrue(recovery.report['ready'], recovery.report['blockers'])
        recovery.apply(self.state)
        self.assertEqual(operation.world(), before_update)

    def assert_independent_fresh_rollback(self, agent, variable):
        self.setup_plugin(agent, variable)
        plan = self.real_manager().plan()
        self.assertTrue(plan.report['ready'], plan.report['blockers'])
        before = plan.native_operations[0].world()
        plan.apply(self.state)
        run = Path(plan.report['recovery']).parent.name
        rollback = rollback_plan(self.real_manager(), run)
        self.assertTrue(rollback.report['ready'], rollback.report['blockers'])
        rollback.apply(self.state)
        self.assertEqual(plan.native_operations[0].world(), before)

    def assert_failure_recovery(self, agent, variable, *, existing):
        root, entry = self.setup_plugin(agent, variable)
        if existing:
            self.real_manager().plan().apply(self.state)
            document = json.loads((root / entry).read_text())
            document['version'] = '1.1.0'
            (root / entry).write_text(json.dumps(document))
            (root / 'body.txt').write_text('new source')
            self.save()
        plan = self.real_manager().plan()
        operation = plan.native_operations[0]
        before = operation.world()
        def writer(path, content, mode):
            if path == self.real_manager().state_path:
                raise OSError('isolated finalization failure')
            atomic_write(path, content, mode)
        with self.assertRaises(OSError):
            plan.apply(self.state, writer=writer)
        journal = json.loads(Path(plan.report['recovery']).read_text())
        native = next(row for row in journal['operations'] if row.get('kind') == 'native-plugin')
        self.assertEqual(native['status'], 'rolled_back', native)
        self.assertEqual(operation.world(), before)

    def test_claude_skill_update_while_user_config_is_busy(self):
        import subprocess
        from claude_config.core.deployment_scope import DeploymentScope
        self.setup_plugin('claude-code', 'CLAUDE_CONFIG_NATIVE_CLAUDE')
        skill = self.add('guide', 'skill', {'SKILL.md': '---\nname: guide\ndescription: test\n---\nold'}, 'SKILL.md')
        self.save()
        self.real_manager().plan().apply(self.state)
        (skill / 'SKILL.md').write_text('---\nname: guide\ndescription: test\n---\nnew')
        self.save()
        script = (
            'import json,sys,time\nfrom pathlib import Path\n'
            'root=Path(sys.argv[1]);(root/"backups").mkdir(exist_ok=True)\n'
            'for i in range(2000):\n'
            ' (root/".claude.json").write_text(json.dumps({"runtime_counter":i}))\n'
            ' (root/"backups/.claude.json.backup.synthetic").write_text(str(i))\n'
            ' (root/"ready").touch()\n'
            ' time.sleep(0.02)\n')
        child = subprocess.Popen([sys.executable, '-c', script, str(self.native)])
        try:
            import time
            deadline = time.monotonic() + 5
            while not (self.native / 'ready').exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue((self.native / 'ready').exists())
            plan = self.real_manager().plan()
            self.assertTrue(plan.report['ready'], plan.report['blockers'])
            self.assertFalse(plan.native_operations)
            with patch.object(PluginOperation, '_call', side_effect=AssertionError('No installer for skill update')):
                plan.apply(self.state)
            self.assertTrue((self.native / 'skills/guide/SKILL.md').read_text().endswith('new'))
        finally:
            child.terminate(); child.wait(timeout=5)
        self.assertGreater(json.loads((self.native / '.claude.json').read_text())['runtime_counter'], 0)
        repeated = self.real_manager().plan(selection=DeploymentScope(resources=('guide@test',)))
        self.assertTrue(repeated.report['ready'], repeated.report['blockers'])
        self.assertTrue(all(row['status'] == 'unchanged' for row in repeated.report['results']))

    def test_claude_install_update_and_enabled(self):
        self.assert_install_update('claude-code', 'CLAUDE_CONFIG_NATIVE_CLAUDE')

    def test_codex_install_update_and_enabled(self):
        self.assert_install_update('codex', 'CLAUDE_CONFIG_NATIVE_CODEX')

    def test_codex_app_backend_install_update_and_enabled(self):
        self.assert_install_update('codex', 'CLAUDE_CONFIG_NATIVE_CODEX_APP')

    def test_claude_new_install_failure_restores_native_state(self):
        self.assert_failure_recovery('claude-code', 'CLAUDE_CONFIG_NATIVE_CLAUDE', existing=False)

    def test_codex_new_install_failure_restores_native_state(self):
        self.assert_failure_recovery('codex', 'CLAUDE_CONFIG_NATIVE_CODEX', existing=False)

    def test_claude_update_failure_restores_old_version(self):
        self.assert_failure_recovery('claude-code', 'CLAUDE_CONFIG_NATIVE_CLAUDE', existing=True)

    def test_codex_update_failure_restores_old_version(self):
        self.assert_failure_recovery('codex', 'CLAUDE_CONFIG_NATIVE_CODEX', existing=True)

    def test_claude_independent_fresh_rollback(self):
        self.assert_independent_fresh_rollback('claude-code', 'CLAUDE_CONFIG_NATIVE_CLAUDE')

    def test_codex_independent_fresh_rollback(self):
        self.assert_independent_fresh_rollback('codex', 'CLAUDE_CONFIG_NATIVE_CODEX')

    def test_claude_multiple_historical_versions_restore_native_gc_metadata(self):
        root, entry = self.setup_plugin('claude-code', 'CLAUDE_CONFIG_NATIVE_CLAUDE')
        self.real_manager().plan().apply(self.state)
        for version in ('1.1.0', '1.2.0'):
            document = json.loads((root / entry).read_text())
            document['version'] = version
            (root / entry).write_text(json.dumps(document))
            (root / 'body.txt').write_text(version)
            self.save()
            plan = self.real_manager().plan()
            before = plan.native_operations[0].world()
            plan.apply(self.state)
        recovery = rollback_plan(self.real_manager(), Path(plan.report['recovery']).parent.name)
        self.assertTrue(recovery.report['ready'], recovery.report['blockers'])
        recovery.apply(self.state)
        self.assertEqual(plan.native_operations[0].world(), before)

    def assert_plus_version_recovery(self, agent, variable):
        root, entry = self.setup_plugin(agent, variable)
        document = json.loads((root / entry).read_text())
        document['version'] = '0.1.0+packaging.1'
        (root / entry).write_text(json.dumps(document))
        self.save()
        first = self.real_manager().plan()
        self.assertTrue(first.report['ready'], first.report['blockers'])
        first.apply(self.state)
        before = first.native_operations[0].world()
        document['version'] = '0.1.0+packaging.2'
        (root / entry).write_text(json.dumps(document))
        (root / 'body.txt').write_text('updated package')
        self.save()
        update = self.real_manager().plan()
        self.assertTrue(update.report['ready'], update.report['blockers'])
        update.apply(self.state)
        recovery = rollback_plan(self.real_manager(), Path(update.report['recovery']).parent.name)
        self.assertTrue(recovery.report['ready'], recovery.report['blockers'])
        recovery.apply(self.state)
        self.assertEqual(first.native_operations[0].world(), before)

    def test_claude_plus_version_update_and_recovery(self):
        self.assert_plus_version_recovery('claude-code', 'CLAUDE_CONFIG_NATIVE_CLAUDE')

    def test_codex_plus_version_update_and_recovery(self):
        self.assert_plus_version_recovery('codex', 'CLAUDE_CONFIG_NATIVE_CODEX')

    def assert_native_adoption(self, agent, variable, *, retained_symlinks=False):
        root, entry = self.setup_plugin(agent, variable)
        document = json.loads((root / entry).read_text())
        document['version'] = '0.1.0+packaging.1'
        (root / entry).write_text(json.dumps(document))
        self.profile['resources']['plugins']['tiny@test'].pop('options')
        self.save()
        legacy = self.root / 'legacy-market'
        shutil.copytree(root, legacy / 'packages/tiny')
        location = '.claude-plugin/marketplace.json' if agent == 'claude-code' else '.agents/plugins/marketplace.json'
        plugin = {'name': 'tiny', 'source': './packages/tiny', 'description': 'isolated'}
        if agent == 'codex':
            plugin.update(category='Productivity', policy={'installation': 'AVAILABLE', 'authentication': 'ON_USE'})
        path = legacy / location
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({'name': 'legacy-market', 'owner': {'name': 'test'}, 'plugins': [plugin]}))
        seed = self.real_manager().plan().native_operations[0]
        self.native.mkdir(parents=True, exist_ok=True)
        seed._call(['plugin', 'marketplace', 'add', str(legacy)], self.root / 'seed')
        seed._call(['plugin', 'install' if agent == 'claude-code' else 'add', 'tiny@legacy-market',
                    *(['--scope', 'user'] if agent == 'claude-code' else []), '--json'], self.root / 'seed')
        if retained_symlinks:
            historical = self.native / 'plugins/cache/legacy-market/tiny/0.3.0'
            historical.mkdir()
            (historical / 'command').symlink_to('../unowned-script')
            (historical / 'body.txt').write_text('untouched historical version')
        plan = self.real_manager().plan(adopt_plugins={'tiny@test': 'tiny@legacy-market'})
        self.assertTrue(plan.report['ready'], plan.report['blockers'])
        operation = plan.native_operations[0]
        before = operation.world()
        with patch.object(operation, '_call', wraps=operation._call) as calls:
            plan.apply(self.state)
        expected = ['marketplace'] if agent == 'claude-code' else ['marketplace', 'marketplace']
        self.assertEqual([call.args[0][1] for call in calls.call_args_list], expected)
        self.assertEqual(operation.native(operation.plugins[0]['desired'])['version'], '0.1.0+packaging.1')
        repeat = self.real_manager().plan()
        self.assertTrue(repeat.report['ready'], repeat.report['blockers'])
        with patch.object(PluginOperation, '_call', side_effect=AssertionError('Existing image must not be reinstalled')):
            repeat.apply(self.state)
            # Recover the adoption run; repeat apply changed no native state.
            recovery = rollback_plan(self.real_manager(), Path(plan.report['recovery']).parent.name)
            self.assertTrue(recovery.report['ready'], recovery.report['blockers'])
            recovery.apply(self.state)
        self.assertEqual(operation.world(), before)

    def test_claude_exact_native_adoption_and_rollback(self):
        self.assert_native_adoption('claude-code', 'CLAUDE_CONFIG_NATIVE_CLAUDE')

    def test_claude_retained_symlinks_native_adoption_and_rollback(self):
        self.assert_native_adoption('claude-code', 'CLAUDE_CONFIG_NATIVE_CLAUDE', retained_symlinks=True)

    def test_codex_exact_native_adoption_and_rollback(self):
        self.assert_native_adoption('codex', 'CLAUDE_CONFIG_NATIVE_CODEX')

    def test_codex_app_exact_native_adoption_and_rollback(self):
        self.assert_native_adoption('codex', 'CLAUDE_CONFIG_NATIVE_CODEX_APP')


if __name__ == '__main__':
    unittest.main()
