"""Phase 4 UX polish tests: I-12 (CLI help grouping, typo hints, quiet --help, SKILL.md), I-13 (README & dev-log)."""
from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from claude_config import cli as cli_entry

REPO_ROOT = Path(__file__).resolve().parents[1]


class Phase4CliHelpAndTypoTests(unittest.TestCase):
    def test_root_help_includes_grouped_sections_and_examples(self):
        out = io.StringIO()
        err = io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli_entry.main(['--help'])
        self.assertEqual(code, 0)
        self.assertEqual(err.getvalue(), '')
        text = out.getvalue()
        for expected in ('日常工作流', '配置操作', '本机设置', '专项操作', '常用示例', 'claude-config status', 'claude-config sync'):
            self.assertIn(expected, text)

    def test_subcommand_help_exits_zero_without_progress_noise(self):
        cases = (
            ['status', '--help'],
            ['sync', '--help'],
            ['baseline', '--help'],
            ['baseline', 'status', '--help'],
            ['baseline', 'sync', '--help'],
            ['baseline', 'adopt', '--help'],
            ['resolve', '--help'],
            ['report', '--help'],
            ['plan', '--help'],
            ['doctor', '--help'],
            ['fetch-baseline', '--help'],
        )
        for argv in cases:
            with self.subTest(argv=argv):
                out = io.StringIO()
                err = io.StringIO()
                with redirect_stdout(out), redirect_stderr(err):
                    try:
                        code = cli_entry.main(argv)
                    except SystemExit as exc:
                        code = exc.code
                self.assertEqual(code, 0, f'{argv} failed with stderr={err.getvalue()}')
                self.assertEqual(err.getvalue(), '', f'{argv} emitted unexpected stderr: {err.getvalue()}')
                self.assertIn('usage:', out.getvalue())

    def test_unknown_command_suggests_close_matches_and_redacts_arbitrary_tokens(self):
        err1 = io.StringIO()
        with redirect_stderr(err1):
            code1 = cli_entry.main(['stauts'])
        self.assertEqual(code1, 2)
        self.assertIn('status', err1.getvalue())
        self.assertIn('你是否想输入', err1.getvalue())

        err2 = io.StringIO()
        with redirect_stderr(err2):
            code2 = cli_entry.main(['synk'])
        self.assertEqual(code2, 2)
        self.assertIn('sync', err2.getvalue())

        out_json = io.StringIO()
        with redirect_stdout(out_json):
            code_json = cli_entry.main(['stauts', '--format', 'json'])
        self.assertEqual(code_json, 2)
        payload = json.loads(out_json.getvalue())
        self.assertFalse(payload['ready'])
        self.assertEqual(payload['error']['code'], 'unknown_command')
        self.assertIn('status', payload['error']['reason'])

        err_secret = io.StringIO()
        with redirect_stderr(err_secret):
            code_secret = cli_entry.main(['TOKEN=sk-secret-123'])
        self.assertEqual(code_secret, 2)
        self.assertNotIn('sk-secret-123', err_secret.getvalue())
        self.assertIn('<command>', err_secret.getvalue())


class Phase4DocsAndSkillTests(unittest.TestCase):
    def test_readme_cleaned_and_dev_log_extracted(self):
        readme = (REPO_ROOT / 'README.md').read_text(encoding='utf-8')
        self.assertNotIn('v0.7.0', readme)
        self.assertNotIn('> 2026-10-28', readme)
        self.assertNotIn('> 2026-11-04', readme)
        for token in ('claude-config status', 'claude-config sync', 'claude-config resolve', 'claude-config baseline status', 'claude-config report last', 'docs/dev-log.md'):
            self.assertIn(token, readme)

        dev_log = (REPO_ROOT / 'docs/dev-log.md').read_text(encoding='utf-8')
        for doc in ('implementation-20261104-desktop.md', 'implementation-20261028-s9.md', 'implementation-20261028-s7.md'):
            self.assertIn(doc, dev_log)
            self.assertTrue((REPO_ROOT / 'docs' / doc).is_file())

    def test_skill_md_documents_daily_workflows_and_short_plan_id(self):
        skill = (REPO_ROOT / 'src/claude_config/resources/SKILL.md').read_text(encoding='utf-8')
        for token in ('Daily workflows', 'status', 'sync', 'resolve', 'baseline', '--plan-id'):
            self.assertIn(token, skill)


if __name__ == '__main__':
    unittest.main()
