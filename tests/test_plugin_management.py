"""Isolated Git, native adapter and transaction tests; never use daily configuration."""
from copy import deepcopy
import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import sys
import unittest
from unittest.mock import patch

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from claude_config.core.plugin_manager import PluginManager, release_commit
from claude_config.core.plugin_locks import lock_path
from claude_config.core.native_plugins import PluginOperation
from claude_config.core.schema import SchemaError
from claude_config.core.transaction import PlanStale, atomic_write
from claude_config.core.recovery import rollback_plan
from claude_config.plugin import main
from test_profile_plugins import FakeInstaller
from test_profile_resources import ResourceFixture, run_git


class PluginManagementTests(ResourceFixture):
    def setUp(self):
        super().setUp()
        self.installer = FakeInstaller()
        installer = self.installer
        self.mock_native = patch.object(PluginOperation, '_call', lambda op, args, work: installer(op, args, work))
        self.mock_native.start()
        self.addCleanup(self.mock_native.stop)
        self.plugin_root = self.add('tiny', 'plugin', {
            '.claude-plugin/plugin.json': json.dumps({'name': 'tiny', 'version': '1.0.0'}), 'body.txt': 'one'
        }, '.claude-plugin/plugin.json')
        self.commit = self.save()
        self.profile_path = self.data / self.machine['agents']['claude-code']['profile']
        self.lock = self.profile_path.with_suffix('.plugins.lock.yaml')

    def fetch(self, root, repo, commit):
        if root.exists():
            return False
        root.parent.mkdir(parents=True, exist_ok=True)
        # Exact local commits model preparation; no GitHub or user credentials.
        run_git(self.root, 'clone', '--no-hardlinks', str(self.repo), str(root))
        run_git(root, 'remote', 'set-url', 'origin', 'https://github.com/' + repo + '.git')
        run_git(root, 'checkout', '--detach', commit)
        return True

    def service(self):
        return PluginManager(self.manager(), fetcher=self.fetch)

    def install(self, **kwargs):
        plan = self.service().plan('install', 'tiny', **kwargs)
        self.assertTrue(plan.report['ready'], plan.report['blockers'])
        plan.apply(self.state)
        return plan

    def update_source(self, version='2.0.0'):
        (self.plugin_root / '.claude-plugin/plugin.json').write_text(json.dumps({'name': 'tiny', 'version': version}))
        (self.plugin_root / 'body.txt').write_text('two')
        # Reload current declarations rather than overwriting the command's work.
        self.profile = yaml.safe_load(self.profile_path.read_text())
        return self.save()

    def receipt(self):
        return json.loads(self.manager().state_path.read_text())['resources']['tiny@test']['native']['plugin']

    def test_install_adds_selection_lock_and_installs_without_preview_mutation(self):
        self.profile['resources']['plugins'].clear()
        self.save_profile()
        before = self.profile_path.read_bytes()
        plan = self.service().plan('install', 'tiny')
        self.assertEqual(self.profile_path.read_bytes(), before)
        self.assertFalse(self.lock.exists())
        self.assertFalse(self.native.exists())
        self.assertEqual(plan.report['plugin_management']['after'], {'version': '1.0.0', 'enabled': True})
        plan.apply(self.state)
        self.assertTrue(self.receipt()['enabled'])
        self.assertEqual(yaml.safe_load(self.lock.read_text())['plugins']['tiny@test']['source']['commit'], self.commit)
        self.assertEqual(yaml.safe_load(self.profile_path.read_text())['settings'], {'model': 'selected'})
        repeated = self.service().plan('install', 'tiny')
        self.assertTrue(all(row['status'] == 'unchanged' for row in repeated.report['results']))

    def test_preview_id_is_stable_after_cache_preparation(self):
        first = self.service().plan('install', 'tiny')
        second = self.service().plan('install', 'tiny')
        self.assertEqual(first.report['plan_id'], second.report['plan_id'])

    def test_upgrade_preserves_disabled_and_can_enable_in_same_transaction(self):
        self.install(disabled=True)
        commit = self.update_source()
        plan = self.service().plan('upgrade', 'tiny', commit=commit)
        self.assertEqual(plan.report['plugin_management']['after'], {'version': '2.0.0', 'enabled': False})
        plan.apply(self.state)
        self.assertFalse(self.receipt()['enabled'])
        again = self.service().plan('upgrade', 'tiny', commit=commit, enable=True)
        again.apply(self.state)
        self.assertTrue(self.receipt()['enabled'])
        legacy = self.manager().plan()
        self.assertTrue(legacy.report['ready'], legacy.report['blockers'])
        self.assertFalse(legacy.native_operations)

    def test_legacy_scoped_apply_obeys_lock_after_catalog_advances(self):
        self.install()
        self.update_source()
        from claude_config.core.deployment_scope import DeploymentScope
        legacy = self.manager().plan(selection=DeploymentScope(resources=('tiny@test',)))
        self.assertTrue(legacy.report['ready'], legacy.report['blockers'])
        self.assertFalse(legacy.native_operations)
        self.assertEqual(self.receipt()['version'], '1.0.0')

    def test_install_does_not_upgrade_and_upgrade_does_not_install(self):
        with self.assertRaisesRegex(SchemaError, 'managed installation'):
            self.service().plan('upgrade', 'tiny')
        self.install()
        commit = self.update_source()
        with self.assertRaisesRegex(SchemaError, 'use plugin upgrade'):
            self.service().plan('install', 'tiny', commit=commit)

    def test_enable_disable_are_offline_and_legacy_apply_stays_consistent(self):
        self.install()
        with patch.object(self, 'fetch', side_effect=AssertionError('no fetch')):
            for action, enabled in [('disable', False), ('enable', True)]:
                plan = self.service().plan(action, 'tiny')
                plan.apply(self.state)
                self.assertEqual(self.receipt()['enabled'], enabled)
                self.assertEqual(yaml.safe_load(self.profile_path.read_text())['resources']['plugins']['tiny@test']['options']['enabled'], enabled)
                self.assertTrue(self.manager().plan().report['ready'])
                self.assertFalse(self.manager().plan().native_operations)

    def test_disable_works_with_unavailable_git_sources(self):
        self.install()
        shutil.rmtree(self.repo)
        shutil.rmtree(self.state / 'sources')
        plan = self.service().plan('disable', 'tiny')
        plan.apply(self.state)
        self.assertFalse(self.receipt()['enabled'])
        self.assertEqual(self.service().inventory('tiny')['plugins'][0]['enabled'], False)

    def test_profile_change_after_preview_is_stale(self):
        plan = self.service().plan('install', 'tiny')
        self.profile_path.write_text(self.profile_path.read_text() + '\n# later edit\n')
        with self.assertRaises(PlanStale):
            plan.apply(self.state)
        self.assertFalse(self.lock.exists())

    def test_lock_change_after_preview_is_stale(self):
        plan = self.service().plan('install', 'tiny')
        self.lock.write_text('unexpected')
        with self.assertRaises(PlanStale):
            plan.apply(self.state)
        self.assertFalse(self.manager().native_path.exists())

    def test_failure_after_install_rolls_back_native_and_declarations_together(self):
        original = self.profile_path.read_bytes()
        plan = self.service().plan('install', 'tiny')
        def writer(path, content, mode=0o600):
            if path.resolve() == self.lock.resolve():
                raise OSError('fixture disk failure')
            atomic_write(path, content, mode)
        with self.assertRaises(OSError):
            plan.apply(self.state, writer=writer)
        self.assertEqual(self.profile_path.read_bytes(), original)
        self.assertFalse(self.lock.exists())
        self.assertFalse(self.manager().state_path.exists())
        self.assertFalse(self.manager().native_path.exists())

    def test_explicit_rollback_restores_profile_lock_and_native_version(self):
        self.install()
        old_profile, old_lock = self.profile_path.read_bytes(), self.lock.read_bytes()
        commit = self.update_source()
        plan = self.service().plan('upgrade', 'tiny', commit=commit, enable=True)
        result = plan.apply(self.state)
        run = Path(result['recovery']).parent.name
        rollback_plan(self.manager(), run).apply(self.state)
        self.assertEqual(self.profile_path.read_bytes(), old_profile)
        self.assertEqual(self.lock.read_bytes(), old_lock)
        self.assertEqual(self.receipt()['version'], '1.0.0')

    def test_release_manifest_must_match_requested_version(self):
        service = PluginManager(self.manager(), fetcher=self.fetch, resolve_release=lambda repo, version: self.commit)
        with self.assertRaisesRegex(SchemaError, 'manifest disagree'):
            service.plan('install', 'tiny', version='2.0.0')
        self.assertFalse(self.lock.exists())

    def test_no_downgrade_or_short_sha(self):
        for invalid in ['latest', '1.0.0-01', '1.0.0-a..b', '1.0.0+.']:
            with self.subTest(version=invalid), self.assertRaisesRegex(SchemaError, 'exact SemVer'):
                self.service().plan('install', 'tiny', version=invalid)
        self.install()
        with self.assertRaisesRegex(SchemaError, 'full 40'):
            self.service().plan('upgrade', 'tiny', commit=self.commit[:7])
        commit = self.update_source('0.9.0')
        with self.assertRaisesRegex(SchemaError, 'cannot downgrade'):
            self.service().plan('upgrade', 'tiny', commit=commit)

    def test_lock_cannot_redirect_repository(self):
        self.install()
        value = yaml.safe_load(self.lock.read_text())
        value['plugins']['tiny@test']['source']['repo'] = 'elsewhere/other'
        self.lock.write_text(yaml.safe_dump(value))
        self.assertFalse(self.manager().plan().report['ready'])

    def test_unselected_sibling_plugin_and_other_profile_do_not_change(self):
        self.add('other', 'plugin', {'.claude-plugin/plugin.json': json.dumps({'name': 'other', 'version': '1.0.0'})}, '.claude-plugin/plugin.json')
        self.save()
        self.manager().plan().apply(self.state)
        prior = deepcopy(json.loads(self.manager().state_path.read_text())['resources']['other@test'])
        other_profile = self.profile_path.parent / 'other.yaml'
        other_profile.write_bytes(self.profile_path.read_bytes())
        old = other_profile.read_bytes()
        self.service().plan('install', 'tiny').apply(self.state)
        self.service().plan('disable', 'tiny').apply(self.state)
        self.assertEqual(other_profile.read_bytes(), old)
        self.assertEqual(json.loads(self.manager().state_path.read_text())['resources']['other@test'], prior)

    def test_cli_no_tty_previews_then_exact_plan_write(self):
        factory = lambda machine, agent: self.manager()
        with patch('claude_config.plugin.ProfileManager', side_effect=factory), patch('claude_config.fetch_baseline.prepare_git', side_effect=self.fetch):
            args = ['install', 'tiny', '--agent', 'claude-code', '--machine', str(self.machine_path), '--format', 'json']
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = main(args)
            self.assertEqual(code, 0, out.getvalue())
            preview = json.loads(out.getvalue())
            self.assertTrue(preview['preview_only'])
            self.assertFalse(self.lock.exists())
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = main([*args, '--write', '--expect-plan-id', preview['plan_id']])
            self.assertEqual(code, 0, out.getvalue())
            self.assertTrue(self.lock.exists())

    def cli(self, *args):
        out = io.StringIO()
        with patch('claude_config.plugin.ProfileManager', side_effect=lambda *a: self.manager()), \
                patch('claude_config.fetch_baseline.prepare_git', side_effect=self.fetch), contextlib.redirect_stdout(out):
            code = main([*args, '--agent', 'claude-code', '--format', 'json'])
        return code, json.loads(out.getvalue())

    def test_cli_direct_write_installs_upgrades_disables_and_enables(self):
        code, report = self.cli('install', 'tiny', '--write')
        self.assertEqual(code, 0, report)
        self.assertFalse(report['preview_only'])
        self.assertTrue(self.receipt()['enabled'])
        commit = self.update_source()
        code, report = self.cli('upgrade', 'tiny', '--commit', commit, '--write')
        self.assertEqual(code, 0, report)
        self.assertEqual(report['plugin_management']['after']['version'], '2.0.0')
        for action, enabled in [('disable', False), ('enable', True)]:
            code, report = self.cli(action, 'tiny', '--write')
            self.assertEqual(code, 0, report)
            self.assertEqual(self.receipt()['enabled'], enabled)

    def test_cli_explicit_wrong_plan_id_rejects_without_installation(self):
        code, report = self.cli('install', 'tiny', '--write', '--expect-plan-id', 'wrong')
        self.assertNotEqual(code, 0)
        self.assertEqual(report['errors'][0]['code'], 'plan_stale')
        self.assertFalse(self.lock.exists())
        self.assertFalse(self.native.exists())

    def test_cli_direct_write_rechecks_changes_between_plan_and_execution(self):
        original = PluginManager.plan
        def changed(service, *args, **kwargs):
            plan = original(service, *args, **kwargs)
            self.profile_path.write_text(self.profile_path.read_text() + '\n# concurrent edit\n')
            return plan
        with patch.object(PluginManager, 'plan', changed):
            code, report = self.cli('install', 'tiny', '--write')
        self.assertNotEqual(code, 0)
        self.assertEqual(report['errors'][0]['code'], 'plan_stale')
        self.assertFalse(self.lock.exists())
        self.assertFalse(self.native.exists())
        self.assertIn('# concurrent edit', self.profile_path.read_text())

    def test_cli_direct_write_does_not_apply_blocked_plan(self):
        plan = self.service().plan('install', 'tiny')
        plan.report['ready'] = False
        plan.report['blockers'].append({'code': 'fixture_blocker', 'reason': 'Blocked fixture'})
        with patch.object(PluginManager, 'plan', return_value=plan), patch.object(plan, 'apply') as apply:
            code, report = self.cli('install', 'tiny', '--write')
        self.assertNotEqual(code, 0)
        apply.assert_not_called()
        self.assertFalse(self.lock.exists())

    def test_cli_text_preview_displays_plan_id_without_installation(self):
        out = io.StringIO()
        with patch('claude_config.plugin.ProfileManager', side_effect=lambda *a: self.manager()), \
                patch('claude_config.fetch_baseline.prepare_git', side_effect=self.fetch), contextlib.redirect_stdout(out):
            code = main(['install', 'tiny', '--agent', 'claude-code', '--dry-run', '--format', 'text'])
        self.assertEqual(code, 0, out.getvalue())
        self.assertRegex(out.getvalue(), r'计划 ID：[a-f0-9]{64}')
        self.assertFalse(self.lock.exists())
        self.assertFalse(self.native.exists())

    def test_cli_unknown_action_never_loads_machine(self):
        with patch('claude_config.plugin.ProfileManager', side_effect=AssertionError('must not load')):
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertNotEqual(main(['remove', 'tiny', '--agent', 'claude-code', '--format', 'json']), 0)

    def test_query_does_not_create_state(self):
        report = self.service().inventory()
        self.assertEqual(report['plugins'][0]['status'], 'not_installed')
        self.assertFalse(self.state.exists())

    def test_profile_minimum_version_rejects_old_tools(self):
        self.install()
        from claude_config.core.schema import validate_profile
        with patch('claude_config.__version__', '0.7.0'):
            with self.assertRaisesRegex(SchemaError, 'requires claude-config'):
                validate_profile(yaml.safe_load(self.profile_path.read_text()), expected='claude-code')

    def test_offline_enable_refuses_changed_unlocked_catalog(self):
        self.manager().plan().apply(self.state)
        self.update_source()
        with self.assertRaisesRegex(SchemaError, 'Catalog selection changed'):
            self.service().plan('enable', 'tiny')
        self.service().plan('disable', 'tiny').apply(self.state)
        self.assertFalse(self.receipt()['enabled'])

    def test_scope_expands_only_to_declared_dependency(self):
        self.add('helper', 'plugin', {'.claude-plugin/plugin.json': json.dumps({'name': 'helper', 'version': '1.0.0'})}, '.claude-plugin/plugin.json')
        self.catalog['resources']['tiny']['requires']['resources'] = ['helper@test']
        self.save()
        plan = self.service().plan('install', 'tiny')
        self.assertTrue(plan.report['ready'], plan.report['blockers'])
        self.assertEqual(plan.report['deployment_scope']['expanded']['resources'], ['helper@test'])
        self.assertFalse(self.lock.exists())

    def test_fetch_failure_leaves_profile_and_native_untouched(self):
        old = self.profile_path.read_bytes()
        service = PluginManager(self.manager(), fetcher=lambda *a: (_ for _ in ()).throw(SchemaError('fixture download failed')))
        with self.assertRaises(SchemaError):
            service.plan('install', 'tiny')
        self.assertEqual(self.profile_path.read_bytes(), old)
        self.assertFalse(self.lock.exists())
        self.assertFalse(self.native.exists())

    def test_native_failure_restores_declared_and_installed_state(self):
        old = self.profile_path.read_bytes()
        plan = self.service().plan('install', 'tiny')
        def fail(op, args, work):
            value = self.installer(op, args, work)
            if args[:2] == ['plugin', 'install']:
                raise RuntimeError('fixture partial native install')
            return value
        with patch.object(PluginOperation, '_call', fail), self.assertRaises(RuntimeError):
            plan.apply(self.state)
        self.assertEqual(self.profile_path.read_bytes(), old)
        self.assertFalse(self.lock.exists())
        self.assertFalse(self.manager().native_path.exists())

    def test_later_lock_edit_blocks_rollback_without_losing_edit(self):
        plan = self.install()
        self.lock.write_text(self.lock.read_text() + '\n# user change\n')
        old = self.lock.read_bytes()
        recovery = rollback_plan(self.manager(), Path(plan.report['recovery']).parent.name)
        self.assertFalse(recovery.report['ready'])
        self.assertEqual(self.lock.read_bytes(), old)

    def test_install_without_selector_honors_existing_lock(self):
        self.install()
        self.update_source()
        plan = self.service().plan('install', 'tiny')
        self.assertEqual(plan.report['plugin_management']['after']['version'], '1.0.0')
        self.assertTrue(plan.report['ready'])

    def test_changed_cached_source_blocks_legacy_apply(self):
        self.install()
        cached = self.state / 'sources/owner--catalog' / self.commit / 'packages/tiny/body.txt'
        cached.write_text('unexpected')
        self.assertFalse(self.manager().plan().report['ready'])

    def test_symlink_lock_refused(self):
        elsewhere = self.root / 'other-lock'
        elsewhere.write_text('{}')
        self.lock.symlink_to(elsewhere)
        with self.assertRaises(SchemaError):
            self.service().plan('install', 'tiny')
        self.assertEqual(elsewhere.read_text(), '{}')

    def test_enable_never_installs_missing_plugin(self):
        with self.assertRaisesRegex(SchemaError, 'managed installation'):
            self.service().plan('enable', 'tiny')
        self.assertFalse(self.native.exists())

    def test_interactive_decline_does_not_change_configuration(self):
        class Tty(io.StringIO):
            def isatty(self): return True
        original = self.profile_path.read_bytes()
        with patch('claude_config.plugin.ProfileManager', side_effect=lambda *a: self.manager()), \
             patch('claude_config.fetch_baseline.prepare_git', side_effect=self.fetch), \
             patch('sys.stdin', Tty()), contextlib.redirect_stdout(Tty()), patch('builtins.input', return_value='n'):
            self.assertEqual(main(['install', 'tiny', '--agent', 'claude-code', '--machine', str(self.machine_path)]), 0)
        self.assertEqual(self.profile_path.read_bytes(), original)
        self.assertFalse(self.lock.exists())

    def test_interactive_accept_rechecks_state_after_confirmation(self):
        class Tty(io.StringIO):
            def isatty(self): return True
        def confirm(_):
            self.profile_path.write_text(self.profile_path.read_text() + '\n# simultaneous edit\n')
            return 'y'
        with patch('claude_config.plugin.ProfileManager', side_effect=lambda *a: self.manager()), \
             patch('claude_config.fetch_baseline.prepare_git', side_effect=self.fetch), \
             patch('sys.stdin', Tty()), contextlib.redirect_stdout(Tty()), patch('builtins.input', side_effect=confirm):
            self.assertNotEqual(main(['install', 'tiny', '--agent', 'claude-code', '--machine', str(self.machine_path)]), 0)
        self.assertFalse(self.lock.exists())
        self.assertFalse(self.manager().native_path.exists())

    def test_status_cli_does_not_write_reports_or_state(self):
        with patch('claude_config.plugin.ProfileManager', side_effect=lambda *a: self.manager()), contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(main(['status', 'tiny', '--agent', 'claude-code', '--machine', str(self.machine_path), '--format', 'json']), 0)
        report = json.loads(out.getvalue())
        self.assertEqual(report['plugins'][0]['status'], 'not_installed')
        self.assertFalse(self.state.exists())

    def test_release_resolver_rejects_ambiguous_or_draft_tags(self):
        for releases in [[{'tag_name': '1.0.0', 'draft': True}], [{'tag_name': '1.0.0'}, {'tag_name': 'v1.0.0'}]]:
            with patch('claude_config.fetch_baseline.api', return_value=releases), self.assertRaises(SchemaError):
                release_commit('owner/repo', '1.0.0')

    def test_release_resolver_follows_annotated_tag(self):
        def api(path):
            if '/releases?' in path: return [{'tag_name': 'v1.0.0', 'draft': False}]
            if '/ref/' in path: return {'object': {'type': 'tag', 'sha': 'a' * 40}}
            return {'object': {'type': 'commit', 'sha': 'b' * 40}}
        with patch('claude_config.fetch_baseline.api', side_effect=api):
            self.assertEqual(release_commit('owner/repo', '1.0.0'), 'b' * 40)

    def test_short_name_ambiguity_requires_full_reference(self):
        self.profile['resources']['plugins']['tiny@other'] = {}
        self.save_profile()
        with self.assertRaisesRegex(SchemaError, 'ambiguous'):
            self.service().plan('install', 'tiny')

    def test_cold_declared_catalog_is_prepared_only_in_private_cache(self):
        self.machine['catalog_roots']['test'] = str(self.state / 'sources/owner--catalog' / self.commit)
        self.save_profile()
        plan = self.service().plan('install', 'tiny@test')
        self.assertTrue(plan.report['ready'], plan.report['blockers'])
        self.assertTrue(plan.report['plugin_management']['prepared_sources'])
        self.assertFalse(self.lock.exists())

    def test_missing_custom_catalog_directory_is_not_written(self):
        self.machine['catalog_roots']['test'] = str(self.root / 'custom-repo')
        self.save_profile()
        with self.assertRaisesRegex(SchemaError, 'custom catalog'):
            self.service().plan('install', 'tiny@test')
        self.assertFalse((self.root / 'custom-repo').exists())

    def test_interactive_accept_executes_the_displayed_plan(self):
        class Tty(io.StringIO):
            def isatty(self): return True
        with patch('claude_config.plugin.ProfileManager', side_effect=lambda *a: self.manager()), \
             patch('claude_config.fetch_baseline.prepare_git', side_effect=self.fetch), \
             patch('sys.stdin', Tty()), contextlib.redirect_stdout(Tty()), patch('builtins.input', return_value='y'):
            self.assertEqual(main(['install', 'tiny', '--agent', 'claude-code', '--machine', str(self.machine_path)]), 0)
        self.assertTrue(self.lock.exists())
        self.assertTrue(self.receipt()['enabled'])

    def test_toggle_text_report_includes_activation_boundary(self):
        self.install()
        with patch('claude_config.plugin.ProfileManager', side_effect=lambda *a: self.manager()), contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(main(['disable', 'tiny', '--agent', 'claude-code', '--machine', str(self.machine_path), '--dry-run', '--format', 'text']), 0)
        self.assertIn('会话', out.getvalue())
        self.assertIn('禁用', out.getvalue())
        self.assertTrue(self.receipt()['enabled'])

    def test_retained_unselected_plugin_remains_visible_in_status(self):
        self.install()
        profile = yaml.safe_load(self.profile_path.read_text())
        profile['resources']['plugins'] = {}
        self.profile_path.write_text(yaml.safe_dump(profile))
        row = self.service().inventory('tiny')['plugins'][0]
        self.assertFalse(row['selected'])
        self.assertTrue(row['installed'])
        self.assertEqual(row['status'], 'retained_unselected')

    def test_status_reports_catalog_version_without_installing_or_fetching(self):
        row = self.service().inventory('tiny')['plugins'][0]
        self.assertEqual(row['selected_version'], '1.0.0')
        self.assertFalse(row['installed'])
        self.assertFalse(self.state.exists())

    def test_version_selector_pins_resolved_release_and_validates_manifest(self):
        service = PluginManager(self.manager(), fetcher=self.fetch, resolve_release=lambda repo, version: self.commit)
        plan = service.plan('install', 'tiny', version='1.0.0')
        plan.apply(self.state)
        pin = yaml.safe_load(self.lock.read_text())['plugins']['tiny@test']
        self.assertEqual(pin['resolution'], {'kind': 'release', 'requested': '1.0.0'})
        self.assertEqual(pin['source']['commit'], self.commit)

    def test_dependency_not_selected_is_not_implicitly_added(self):
        self.catalog['resources']['tiny']['requires']['resources'] = ['absent@test']
        self.save()
        old = self.profile_path.read_bytes()
        with self.assertRaisesRegex(SchemaError, 'explicitly selected'):
            self.service().plan('install', 'tiny')
        self.assertEqual(self.profile_path.read_bytes(), old)
        self.assertFalse(self.native.exists())

    def test_changed_native_enablement_after_preview_is_stale(self):
        self.install()
        plan = self.service().plan('disable', 'tiny')
        native = self.manager().native_path
        value = json.loads(native.read_text())
        value['enabledPlugins'][self.receipt()['selector']] = False
        native.write_text(json.dumps(value))
        with self.assertRaises(PlanStale):
            plan.apply(self.state)

    def test_in_use_upgrade_blocks_without_changing_declarations(self):
        self.install()
        commit = self.update_source()
        old = self.profile_path.read_bytes(), self.lock.read_bytes()
        with patch('claude_config.core.native_plugins.image_in_use', return_value=True), self.assertRaisesRegex(SchemaError, 'in use'):
            self.service().plan('upgrade', 'tiny', commit=commit)
        self.assertEqual((self.profile_path.read_bytes(), self.lock.read_bytes()), old)
        self.assertEqual(self.receipt()['version'], '1.0.0')

    def test_corrupt_lock_provenance_cannot_mislabel_an_arbitrary_commit(self):
        self.install()
        pin = yaml.safe_load(self.lock.read_text())
        pin['plugins']['tiny@test']['resolution'] = {'kind': 'commit', 'requested': 'f' * 40}
        self.lock.write_text(yaml.safe_dump(pin))
        with self.assertRaisesRegex(SchemaError, 'provenance disagrees'):
            self.service().plan('enable', 'tiny')

    def test_repeating_enable_and_disable_has_zero_native_changes(self):
        self.install()
        for action in ['disable', 'enable']:
            self.service().plan(action, 'tiny').apply(self.state)
            plan = self.service().plan(action, 'tiny')
            self.assertTrue(all(row['status'] == 'unchanged' for row in plan.report['results']))

    def test_failed_toggle_state_write_recovers_previous_enablement(self):
        self.install()
        profile = self.profile_path.read_bytes()
        plan = self.service().plan('disable', 'tiny')
        state_path = self.manager().state_path.resolve()
        def writer(path, content, mode=0o600):
            if path.resolve() == state_path: raise OSError('fixture state failure')
            atomic_write(path, content, mode)
        with self.assertRaises(OSError): plan.apply(self.state, writer=writer)
        self.assertTrue(self.receipt()['enabled'])
        self.assertTrue(self.service().inventory('tiny')['plugins'][0]['enabled'])
        self.assertEqual(self.profile_path.read_bytes(), profile)

    def test_unfinished_run_blocks_planning_before_source_fetch(self):
        from claude_config.core.transaction import UnfinishedRun
        with patch('claude_config.core.plugin_manager.unfinished', return_value=[{'run': 'fixture'}]), \
             patch.object(self, 'fetch', side_effect=AssertionError('must not fetch')), self.assertRaises(UnfinishedRun):
            self.service().plan('install', 'tiny')
        self.assertFalse(self.lock.exists())

    def test_cli_partial_source_preparation_is_reported_on_later_failure(self):
        self.catalog['resources']['tiny']['requires']['resources'] = ['missing@test']
        self.save()
        with patch('claude_config.plugin.ProfileManager', side_effect=lambda *a: self.manager()), \
             patch('claude_config.fetch_baseline.prepare_git', side_effect=self.fetch), contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertNotEqual(main(['install', 'tiny', '--agent', 'claude-code', '--machine', str(self.machine_path), '--format', 'json']), 0)
        self.assertTrue(json.loads(out.getvalue())['prepared_sources'])
        self.assertFalse(self.lock.exists())


if __name__ == '__main__':
    unittest.main()
