"""Stage 1 architecture tests: L2 semantic AppProbe, no-op warning downgrade, sync_mode, and --agents-only."""
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import plistlib
import sys
import tempfile
import unittest
from unittest.mock import patch

import tomlkit

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from claude_config import cli as cli_entry
from claude_config.core.app_manager import AppProfileManager
from claude_config.core.app_native import AppProbe
from claude_config.core.schema import SchemaError
from test_claude_desktop_429_audit import BUNDLE as CLAUDE_BUNDLE, archive
import test_profile_apps as app_fixtures

CODEX_BUNDLE = 'com.openai.codex'


def _make_synthetic_app(root, *, bundle, version, files):
    app = Path(root) / 'Synthetic.app'
    resources = app / 'Contents/Resources'
    resources.mkdir(parents=True, exist_ok=True)
    (app / 'Contents/Info.plist').write_bytes(plistlib.dumps({
        'CFBundleIdentifier': bundle,
        'CFBundleShortVersionString': version,
    }))
    (resources / 'app.asar').write_bytes(archive(files))
    return app


class SemanticAppProbeTests(unittest.TestCase):
    def test_claude_desktop_semantic_probe_accepts_renamed_7x_modules_and_packed_locales(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = _make_synthetic_app(
                tmp,
                bundle=CLAUDE_BUNDLE,
                version='7.37095.0',
                files={
                    '.vite/build/index.chunk-zlLu4KiP.js': (
                        b'/* 7.x bundle */ configLibrary _meta.json config.json '
                        b'ko.set("userThemeMode", e) desktopHome autoModeEnabled i18n'
                    ),
                    '.vite/build/managedConfigSchemas.js': (
                        b'({flatKey:"chatTabEnabled",flatKey:"desktopHome",flatKey:"autoModeEnabled",flatKey:"builtinBrowserEnabled"})'
                    ),
                    'resources/i18n/zh-CN.json': b'{"hello":"world"}',
                },
            )
            sources = {}
            checks = AppProbe().check(
                'claude-desktop',
                app,
                {
                    'userThemeMode': 'system',
                    'locale': 'zh-CN',
                    'desktopHome': 'standard',
                    'chatTabEnabled': True,
                    'autoModeEnabled': False,
                    'builtinBrowserEnabled': True,
                },
                sources,
            )
            self.assertEqual(len(checks), 1)
            row = checks[0]
            self.assertEqual(row['status'], 'passed')
            self.assertEqual(row['version'], '7.37095.0')
            self.assertIsNone(row['reviewed_version'])
            self.assertEqual(row['level'], 'semantic-source-probe')
            self.assertTrue(row['desktop_home_supported'])
            self.assertIn('.vite/build/index.chunk-zlLu4KiP.js', row['matched_modules'])
            self.assertIn('.vite/build/managedConfigSchemas.js', row['matched_modules'])

    def test_claude_desktop_semantic_probe_still_fails_closed_on_missing_contracts(self):
        with tempfile.TemporaryDirectory() as tmp:
            base_files = {
                '.vite/build/main-AAA.js': b'configLibrary _meta.json config.json ko.set("userThemeMode", e) i18n',
                'resources/i18n/en-US.json': b'{}',
            }
            # 1) Missing core storage marker (configLibrary removed) -> preference_interface
            broken_storage = _make_synthetic_app(
                Path(tmp) / 's1', bundle=CLAUDE_BUNDLE, version='8.0.0',
                files={'.vite/build/main-AAA.js': b'config.json userThemeMode _meta.json'},
            )
            row1 = AppProbe().check('claude-desktop', broken_storage, {'userThemeMode': 'dark'}, {})[0]
            self.assertEqual((row1['status'], row1['stage']), ('blocked', 'preference_interface'))

            # 2) Missing declared preference field (builtinBrowserEnabled absent) -> preference_field
            missing_field = _make_synthetic_app(
                Path(tmp) / 's2', bundle=CLAUDE_BUNDLE, version='8.0.0', files=base_files,
            )
            row2 = AppProbe().check('claude-desktop', missing_field, {'builtinBrowserEnabled': True}, {})[0]
            self.assertEqual((row2['status'], row2['stage']), ('blocked', 'preference_field'))

            # 3) Missing desktopHome support -> preference_field
            row3 = AppProbe().check('claude-desktop', missing_field, {'desktopHome': 'standard'}, {})[0]
            self.assertEqual((row3['status'], row3['stage']), ('blocked', 'preference_field'))

            # 4) Missing locale JSON -> locale_catalog
            row4 = AppProbe().check('claude-desktop', missing_field, {'locale': 'fr-FR'}, {})[0]
            self.assertEqual((row4['status'], row4['stage']), ('blocked', 'locale_catalog'))

            # 5) Wrong bundle identity -> application_identity
            wrong_bundle = _make_synthetic_app(
                Path(tmp) / 's5', bundle='com.example.other', version='7.37095.0', files=base_files,
            )
            row5 = AppProbe().check('claude-desktop', wrong_bundle, {'userThemeMode': 'dark'}, {})[0]
            self.assertEqual((row5['status'], row5['stage']), ('blocked', 'application_identity'))

    def test_codex_app_semantic_probe_accepts_renamed_modules_and_blocks_missing_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            good = _make_synthetic_app(
                tmp, bundle=CODEX_BUNDLE, version='26.9999.1',
                files={'.vite/build/app-999.js': b'appearanceTheme composerEnterBehavior reviewDelivery desktop config.toml'},
            )
            ok = AppProbe().check('codex-app', good, {'appearanceTheme': 'dark', 'reviewDelivery': 'inline'}, {})[0]
            self.assertEqual(ok['status'], 'passed')
            self.assertEqual(ok['level'], 'semantic-source-probe')

            missing = AppProbe().check('codex-app', good, {'preventSleepWhileRunning': True}, {})[0]
            self.assertEqual((missing['status'], missing['stage']), ('blocked', 'preference_field'))

            no_markers = _make_synthetic_app(
                Path(tmp) / 'bad', bundle=CODEX_BUNDLE, version='26.9999.2',
                files={'.vite/build/app-999.js': b'totally unrelated module'},
            )
            bad = AppProbe().check('codex-app', no_markers, {'appearanceTheme': 'dark'}, {})[0]
            self.assertEqual((bad['status'], bad['stage']), ('blocked', 'preference_interface'))

    def test_app_manager_noop_downgrades_unreviewed_probe_to_warning_until_write_needed(self):
        fixture = app_fixtures.AppTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)

        # Initial apply with TestProbe succeeds and establishes matching state + native.
        fixture.profile('claude-desktop', {'userThemeMode': 'dark'})
        fixture.native('claude-desktop', {'userThemeMode': 'dark'})
        plan0 = fixture.manager('claude-desktop').plan()
        self.assertTrue(plan0.report['ready'], plan0.report['blockers'])
        plan0.apply(fixture.state)

        # Swap in a real AppProbe with an unreviewed app.asar while native/state already match Profile.
        unreviewed = _make_synthetic_app(
            fixture.root / 'unreviewed', bundle=CLAUDE_BUNDLE, version='9.0.0',
            files={'.vite/build/unknown.js': b'completely rewritten engine'},
        )
        fixture.machine['apps']['claude-desktop']['binding']['application'] = str(unreviewed)
        fixture.save_machine()

        noop_plan = AppProfileManager(fixture.machine_path, 'claude-desktop').plan()
        self.assertTrue(noop_plan.report['ready'], noop_plan.report['blockers'])
        self.assertEqual(noop_plan.changes, ())
        probe_row = next(c for c in noop_plan.report['checks'] if c['name'] == 'app_preference_interface')
        self.assertEqual(probe_row['status'], 'warning')

        # Once a write IS required, real AppProbe must block again (fail-closed).
        fixture.profile('claude-desktop', {'userThemeMode': 'light'})
        write_plan = AppProfileManager(fixture.machine_path, 'claude-desktop').plan()
        self.assertFalse(write_plan.report['ready'])
        self.assertTrue(any(b['code'] == 'capability_unknown' for b in write_plan.report['blockers']))

    def test_semantic_probe_enables_desktop_home_semantics_notice(self):
        fixture = app_fixtures.AppTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)

        feature_path = fixture.features()
        data = json.loads(feature_path.read_text())
        data['desktopHome'] = 'standard'
        feature_path.write_text(json.dumps(data))
        fixture.profile('claude-desktop', {'chatTabEnabled': False})

        seven_x = _make_synthetic_app(
            fixture.root / 'app7', bundle=CLAUDE_BUNDLE, version='7.37095.0',
            files={
                '.vite/build/index.chunk-zlLu4KiP.js': b'configLibrary _meta.json config.json userThemeMode desktopHome',
                '.vite/build/managedConfigSchemas.js': b'desktopHome chatTabEnabled',
            },
        )
        fixture.machine['apps']['claude-desktop']['binding']['application'] = str(seven_x)
        fixture.save_machine()

        plan = AppProfileManager(fixture.machine_path, 'claude-desktop').plan()
        self.assertTrue(plan.report['ready'], plan.report['blockers'])
        self.assertTrue(any(c['name'] == 'claude_desktop_home_semantics' for c in plan.report['checks']))


class AppSyncModeAndAgentsOnlyTests(unittest.TestCase):
    def _setup_fixture_with_app(self, *, sync_mode=None):
        fixture = app_fixtures.AppTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)

        # Configure codex agent with a pending setting update and claude-desktop with an unreviewed app (blocked if selected).
        bin_path = fixture.root / 'bin'
        bin_path.write_text('#!/bin/sh\nexit 0\n')
        bin_path.chmod(0o755)
        codex_profile = fixture.data / fixture.machine['agents']['codex']['profile']
        codex_profile.parent.mkdir(parents=True, exist_ok=True)
        codex_profile.write_text('schema_version: 1\nagent_type: codex\nsettings:\n  model: new-model\n')
        (fixture.codex / 'config.toml').write_text(tomlkit.dumps({'model': 'old-model'}))
        fixture.profile('claude-desktop', {'userThemeMode': 'dark'})
        fixture.native('claude-desktop', {'userThemeMode': 'light'})
        unreviewed = _make_synthetic_app(
            fixture.root / 'unreviewed', bundle=CLAUDE_BUNDLE, version='9.0.0',
            files={'.vite/build/unknown.js': b'rewritten'},
        )
        fixture.machine['apps'] = {
            'claude-desktop': {
                'profile': fixture.machine['apps']['claude-desktop']['profile'],
                'binding': {
                    'application': str(unreviewed),
                    'data_root': str(fixture.root / 'claude-desktop'),
                },
            }
        }
        if sync_mode is not None:
            fixture.machine['apps']['claude-desktop']['sync_mode'] = sync_mode
        fixture.save_machine()
        return fixture

    def test_schema_rejects_invalid_sync_mode(self):
        fixture = self._setup_fixture_with_app(sync_mode='invalid-mode')
        out = io.StringIO()
        with redirect_stdout(out):
            code = cli_entry.main(['status', '--machine', str(fixture.machine_path), '--format', 'json'])
        self.assertEqual(code, 2)
        self.assertIn('sync_mode', out.getvalue())

    def test_sync_mode_manual_and_agents_only_behavior(self):
        fixture = self._setup_fixture_with_app(sync_mode='manual')
        # 1) status surfaces sync_mode and tags [manual] in text
        out_st = io.StringIO()
        with patch('claude_config.core.manager.NativeProbe',
                   return_value=lambda *args: [{'name': 'runtime_version', 'status': 'passed'}]), \
                redirect_stdout(out_st):
            code = cli_entry.main(['status', '--machine', str(fixture.machine_path), '--format', 'text'])
        self.assertEqual(code, 0)
        self.assertIn('| manual]', out_st.getvalue())

        # 2) default sync skips manual apps cleanly (exits 0) and applies codex
        out_sync = io.StringIO()
        with patch('claude_config.core.manager.NativeProbe',
                   return_value=lambda *args: [{'name': 'runtime_version', 'status': 'passed'}]), \
                redirect_stdout(out_sync):
            code = cli_entry.main(['sync', '--machine', str(fixture.machine_path), '--yes', '--format', 'json'])
        self.assertEqual(code, 0, out_sync.getvalue())
        rep = json.loads(out_sync.getvalue())
        self.assertEqual(rep['skipped_manual_apps'], ['claude-desktop'])
        self.assertEqual([t['name'] for t in rep['targets']], ['codex'])
        self.assertEqual(tomlkit.parse((fixture.codex / 'config.toml').read_text())['model'], 'new-model')

        # 3) explicit --app overrides sync_mode == 'manual' (and here hits unreviewed app block -> exit 1)
        out_app = io.StringIO()
        with redirect_stdout(out_app):
            code_app = cli_entry.main([
                'sync', '--machine', str(fixture.machine_path), '--app', 'claude-desktop', '--dry-run', '--format', 'json',
            ])
        self.assertEqual(code_app, 1, out_app.getvalue())
        self.assertEqual(json.loads(out_app.getvalue())['targets'][0]['outcome'], 'skipped_blocked')

        # 4) --agents-only skips auto apps too, and forbids combining with --app
        fixture2 = self._setup_fixture_with_app(sync_mode='auto')
        out_ao = io.StringIO()
        with patch('claude_config.core.manager.NativeProbe',
                   return_value=lambda *args: [{'name': 'runtime_version', 'status': 'passed'}]), \
                redirect_stdout(out_ao):
            code_ao = cli_entry.main([
                'sync', '--machine', str(fixture2.machine_path), '--agents-only', '--yes', '--format', 'json',
            ])
        self.assertEqual(code_ao, 0, out_ao.getvalue())
        self.assertTrue(json.loads(out_ao.getvalue())['agents_only'])
        self.assertEqual([t['name'] for t in json.loads(out_ao.getvalue())['targets']], ['codex'])

        out_bad = io.StringIO()
        with redirect_stdout(out_bad):
            code_bad = cli_entry.main([
                'sync', '--machine', str(fixture2.machine_path), '--agents-only', '--app', 'claude-desktop', '--format', 'json',
            ])
        self.assertEqual(code_bad, 2)


if __name__ == '__main__':
    unittest.main()
