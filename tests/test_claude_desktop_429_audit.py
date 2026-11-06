"""Synthetic archive checks for the audited 4.29 catalog relocation; no App runs."""
import hashlib
import json
from pathlib import Path
import plistlib
import struct
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from claude_config.core.app_native import AUDITS, PREVIOUS_AUDITS, AppProbe, asar_entries

MODULE = '.vite/build/index.chunk-C7NkbHFq.js'
FIXTURE_SOURCE = b'synthetic audited preference interface'
BUNDLE = 'com.anthropic.claudefordesktop'


def archive(files, unpacked=()):
    tree, payload = {'files': {}}, bytearray()
    for name, content in files.items():
        node = tree
        parts = name.split('/')
        for part in parts[:-1]:
            node = node['files'].setdefault(part, {'files': {}})
        node['files'][parts[-1]] = ({'size': len(content), 'unpacked': True} if name in unpacked
                                    else {'size': len(content), 'offset': str(len(payload))})
        if name not in unpacked:
            payload.extend(content)
    header = json.dumps(tree).encode()
    return struct.pack('<IIII', 4, 8 + len(header), 4 + len(header), len(header)) + header + payload


class ClaudeDesktop429AuditTests(unittest.TestCase):
    def check(self, *, catalog=None, loose=None, locale='fr-FR', payload=FIXTURE_SOURCE,
              build='4.29.0', reviewed='4.29.0', bundle=BUNDLE, unpacked=()):
        with tempfile.TemporaryDirectory() as directory:
            app = Path(directory) / 'Synthetic.app'
            resources = app / 'Contents/Resources'
            resources.mkdir(parents=True)
            (app / 'Contents/Info.plist').write_bytes(plistlib.dumps({
                'CFBundleIdentifier': bundle, 'CFBundleShortVersionString': build}))
            files = {MODULE: payload, **(catalog or {})}
            data = archive(files, unpacked)
            (resources / 'app.asar').write_bytes(data)
            for name, content in (loose or {}).items():
                (resources / name).write_bytes(content)
            audits = {'claude-desktop': (reviewed, BUNDLE, {MODULE: hashlib.sha256(FIXTURE_SOURCE).hexdigest()})}
            sources = {}
            with patch('claude_config.core.app_native.AUDITS', audits), \
                 patch('claude_config.core.app_native.PREVIOUS_AUDITS', {}):
                checks = AppProbe().check('claude-desktop', app, {'locale': locale}, sources)
            return checks, {path.relative_to(app).as_posix(): value for path, value in sources.items()}

    def test_exact_new_registration_keeps_older_interfaces(self):
        earlier = {version: hashes for version, bundle, hashes in PREVIOUS_AUDITS['claude-desktop']}
        self.assertEqual(earlier['4.29.0'], {
            MODULE: '92e8f69fb04f151845edc56f4246a0d3a2d2f91b311cfdb26e4f64edc43819e0'})
        self.assertTrue({'3.6556.2', '2.42304.2', '2.42304.0', '2.26454.2'} <= set(earlier))
        self.assertEqual(earlier['3.6556.2']['.vite/build/index.chunk-jPKPzV-P.js'],
                         '6e898798943b990e6eefa9209c61d977866b7898f681864f02c8cad3bb691ed5')

    def test_catalog_is_read_from_packed_archive_and_archive_is_a_plan_input(self):
        checks, sources = self.check(catalog={'resources/i18n/fr-FR.json': b'{"message":"bonjour"}'})
        self.assertEqual(checks[0]['status'], 'passed')
        self.assertEqual(checks[0]['reviewed_version'], '4.29.0')
        self.assertIn('Contents/Resources/app.asar', sources)
        self.assertNotIn('Contents/Resources/fr-FR.json', sources)

    def test_old_loose_location_cannot_make_a_missing_new_catalog_pass(self):
        checks, _ = self.check(loose={'fr-FR.json': b'{}'})
        self.assertEqual(checks[0]['status'], 'blocked')
        self.assertEqual(checks[0]['stage'], 'locale_catalog')

    def test_chinese_locale_is_not_silently_remapped(self):
        catalogs = {'resources/i18n/zh-Hans.json': b'{}', 'resources/i18n/zh-Hant.json': b'{}'}
        self.assertEqual(self.check(catalog=catalogs, locale='zh-CN')[0][0]['status'], 'blocked')
        self.assertEqual(self.check(catalog=catalogs, locale='zh-Hans')[0][0]['status'], 'passed')
        self.assertEqual(self.check(catalog=catalogs, locale='zh-Hant')[0][0]['status'], 'passed')

    def test_malformed_or_unpacked_catalog_is_not_supported_by_this_audit(self):
        for payload in (b'not-json', b'[]', b'', b'null'):
            with self.subTest(payload=payload):
                self.assertEqual(self.check(catalog={'resources/i18n/fr-FR.json': payload})[0][0]['status'], 'blocked')
        checks, _ = self.check(catalog={'resources/i18n/fr-FR.json': b'{}'},
                               unpacked={'resources/i18n/fr-FR.json'})
        self.assertEqual(checks[0]['status'], 'blocked')

    def test_same_build_label_or_loose_catalog_cannot_bypass_unknown_source(self):
        checks, _ = self.check(payload=b'changed interface', catalog={'resources/i18n/fr-FR.json': b'{}'},
                               loose={'fr-FR.json': b'{}'})
        self.assertEqual(checks[0]['status'], 'blocked')
        self.assertEqual(checks[0]['stage'], 'preference_interface')
        self.assertEqual(self.check(catalog={'resources/i18n/fr-FR.json': b'{}'}, bundle='other.app')[0][0]['stage'],
                         'application_identity')

    def test_unpacked_or_link_metadata_cannot_be_disguised_by_a_packed_offset(self):
        for metadata in ({'unpacked': True}, {'link': 'elsewhere.json'}):
            entry = {'offset': '0', 'size': 2, **metadata}
            header = json.dumps({'files': {'catalog.json': entry}}).encode()
            data = struct.pack('<IIII', 4, 8 + len(header), 4 + len(header), len(header)) + header + b'{}'
            with self.subTest(metadata=metadata), self.assertRaises(ValueError):
                asar_entries(data, ['catalog.json'])

    def test_catalog_layout_follows_matched_interface_not_build_label(self):
        checks, _ = self.check(build='4.99.0', catalog={'resources/i18n/fr-FR.json': b'{}'})
        self.assertEqual(checks[0]['status'], 'passed')
        self.assertEqual(checks[0]['reviewed_version'], '4.29.0')
        checks, sources = self.check(reviewed='3.6556.2', build='4.29.0', loose={'fr-FR.json': b'{}'})
        self.assertEqual(checks[0]['status'], 'passed')
        self.assertIn('Contents/Resources/fr-FR.json', sources)


if __name__ == '__main__':
    unittest.main()
