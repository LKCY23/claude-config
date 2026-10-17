import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


spec = importlib.util.spec_from_file_location('release_assets_tested', Path(__file__).resolve().parents[1] / 'scripts/release_assets.py')
release_assets = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release_assets)


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.asset = self.root / 'test.baseline.json'
        self.asset.write_text('{"fixture":true}\n')
        self.repo, self.commit = 'owner/catalog', 'a' * 40
        self.tag = 'baseline-' + self.commit
        self.release, self.tagged, self.contents, self.writes = None, None, {}, []
        self.api_patch = patch.object(release_assets, 'api', side_effect=self.api)
        self.gh_patch = patch.object(release_assets, 'gh', side_effect=self.gh)
        self.api_patch.start()
        self.gh_patch.start()

    def tearDown(self):
        self.api_patch.stop()
        self.gh_patch.stop()
        self.tmp.cleanup()

    def api(self, path, *, optional=False, binary=False):
        if '/git/ref/tags/' in path:
            return {'object': {'type': 'commit', 'sha': self.tagged}} if self.tagged else None
        if '/releases/assets/' in path:
            return self.contents[int(path.rsplit('/', 1)[1])]
        if '/releases/tags/' in path and self.release and self.release['draft']:
            self.fail('Pending-tag drafts cannot be queried through the published tag endpoint')
        return json.loads(json.dumps(self.release)) if self.release else None

    def gh(self, args, **kwargs):
        if args[:2] == ['release', 'view']:
            self.assertEqual(args[-2:], ['--json', 'databaseId'])
            return b'{"databaseId": 123}' if self.release else None
        self.writes.append(args)
        if args[:2] == ['release', 'create']:
            self.assertIn('--draft', args)
            self.release = {'tag_name': self.tag, 'target_commitish': self.commit, 'draft': True, 'assets': []}
        elif args[:2] == ['release', 'upload']:
            self.assertTrue(self.release['draft'])
            for file in args[5:]:
                path = Path(file)
                identifier = len(self.contents) + 1
                self.contents[identifier] = path.read_bytes()
                self.release['assets'].append({'id': identifier, 'name': path.name})
        elif args[:2] == ['release', 'edit']:
            self.assertEqual(len(self.release['assets']), 1)
            self.release['draft'] = False
            self.tagged = self.commit
        else:
            self.fail(str(args))
        return b''

    def publish(self):
        return release_assets.publish(self.repo, self.tag, self.commit, [self.asset], title='test', notes='test')

    def test_publish_only_after_complete_upload_and_idempotent_repeat(self):
        self.assertEqual(self.publish()['status'], 'published')
        self.writes.clear()
        self.assertEqual(self.publish()['status'], 'unchanged')
        self.assertEqual(self.writes, [])

    def test_resume_empty_draft_without_recreating_or_overwriting(self):
        self.release = {'tag_name': self.tag, 'target_commitish': self.commit, 'draft': True, 'assets': []}
        self.assertEqual(self.publish()['status'], 'published')
        self.assertFalse(any(args[:2] == ['release', 'create'] for args in self.writes))

    def test_published_asset_change_or_missing_asset_cannot_be_repaired_silently(self):
        self.publish()
        self.writes.clear()
        self.asset.write_text('changed')
        with self.assertRaises(ValueError):
            self.publish()
        self.release['assets'] = []
        with self.assertRaises(ValueError):
            self.publish()
        self.assertEqual(self.writes, [])

    def test_wrong_tag_commit_and_wrong_draft_target_are_rejected(self):
        self.tagged = 'b' * 40
        with self.assertRaises(ValueError):
            self.publish()
        self.tagged = None
        self.release = {'tag_name': self.tag, 'target_commitish': 'b' * 40, 'draft': True, 'assets': []}
        with self.assertRaises(ValueError):
            self.publish()
        self.assertEqual(self.writes, [])

    def test_network_error_is_not_treated_as_absent_release(self):
        self.gh_patch.stop()
        fake = type('Response', (), {'returncode': 1, 'stdout': b'', 'stderr': b'network timeout'})()
        with patch.object(release_assets.subprocess, 'run', return_value=fake):
            with self.assertRaises(ValueError):
                release_assets.gh(['api', 'repos/owner/catalog/releases/tags/test'], optional=True)
        self.gh_patch.start()


if __name__ == '__main__':
    unittest.main()
