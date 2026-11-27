"""Tests for `claude-config profiles matrix|diff|copy-item`."""
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from claude_config import cli as cli_entry
from claude_config.core.native_plugins import PluginOperation
from claude_config.core.plugin_manager import PluginManager
from test_profile_plugins import FakeInstaller
from test_profile_resources import ResourceFixture, run_git


class ProfilesCommandTests(ResourceFixture):
    def setUp(self):
        super().setUp()
        self.installer = FakeInstaller()
        installer = self.installer
        self.mock_native = patch.object(PluginOperation, '_call', lambda op, args, work: installer(op, args, work))
        self.mock_native.start()
        self.addCleanup(self.mock_native.stop)

        self.add('tiny', 'plugin', {
            '.claude-plugin/plugin.json': json.dumps({'name': 'tiny', 'version': '1.0.0'}), 'body.txt': 'one',
        }, '.claude-plugin/plugin.json')
        self.add('helper', 'skill', {'SKILL.md': '# Helper\n'}, 'SKILL.md')
        self.commit = self.save()

        self.profile_path = self.data / self.machine['agents']['claude-code']['profile']  # .../work.yaml
        work_data = yaml.safe_load(self.profile_path.read_text())
        work_data['settings'] = {'model': 'sonnet', 'language': 'zh'}
        work_data['resources']['skills'] = {'helper@test': {}}
        self.profile_path.write_text(yaml.safe_dump(work_data, sort_keys=False))

        self.official_path = self.profile_path.parent / 'official.yaml'
        off_data = {
            'schema_version': 1,
            'agent_type': 'claude-code',
            'settings': {'model': 'opus'},
            'resources': {'plugins': {'tiny@test': {'options': {'enabled': False}}}},
        }
        self.official_path.write_text(yaml.safe_dump(off_data, sort_keys=False))
        self.official_lock = self.official_path.with_suffix('.plugins.lock.yaml')

        machines_dir = self.data / 'machines'
        machines_dir.mkdir(exist_ok=True)
        (machines_dir / f"{self.machine['machine_id']}.yaml").write_text(yaml.safe_dump({
            'schema_version': 1,
            'machine_id': self.machine['machine_id'],
            'agents': {'claude-code': 'profiles/agents/claude-code/work.yaml'},
            'apps': {},
        }, sort_keys=False))
        (machines_dir / 'macbook-air.yaml').write_text(yaml.safe_dump({
            'schema_version': 1,
            'machine_id': 'macbook-air',
            'agents': {'claude-code': 'profiles/agents/claude-code/official.yaml'},
            'apps': {},
        }, sort_keys=False))

        # Install tiny in work profile so work.plugins.lock.yaml exists cleanly
        def fetch(root, repo, commit):
            if root.exists():
                return False
            root.parent.mkdir(parents=True, exist_ok=True)
            run_git(self.root, 'clone', '--no-hardlinks', str(self.repo), str(root))
            run_git(root, 'remote', 'set-url', 'origin', 'https://github.com/' + repo + '.git')
            run_git(root, 'checkout', '--detach', commit)
            return True

        PluginManager(self.manager(), fetcher=fetch).plan('install', 'tiny').apply(self.state)

    def _run_cli(self, argv):
        out = io.StringIO()
        with redirect_stdout(out):
            code = cli_entry.main(argv)
        return code, out.getvalue()

    def test_matrix_and_diff_across_profiles_and_machines(self):
        code_m, out_m = self._run_cli([
            'profiles', 'matrix', '--machine', str(self.machine_path), '--agent', 'claude-code', '--format', 'json',
        ])
        self.assertEqual(code_m, 0, out_m)
        mat = json.loads(out_m)
        cc = mat['agents']['claude-code']
        self.assertEqual(len(cc['profiles']), 2)
        self.assertIn('/settings/language', cc['diff_settings'])
        self.assertIn('skills:helper@test', cc['diff_resources'])

        code_mt, out_mt = self._run_cli([
            'profiles', 'matrix', '--machine', str(self.machine_path), '--format', 'text',
        ])
        self.assertEqual(code_mt, 0, out_mt)
        self.assertIn('Profile 矩阵总览', out_mt)
        self.assertIn('macbook-air', out_mt)

        code_d, out_d = self._run_cli([
            'profiles', 'diff', 'claude-code/work', 'claude-code/official',
            '--machine', str(self.machine_path), '--format', 'json',
        ])
        self.assertEqual(code_d, 0, out_d)
        diff_rep = json.loads(out_d)
        self.assertFalse(diff_rep['identical'])
        diff_paths = {item['path'] for item in diff_rep['settings_diff']}
        self.assertEqual(diff_paths, {'/settings/model', '/settings/language'})
        res_refs = {(item['group'], item['ref']) for item in diff_rep['resources_diff']}
        self.assertIn(('skills', 'helper@test'), res_refs)
        self.assertIn(('plugins', 'tiny@test'), res_refs)

    def test_copy_item_dry_run_and_write_for_resource_lock_and_field(self):
        (self.data / '.gitignore').write_text('/.state/\n', encoding='utf-8')
        run_git(self.data, 'init', '-q', '-b', 'master')
        run_git(self.data, 'config', 'user.name', 'Test User')
        run_git(self.data, 'config', 'user.email', 'test@example.com')
        run_git(self.data, 'add', '.')
        run_git(self.data, 'commit', '-qm', 'baseline')

        before_off = self.official_path.read_text()

        code_dry, out_dry = self._run_cli([
            'profiles', 'copy-item', '--machine', str(self.machine_path),
            '--from', 'claude-code/work', '--to', 'claude-code/official',
            '--resource', 'tiny@test', '--resource', 'helper@test',
            '--field', '/settings/language', '--include-lock',
            '--dry-run', '--format', 'json',
        ])
        self.assertEqual(code_dry, 0, out_dry)
        dry_rep = json.loads(out_dry)
        self.assertTrue(dry_rep['preview_only'])
        self.assertTrue(dry_rep['has_writes'])
        self.assertEqual(self.official_path.read_text(), before_off)
        self.assertFalse(self.official_lock.exists())

        code_w, out_w = self._run_cli([
            'profiles', 'copy-item', '--machine', str(self.machine_path),
            '--from', 'claude-code/work', '--to', 'claude-code/official',
            '--resource', 'tiny@test', '--resource', 'helper@test',
            '--field', '/settings/language', '--include-lock',
            '--write', '--format', 'text',
        ])
        self.assertEqual(code_w, 0, out_w)
        self.assertIn('claude-config data publish', out_w)

        off_after = yaml.safe_load(self.official_path.read_text())
        self.assertEqual(off_after['settings']['language'], 'zh')
        self.assertEqual(off_after['settings']['model'], 'opus')
        self.assertIn('helper@test', off_after['resources']['skills'])
        self.assertTrue(off_after['resources']['plugins']['tiny@test']['options']['enabled'])
        self.assertTrue(self.official_lock.is_file())
        self.assertEqual(yaml.safe_load(self.official_lock.read_text())['plugins']['tiny@test']['version'], '1.0.0')

    def test_copy_item_and_diff_reject_invalid_cross_agent_or_missing(self):
        codex_path = self.data / 'profiles/agents/codex/work.yaml'
        codex_path.parent.mkdir(parents=True, exist_ok=True)
        codex_path.write_text(yaml.safe_dump({
            'schema_version': 1, 'agent_type': 'codex', 'settings': {'model': 'gpt-5'},
        }, sort_keys=False))

        code_xd, out_xd = self._run_cli([
            'profiles', 'diff', 'claude-code/work', 'codex/work',
            '--machine', str(self.machine_path), '--format', 'json',
        ])
        self.assertEqual(code_xd, 2)
        self.assertIn('同类型', out_xd)

        code_xc, _ = self._run_cli([
            'profiles', 'copy-item', '--machine', str(self.machine_path),
            '--from', 'claude-code/work', '--to', 'codex/work',
            '--field', '/settings/model', '--write', '--format', 'json',
        ])
        self.assertEqual(code_xc, 2)

        code_nf, _ = self._run_cli([
            'profiles', 'copy-item', '--machine', str(self.machine_path),
            '--from', 'claude-code/work', '--to', 'claude-code/official',
            '--field', '/settings/nonexistent', '--write', '--format', 'json',
        ])
        self.assertEqual(code_nf, 2)


if __name__ == '__main__':
    unittest.main()
