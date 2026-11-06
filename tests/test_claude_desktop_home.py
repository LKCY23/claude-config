"""desktopHome declaration, source gating and local-storage semantics."""
import hashlib
import json
from pathlib import Path
import plistlib
import tempfile
import unittest
from unittest.mock import patch

from test_claude_desktop_429_audit import archive
import test_profile_apps as fixtures
from claude_config.core.app_fields import validate_settings
from claude_config.core.app_native import AppProbe, AUDITS, PREVIOUS_AUDITS
from claude_config.core.reporting import build_report
from claude_config.core.schema import SchemaError


class NewProbe(fixtures.FakeAppProbe):
    def check(self, *args):
        return [{'name': 'app_preference_interface', 'status': 'passed', 'reviewed_version': '4.16336.0'}]


class DesktopHomeTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.AppTests(); self.fixture.setUp(); self.fixture.probe = NewProbe()
    def tearDown(self):
        self.fixture.tearDown()

    def test_only_explicit_public_values_are_accepted(self):
        for value in ('standard', 'off'):
            validate_settings('claude-desktop', {'desktopHome': value})
        for value in (True, False, None, '', 'simple', 'no-vm', 'STANDARD'):
            with self.subTest(value=value), self.assertRaises(SchemaError):
                validate_settings('claude-desktop', {'desktopHome': value})

    def test_home_writes_only_applied_uuid_preserving_unmanaged_and_sealed_values(self):
        f = self.fixture
        path = f.features(); data = json.loads(path.read_text())
        data['inferenceGatewayApiKey'] = 'sealed:v1:DO_NOT_OPEN'; path.write_text(json.dumps(data))
        for value in ('standard', 'off'):
            with self.subTest(value=value):
                f.profile('claude-desktop', {'desktopHome': value})
                before_main = f.native('claude-desktop').read_bytes()
                plan = f.manager('claude-desktop').plan()
                self.assertTrue(plan.report['ready'], plan.report['blockers'])
                self.assertNotIn('DO_NOT_OPEN', json.dumps(plan.report))
                plan.apply(f.state)
                self.assertEqual(json.loads(path.read_text()), {**data, 'desktopHome': value})
                self.assertEqual(f.native('claude-desktop').read_bytes(), before_main)
                f.manager('claude-desktop').export(['/settings/desktopHome']).apply(f.state)

    def test_undeclared_home_is_never_added_and_legacy_is_not_rewritten(self):
        f = self.fixture; path = f.features()
        for value in (None, 'simple', 'no-vm'):
            data = json.loads(path.read_text()); data.pop('desktopHome', None)
            if value is not None: data['desktopHome'] = value
            path.write_text(json.dumps(data)); f.profile('claude-desktop', {'chatTabEnabled': False})
            plan = f.manager('claude-desktop').plan(); self.assertTrue(plan.report['ready'])
            plan.apply(f.state)
            self.assertEqual(json.loads(path.read_text()), data)

    def test_report_distinguishes_storage_from_surface_override_without_leaking(self):
        f = self.fixture; path = f.features()
        for home in ('standard', 'off', 'simple', 'no-vm', {'secret': 'DO_NOT_REPORT'}):
            data = json.loads(path.read_text()); data['desktopHome'] = home; path.write_text(json.dumps(data))
            f.profile('claude-desktop', {'chatTabEnabled': False})
            manager = f.manager('claude-desktop'); plan = manager.plan()
            report = build_report(plan.report, operation='plan', manager=manager)
            row = next(row for row in report['checks'] if row['name'] == 'claude_desktop_home_semantics')
            self.assertEqual(row['state'], 'unrecognized' if isinstance(home, dict) else 'overridden')
            self.assertEqual(row['gui_effect'], 'not_verified')
            self.assertNotIn('DO_NOT_REPORT', json.dumps(report))
            self.assertTrue(report['ready'])

    def test_absent_home_reports_startup_default_without_writing_it(self):
        f = self.fixture; f.features(); f.profile('claude-desktop', {'chatTabEnabled': True})
        plan = f.manager('claude-desktop').plan()
        row = next(row for row in plan.report['checks'] if row['name'] == 'claude_desktop_home_semantics')
        self.assertEqual(row['state'], 'default_not_verified'); self.assertEqual(row['default_at'], '2026-11-10T20:00:00Z')
        self.assertFalse(any(row['path'] == ['settings', 'desktopHome'] for row in plan.report['decisions']))

    def test_home_still_requires_existing_third_party_and_local_applied_store(self):
        f = self.fixture; f.features(); f.profile('claude-desktop', {'desktopHome': 'off'})
        mode = f.root / 'claude-desktop/claude_desktop_config.json'; mode.write_text('{"deploymentMode":"1p"}')
        self.assertFalse(f.manager('claude-desktop').plan().report['ready'])
        mode.write_text('{"deploymentMode":"3p"}')
        meta = f.root / 'claude-desktop/configLibrary/_meta.json'; data = json.loads(meta.read_text())
        data['hybridPointer'] = {'bootstrapUrl': 'https://example.invalid'}; meta.write_text(json.dumps(data))
        self.assertFalse(f.manager('claude-desktop').plan().report['ready'])

    def test_new_exact_audits_preserve_all_old_signatures(self):
        rows = (AUDITS['claude-desktop'], *PREVIOUS_AUDITS['claude-desktop'])
        self.assertEqual(rows[0][0], '4.16336.0')
        self.assertEqual({row[0] for row in rows}, {'4.16336.0', '4.23118.0', '4.4212.0', '4.29.0', '3.6556.2', '2.42304.2', '2.42304.0', '2.26454.2'})

    def test_home_compatibility_tracks_exact_source_not_build_number(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = Path(tmp); resources = app / 'Contents/Resources'; resources.mkdir(parents=True)
            info = {'CFBundleIdentifier': 'com.anthropic.claudefordesktop', 'CFBundleShortVersionString': '99.0.0'}
            (app / 'Contents/Info.plist').write_bytes(plistlib.dumps(info))
            (resources / 'app.asar').write_bytes(archive({'source.js': b'audited', 'resources/i18n/en-US.json': b'{}'}))
            for reviewed in ('4.29.0', '4.4212.0', '4.16336.0'):
                audit = (reviewed, info['CFBundleIdentifier'], {'source.js': hashlib.sha256(b'audited').hexdigest()})
                with patch('claude_config.core.app_native.AUDITS', {'claude-desktop': audit}), patch('claude_config.core.app_native.PREVIOUS_AUDITS', {}):
                    row = AppProbe().check('claude-desktop', app, {'desktopHome': 'standard', 'locale': 'en-US'}, {})[0]
                self.assertEqual(row['status'], 'blocked' if reviewed == '4.29.0' else 'passed')
                if reviewed == '4.29.0': self.assertEqual(row['stage'], 'preference_field')
