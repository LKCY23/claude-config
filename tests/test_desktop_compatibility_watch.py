"""Release identity, failure classification and rolling issue watch boundaries."""
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
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
sys.path.insert(0, str(ROOT / 'src'))
import desktop_compatibility as watch
import report_desktop_compatibility_issue as publisher


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

    def test_unknown_asar_format_still_produces_review_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            app = Path(directory); (app / 'Contents/Resources').mkdir(parents=True)
            (app / 'Contents/Info.plist').write_bytes(plistlib.dumps({
                'CFBundleIdentifier': watch.BUNDLE, 'CFBundleShortVersionString': '3.6556.2'}))
            (app / 'Contents/Resources/app.asar').write_bytes(b'new-format')
            result = subprocess.CompletedProcess([], 0, '', 'TeamIdentifier=' + watch.TEAM + '\n')
            with patch.object(watch.subprocess, 'run', return_value=result):
                report = watch.run(release(), app, app=app)
            self.assertEqual(report['status'], 'needs-review')
            self.assertEqual(report['candidate_modules'], [])

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
                [{'status': 'passed'}], [])), patch.object(watch.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, 'ok', '')) as run:
            self.assertEqual(watch.run(release(), Path(directory), app=Path(directory))['status'], 'compatible')
            command = run.call_args.args[0]
            self.assertEqual(command[command.index('-k') + 1], 'claude')


class PublisherTests(unittest.TestCase):
    def sample_report(self, version='3.6556.2', status='needs-review', module='.vite/build/index.chunk-abc123.js'):
        rel = dict(release(), version=version,
                   url=f'https://downloads.claude.ai/releases/darwin/universal/{version}/Claude-fixed.zip')
        return {
            'schema_version': 1,
            'release': rel,
            'status': status,
            'checks': [{'name': 'app_preference_interface', 'status': 'blocked' if status == 'needs-review' else 'passed'}],
            'candidate_modules': [{'path': module, 'sha256': 'b' * 64, 'fields': ['userThemeMode', 'locale']}] if module else [],
            'native_smoke': 'passed' if status == 'compatible' else 'not-run',
            'scope': 'Source interface and temporary preference files',
        }

    def test_failed_report_calls_no_api(self):
        with patch.object(publisher, 'api') as api:
            self.assertEqual(publisher.publish('owner/repo', {'release': release(), 'status': 'failed'})['status'],
                             'no-issue-needed')
            api.assert_not_called()

    def test_compatible_report_with_no_open_tracking_issue_is_noop(self):
        with patch.object(publisher, 'api', return_value=[]) as api:
            result = publisher.publish('owner/repo', self.sample_report(status='compatible'))
            self.assertEqual(result['status'], 'compatible-no-issue')
            self.assertEqual(api.call_count, 1)

    def test_compatible_report_closes_existing_bot_tracking_issue(self):
        issue = {'number': 42, 'html_url': 'https://github.com/owner/repo/issues/42',
                 'user': {'login': 'github-actions[bot]'},
                 'body': publisher.MARKER + '\nold tracking issue'}
        with patch.object(publisher, 'api', side_effect=[[issue], {}, {}]) as api:
            result = publisher.publish('owner/repo', self.sample_report(version='6.28899.6', status='compatible'))
            self.assertEqual(result, {'status': 'closed', 'number': 42, 'url': 'https://github.com/owner/repo/issues/42'})
            self.assertEqual(api.call_args_list[1].args[:2], ('POST', 'repos/owner/repo/issues/42/comments'))
            self.assertIn('6.28899.6', api.call_args_list[1].args[2]['body'])
            self.assertEqual(api.call_args_list[2].args,
                             ('PATCH', 'repos/owner/repo/issues/42', {'state': 'closed', 'state_reason': 'completed'}))

    def test_needs_review_ignores_prs_and_non_bot_issues_and_creates_single_labeled_issue(self):
        noise = [
            {'number': 1, 'pull_request': {}, 'user': {'login': 'github-actions[bot]'}, 'body': publisher.MARKER},
            {'number': 2, 'user': {'login': 'human'}, 'body': publisher.MARKER},
        ]
        created = {'number': 7, 'html_url': 'https://github.com/owner/repo/issues/7'}
        report = self.sample_report(version='6.28899.6', status='needs-review')
        with patch.object(publisher, 'api', side_effect=[noise, {}, created]) as api:
            result = publisher.publish('owner/repo', report)
            self.assertEqual(result, {'status': 'created', 'number': 7, 'url': 'https://github.com/owner/repo/issues/7'})
            self.assertEqual(api.call_args_list[1].args[:2], ('POST', 'repos/owner/repo/labels'))
            method, path, payload = api.call_args_list[2].args
            self.assertEqual((method, path), ('POST', 'repos/owner/repo/issues'))
            self.assertEqual(payload['title'], 'Claude Desktop compatibility watch: 6.28899.6 needs review')
            self.assertEqual(payload['labels'], [publisher.LABEL])
            self.assertIn(publisher.MARKER, payload['body'])
            self.assertIn(report['release']['url'], payload['body'])
            self.assertIn(report['release']['sha256'], payload['body'])
            self.assertIn('.vite/build/index.chunk-abc123.js', payload['body'])

    def test_repeated_needs_review_for_same_version_is_idempotent(self):
        report = self.sample_report(version='6.28899.6', status='needs-review')
        body = publisher.render_body(report)
        existing = {
            'number': 7,
            'html_url': 'https://github.com/owner/repo/issues/7',
            'title': 'Claude Desktop compatibility watch: 6.28899.6 needs review',
            'user': {'login': 'github-actions[bot]'},
            'body': body,
        }
        with patch.object(publisher, 'api', return_value=[existing]) as api:
            result = publisher.publish('owner/repo', report)
            self.assertEqual(result, {'status': 'unchanged', 'number': 7, 'url': 'https://github.com/owner/repo/issues/7'})
            self.assertEqual(api.call_count, 1)

    def test_newer_needs_review_updates_existing_issue_and_retains_history(self):
        old_report = self.sample_report(version='6.28899.2', status='needs-review', module='.vite/build/old.js')
        old_body = publisher.render_body(old_report)
        existing = {
            'number': 7,
            'html_url': 'https://github.com/owner/repo/issues/7',
            'title': 'Claude Desktop compatibility watch: 6.28899.2 needs review',
            'user': {'login': 'github-actions[bot]'},
            'body': old_body,
        }
        new_report = self.sample_report(version='6.28899.6', status='needs-review', module='.vite/build/new.js')
        updated = {'number': 7, 'html_url': 'https://github.com/owner/repo/issues/7'}
        with patch.object(publisher, 'api', side_effect=[[existing], updated]) as api:
            result = publisher.publish('owner/repo', new_report)
            self.assertEqual(result, {'status': 'updated', 'number': 7, 'url': 'https://github.com/owner/repo/issues/7'})
            method, path, payload = api.call_args_list[1].args
            self.assertEqual((method, path), ('PATCH', 'repos/owner/repo/issues/7'))
            self.assertEqual(payload['title'], 'Claude Desktop compatibility watch: 6.28899.6 needs review')
            self.assertIn('| `6.28899.6` | `needs-review` |', payload['body'])
            self.assertIn('| `6.28899.2` | `needs-review` |', payload['body'])

    def test_watch_workflow_schedule_permissions_and_publisher(self):
        workflow = yaml.load((ROOT / '.github/workflows/desktop-watch.yml').read_text(), Loader=yaml.BaseLoader)
        self.assertEqual(workflow['on']['schedule'], [{'cron': '23 3 * * 1'}])
        self.assertIn('workflow_dispatch', workflow['on'])
        self.assertIn('report-issue', workflow['jobs'])
        self.assertNotIn('report-pr', workflow['jobs'])
        job = workflow['jobs']['report-issue']
        self.assertEqual(job['permissions'], {'contents': 'read', 'issues': 'write'})
        steps_text = json.dumps(job['steps'])
        self.assertIn('scripts/report_desktop_compatibility_issue.py', steps_text)


if __name__ == '__main__':
    unittest.main()
