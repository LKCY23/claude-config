from copy import deepcopy
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import tomlkit
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from claude_config.core.app_fields import validate_settings
from claude_config.core.app_manager import AppProfileManager
from claude_config.core.app_native import AppProbe, asar_entries
from claude_config.core.merge import fingerprint
from claude_config.core.recovery import rollback_plan
from claude_config.core.reporting import build_report, overview_lines
from claude_config.core.schema import SchemaError
from claude_config.core.transaction import PlanStale


class FakeAppProbe:
    def __init__(self):
        self.running = False
        self.calls = []

    def check(self, app, application, settings, sources):
        self.calls.append((app, deepcopy(settings)))
        return [{'name': 'app_preference_interface', 'status': 'passed', 'level': 'test-fixture'}]

    def assert_stopped(self, application):
        if self.running:
            raise PlanStale('App is running; close normally and replan')


class AppTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.data = self.root / 'profiles-data'
        self.state = self.root / 'state'
        self.machine_path = self.root / 'machine.yaml'
        self.codex = self.root / 'codex'
        self.codex.mkdir()
        self.probe = FakeAppProbe()
        self.machine = {'schema_version': 1, 'machine_id': 'fixture', 'config_dir': str(self.data),
                        'catalog_roots': {}, 'agents': {'codex': {'profile': 'profiles/agents/codex/work.yaml',
                        'binding': {'executable': str(self.root / 'bin'), 'config_root': str(self.codex)}}},
                        'apps': {}, 'bindings': {}, 'runtime': {'state_dir': str(self.state)}}
        for app in ('codex-app', 'claude-desktop'):
            name = f'profiles/apps/{app}/work.yaml'
            self.machine['apps'][app] = {'profile': name, 'binding': {'application': str(self.root / (app + '.app')),
                                                                    'data_root': str(self.root / app)}}
            self.profile(app, {})
        self.save_machine()

    def tearDown(self):
        self.temp.cleanup()

    def save_machine(self):
        self.machine_path.write_text(yaml.safe_dump(self.machine, sort_keys=False))

    def profile(self, app='codex-app', settings=None, name=None, **extra):
        path = self.data / (name or self.machine['apps'][app]['profile'])
        path.parent.mkdir(parents=True, exist_ok=True)
        value = {'schema_version': 1, 'app': app, **extra}
        if settings is not None:
            value['settings'] = settings
        path.write_text(yaml.safe_dump(value, sort_keys=False))
        return path

    def manager(self, app='codex-app', probe=None):
        return AppProfileManager(self.machine_path, app, probe=probe or self.probe)

    def native(self, app='codex-app', value=None, text=None):
        path = self.codex / 'config.toml' if app == 'codex-app' else self.root / app / 'config.json'
        path.parent.mkdir(parents=True, exist_ok=True)
        if value is not None:
            text = tomlkit.dumps(value) if app == 'codex-app' else json.dumps(value)
        if text is not None:
            path.write_text(text)
        return path

    def features(self):
        self.native('claude-desktop', {'userThemeMode': 'light'})
        (self.root / 'claude-desktop/claude_desktop_config.json').write_text('{"deploymentMode":"3p","credential":"PRIVATE_MODE_FILE"}')
        library = self.root / 'claude-desktop' / 'configLibrary'
        library.mkdir(exist_ok=True)
        uid = '11111111-2222-3333-4444-555555555555'
        (library / '_meta.json').write_text(json.dumps({'appliedId': uid}))
        path = library / (uid + '.json')
        path.write_text(json.dumps({'inferenceGatewayApiKey': 'PRIVATE_GATEWAY', 'chatTabEnabled': False,
                                    'mcpServers': {'existing': {'secret': 'KEEP'}}}))
        return path

    def test_codex_selected_fields_preserve_agent_credentials_comments_and_state(self):
        self.profile(settings={'followUpQueueMode': 'queue', 'git-branch-prefix': '', 'preventSleepWhileRunning': False})
        path = self.native(text='# retain\nmodel = "agent-model" # unchanged\n[model_providers.local]\napi_key = "PRIVATE_KEY"\n[desktop]\nfollowUpQueueMode = "steer"\nvoiceSetting = "KEEP"\n')
        legacy = self.codex / '.codex-global-state.json'
        legacy.write_text('{"git-branch-prefix":"legacy","runtime":"KEEP"}')
        original = legacy.read_bytes()
        plan = self.manager().plan()
        self.assertTrue(plan.report['ready'])
        self.assertNotIn('PRIVATE_KEY', json.dumps(plan.report))
        self.assertFalse(self.state.exists())
        plan.apply(self.state)
        text = path.read_text()
        self.assertIn('# retain', text)
        self.assertIn('model = "agent-model" # unchanged', text)
        value = tomlkit.parse(text).unwrap()
        self.assertEqual(value['model_providers']['local']['api_key'], 'PRIVATE_KEY')
        self.assertEqual(value['desktop']['voiceSetting'], 'KEEP')
        self.assertEqual(value['desktop']['git-branch-prefix'], '')
        self.assertIs(value['desktop']['preventSleepWhileRunning'], False)
        self.assertEqual(legacy.read_bytes(), original)

    def test_empty_profile_does_not_create_native_or_managed_state(self):
        plan = self.manager().plan()
        self.assertTrue(plan.report['ready'])
        self.assertEqual(plan.changes, ())
        plan.apply(self.state)
        self.assertFalse((self.codex / 'config.toml').exists())
        self.assertFalse(self.manager().state_path.exists())

    def test_noop_preserves_native_bytes(self):
        self.profile(settings={'reviewDelivery': 'inline'})
        path = self.native(text='[desktop]\nreviewDelivery = "inline" # keep\n')
        before = path.read_bytes()
        self.manager().plan().apply(self.state)
        self.assertEqual(before, path.read_bytes())
        repeated = self.manager().plan().apply(self.state)
        self.assertTrue(all(row['status'] == 'unchanged' for row in repeated['results']))

    def test_open_target_preserves_machine_project_preferences(self):
        self.profile(settings={'open-in-target-preferences': {'global': 'vscode'}})
        path = self.native(value={'desktop': {'open-in-target-preferences': {'global': 'finder', 'perPath': {'/local/project': 'cursor'}}}})
        self.manager().plan().apply(self.state)
        opening = tomlkit.parse(path.read_text()).unwrap()['desktop']['open-in-target-preferences']
        self.assertEqual(opening, {'global': 'vscode', 'perPath': {'/local/project': 'cursor'}})
        exported = self.manager().export(['/settings/open-in-target-preferences/global'])
        self.assertNotIn('/local/project', json.dumps(exported.report))

    def test_excluded_agent_shortcut_voice_and_desktop_resource_fields_rejected(self):
        for app, key in [('codex-app', key) for key in ('model', 'mcp_servers', 'plugins', 'dictationSoundsEnabled', 'external-browser-link-shortcut', 'custom_file_handlers', 'git-worktree-root')] + [('claude-desktop', key) for key in ('mcpServers', 'inferenceProvider', 'quickEntry', 'voiceEntry')]:
            with self.subTest(app=app, key=key), self.assertRaises(SchemaError):
                self.profile(app, {key: True})
                self.manager(app).plan()

    def test_resources_in_app_profile_rejected(self):
        self.profile(settings={}, resources={'skills': {}})
        with self.assertRaises(SchemaError):
            self.manager().plan()

    def test_types_enums_ranges_and_local_paths_rejected(self):
        cases = [{'preventSleepWhileRunning': 0}, {'git-pr-poll-interval-seconds': 14},
                 {'worktree-keep-count': True}, {'codeFontSize': 25}, {'composerEnterBehavior': 'bad'},
                 {'followUpQueueMode': None}, {'open-in-target-preferences': {'perPath': {'/tmp/x': 'vscode'}}},
                 {'open-in-target-preferences': {'global': '/Applications/Editor.app'}}]
        for settings in cases:
            with self.subTest(settings=settings), self.assertRaises(SchemaError):
                validate_settings('codex-app', settings)

    def test_excluded_fonts_are_rejected_with_other_selected_fields(self):
        self.profile(settings={'followUpQueueMode': 'queue', 'appearanceLightChromeTheme': {'fonts': {'code': 'LOCAL_PROGRAM'}}})
        with self.assertRaises(SchemaError):
            self.manager().plan()

    def test_missing_app_audit_blocks_and_empty_profile_needs_no_app(self):
        self.profile(settings={'followUpQueueMode': 'queue'})
        plan = self.manager(probe=AppProbe()).plan()
        self.assertFalse(plan.report['ready'])
        self.assertEqual(plan.changes, ())
        self.profile(settings={})
        self.assertTrue(self.manager(probe=AppProbe()).plan().report['ready'])

    def test_running_app_blocks_native_write_but_allows_export_and_noop(self):
        self.profile(settings={'followUpQueueMode': 'queue'})
        self.native(value={'desktop': {'followUpQueueMode': 'steer'}})
        self.probe.running = True
        self.assertFalse(self.manager().plan().report['ready'])
        export = self.manager().export(['/settings/followUpQueueMode'])
        self.assertTrue(export.report['ready'])
        export.apply(self.state)
        self.assertTrue(self.manager().plan().report['ready'])

    def test_app_starts_after_plan_blocks_commit(self):
        self.profile(settings={'preventSleepWhileRunning': True})
        plan = self.manager().plan()
        self.probe.running = True
        with self.assertRaises(PlanStale):
            plan.apply(self.state)
        self.assertFalse((self.codex / 'config.toml').exists())

    def test_stale_shared_native_file_blocks_commit(self):
        self.profile(settings={'reviewDelivery': 'detached'})
        path = self.native(value={'model': 'a'})
        plan = self.manager().plan()
        path.write_text('model = "new-agent-write"\n')
        with self.assertRaises(PlanStale):
            plan.apply(self.state)
        self.assertEqual(path.read_text(), 'model = "new-agent-write"\n')

    def test_managed_drift_requires_fingerprint_resolution(self):
        self.profile(settings={'followUpQueueMode': 'queue'})
        self.manager().plan().apply(self.state)
        self.native(value={'desktop': {'followUpQueueMode': 'interrupt'}})
        self.assertFalse(self.manager().plan(run_policy={'default': 'source_wins'}).report['ready'])
        target = {'settings': {'followUpQueueMode': 'interrupt'}}
        plan = self.manager().plan(resolutions={'/settings/followUpQueueMode': {'target_fingerprint': fingerprint(target), 'policy': 'keep_target'}})
        self.assertTrue(plan.report['ready'])
        plan.apply(self.state)
        self.assertEqual(tomlkit.parse((self.codex / 'config.toml').read_text())['desktop']['followUpQueueMode'], 'interrupt')

    def test_switch_preserves_omitted_preferences_and_records_residual(self):
        self.profile(settings={'reviewDelivery': 'detached', 'preventSleepWhileRunning': True})
        self.manager().plan().apply(self.state)
        name = 'profiles/apps/codex-app/other.yaml'
        self.profile(settings={'reviewDelivery': 'inline'}, name=name)
        plan = self.manager().plan(profile=name)
        self.assertTrue(any(row['action'] == 'residual' for row in plan.report['decisions']))
        plan.apply(self.state)
        value = tomlkit.parse((self.codex / 'config.toml').read_text()).unwrap()['desktop']
        self.assertTrue(value['preventSleepWhileRunning'])
        self.assertEqual(yaml.safe_load(self.machine_path.read_text())['apps']['codex-app']['profile'], name)

    def test_export_selected_present_only_preserves_missing_and_unselected(self):
        self.profile(settings={'preventSleepWhileRunning': True, 'reviewDelivery': 'inline'})
        path = self.native(value={'model': 'PRIVATE', 'desktop': {'reviewDelivery': 'detached'}})
        before = path.read_bytes()
        plan = self.manager().export(['/settings/preventSleepWhileRunning', '/settings/reviewDelivery'])
        self.assertTrue(plan.report['ready'])
        plan.apply(self.state)
        profile = yaml.safe_load((self.data / self.machine['apps']['codex-app']['profile']).read_text())
        self.assertEqual(profile['settings'], {'preventSleepWhileRunning': True, 'reviewDelivery': 'detached'})
        self.assertEqual(path.read_bytes(), before)
        self.assertFalse(self.manager().state_path.exists())
        self.assertNotIn('PRIVATE', json.dumps(plan.report))

    def test_export_new_profile_and_invalid_selectors(self):
        self.native(value={'desktop': {'preventSleepWhileRunning': False}})
        name = 'profiles/apps/codex-app/exported.yaml'
        self.manager().export(['/settings/preventSleepWhileRunning'], profile=name).apply(self.state)
        self.assertIs(yaml.safe_load((self.data / name).read_text())['settings']['preventSleepWhileRunning'], False)
        for selectors in ([], ['/settings'], ['/settings/model'], ['/settings/open-in-target-preferences'], ['/settings/voice']):
            with self.subTest(selectors=selectors), self.assertRaises(SchemaError):
                self.manager().export(selectors)

    def test_claude_official_theme_and_locale_preserve_mcp_and_auth(self):
        self.profile('claude-desktop', {'userThemeMode': 'dark', 'locale': 'en-US'})
        path = self.native('claude-desktop', {'deploymentMode': '1p', 'token': 'PRIVATE', 'mcpServers': {'existing': {}}})
        plan = self.manager('claude-desktop').plan()
        self.assertTrue(plan.report['ready'])
        plan.apply(self.state)
        value = json.loads(path.read_text())
        self.assertEqual(value['deploymentMode'], '1p')
        self.assertEqual(value['token'], 'PRIVATE')
        self.assertEqual(value['userThemeMode'], 'dark')
        self.assertEqual(value['mcpServers'], {'existing': {}})
        self.assertNotIn('PRIVATE', json.dumps(plan.report))

    def test_claude_third_party_features_update_applied_file_only(self):
        path = self.features()
        main = self.root / 'claude-desktop' / 'config.json'
        main_before = main.read_bytes()
        self.profile('claude-desktop', {'chatTabEnabled': True, 'autoModeEnabled': False})
        plan = self.manager('claude-desktop').plan()
        self.assertTrue(plan.report['ready'])
        self.assertNotIn('PRIVATE_GATEWAY', json.dumps(plan.report))
        plan.apply(self.state)
        value = json.loads(path.read_text())
        self.assertTrue(value['chatTabEnabled'])
        self.assertIs(value['autoModeEnabled'], False)
        self.assertEqual(value['inferenceGatewayApiKey'], 'PRIVATE_GATEWAY')
        self.assertEqual(main.read_bytes(), main_before)

    def test_claude_official_cannot_write_or_export_third_party_flags(self):
        self.native('claude-desktop', {'deploymentMode': '1p'})
        self.profile('claude-desktop', {'chatTabEnabled': True})
        self.assertFalse(self.manager('claude-desktop').plan().report['ready'])
        self.assertFalse(self.manager('claude-desktop').export(['/settings/chatTabEnabled']).report['ready'])

    def test_claude_metadata_change_blocks_and_missing_applied_file_not_created(self):
        path = self.features()
        self.profile('claude-desktop', {'chatTabEnabled': True})
        plan = self.manager('claude-desktop').plan()
        meta = path.parent / '_meta.json'
        meta.write_text('{"appliedId":"different"}')
        with self.assertRaises(PlanStale):
            plan.apply(self.state)
        path.unlink()
        meta.write_text(json.dumps({'appliedId': path.stem}))
        self.assertFalse(self.manager('claude-desktop').plan().report['ready'])
        self.assertFalse(path.exists())

    def test_claude_feature_export_does_not_copy_provider_or_auth(self):
        path = self.features()
        self.profile('claude-desktop', {})
        self.manager('claude-desktop').export(['/settings/chatTabEnabled']).apply(self.state)
        profile = yaml.safe_load((self.data / self.machine['apps']['claude-desktop']['profile']).read_text())
        self.assertEqual(profile['settings'], {'chatTabEnabled': False})
        self.assertEqual(json.loads(path.read_text())['inferenceGatewayApiKey'], 'PRIVATE_GATEWAY')

    def test_claude_hybrid_pointer_blocks_feature_apply_and_export(self):
        entry = self.features()
        meta = entry.parent / '_meta.json'
        for pointer in ({'bootstrapUrl': 'https://example.invalid/PRIVATE'}, {}, None):
            meta.write_text(json.dumps({'appliedId': entry.stem, 'hybridPointer': pointer}))
            before = {p: p.read_bytes() for p in (entry, meta, self.root / 'claude-desktop/config.json')}
            for key in ('chatTabEnabled', 'autoModeEnabled', 'builtinBrowserEnabled'):
                with self.subTest(pointer=pointer, key=key):
                    self.profile('claude-desktop', {key: True})
                    manager = self.manager('claude-desktop')
                    for plan in (manager.plan(), manager.export(['/settings/' + key])):
                        self.assertFalse(plan.report['ready'])
                        self.assertIn('claude_applied_profile', [r['name'] for r in plan.report['blockers']])
                        self.assertNotIn('PRIVATE', json.dumps(plan.report))
                    self.assertEqual({p: p.read_bytes() for p in before}, before)

    def test_claude_hybrid_pointer_does_not_block_theme_and_locale(self):
        entry = self.features()
        meta = entry.parent / '_meta.json'
        meta.write_text(json.dumps({'appliedId': entry.stem, 'hybridPointer': {'bootstrapUrl': 'https://example.invalid/'}}))
        before = {p: p.read_bytes() for p in (entry, meta)}
        self.profile('claude-desktop', {'userThemeMode': 'dark', 'locale': 'en-US'})
        self.manager('claude-desktop').plan().apply(self.state)
        self.assertEqual({p: p.read_bytes() for p in before}, before)

    def test_claude_hybrid_pointer_added_after_plan_prevents_write(self):
        entry = self.features()
        self.profile('claude-desktop', {'autoModeEnabled': False})
        plan = self.manager('claude-desktop').plan()
        before = entry.read_bytes()
        (entry.parent / '_meta.json').write_text(json.dumps({
            'appliedId': entry.stem, 'hybridPointer': {'bootstrapUrl': 'https://example.invalid/'}}))
        with self.assertRaises(PlanStale):
            plan.apply(self.state)
        self.assertEqual(entry.read_bytes(), before)

    def test_rollback_restores_only_owned_run_and_preserves_later_changes(self):
        self.profile(settings={'reviewDelivery': 'detached'})
        path = self.native(value={'desktop': {'reviewDelivery': 'inline'}})
        before = path.read_bytes()
        result = self.manager().plan().apply(self.state)
        run = Path(result['recovery']).parent.name
        rollback_plan(self.manager(), run).apply(self.state)
        self.assertEqual(path.read_bytes(), before)
        result = self.manager().plan().apply(self.state)
        run = Path(result['recovery']).parent.name
        path.write_text('[desktop]\nreviewDelivery = "inline" # manual\n')
        self.assertFalse(rollback_plan(self.manager(), run).report['ready'])

    def test_rollback_lifecycle_blocks_native_but_not_profile_only(self):
        self.profile(settings={'reviewDelivery': 'detached'})
        self.native(value={'desktop': {'reviewDelivery': 'inline'}})
        result = self.manager().plan().apply(self.state)
        run = Path(result['recovery']).parent.name
        self.probe.running = True
        self.assertFalse(rollback_plan(self.manager(), run).report['ready'])
        result = self.manager().export(['/settings/reviewDelivery']).apply(self.state)
        run = Path(result['recovery']).parent.name
        self.assertTrue(rollback_plan(self.manager(), run).report['ready'])

    def test_codex_binding_required_and_app_agent_recovery_scopes_disjoint(self):
        from claude_config.core.manager import ProfileManager
        self.profile(settings={'reviewDelivery': 'inline'})
        result = self.manager().plan().apply(self.state)
        run = Path(result['recovery']).parent.name
        with self.assertRaises(SchemaError):
            rollback_plan(ProfileManager(self.machine_path, 'codex'), run)
        self.machine['agents'] = {}
        self.save_machine()
        with self.assertRaises(SchemaError):
            self.manager()

    def test_app_start_after_recovery_preview_blocks_restoration(self):
        self.profile(settings={'reviewDelivery': 'detached'})
        path = self.native(value={'desktop': {'reviewDelivery': 'inline'}})
        result = self.manager().plan().apply(self.state)
        run = Path(result['recovery']).parent.name
        plan = rollback_plan(self.manager(), run)
        after = path.read_bytes()
        self.probe.running = True
        with self.assertRaises(PlanStale):
            plan.apply(self.state)
        self.assertEqual(path.read_bytes(), after)

    def test_native_and_profile_policies_are_reported_without_silent_override(self):
        self.profile(settings={'reviewDelivery': 'detached'}, merge_policy={'default': 'keep_target'})
        path = self.native(value={'desktop': {'reviewDelivery': 'inline'}})
        plan = self.manager().plan()
        self.assertTrue(plan.report['ready'])
        decision = next(row for row in plan.report['decisions'] if row['path'] == ['settings', 'reviewDelivery'])
        self.assertEqual(decision['policy_origin'], 'profile_default')
        plan.apply(self.state)
        self.assertEqual(tomlkit.parse(path.read_text())['desktop']['reviewDelivery'], 'inline')
        self.profile(settings={'reviewDelivery': 'detached'}, merge_policy={'default': 'fail_if_different'})
        self.assertFalse(self.manager().plan().report['ready'])

    def test_duplicate_json_and_toml_parent_type_block_before_writes(self):
        self.profile('claude-desktop', {'userThemeMode': 'dark'})
        self.native('claude-desktop', text='{"userThemeMode":"dark","userThemeMode":"light"}')
        with self.assertRaises(ValueError):
            self.manager('claude-desktop').plan()
        self.profile(settings={'reviewDelivery': 'inline'})
        self.native(text='desktop = "wrong-parent-type"\n')
        with self.assertRaises(SchemaError):
            self.manager().plan()

    def test_shared_schema_enforces_app_field_boundary(self):
        from claude_config.core.schema import validate_profile
        with self.assertRaises(SchemaError):
            validate_profile({'schema_version': 1, 'app': 'codex-app', 'settings': {'plugins': {}}}, kind='app')

    def test_excluded_worktree_path_and_custom_handlers_are_never_exported_or_modified(self):
        value = {'desktop': {'reviewDelivery': 'inline', 'git-worktree-root': '/local/worktrees',
                             'custom_file_handlers': {'tex': {'command': '/local/editor'}}}}
        path = self.native(value=value)
        self.profile(settings={'reviewDelivery': 'detached'})
        self.manager().plan().apply(self.state)
        actual = tomlkit.parse(path.read_text()).unwrap()['desktop']
        for key in ('git-worktree-root', 'custom_file_handlers'):
            self.assertEqual(actual[key], value['desktop'][key])
            with self.assertRaises(SchemaError):
                self.manager().export(['/settings/' + key])
        self.assertNotIn('/local/', self.manager().state_path.read_text())
        from claude_config.core.schema import validate_profile
        for key in ('git-worktree-root', 'custom_file_handlers'):
            with self.assertRaises(SchemaError):
                validate_profile({'schema_version': 1, 'app': 'codex-app', 'settings': {key: 'ignored'}}, kind='app')
        with self.assertRaises(SchemaError):
            self.manager().plan(run_policy={'fields': {'/settings/git-worktree-root': 'keep_target'}})

    def test_cli_requires_exclusive_target_and_rejects_app_resources(self):
        script = Path(__file__).resolve().parents[1] / 'src/claude_config/profile.py'
        for arguments in (['--app', 'codex-app', '--agent', 'codex'], ['--app', 'codex-app', '--resource', 'x@y']):
            result = subprocess.run([sys.executable, '-m', 'claude_config.profile', 'plan', '--machine', str(self.machine_path), *arguments], capture_output=True, env={**os.environ, 'HOME': str(self.root)})
            self.assertEqual(result.returncode, 2)
        result = subprocess.run([sys.executable, '-m', 'claude_config.profile', 'plan', '--machine', str(self.machine_path), '--app', 'codex-app', '--format', 'text'], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('codex-app', result.stdout)


    def report(self, manager):
        return build_report(manager.plan().report, operation='plan', manager=manager,
                            context={'machine_file': str(self.machine_path), 'app': manager.app})

    def test_third_party_target_metadata_and_no_provider_values(self):
        self.features()
        self.profile('claude-desktop', {'chatTabEnabled': True})
        manager = self.manager('claude-desktop')
        report = self.report(manager)
        target = report['targets'][0]
        self.assertEqual(target['application'], str(self.root / 'claude-desktop.app'))
        self.assertEqual(target['data_root'], str(self.root / 'claude-desktop'))
        self.assertEqual(target['deployment_mode'], '3p')
        self.assertNotIn('PRIVATE_GATEWAY', json.dumps(report))
        self.assertNotIn('PRIVATE_MODE_FILE', json.dumps(report))
        self.assertEqual(target['default_third_party_data_root'], str(Path.home() / 'Library/Application Support/Claude-3p'))
        self.assertIn('Claude Desktop', '\n'.join(overview_lines(report)))
        self.assertNotIn('对象：配置数据仓库', '\n'.join(overview_lines(report)))

    def test_official_mode_is_not_inferred_as_third_party(self):
        self.native('claude-desktop', {'userThemeMode': 'system'})
        self.profile('claude-desktop', {'userThemeMode': 'system'})
        report = self.report(self.manager('claude-desktop'))
        self.assertTrue(report['ready'])
        self.assertIsNone(report['targets'][0]['deployment_mode'])

    def test_missing_third_party_library_blocks_feature_profile(self):
        self.native('claude-desktop', {})
        (self.root / 'claude-desktop/claude_desktop_config.json').write_text('{"deploymentMode":"3p"}')
        self.profile('claude-desktop', {'chatTabEnabled': True})
        report = self.report(self.manager('claude-desktop'))
        self.assertFalse(report['ready'])

    def test_codex_binding_does_not_get_a_claude_deployment_mode(self):
        self.native(value={'desktop': {'followUpQueueMode': 'queue'}})
        self.profile(settings={'followUpQueueMode': 'queue'})
        report = self.report(self.manager())
        self.assertEqual(report['targets'][0]['app'], 'codex-app')
        self.assertNotIn('deployment_mode', report['targets'][0])

    def test_mode_file_change_invalidates_plan_and_config_json_cannot_select_mode(self):
        self.features()
        self.profile('claude-desktop', {'chatTabEnabled': True})
        mode = self.root / 'claude-desktop/claude_desktop_config.json'
        plan = self.manager('claude-desktop').plan()
        mode.write_text('{"deploymentMode":"1p"}')
        with self.assertRaises(PlanStale):
            plan.apply(self.state)
        self.native('claude-desktop', {'deploymentMode': '3p'})
        self.assertFalse(self.manager('claude-desktop').plan().report['ready'])


class AppNativeChecks(unittest.TestCase):
    def test_asar_bounds_and_exact_entry_selection(self):
        payload = b'preferred-schema'
        tree = {'files': {'x.js': {'offset': '0', 'size': len(payload)}}}
        encoded = json.dumps(tree).encode()
        data = struct.pack('<IIII', 4, len(encoded) + 8, len(encoded) + 4, len(encoded)) + encoded + payload
        self.assertEqual(asar_entries(data, ['x.js']), {'x.js': payload})
        for broken in (b'', data[:20], data[:-1]):
            with self.assertRaises(SchemaError):
                asar_entries(broken, ['x.js'])

    def test_process_check_does_not_launch_kill_or_print_process_arguments(self):
        probe = AppProbe()
        result = subprocess.CompletedProcess([], 0, '/Applications/Test.app/Contents/MacOS/Main --private\n')
        with patch('claude_config.core.app_native.subprocess.run', return_value=result) as call:
            with self.assertRaises(PlanStale) as error:
                probe.assert_stopped('/Applications/Test.app')
            self.assertNotIn('private', str(error.exception))
            self.assertEqual(call.call_args.args[0], ['/bin/ps', '-axo', 'command='])


if __name__ == '__main__':
    unittest.main()
