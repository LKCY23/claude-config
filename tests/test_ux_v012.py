"""Tests for v0.12.0 UX refinements: sync unified counts & summaries and concise plugin target reporting."""
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
from claude_config.sync_cmd import _confirm_sync, render_sync
import test_profile_manager as manager_fixtures
from test_plugin_management import PluginManagementTests


class SyncUXTests(unittest.TestCase):
    def test_sync_preview_shows_summaries_aligned_columns_and_unified_counts(self):
        fixture = manager_fixtures.ManagerTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.addCleanup(fixture.tearDown)

        fixture.native('claude-code', value={'model': 'selected'})
        fixture.manager('claude-code').plan().apply(fixture.state)
        fixture.native('codex', value={'model': 'old-model'})

        with patch('claude_config.core.manager.NativeProbe', return_value=fixture.probe):
            out_dry = io.StringIO()
            with redirect_stdout(out_dry):
                code = cli_entry.main(['sync', '--machine', str(fixture.machine_path), '--dry-run'])
            self.assertEqual(code, 0, out_dry.getvalue())
            txt = out_dry.getvalue()
            self.assertIn('待应用 1 项配置', txt)
            self.assertIn('默认模型: "old-model" → "selected"', txt)
            # Both lines align their `—` separator at the exact same display column.
            target_lines = [line for line in txt.splitlines() if '—' in line]
            self.assertEqual(len(target_lines), 2)
            self.assertEqual(target_lines[0].index('—'), target_lines[1].index('—'))

            out_yes = io.StringIO()
            with redirect_stdout(out_yes):
                code = cli_entry.main(['sync', '--machine', str(fixture.machine_path), '--agent', 'codex', '--yes'])
            self.assertEqual(code, 0, out_yes.getvalue())
            self.assertRegex(out_yes.getvalue(), r'已同步 1 项配置（已写入 \d+ 个文件）')

    def test_confirm_sync_hides_redundant_yes_hint(self):
        sample = {
            'preview_only': True,
            'machine_id': 'm1',
            'machine_file': '/tmp/m1.yaml',
            'has_pending': True,
            'baseline': {'skipped': True, 'actions': []},
            'targets': [{
                'name': 'codex',
                'profile': 'profiles/agents/codex/official.yaml',
                'outcome': 'pending',
                'logical_changes': {'total': 1},
                'change_summaries': ['默认模型: "a" → "b"'],
                'pending_file_writes': 2,
            }],
            'skipped_manual_apps': [],
        }
        self.assertIn('追加 --yes', render_sync(sample, color=False))
        buf = io.StringIO()
        with patch('builtins.input', return_value='n'), redirect_stdout(buf):
            self.assertFalse(_confirm_sync(sample, False))
        self.assertNotIn('追加 --yes', buf.getvalue())


class PluginUXTests(unittest.TestCase):
    def test_plugin_reports_target_agent_and_collapses_clean_noop_unless_verbose(self):
        p = PluginManagementTests()
        p.setUp()
        self.addCleanup(p.doCleanups)
        self.addCleanup(p.tearDown)

        # Install once so a repeat upgrade to the same commit produces zero changes.
        code_first, _ = p.cli('install', 'tiny', '--write')
        self.assertEqual(code_first, 0)

        out_concise = io.StringIO()
        with patch('claude_config.plugin.ProfileManager', side_effect=lambda *a: p.manager()), \
                patch('claude_config.fetch_baseline.prepare_git', side_effect=p.fetch), \
                redirect_stdout(out_concise):
            code = cli_entry.main(['plugin', 'upgrade', 'tiny', '--agent', 'claude-code', '--commit', p.commit, '--write'])
        self.assertEqual(code, 0, out_concise.getvalue())
        concise = out_concise.getvalue()
        self.assertIn('执行完成，未发生配置变化', concise)
        self.assertNotIn('对象：未确定', concise)
        self.assertNotIn('无变化', concise)
        self.assertIn('--verbose', concise)

        # --verbose preserves detailed report with resolved agent label (`Claude Code (claude-code)`).
        out_verbose = io.StringIO()
        with patch('claude_config.plugin.ProfileManager', side_effect=lambda *a: p.manager()), \
                patch('claude_config.fetch_baseline.prepare_git', side_effect=p.fetch), \
                redirect_stdout(out_verbose):
            code = cli_entry.main(['plugin', 'upgrade', 'tiny', '--agent', 'claude-code', '--commit', p.commit, '--write', '--verbose'])
        self.assertEqual(code, 0, out_verbose.getvalue())
        verbose = out_verbose.getvalue()
        self.assertIn('对象：Claude Code (claude-code)', verbose)
        self.assertNotIn('对象：未确定', verbose)


if __name__ == '__main__':
    unittest.main()
