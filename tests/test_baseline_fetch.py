"""Explicit release retrieval never changes selection or native configuration."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from unittest.mock import patch

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from claude_config.fetch_baseline import command, fetch, prepare_git
from claude_config.core.baseline import adopt_baseline
from claude_config.core.recovery import rollback_plan
from claude_config.core.reporting import build_report, render_text, save_report
from test_profile_resources import ResourceFixture, run_git


class FetchTests(ResourceFixture):
    def prepare(self):
        self.add('guide', 'instruction', {'guide.md': 'first'}, 'guide.md')
        self.old = self.save()
        (self.repo / 'packages/guide/guide.md').write_text('second')
        run_git(self.repo, 'add', '.')
        run_git(self.repo, 'commit', '-qm', 'next baseline')
        self.commit = run_git(self.repo, 'rev-parse', 'HEAD')
        self.release = 'baseline-' + self.commit
        self.record = {'schema_version': 1, 'kind': 'claudespace-baseline', 'repo': 'owner/catalog',
                       'commit': self.commit, 'catalog_path': '.',
                       'catalog_sha256': hashlib.sha256((self.repo / 'catalog.json').read_bytes()).hexdigest(),
                       'validation': {'status': 'passed', 'level': 'structural', 'scope': 'catalog-schema'}}
        self.draft = False
        self.tag_commit = self.commit
        self.checkouts = []

    def api(self, path, *, binary=False):
        raw = (json.dumps(self.record) + '\n').encode()
        if '/releases/tags/' in path:
            return {'tag_name': self.release, 'draft': self.draft,
                    'assets': [{'id': 7, 'name': 'test.baseline.json', 'size': len(raw)}]}
        if '/git/ref/tags/' in path:
            return {'object': {'type': 'commit', 'sha': self.tag_commit}}
        if path.endswith('/releases/assets/7') and binary:
            return raw
        self.fail('Unexpected GitHub request: ' + path)

    def prepare_checkout(self, root, repo, commit):
        self.checkouts.append((repo, commit))
        if root.exists():
            return prepare_git(root, repo, commit)
        root.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(['git', 'clone', '-q', str(self.repo), str(root)], check=True, capture_output=True)
        run_git(root, 'remote', 'set-url', 'origin', 'https://github.com/' + repo + '.git')
        run_git(root, 'checkout', '--detach', commit)
        return True

    def fetched(self, **kwargs):
        with patch('claude_config.fetch_baseline.api', side_effect=self.api), \
                patch('claude_config.fetch_baseline.prepare_git', side_effect=self.prepare_checkout):
            return fetch(self.manager(), 'test', self.release, **kwargs)

    def test_fetch_reuses_exact_checkout_and_does_not_adopt(self):
        self.prepare()
        before = {p: p.read_bytes() for p in (self.data / 'catalogs.yaml', self.machine_path)}
        first = self.fetched()
        self.assertTrue(first['ready'])
        for path, content in before.items():
            self.assertEqual(path.read_bytes(), content)
        self.assertFalse(self.native.exists())
        second = self.fetched()
        self.assertTrue(all(row['status'] == 'unchanged' for row in second['results']))
        self.assertEqual(first['prepared'], second['prepared'])
        report = build_report(first, operation='fetch-baseline', preview_only=False, execution_started=True)
        save_report(report, self.state)
        self.assertIn('完整 JSON：' + report['artifacts']['json_path'], render_text(report))

    def test_unpublished_moved_tag_wrong_record_and_dirty_checkout_fail(self):
        self.prepare()
        self.draft = True
        with self.assertRaises(ValueError):
            self.fetched()
        self.draft = False
        self.tag_commit = self.old
        with self.assertRaises(ValueError):
            self.fetched()
        self.tag_commit = self.commit
        good = deepcopy(self.record)
        for changes in ({'commit': self.old}, {'repo': 'other/repo'}, {'catalog_sha256': '0' * 64}):
            self.record = {**good, **changes}
            with self.assertRaises(ValueError):
                self.fetched()
        self.record = good
        result = self.fetched()
        (Path(result['prepared']['checkout']) / 'local.txt').write_text('keep this local change')
        with self.assertRaises(ValueError):
            self.fetched()
        self.assertFalse(self.native.exists())

    def test_existing_record_is_never_overwritten(self):
        self.prepare()
        result = self.fetched()
        record = Path(result['prepared']['release_record'])
        record.write_text('local change')
        with self.assertRaises(ValueError):
            self.fetched()
        self.assertEqual(record.read_text(), 'local change')

    def test_source_map_fetch_adopt_deploy_and_restore(self):
        self.prepare()
        # The fixture's first source remains available for independent rollback.
        run_git(self.repo, 'checkout', '--detach', self.old)
        self.manager().plan().apply(self.state)
        before = (self.native / 'CLAUDE.md').read_bytes()
        run_git(self.repo, 'checkout', '--detach', self.commit)
        mapping = {'schema_version': 1, 'catalogs': {'test': {
            'from': {'repo': 'owner/catalog', 'commit': self.old, 'path': '.'},
            'to': {'repo': 'owner/published', 'commit': self.commit, 'path': '.'}}}}
        map_file = self.data / 'source-map.yaml'
        map_file.write_text(yaml.safe_dump(mapping))
        self.record['repo'] = 'owner/published'
        result = self.fetched(source_map=map_file)
        run_git(self.repo, 'checkout', '--detach', self.old)
        prepared = result['prepared']
        with self.assertRaises(ValueError):
            adopt_baseline(self.manager(), 'test', self.commit, Path(prepared['checkout']), Path(prepared['release_record']))
        adoption = adopt_baseline(self.manager(), 'test', self.commit, Path(prepared['checkout']), Path(prepared['release_record']), map_file)
        adoption.apply(self.state)
        adoption_run = Path(adoption.report['recovery']).parent.name
        self.assertEqual((self.native / 'CLAUDE.md').read_bytes(), before)
        deployment = self.manager().plan()
        deployment.apply(self.state)
        self.assertIn('second', (self.native / 'CLAUDE.md').read_text())
        rollback_plan(self.manager(), Path(deployment.report['recovery']).parent.name).apply(self.state)
        rollback_plan(self.manager(), adoption_run).apply(self.state)
        self.assertEqual((self.native / 'CLAUDE.md').read_bytes(), before)
        source = yaml.safe_load((self.data / 'catalogs.yaml').read_text())['catalogs']['test']
        self.assertEqual(source, mapping['catalogs']['test']['from'])

    def test_unbound_codex_profile_can_prepare_skill_sources_without_local_paths(self):
        self.add('guide', 'skill', {'SKILL.md': '---\nname: guide\ndescription: test\n---\nhello'}, 'SKILL.md')
        self.old = self.save()
        self.commit = self.old
        self.release = 'baseline-' + self.commit
        self.draft, self.tag_commit, self.checkouts = False, self.commit, []
        self.record = {'schema_version': 1, 'kind': 'claudespace-baseline', 'repo': 'owner/catalog',
                       'commit': self.commit, 'catalog_path': '.',
                       'catalog_sha256': hashlib.sha256((self.repo / 'catalog.json').read_bytes()).hexdigest(),
                       'validation': {'status': 'passed', 'level': 'structural', 'scope': 'catalog-schema'}}
        p = self.data / 'profiles/agents/codex/later.yaml'
        p.parent.mkdir(parents=True)
        p.write_text(yaml.safe_dump({'schema_version': 1, 'agent_type': 'codex',
                                    'resources': {'skills': {'guide@test': {'bindings': {'install_root': 'paths.future-device'}}}}}))
        result = self.fetched()
        self.assertEqual(len(result['impacts']), 2)
        prepared = result['prepared']
        plan = adopt_baseline(self.manager(), 'test', self.commit, Path(prepared['checkout']), Path(prepared['release_record']))
        self.assertTrue(plan.report['ready'])

    def test_git_staging_failure_and_symlink_destination_preserve_existing_files(self):
        root = self.state / 'sources/new'
        with patch('claude_config.fetch_baseline.command', side_effect=ValueError('network failed')):
            with self.assertRaises(ValueError):
                prepare_git(root, 'owner/catalog', 'a' * 40)
        self.assertFalse(root.exists())
        self.assertEqual(list(root.parent.glob('.fetch-*')), [])
        other = self.root / 'private'
        other.mkdir()
        (other / 'keep').write_text('keep')
        root.symlink_to(other, target_is_directory=True)
        with self.assertRaises(ValueError):
            prepare_git(root, 'owner/catalog', 'a' * 40)
        self.assertEqual((other / 'keep').read_text(), 'keep')

    def test_actual_git_preparation_and_reuse_with_local_network_fixture(self):
        self.prepare()
        root = self.state / 'sources/prepared'
        def local_network(args, **kwargs):
            args = list(args)
            if 'fetch' in args:
                args[args.index('protocol.file.allow=never')] = 'protocol.file.allow=always'
                args[args.index('origin')] = str(self.repo)
            return command(args, **kwargs)
        with patch('claude_config.fetch_baseline.command', side_effect=local_network):
            self.assertTrue(prepare_git(root, 'owner/catalog', self.commit))
        self.assertEqual(run_git(root, 'rev-parse', 'HEAD'), self.commit)
        with patch('claude_config.fetch_baseline.command', side_effect=AssertionError('must not fetch again')):
            self.assertFalse(prepare_git(root, 'owner/catalog', self.commit))

    def test_only_selected_external_sources_are_fetched(self):
        self.prepare()
        self.catalog['resources']['guide']['source'] = {
            'kind': 'external', 'repo': 'private/upstream', 'commit': self.old, 'path': 'packages/guide'}
        unused = deepcopy(self.catalog['resources']['guide'])
        unused['source']['repo'] = 'unselected/private'
        self.catalog['resources']['unused'] = unused
        (self.repo / 'catalog.json').write_text(json.dumps(self.catalog))
        run_git(self.repo, 'add', '.')
        run_git(self.repo, 'commit', '-qm', 'external source declarations')
        self.commit = self.tag_commit = run_git(self.repo, 'rev-parse', 'HEAD')
        self.release = 'baseline-' + self.commit
        self.record.update(commit=self.commit, catalog_sha256=hashlib.sha256((self.repo / 'catalog.json').read_bytes()).hexdigest())
        self.fetched()
        self.assertIn(('private/upstream', self.old), self.checkouts)
        self.assertFalse(any(repo == 'unselected/private' for repo, _ in self.checkouts))

    def test_failure_report_keeps_successfully_prepared_sources(self):
        self.prepare()
        progress = {}
        with patch('claude_config.fetch_baseline.api', side_effect=self.api), \
                patch('claude_config.fetch_baseline.prepare_git', side_effect=self.prepare_checkout), \
                patch('claude_config.fetch_baseline.CatalogResolver.resolve', side_effect=ValueError('dependency unavailable')):
            with self.assertRaises(ValueError):
                fetch(self.manager(), 'test', self.release, report=progress)
        self.assertFalse(progress['ready'])
        self.assertEqual(len(progress['results']), 1)
        self.assertEqual(progress['results'][0]['status'], 'applied')
        self.assertTrue(Path(progress['results'][0]['path']).is_dir())
        self.assertFalse(self.native.exists())
        report = build_report(progress, operation='fetch-baseline', preview_only=False, execution_started=True,
                              error={'code': 'fetch_failed', 'phase': 'retrieval', 'reason': 'dependency unavailable', 'exit_code': 2})
        save_report(report, self.state)
        self.assertEqual(report['execution']['status'], 'partial')
        self.assertIn('完整 JSON：' + report['artifacts']['json_path'], render_text(report))
