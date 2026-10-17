"""Actual native upgrade/reconstruction of an older Claude-style plugin package."""
import json
import os
from pathlib import Path
import shutil
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from claude_config.core.native_plugins import image_fingerprint
from claude_config.core.recovery import rollback_plan
from claude_config.core.transaction import atomic_write
import test_native_plugin_smoke as fixtures
from test_profile_resources import ResourceFixture


@unittest.skipUnless(os.environ.get('CLAUDE_CONFIG_RUN_NATIVE_SMOKE') == '1', 'Opt-in native plugin replacement checks')
class NativeReplacementSmokeTests(ResourceFixture):
    setup_plugin = fixtures.NativePluginSmokeTests.setup_plugin
    real_manager = fixtures.NativePluginSmokeTests.real_manager

    def setup_legacy(self, agent, variable):
        root, entry = self.setup_plugin(agent, variable)
        recovery = self.root / 'fixed-old-source'
        shutil.copytree(root, recovery)
        if agent == 'codex':
            manifest = json.loads((recovery / entry).read_text())
            shutil.rmtree(recovery / '.codex-plugin')
            (recovery / '.claude-plugin').mkdir()
            (recovery / '.claude-plugin/plugin.json').write_text(json.dumps(manifest))
            # A legacy submodule pointer is inert package metadata. The current
            # installer reproduces it; it is not a copied Git database/cache.
            (recovery / '.git').write_text('gitdir: ../../legacy-submodule-location\n')
        manifest = json.loads((root / entry).read_text())
        manifest['version'] = '1.0.0+adapter.1'
        (root / entry).write_text(json.dumps(manifest))
        self.save()
        market = self.root / 'old-market'
        shutil.copytree(recovery, market / 'packages/tiny')
        location = '.claude-plugin/marketplace.json' if agent == 'claude-code' else '.agents/plugins/marketplace.json'
        plugin = {'name': 'tiny', 'source': './packages/tiny', 'description': 'isolated older package'}
        if agent == 'codex':
            plugin.update(category='Productivity', policy={'installation': 'AVAILABLE', 'authentication': 'ON_USE'})
        path = market / location
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({'name': 'old-market', 'owner': {'name': 'test'}, 'plugins': [plugin]}))
        seed = self.real_manager().plan().native_operations[0]
        self.native.mkdir(parents=True, exist_ok=True)
        seed._call(['plugin', 'marketplace', 'add', str(market)], self.root / 'seed')
        seed._call(['plugin', 'install' if agent == 'claude-code' else 'add', 'tiny@old-market',
                    *(['--scope', 'user'] if agent == 'claude-code' else []), '--json'], self.root / 'seed')
        # Keep the installer's enabled=True default. Rollback must not issue
        # redundant `enable`: Claude reports already_in_goal_state as failure.
        spec = self.root / 'replacement.json'
        spec.write_text(json.dumps({'schema_version': 1, 'selector': 'tiny@old-market', 'version': '1.0.0',
                                    'image': image_fingerprint(recovery), 'source': str(recovery)}))
        return spec

    def assert_native_replacement(self, agent, variable, failure=False):
        spec = self.setup_legacy(agent, variable)
        plan = self.real_manager().plan(replace_plugins={'tiny@test': str(spec)})
        self.assertTrue(plan.report['ready'], plan.report['blockers'])
        operation = plan.native_operations[0]
        before = operation.world()
        if failure:
            def writer(path, content, mode):
                if path == self.real_manager().state_path:
                    raise OSError('isolated finalization failure')
                atomic_write(path, content, mode)
            with self.assertRaises(OSError):
                plan.apply(self.state, writer=writer)
            self.assertEqual(operation.world(), before)
            return
        plan.apply(self.state)
        actual = operation.native(operation.plugins[0]['desired'])
        self.assertEqual(actual['version'], '1.0.0+adapter.1')
        self.assertFalse(actual['enabled'])
        self.assertTrue(self.real_manager().plan().report['ready'])
        rollback = rollback_plan(self.real_manager(), Path(plan.report['recovery']).parent.name)
        self.assertTrue(rollback.report['ready'], rollback.report['blockers'])
        rollback.apply(self.state)
        self.assertEqual(operation.world(), before)

    def test_claude_upgrade_and_reconstruction(self):
        self.assert_native_replacement('claude-code', 'CLAUDE_CONFIG_NATIVE_CLAUDE')

    def test_codex_upgrade_and_legacy_fallback_reconstruction(self):
        self.assert_native_replacement('codex', 'CLAUDE_CONFIG_NATIVE_CODEX')

    def test_codex_app_upgrade_and_legacy_fallback_reconstruction(self):
        self.assert_native_replacement('codex', 'CLAUDE_CONFIG_NATIVE_CODEX_APP')

    def test_claude_failed_upgrade_restores_old_installation(self):
        self.assert_native_replacement('claude-code', 'CLAUDE_CONFIG_NATIVE_CLAUDE', True)

    def test_codex_failed_upgrade_restores_old_installation(self):
        self.assert_native_replacement('codex', 'CLAUDE_CONFIG_NATIVE_CODEX', True)

    def test_codex_app_failed_upgrade_restores_old_installation(self):
        self.assert_native_replacement('codex', 'CLAUDE_CONFIG_NATIVE_CODEX_APP', True)


if __name__ == '__main__':
    unittest.main()
