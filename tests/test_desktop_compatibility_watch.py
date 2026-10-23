"""Release identity, failure classification and PR deduplication boundaries."""
import importlib.util
import io
import json
from pathlib import Path
import plistlib
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
sys.path.insert(0, str(ROOT / 'src'))
import desktop_compatibility as watch
import open_desktop_compatibility_pr as publisher


def release():
    return {'version': '3.6556.2', 'url': 'https://downloads.claude.ai/releases/darwin/universal/3.6556.2/Claude-fixed.zip',
            'sha256': 'a' * 64, 'size': 100}


class ReleaseTests(unittest.TestCase):
    def test_selects_current_release_not_first_or_release_notes(self):
        current = release()
        feed = {'currentRelease': current['version'], 'releases': [
            {'version': '1.0.0', 'updateTo': dict(current, version='1.0.0')},
            {'version': current['version'], 'updateTo': dict(current, notes='untrusted')} ]}
        self.assertEqual(watch.select_release(feed), current)

    def test_rejects_external_urls_missing_checksums_and_ambiguous_feed(self):
        for changes in [{'url': 'https://evil.example/Claude.zip'}, {'sha256': ''}, {'size': -1},
                        {'version': '../3.0.0'}, {'url': release()['url'] + '?token=private'},
                        {'url': release()['url'].replace('3.6556.2/', '2.0.0/')}]:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                watch.validate_release(dict(release(), **changes))
        with self.assertRaises(ValueError):
            watch.select_release({'currentRelease': '3.6556.2', 'releases': []})

    def test_zip_rejects_parent_paths_and_escaping_symlinks(self):
        for name, target, symlink in [('../outside', '', False), ('link', '../outside', True)]:
            with self.subTest(name=name), io.BytesIO() as data:
                with zipfile.ZipFile(data, 'w') as z:
                    item = zipfile.ZipInfo(name)
                    if symlink:
                        item.external_attr = 0o120777 << 16
                    z.writestr(item, target)
                data.seek(0)
                with zipfile.ZipFile(data) as z, self.assertRaises(ValueError):
                    watch.checked_members(z)

    def test_wrong_signer_is_rejected_before_reading_application_code(self):
        with tempfile.TemporaryDirectory() as directory:
            app = Path(directory); (app / 'Contents').mkdir()
            (app / 'Contents/Info.plist').write_bytes(plistlib.dumps({
                'CFBundleIdentifier': watch.BUNDLE, 'CFBundleShortVersionString': '3.6556.2'}))
            result = subprocess.CompletedProcess([], 0, '', 'TeamIdentifier=OTHER\n')
            with patch.object(watch.subprocess, 'run', return_value=result), self.assertRaises(ValueError):
                watch.inspect(app, '3.6556.2')

    def test_unknown_interface_is_review_not_compatible_and_does_not_run_smoke(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(watch, 'inspect', return_value=(
                [{'status': 'blocked', 'code': 'capability_unknown'}], [])), patch.object(watch.subprocess, 'run') as run:
            result = watch.run(release(), Path(directory), app=Path(directory))
            self.assertEqual(result['status'], 'needs-review')
            self.assertEqual(result['native_smoke'], 'not-run')
            run.assert_not_called()

    def test_known_signature_does_not_mask_failed_behavior_test(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(watch, 'inspect', return_value=(
                [{'status': 'passed'}], [])), patch.object(watch.subprocess, 'run', return_value=subprocess.CompletedProcess([], 1, '', 'failure')):
            result = watch.run(release(), Path(directory), app=Path(directory))
            self.assertEqual(result['status'], 'failed')
            self.assertEqual(result['native_smoke'], 'failed')

    def test_success_requires_passing_smoke(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(watch, 'inspect', return_value=(
                [{'status': 'passed'}], [])), patch.object(watch.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, 'ok', '')):
            self.assertEqual(watch.run(release(), Path(directory), app=Path(directory))['status'], 'compatible')


class PublisherTests(unittest.TestCase):
    def test_existing_open_or_closed_pr_is_never_rewritten(self):
        for state in ['open', 'closed']:
            with patch.object(publisher, 'api', return_value=[{'state': state, 'html_url': 'https://github.com/example/pr/1'}]) as api:
                result = publisher.publish('owner/repo', {'release': release(), 'status': 'needs-review'})
                self.assertEqual(result['status'], 'existing-' + state)
                self.assertEqual(api.call_count, 1)

    def test_known_compatible_release_creates_no_pr(self):
        with patch.object(publisher, 'api') as api:
            self.assertEqual(publisher.publish('owner/repo', {'release': release(), 'status': 'compatible'})['status'], 'no-pr-needed')
            api.assert_not_called()

    def test_new_report_creates_only_draft_and_human_handoff(self):
        responses = [[], {'default_branch': 'master'}, {'object': {'sha': 'base'}}, {'tree': {'sha': 'base-tree'}},
                     {'sha': 'blob'}, {'sha': 'tree'}, {'sha': 'commit'}, {}, {'html_url': 'https://github.com/owner/repo/pull/1'}]
        with patch.object(publisher, 'api', side_effect=responses) as api:
            publisher.publish('owner/repo', {'release': release(), 'status': 'needs-review'})
            call = api.call_args_list[-1]
            payload = call.args[1]
            self.assertTrue(payload['draft'])
            self.assertIn('post this as a new PR comment', payload['body'])
            self.assertFalse(any('/comments' in c.args[0] or '/merge' in c.args[0] for c in api.call_args_list))


if __name__ == '__main__':
    unittest.main()
