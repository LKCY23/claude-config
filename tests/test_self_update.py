"""Tests for `claude-config self-update` command."""
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from claude_config import __version__, cli as cli_entry
from claude_config.self_update_cmd import normalize_tag, parse_semver


def _release_payload(tag='v0.99.0', *, draft=False, include_all_assets=True):
    ver = tag.lstrip('v')
    assets = [{'name': f'claude_config-{ver}-py3-none-any.whl'}, {'name': 'constraints.txt'}]
    if include_all_assets:
        assets.append({'name': 'install.sh'})
    return json.dumps({
        'tagName': tag,
        'url': f'https://github.com/LKCY23/claude-config/releases/tag/{tag}',
        'isDraft': draft,
        'isPrerelease': False,
        'assets': assets,
    })


class SelfUpdateTests(unittest.TestCase):
    def test_semver_helpers_and_help(self):
        self.assertGreater(parse_semver('0.12.0'), parse_semver('0.12.0-rc.1'))
        self.assertGreater(parse_semver('0.12.0'), parse_semver('0.11.1'))
        self.assertEqual(normalize_tag('0.12.0'), 'v0.12.0')
        self.assertEqual(normalize_tag('v0.12.0'), 'v0.12.0')
        out = io.StringIO()
        with redirect_stdout(out):
            with self.assertRaises(SystemExit) as ctx:
                cli_entry.main(['self-update', '--help'])
            self.assertEqual(ctx.exception.code, 0)
        self.assertIn('self-update', out.getvalue())

    def test_up_to_date_and_missing_assets_and_draft_release(self):
        cur_payload = _release_payload(f'v{__version__}')
        with patch('shutil.which', return_value='/usr/bin/tool'), \
                patch('claude_config.self_update_cmd.subprocess.run', return_value=subprocess.CompletedProcess([], 0, cur_payload, '')):
            out = io.StringIO()
            with redirect_stdout(out):
                code = cli_entry.main(['self-update', '--format', 'json'])
            self.assertEqual(code, 0)
            rep = json.loads(out.getvalue())
            self.assertEqual(rep['status'], 'up_to_date')
            self.assertTrue(rep['ready'])

        missing_payload = _release_payload('v0.99.0', include_all_assets=False)
        with patch('shutil.which', return_value='/usr/bin/tool'), \
                patch('claude_config.self_update_cmd.subprocess.run', return_value=subprocess.CompletedProcess([], 0, missing_payload, '')):
            out = io.StringIO()
            with redirect_stdout(out):
                code = cli_entry.main(['self-update', '--format', 'json'])
            self.assertEqual(code, 1)
            rep = json.loads(out.getvalue())
            self.assertEqual(rep['status'], 'missing_assets')
            self.assertIn('install.sh', rep['missing_assets'])

        draft_payload = _release_payload('v0.99.0', draft=True)
        with patch('shutil.which', return_value='/usr/bin/tool'), \
                patch('claude_config.self_update_cmd.subprocess.run', return_value=subprocess.CompletedProcess([], 0, draft_payload, '')):
            out = io.StringIO()
            with redirect_stdout(out):
                code = cli_entry.main(['self-update', '--format', 'json'])
            self.assertEqual(code, 1)
            self.assertEqual(json.loads(out.getvalue())['status'], 'draft_release')

    def test_dry_run_interactive_and_yes_execution(self):
        new_payload = _release_payload('v0.99.0')

        def fake_run(cmd, **kwargs):
            if cmd[:3] == ['gh', 'release', 'view']:
                return subprocess.CompletedProcess(cmd, 0, new_payload, '')
            if cmd[:3] == ['gh', 'release', 'download']:
                dl_dir = Path(cmd[cmd.index('--dir') + 1])
                (dl_dir / 'claude_config-0.99.0-py3-none-any.whl').write_bytes(b'wheel')
                (dl_dir / 'constraints.txt').write_text('PyYAML==6.0.1\n')
                (dl_dir / 'install.sh').write_text('#!/usr/bin/env bash\nexit 0\n')
                return subprocess.CompletedProcess(cmd, 0, '', '')
            if cmd[0] == 'bash':
                return subprocess.CompletedProcess(cmd, 0, 'claude-config 0.99.0\n', '')
            raise AssertionError(f'Unexpected command: {cmd}')

        # 1) --dry-run
        with patch('shutil.which', return_value='/usr/bin/tool'), \
                patch('claude_config.self_update_cmd.subprocess.run', side_effect=fake_run):
            out = io.StringIO()
            with redirect_stdout(out):
                code = cli_entry.main(['self-update', '--dry-run'])
            self.assertEqual(code, 0)
            self.assertIn('0.99.0', out.getvalue())
            self.assertIn('追加 --yes', out.getvalue())

        # 2) Interactive decline hides `--yes` hint
        out = io.StringIO()
        out.isatty = lambda: True
        with patch('shutil.which', return_value='/usr/bin/tool'), \
                patch('claude_config.self_update_cmd.subprocess.run', side_effect=fake_run), \
                patch('sys.stdin.isatty', return_value=True), \
                patch('builtins.input', return_value='n'), \
                redirect_stdout(out):
            code = cli_entry.main(['self-update'])
        self.assertEqual(code, 0)
        self.assertNotIn('追加 --yes', out.getvalue())
        self.assertIn('已取消', out.getvalue())

        # 3) --yes downloads and runs install.sh cleanly
        with patch('shutil.which', return_value='/usr/bin/tool'), \
                patch('claude_config.self_update_cmd.subprocess.run', side_effect=fake_run) as mock_run:
            out = io.StringIO()
            with redirect_stdout(out):
                code = cli_entry.main(['self-update', '--yes', '--format', 'json'])
            self.assertEqual(code, 0, out.getvalue())
            rep = json.loads(out.getvalue())
            self.assertEqual(rep['status'], 'updated')
            self.assertEqual(rep['target_version'], '0.99.0')
            self.assertEqual(mock_run.call_count, 3)


if __name__ == '__main__':
    unittest.main()
