"""Plugin ownership, guarded side effects and shared-plan failures."""
import json
import os
import subprocess
from pathlib import Path
import shutil
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from claude_config.core.native import parse_native, render_native, snapshot_read
from claude_config.core.native_plugins import PluginOperation, marketplace_name, cache_version
from claude_config.core.recovery import rollback_plan
from claude_config.core.transaction import PlanStale, atomic_write
from test_profile_resources import ResourceFixture


class FakeInstaller:
    """Models only documented installer mutations; production never calls this."""
    def __init__(self):
        self.calls = []
        self.after = None

    def __call__(self, operation, args, work):
        self.calls.append(args)
        config, content = parse_native(operation.agent, snapshot_read(operation.config)[0]), snapshot_read(operation.config)[0]
        verb = args[1]
        if verb == 'marketplace':
            if args[2] == 'remove':
                config.get('marketplaces', {}).pop(args[3], None)
                atomic_write(operation.config, render_native(operation.agent, content, config))
                if self.after:
                    self.after(operation, args)
                return
            market_root = Path(args[3])
            relative = '.claude-plugin/marketplace.json' if operation.agent == 'claude-code' else '.agents/plugins/marketplace.json'
            manifest = json.loads((market_root / relative).read_text())
            market = manifest['name']
            if operation.agent == 'claude-code':
                path = operation.root / 'plugins/known_marketplaces.json'
                known = json.loads(path.read_text()) if path.exists() else {}
                known[market] = {'source': {'source': 'directory', 'path': str(market_root)}, 'installLocation': str(market_root)}
                atomic_write(path, json.dumps(known).encode())
                config.setdefault('extraKnownMarketplaces', {})[market] = {'source': {'source': 'directory', 'path': str(market_root)}}
            else:
                config.setdefault('marketplaces', {})[market] = {'source_type': 'local', 'source': str(market_root)}
        else:
            selector = args[2]
            desired = next(item['desired'] for item in operation.plugins if item['desired']['selector'] == selector)
            name = selector.split('@')[0]
            market_manifest = json.loads(Path(desired['manifest']).read_text())
            source = Path(desired['market_root']) / next(entry['source'] for entry in market_manifest['plugins'] if entry['name'] == name)
            inventory_path = operation.root / 'plugins/installed_plugins.json'
            inventory = json.loads(inventory_path.read_text()) if inventory_path.exists() else {'version': 2, 'plugins': {}}
            if verb in ('install', 'update', 'add'):
                entry = '.codex-plugin/plugin.json' if operation.agent == 'codex' and (source / '.codex-plugin/plugin.json').exists() else '.claude-plugin/plugin.json'
                version = json.loads((source / entry).read_text())['version']
                parent = operation.cache_parent(desired)
                if operation.agent == 'codex':
                    shutil.rmtree(parent, ignore_errors=True)
                cache = parent / cache_version(operation.agent, version)
                shutil.copytree(source, cache, dirs_exist_ok=True)
                if operation.agent == 'claude-code':
                    inventory['plugins'][selector] = [{'scope': 'user', 'version': version, 'installPath': str(cache)}]
                    config.setdefault('enabledPlugins', {}).setdefault(selector, True)
                else:
                    config.setdefault('plugins', {}).setdefault(selector, {}).setdefault('enabled', True)
            elif verb in ('uninstall', 'remove'):
                shutil.rmtree(operation.cache_parent(desired), ignore_errors=True)
                if operation.agent == 'claude-code':
                    inventory['plugins'].pop(selector, None)
                    config.get('enabledPlugins', {}).pop(selector, None)
                else:
                    config.get('plugins', {}).pop(selector, None)
            elif verb in ('enable', 'disable'):
                config.setdefault('enabledPlugins', {})[selector] = verb == 'enable'
            if operation.agent == 'claude-code':
                atomic_write(inventory_path, json.dumps(inventory).encode())
        atomic_write(operation.config, render_native(operation.agent, content, config))
        if self.after:
            self.after(operation, args)


class PluginTests(ResourceFixture):
    def setUp(self):
        super().setUp()
        self.installer = FakeInstaller()
        self.native_call = PluginOperation._call
        self.patch = patch.object(PluginOperation, '_call', lambda operation, args, work: self.installer(operation, args, work))
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def plugin(self, name='tiny', *, agent=None, enabled=None):
        if agent:
            self.profile['agent_type'] = agent
        entry = '.codex-plugin/plugin.json' if self.profile['agent_type'] == 'codex' else '.claude-plugin/plugin.json'
        options = {'enabled': enabled} if enabled is not None else {}
        root = self.add(name, 'plugin', {entry: json.dumps({'name': name, 'version': '1.0.0', 'description': 'isolated'}), 'body.txt': 'one'}, entry, options=options)
        return root, entry

    def deploy(self):
        plan = self.manager().plan()
        self.assertTrue(plan.report['ready'], plan.report['blockers'])
        plan.apply(self.state)
        return plan

    def test_plan_readonly_and_exact_native_install(self):
        self.plugin(enabled=False)
        self.save()
        plan = self.manager().plan()
        self.assertTrue(plan.report['ready'], plan.report['blockers'])
        self.assertFalse(self.native.exists())
        self.assertFalse(self.installer.calls)
        self.assertTrue(any(row['action'] == 'native_install' for row in plan.report['decisions']))
        plan.apply(self.state)
        receipt = plan.native_operations[0].plugins[0]['desired']
        self.assertEqual(plan.native_operations[0].native(receipt)['image'], receipt['image'])
        self.assertFalse(plan.native_operations[0].native(receipt)['enabled'])
        self.assertTrue(json.loads(self.manager().state_path.read_text())['resources']['tiny@test']['native']['plugin'])

    def test_process_exit_after_native_install_before_observation_is_recoverable(self):
        self.plugin()
        self.save()
        worker = '''
import os,sys
from pathlib import Path
from unittest.mock import patch
from claude_config.core.manager import ProfileManager
from claude_config.core.native_plugins import PluginOperation
from test_profile_plugins import FakeInstaller
installer=FakeInstaller()
manager=ProfileManager(Path(sys.argv[1]),'claude-code',probe=lambda *args: [{'name':'isolated','status':'passed'}])
with patch.object(PluginOperation,'_call',lambda op,args,work: installer(op,args,work)):
    plan=manager.plan()
    plan.native_operations[0].observe=lambda row: os._exit(23)
    plan.apply(manager.state_root)
'''
        env = dict(os.environ, PYTHONPATH=os.pathsep.join([str(Path(__file__).resolve().parents[1] / 'src'), str(Path(__file__).parent)]))
        result = subprocess.run([sys.executable, '-c', worker, str(self.machine_path)], env=env, capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 23, result.stderr)
        run = next((self.state / 'runs').glob('*/journal.json')).parent.name
        plan = rollback_plan(self.manager(), run)
        self.assertTrue(plan.report['ready'], plan.report['blockers'])
        plan.apply(self.state)
        self.assertFalse(self.manager().native_path.exists())
        self.assertFalse(self.manager().state_path.exists())

    def test_claude_installer_environment_excludes_development_dependencies(self):
        # Pinned packages ship their runtime artifacts; a lockfile-driven install
        # would otherwise add node_modules and break the exact-image contract.
        self.native = self.root / '.claude'
        self.machine['agents']['claude-code']['binding']['config_root'] = str(self.native)
        self.plugin()
        self.save()
        operation = self.manager().plan().native_operations[0]
        env = operation.install_environment(self.root / 'native-home')
        self.assertEqual(env['npm_config_omit'], 'dev')
        self.assertEqual(Path(env['CLAUDE_CONFIG_DIR']).resolve(), self.native.resolve())

    def test_codex_installer_environment_is_unchanged(self):
        self.plugin(agent='codex')
        self.save()
        operation = self.manager().plan().native_operations[0]
        self.assertNotIn('npm_config_omit', operation.install_environment(self.root / 'native-home'))

    def test_installer_call_receives_runtime_only_environment(self):
        self.native = self.root / '.claude'
        self.machine['agents']['claude-code']['binding']['config_root'] = str(self.native)
        self.plugin()
        self.save()
        operation = self.manager().plan().native_operations[0]
        seen = {}
        def fake_run(argv, **kwargs):
            seen['argv'] = argv
            seen['kwargs'] = kwargs
            return SimpleNamespace(returncode=0)
        with patch('claude_config.core.native_plugins.subprocess.run', fake_run):
            self.native_call(operation, ['plugin', 'install', 'tiny@test', '--scope', 'user', '--json'], self.root / 'work')
        self.assertEqual(seen['argv'][1:3], ['plugin', 'install'])
        self.assertEqual(seen['kwargs']['env']['npm_config_omit'], 'dev')

    def test_noop_apply_does_not_call_installer(self):
        self.plugin()
        self.save()
        self.deploy()
        self.installer.calls.clear()
        self.deploy()
        self.assertEqual(self.installer.calls, [])

    def test_explicit_enable_and_omission_preserve_native_state(self):
        self.plugin(enabled=False)
        self.save()
        self.deploy()
        self.profile['resources']['plugins']['tiny@test'].pop('options')
        self.save_profile()
        plan = self.deploy()
        self.assertFalse(plan.native_operations[0].plugins[0]['desired']['enabled'])
        self.profile['resources']['plugins']['tiny@test']['options'] = {'enabled': True}
        self.save_profile()
        plan = self.deploy()
        self.assertTrue(plan.native_operations[0].native(plan.native_operations[0].plugins[0]['desired'])['enabled'])

    def test_source_update_with_same_version_is_blocked(self):
        root, _ = self.plugin()
        self.save()
        self.deploy()
        (root / 'body.txt').write_text('changed without version bump')
        self.save()
        plan = self.manager().plan()
        self.assertFalse(plan.report['ready'])
        self.assertTrue(any('version reused' in row.get('reason', '') for row in plan.report['blockers']))

    def test_native_cache_and_enabled_drift_block_every_write(self):
        self.plugin()
        self.save()
        plan = self.deploy()
        operation = plan.native_operations[0]
        receipt = operation.plugins[0]['desired']
        cache = operation.cache_parent(receipt) / receipt['version'] / 'body.txt'
        cache.write_text('manual native change')
        self.assertFalse(self.manager().plan().report['ready'])
        cache.write_text('one')
        config = json.loads(self.manager().native_path.read_text())
        config['enabledPlugins'][receipt['selector']] = False
        self.manager().native_path.write_text(json.dumps(config))
        self.assertFalse(self.manager().plan(run_policy={'default': 'source_wins'}).report['ready'])

    def test_native_identity_change_is_not_an_implicit_uninstall(self):
        root, entry = self.plugin()
        self.save()
        self.deploy()
        value = json.loads((root / entry).read_text())
        value['name'] = 'renamed'
        (root / entry).write_text(json.dumps(value))
        self.save()
        self.assertFalse(self.manager().plan().report['ready'])

    def test_unselected_plugin_retained_and_shared_marketplace_entries_preserved(self):
        self.plugin('one')
        self.plugin('two')
        self.save()
        original = self.deploy()
        self.profile['resources']['plugins'].pop('one@test')
        self.save_profile()
        plan = self.deploy()
        manifest = json.loads(Path(plan.native_operations[0].plugins[0]['desired']['manifest']).read_text())
        self.assertEqual({entry['name'] for entry in manifest['plugins']}, {'one', 'two'})
        self.assertTrue(any(row['action'] == 'residual' for row in plan.report['decisions']))
        self.assertTrue(original.native_operations[0].native(original.native_operations[0].plugins[0]['desired'])['installed'])

    def test_generated_marketplace_drift_and_unknown_native_registration_block(self):
        self.plugin()
        self.save()
        original = self.deploy()
        manifest = Path(original.native_operations[0].plugins[0]['desired']['manifest'])
        manifest.write_text('{}')
        self.assertFalse(self.manager().plan().report['ready'])

    def test_unknown_native_marketplace_not_silently_adopted(self):
        self.plugin()
        self.save()
        plan = self.manager().plan()
        operation = plan.native_operations[0]
        desired = operation.plugins[0]['desired']
        self.native.mkdir()
        path = self.native / 'plugins/known_marketplaces.json'
        path.parent.mkdir()
        path.write_text(json.dumps({desired['marketplace']: {'source': {'source': 'directory', 'path': desired['market_root']}, 'installLocation': desired['market_root']}}))
        self.assertFalse(self.manager().plan().report['ready'])

    def test_failed_rollback_restores_the_new_native_version(self):
        self.plugin()
        self.save()
        original = self.deploy()
        operation = original.native_operations[0]
        before = operation.world()
        run = Path(original.report['recovery']).parent.name
        plan = rollback_plan(self.manager(), run)
        def writer(path, content, mode):
            if path == self.state / 'runs' / run / 'journal.json':
                raise OSError('isolated final recovery status failure')
            atomic_write(path, content, mode)
        with self.assertRaises(OSError):
            plan.apply(self.state, writer=writer)
        self.assertEqual(operation.world(), before)
        self.assertTrue(self.manager().state_path.exists())

    def test_native_recovery_scope_and_backup_tampering_are_rejected(self):
        self.plugin()
        self.save()
        original = self.deploy()
        run = Path(original.report['recovery']).parent.name
        path = self.state / 'runs' / run / 'journal.json'
        journal = json.loads(path.read_text())
        row = next(row for row in journal['operations'] if row.get('kind') == 'native-plugin')
        row['before']['files']['/another/path'] = {'exists': False}
        path.write_text(json.dumps(journal))
        with self.assertRaises(ValueError):
            rollback_plan(self.manager(), run)

    def test_in_use_run_created_image_is_not_uninstalled_or_deleted(self):
        self.plugin()
        self.save()
        original = self.deploy()
        operation = original.native_operations[0]
        desired = operation.plugins[0]['desired']
        cache = operation.cache_parent(desired) / desired['version']
        (cache / '.in_use').write_text('active native session')
        journal = json.loads(Path(original.report['recovery']).read_text())
        row = next(row for row in journal['operations'] if row.get('kind') == 'native-plugin')
        before = operation.world()
        self.installer.calls.clear()
        operation.restore(row)
        self.assertEqual(row['status'], 'recovery_conflict')
        self.assertEqual(operation.world(), before)
        self.assertEqual(self.installer.calls, [])
        self.assertTrue((cache / '.in_use').exists())

    def test_alias_normalization_remains_collision_free(self):
        self.assertNotEqual(marketplace_name('A.b'), marketplace_name('a-b'))
        self.assertNotEqual(marketplace_name('A'), marketplace_name('a'))

    def test_changed_native_inventory_after_preview_is_stale(self):
        self.plugin()
        self.save()
        plan = self.manager().plan()
        self.native.mkdir()
        path = self.native / 'plugins/installed_plugins.json'
        path.parent.mkdir()
        path.write_text('{"version":2,"plugins":{}}')
        with self.assertRaises(PlanStale):
            plan.apply(self.state)
        self.assertFalse(self.installer.calls)

    def test_failure_after_install_restores_metadata_images_and_settings(self):
        self.plugin()
        self.save()
        plan = self.manager().plan()
        before = plan.native_operations[0].world()
        def writer(path, content, mode):
            if path == self.manager().state_path:
                raise OSError('isolated failure')
            atomic_write(path, content, mode)
        with self.assertRaises(OSError):
            plan.apply(self.state, writer=writer)
        self.assertEqual(plan.native_operations[0].world(), before)
        self.assertFalse(self.manager().state_path.exists())

    def assert_native_backup_rotation_is_recoverable(self, when):
        self.plugin()
        self.save()
        backup = self.native / 'backups/.claude.json.backup.1'
        backup.parent.mkdir(parents=True)
        backup.write_text('{"historical": true}')
        backup.chmod(0o600)
        plan = self.manager().plan()
        before = plan.native_operations[0].world()
        def rotate(operation, args):
            if args[1] == when:
                backup.unlink()
        self.installer.after = rotate
        plan.apply(self.state)
        self.assertEqual(backup.exists(), when != 'install')
        recovery = rollback_plan(self.manager(), Path(plan.report['recovery']).parent.name)
        self.assertTrue(recovery.report['ready'], recovery.report['blockers'])
        recovery.apply(self.state)
        self.assertEqual(plan.native_operations[0].world(), before)
        self.assertEqual(backup.read_text(), '{"historical": true}')

    def test_native_backup_rotation_is_recoverable(self):
        self.assert_native_backup_rotation_is_recoverable('install')

    def test_backup_rotation_during_native_restore_is_recoverable(self):
        self.assert_native_backup_rotation_is_recoverable('uninstall')

    def test_failed_native_restore_preserves_dependencies_for_retry(self):
        self.plugin()
        self.save()
        plan = self.manager().plan()
        operation = plan.native_operations[0]
        before = operation.world()
        desired = operation.plugins[0]['desired']
        def writer(path, content, mode):
            if path == self.manager().state_path:
                raise OSError('isolated finalization failure')
            atomic_write(path, content, mode)
        with patch.object(operation, 'restore', side_effect=OSError('isolated recovery failure')):
            with self.assertRaises(OSError):
                plan.apply(self.state, writer=writer)
        self.assertTrue(Path(desired['source']).is_dir())
        self.assertTrue(Path(desired['manifest']).exists())
        journal = json.loads(Path(plan.report['recovery']).read_text())
        self.assertTrue(any(row['status'] == 'recovery_conflict' and row['owner'] == 'native-plugin-source'
                            for row in journal['operations']))
        recovery = rollback_plan(self.manager(), Path(plan.report['recovery']).parent.name)
        self.assertTrue(recovery.report['ready'], recovery.report['blockers'])
        recovery.apply(self.state)
        self.assertEqual(operation.world(), before)
        self.assertFalse(Path(desired['manifest']).exists())

    def test_unexpected_image_content_is_rejected_and_unwound(self):
        # An installer that adds content of its own (for example a dependency
        # tree with .bin symlinks) must fail verification and still leave the
        # native configuration exactly as it was before the run.
        self.plugin()
        self.save()
        plan = self.manager().plan()
        operation = plan.native_operations[0]
        desired = operation.plugins[0]['desired']
        before = operation.world()
        def mutate(operation, args):
            if args[1] == 'install':
                image = operation.cache_parent(desired) / desired['version']
                (image / 'body.txt').write_text('installer addition')
                (image / 'unexpected-link').symlink_to('body.txt')
        self.installer.after = mutate
        with self.assertRaises(ValueError):
            plan.apply(self.state)
        journal = json.loads(Path(plan.report['recovery']).read_text())
        row = next(row for row in journal['operations'] if row.get('kind') == 'native-plugin')
        self.assertFalse(row['safe_recovery'])
        self.assertTrue(row['cleanup_safe'])
        self.assertEqual(row['status'], 'rolled_back')
        self.assertEqual(operation.world(), before)
        self.assertFalse(self.manager().state_path.exists())
        self.assertEqual([p.name for p in operation.cache_parent(desired).iterdir()] if operation.cache_parent(desired).exists() else [], [])

    def test_five_existing_plugins_survive_failed_fresh_install_without_reinstall(self):
        for number in range(5):
            self.plugin('existing-' + str(number))
        self.save()
        previous = self.deploy()
        before = previous.native_operations[0].world()
        self.plugin('new-plugin')
        self.save()
        plan = self.manager().plan()
        operation = plan.native_operations[0]
        expected = operation.world()
        self.installer.calls.clear()
        def mutate(op, args):
            if args[1] == 'install' and args[2].startswith('new-plugin@'):
                receipt = next(x['desired'] for x in op.plugins if x['desired']['selector'] == args[2])
                (op.cache_parent(receipt) / receipt['version'] / 'unexpected-link').symlink_to('body.txt')
        self.installer.after = mutate
        with self.assertRaises(ValueError):
            plan.apply(self.state)
        self.assertEqual(operation.world(), expected)
        self.assertTrue(all(not any('existing-' in arg for arg in call) for call in self.installer.calls))
        journal = json.loads(Path(plan.report['recovery']).read_text())
        native = next(row for row in journal['operations'] if row.get('kind') == 'native-plugin')
        self.assertEqual(native['status'], 'rolled_back')
        for path, snapshot in before['directories'].items():
            self.assertEqual(operation.world()['directories'][path], snapshot)

    def test_bad_upgrade_image_restores_previous_version_for_each_agent(self):
        for agent in ('claude-code', 'codex'):
            with self.subTest(agent=agent):
                # Each subcase uses its own source, native inventory and journal.
                fixture = PluginTests()
                fixture.setUp()
                try:
                    root, entry = fixture.plugin(agent=agent)
                    fixture.save()
                    first = fixture.deploy()
                    document = json.loads((root / entry).read_text())
                    document['version'] = '1.1.0'
                    (root / entry).write_text(json.dumps(document))
                    fixture.save()
                    plan = fixture.manager().plan()
                    before = plan.native_operations[0].world()
                    def mutate(op, args):
                        if args[1] in ('install', 'update', 'add'):
                            receipt = op.plugins[0]['desired']
                            image = op.cache_parent(receipt) / receipt['version']
                            if image.exists():
                                (image / 'unexpected-link').symlink_to('body.txt')
                    fixture.installer.after = mutate
                    with self.assertRaises(ValueError):
                        plan.apply(fixture.state)
                    self.assertEqual(plan.native_operations[0].world(), before)
                finally:
                    fixture.doCleanups()
                    fixture.tearDown()

    def test_unrelated_mutation_during_installer_is_preserved_as_conflict(self):
        self.plugin()
        self.save()
        plan = self.manager().plan()
        def manual(operation, args):
            config = json.loads(operation.config.read_text())
            config['manual_other_field'] = 'preserve'
            operation.config.write_text(json.dumps(config))
        self.installer.after = manual
        with self.assertRaises(ValueError):
            plan.apply(self.state)
        self.assertEqual(json.loads(self.manager().native_path.read_text())['manual_other_field'], 'preserve')
        self.assertFalse(self.manager().state_path.exists())
        journal = json.loads(Path(plan.report['recovery']).read_text())
        self.assertEqual(next(row['status'] for row in journal['operations'] if row.get('kind') == 'native-plugin'), 'recovery_conflict')

    def test_independent_rollback_and_later_edit_protection(self):
        self.plugin()
        self.save()
        original = self.deploy()
        run = Path(original.report['recovery']).parent.name
        plan = rollback_plan(self.manager(), run)
        self.assertTrue(plan.report['ready'], plan.report['blockers'])
        plan.apply(self.state)
        self.assertFalse(self.manager().native_path.exists())
        original = self.deploy()
        run = Path(original.report['recovery']).parent.name
        operation = original.native_operations[0]
        (operation.cache_parent(operation.plugins[0]['desired']) / '1.0.0/body.txt').write_text('later edit')
        plan = rollback_plan(self.manager(), run)
        self.assertFalse(plan.report['ready'])
        with self.assertRaises(ValueError):
            plan.apply(self.state)

    def test_partial_installer_failure_uses_the_same_guarded_recovery(self):
        self.plugin()
        self.save()
        plan = self.manager().plan()
        def fail(operation, args):
            if args[1] == 'install':
                self.installer.after = None
                raise RuntimeError('isolated failure after native mutation')
        self.installer.after = fail
        with self.assertRaises(RuntimeError):
            plan.apply(self.state)
        journal = json.loads(Path(plan.report['recovery']).read_text())
        self.assertEqual(next(row['status'] for row in journal['operations'] if row.get('kind') == 'native-plugin'), 'rolled_back')

    def test_plugin_and_mcp_share_native_configuration_without_overwriting(self):
        self.plugin(agent='codex')
        self.add('server', 'mcp', {'server.json': json.dumps({'name': 'other', 'server': {'url': 'https://example.invalid/mcp', 'enabled': False}})}, 'server.json')
        self.save()
        plan = self.deploy()
        config = parse_native('codex', self.manager().native_path.read_bytes())
        self.assertFalse(config['mcp_servers']['other']['enabled'])
        self.assertIn(plan.native_operations[0].plugins[0]['desired']['selector'], config['plugins'])


if __name__ == '__main__':
    unittest.main()
