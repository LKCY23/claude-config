"""Compatibility follows reviewed interfaces, independently of build labels."""
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
from claude_config.core.app_native import AppProbe


class AppAuditTests(unittest.TestCase):
    def check(self, version, payload=b'verified', bundle='com.openai.codex'):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);resources=root/'Contents/Resources';resources.mkdir(parents=True)
            (root/'Contents/Info.plist').write_bytes(plistlib.dumps({'CFBundleShortVersionString':version,'CFBundleIdentifier':bundle}))
            header=json.dumps({'files':{'schema.js':{'offset':'0','size':len(payload)}}}).encode()
            (resources/'app.asar').write_bytes(struct.pack('<IIII',4,8+len(header),4+len(header),len(header))+header+payload)
            hashes={'schema.js':hashlib.sha256(b'verified').hexdigest()}
            with patch('claude_config.core.app_native.AUDITS',{'codex-app':('2','com.openai.codex',hashes)}), patch('claude_config.core.app_native.PREVIOUS_AUDITS',{'codex-app':(('1','com.openai.codex',hashes),)}):
                return AppProbe().check('codex-app',root,{'reviewDelivery':'inline'},{})

    def test_current_and_earlier_reviewed_releases_are_supported(self):
        for version in ('1','2'):
            result=self.check(version)
            self.assertEqual(result[0]['status'],'passed')
            self.assertEqual(result[0]['version'],version)

    def test_new_build_with_identical_interface_is_supported(self):
        result=self.check('3')
        self.assertEqual(result[0]['status'],'passed')
        self.assertEqual(result[0]['version'],'3')
        self.assertEqual(result[0]['reviewed_version'],'2')

    def test_other_bundle_and_unknown_interface_are_not_accepted(self):
        identity = self.check('2',bundle='other.app')[0]
        interface = self.check('3',payload=b'new unreviewed interface')[0]
        self.assertEqual(identity['status'],'blocked')
        self.assertEqual(identity['stage'],'application_identity')
        self.assertEqual(interface['status'],'blocked')
        self.assertEqual(interface['stage'],'preference_interface')
        self.assertEqual(interface['version'],'3')

    def test_known_version_does_not_bypass_source_tampering(self):
        self.assertEqual(self.check('1',payload=b'tampered')[0]['status'],'blocked')

    def test_previous_interface_is_supported_when_current_entry_is_missing(self):
        with patch('claude_config.core.app_native.asar_entries', side_effect=[ValueError('current entry absent'), {'schema.js': b'verified'}]):
            result=self.check('3')
        self.assertEqual(result[0]['status'],'passed')
        self.assertEqual(result[0]['reviewed_version'],'1')


if __name__=='__main__':unittest.main()
