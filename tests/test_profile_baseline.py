from copy import deepcopy
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from claude_config.core.baseline import adopt_baseline
from claude_config.core.recovery import rollback_plan
from claude_config.core.transaction import PlanStale, atomic_write
from test_profile_resources import ResourceFixture, run_git


class BaselineTests(ResourceFixture):
    def prepared(self):
        self.add('guide', 'instruction', {'guide.md': 'old baseline'}, 'guide.md')
        old = self.save()
        self.candidate = self.root / 'candidate'
        subprocess.run(['git', 'clone', '-q', str(self.repo), str(self.candidate)], check=True, capture_output=True)
        run_git(self.candidate, 'remote', 'set-url', 'origin', 'https://github.com/owner/catalog.git')
        run_git(self.candidate, 'config', 'user.name', 'isolated')
        run_git(self.candidate, 'config', 'user.email', 'isolated@example.invalid')
        (self.candidate / 'packages/guide/guide.md').write_text('new baseline')
        run_git(self.candidate, 'add', '.')
        run_git(self.candidate, 'commit', '-qm', 'new baseline')
        self.commit = run_git(self.candidate, 'rev-parse', 'HEAD')
        self.record_path = self.root / 'release.json'
        self.write_record()
        return old

    def write_record(self):
        self.record = {'schema_version': 1, 'kind': 'claudespace-baseline', 'repo': 'owner/catalog', 'commit': self.commit,
                       'catalog_path': '.', 'catalog_sha256': hashlib.sha256((self.candidate / 'catalog.json').read_bytes()).hexdigest(),
                       'validation': {'status': 'passed', 'level': 'structural', 'scope': 'catalog-schema'}}
        self.record_path.write_text(json.dumps(self.record))

    def plan(self):
        return adopt_baseline(self.manager(), 'test', self.commit, self.candidate, self.record_path)

    def test_adopt_changes_only_pin_and_prepared_root_after_preview(self):
        old = self.prepared()
        profile_bytes = (self.data / self.machine['agents']['claude-code']['profile']).read_bytes()
        plan = self.plan()
        self.assertTrue(plan.report['ready'])
        self.assertEqual(plan.report['impacts'][0]['resources'], ['guide@test'])
        self.assertEqual(yaml.safe_load((self.data / 'catalogs.yaml').read_text())['catalogs']['test']['commit'], old)
        self.assertFalse(self.native.exists())
        plan.apply(self.state)
        self.assertEqual(yaml.safe_load((self.data / 'catalogs.yaml').read_text())['catalogs']['test']['commit'], self.commit)
        self.assertEqual(yaml.safe_load(self.machine_path.read_text())['catalog_roots']['test'], str(self.candidate.resolve()))
        self.assertEqual(run_git(self.repo, 'rev-parse', 'HEAD'), old)
        self.assertEqual((self.data / self.machine['agents']['claude-code']['profile']).read_bytes(), profile_bytes)
        self.assertFalse(self.native.exists())
        self.assertFalse(self.manager().state_path.exists())

    def test_impacts_include_unbound_profiles_for_both_agents(self):
        self.prepared()
        path = self.data / 'profiles/agents/codex/later.yaml'
        path.parent.mkdir(parents=True)
        path.write_text(yaml.safe_dump({'schema_version': 1, 'agent_type': 'codex', 'resources': {'instructions': {'guide@test': {}}}}))
        plan = self.plan()
        self.assertEqual(len(plan.report['impacts']), 2)
        self.assertFalse(next(row for row in plan.report['impacts'] if row['agent_type'] == 'codex')['bound'])

    def test_release_identity_digest_and_validation_failures_block(self):
        self.prepared()
        original = deepcopy(self.record)
        for updates in ({'repo': 'other/repo'}, {'commit': '0' * 40}, {'catalog_sha256': '0' * 64}, {'catalog_path': 'different'},
                        {'validation': {'status': 'failed', 'level': 'structural', 'scope': 'catalog-schema'}}):
            self.record_path.write_text(json.dumps({**original, **updates}))
            with self.assertRaises(ValueError):
                self.plan()
        self.assertFalse(self.native.exists())

    def test_dirty_or_wrong_candidate_checkout_rejected(self):
        old = self.prepared()
        (self.candidate / 'extra.txt').write_text('untracked')
        with self.assertRaises(ValueError):
            self.plan()
        (self.candidate / 'extra.txt').unlink()
        with self.assertRaises(ValueError):
            adopt_baseline(self.manager(), 'test', old, self.candidate, self.record_path)

    def test_new_baseline_cannot_remove_selected_resource_or_add_implicit_dependency(self):
        self.prepared()
        value = json.loads((self.candidate / 'catalog.json').read_text())
        value['resources']['guide']['requires'] = {'resources': ['not-selected@test']}
        (self.candidate / 'catalog.json').write_text(json.dumps(value))
        run_git(self.candidate, 'add', '.')
        run_git(self.candidate, 'commit', '-qm', 'missing dependency')
        self.commit = run_git(self.candidate, 'rev-parse', 'HEAD')
        self.write_record()
        with self.assertRaises(ValueError):
            self.plan()

    def test_preview_invalidated_by_new_profile_and_release_record_change(self):
        self.prepared()
        plan = self.plan()
        directory = self.data / 'profiles/agents/claude-code'
        (directory / 'late.yaml').write_text(yaml.safe_dump(self.profile))
        with self.assertRaises(PlanStale):
            plan.apply(self.state)
        (directory / 'late.yaml').unlink()
        plan = self.plan()
        self.record_path.write_text(self.record_path.read_text() + ' ')
        with self.assertRaises(PlanStale):
            plan.apply(self.state)

    def test_failed_root_binding_write_restores_pin(self):
        old = self.prepared()
        plan = self.plan()
        def writer(path, content, mode):
            if path == self.machine_path:
                raise OSError('isolated binding failure')
            atomic_write(path, content, mode)
        with self.assertRaises(OSError):
            plan.apply(self.state, writer=writer)
        self.assertEqual(yaml.safe_load((self.data / 'catalogs.yaml').read_text())['catalogs']['test']['commit'], old)

    def test_independent_adoption_rollback_restores_pin_and_root(self):
        old = self.prepared()
        plan = self.plan()
        plan.apply(self.state)
        run = Path(plan.report['recovery']).parent.name
        recovery = rollback_plan(self.manager(), run)
        self.assertTrue(recovery.report['ready'], recovery.report['blockers'])
        recovery.apply(self.state)
        self.assertEqual(yaml.safe_load((self.data / 'catalogs.yaml').read_text())['catalogs']['test']['commit'], old)
        self.assertEqual(yaml.safe_load(self.machine_path.read_text())['catalog_roots']['test'], str(self.repo))

    def test_cli_adoption_previews_by_default(self):
        from claude_config import profile as profile_config
        self.prepared()
        args = ['adopt-baseline', '--machine', str(self.machine_path), '--agent', 'claude-code', '--catalog', 'test',
                '--commit', self.commit, '--checkout', str(self.candidate), '--release-record', str(self.record_path), '--format', 'json']
        with patch('builtins.print') as output:
            self.assertEqual(profile_config.main(args), 0)
            self.assertTrue(json.loads(output.call_args.args[0])['preview_only'])
            self.assertEqual(profile_config.main([*args, '--write']), 0)
        self.assertFalse(self.native.exists())


if __name__ == '__main__':
    unittest.main()
