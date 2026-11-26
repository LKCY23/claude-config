"""Phase 1 UX improvements tests: I-01 through I-06."""
from contextlib import redirect_stdout
from copy import deepcopy
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from claude_config import cli as cli_entry
from claude_config import profile as profile_config
from claude_config import setup as setup_config
from claude_config.core.execution_context import progress_output
from claude_config.core.machine_profiles import MachineProfiles
from claude_config.core.merge import MergePlan
from claude_config.core.reporting import build_report, render_text, save_report
from claude_config.core.report_viewer import terminal_color_enabled, terminal_interactive
from claude_config.core.schema import SchemaError
from claude_config.core.source_transport import classify_command_error, prepare_git
from claude_config.fetch_baseline import fetch as fetch_baseline_run
import test_profile_manager as manager_fixtures


class Phase1ArgumentDiagnosticsTests(unittest.TestCase):
    def _run_cli(self, argv):
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(profile_config, 'fallback_state_root', return_value=Path(tmp)), \
                 redirect_stdout(output):
                code = profile_config.main(argv)
            reports = list((Path(tmp) / 'reports').glob('*.json'))
            saved_text = reports[0].read_text(encoding='utf-8') if reports else ''
        return code, output.getvalue(), saved_text

    def test_unknown_option_suggests_expect_plan_id_without_leaking_values(self):
        code, stdout_text, saved = self._run_cli(
            ['apply', '--agent', 'codex', '--expect-planid=SECRET_PLAN_VAL', '--format', 'text']
        )
        self.assertEqual(code, 2)
        self.assertIn('--expect-planid', stdout_text)
        self.assertIn('--expect-plan-id', stdout_text)
        self.assertNotIn('SECRET_PLAN_VAL', stdout_text)
        self.assertNotIn('SECRET_PLAN_VAL', saved)

    def test_unknown_flag_and_positional_secret_never_leak(self):
        code, stdout_text, saved = self._run_cli(
            ['plan', '--agent', 'codex', '--unexpected-flag', 'SECRET_POS', '--format', 'json']
        )
        self.assertEqual(code, 2)
        report = json.loads(stdout_text)
        reason = report['errors'][0]['reason']
        self.assertIn('--unexpected-flag', reason)
        self.assertNotIn('SECRET_POS', stdout_text)
        self.assertNotIn('SECRET_POS', saved)

    def test_invalid_choice_and_missing_required_and_reject(self):
        code1, out1, saved1 = self._run_cli(['plan', '--agent', 'SECRET_AGENT', '--format', 'json'])
        self.assertEqual(code1, 2)
        reason1 = json.loads(out1)['errors'][0]['reason']
        self.assertIn('--agent', reason1)
        self.assertIn('claude-code', reason1)
        self.assertNotIn('SECRET_AGENT', out1)
        self.assertNotIn('SECRET_AGENT', saved1)

        code2, out2, _ = self._run_cli(['plan', '--format', 'json'])
        self.assertEqual(code2, 2)
        reason2 = json.loads(out2)['errors'][0]['reason']
        self.assertIn('--agent', reason2)
        self.assertIn('--app', reason2)

        code3, out3, _ = self._run_cli(['switch', '--agent', 'codex', '--format', 'json'])
        self.assertEqual(code3, 2)
        self.assertIn('switch requires --profile', json.loads(out3)['errors'][0]['reason'])


class Phase1TransportDiagnosticsTests(unittest.TestCase):
    def test_ssh_drop_and_proxy_port22_diagnostics(self):
        for stderr in (
            'Connection closed by 198.18.0.25 port 22\nfatal: Could not read from remote repository.',
            'kex_exchange_identification: Connection closed by remote host',
            'ssh_exchange_identification: Connection reset by peer',
        ):
            with self.subTest(stderr=stderr):
                exc = classify_command_error(stderr, context={'repo': 'LKCY23/claudespace', 'transport': 'ssh'})
                msg = str(exc)
                self.assertIn('LKCY23/claudespace', msg)
                self.assertIn('22 端口', msg)
                self.assertIn('--git-transport gh-https', msg)

    def test_prepare_git_enriches_error_with_repo_and_transport(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'repo-checkout'
            def failing_runner(args, **kwargs):
                if 'fetch' in args:
                    raise SchemaError('获取失败：检查 GitHub 访问权限、网络及 Git/gh 配置；未修改配置选择。')
                return subprocess.run(args, check=True, capture_output=True).stdout
            with self.assertRaises(SchemaError) as ctx:
                prepare_git(root, 'LKCY23/claudespace', 'a' * 40, git_transport='ssh', runner=failing_runner)
            self.assertIn('LKCY23/claudespace', str(ctx.exception))
            self.assertIn('ssh', str(ctx.exception))
            self.assertIn('--git-transport gh-https', str(ctx.exception))


class Phase1MachineProfileDoctorTests(unittest.TestCase):
    def test_strict_mismatch_names_exact_diff_and_doctor_degrades_gracefully(self):
        fixture = manager_fixtures.ManagerTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.addCleanup(fixture.tearDown)

        local = yaml.safe_load(fixture.machine_path.read_text())
        codex_profile = local['agents']['claude-code']['profile']
        local['profile_selection'] = f"machines/{local['machine_id']}.yaml"
        local['agents'].pop('codex', None)
        for entry in local['agents'].values():
            entry.pop('profile', None)
        # Keep only claude-code locally, while repo selection adds codex;
        # also keep a second local-only agent ('antigravity' missing from repo) would fail if no binding,
        # so add a valid antigravity local binding to test both directions simultaneously.
        ag_root = fixture.root / '.gemini/config'
        ag_root.mkdir(parents=True)
        local['agents']['antigravity'] = {'binding': {'config_root': str(ag_root), 'application': str(fixture.root / 'Antigravity.app')}}
        (fixture.root / 'Antigravity.app').mkdir()
        fixture.machine_path.write_text(yaml.safe_dump(local, sort_keys=False))

        selection_path = fixture.data / local['profile_selection']
        selection_path.parent.mkdir(parents=True, exist_ok=True)
        selection = {
            'schema_version': 1,
            'machine_id': local['machine_id'],
            'agents': {
                'claude-code': codex_profile,
                'codex': 'profiles/agents/codex/official.yaml',
            },
            'apps': {},
        }
        selection_path.write_text(yaml.safe_dump(selection, sort_keys=False))

        with self.assertRaises(SchemaError) as ctx:
            MachineProfiles(fixture.machine_path, strict=True)
        self.assertIn('Repository selections and local runtime bindings differ: agents', str(ctx.exception))
        self.assertIn('repository only: codex', str(ctx.exception))
        self.assertIn('local only: antigravity', str(ctx.exception))

        out = io.StringIO()
        with redirect_stdout(out):
            code = setup_config.main(['doctor', '--machine', str(fixture.machine_path), '--resources', '--no-save', '--format', 'json'])
        self.assertEqual(code, 1)
        report = json.loads(out.getvalue())
        self.assertFalse(report['ready'])
        mismatch_checks = [c for c in report['checks'] if c['name'] == 'machine_profile_selection']
        self.assertEqual(len(mismatch_checks), 1)
        self.assertEqual(mismatch_checks[0]['status'], 'failed')
        self.assertEqual(mismatch_checks[0]['repository_only'], ['codex'])
        self.assertEqual(mismatch_checks[0]['local_only'], ['antigravity'])
        # Remaining checks still ran!
        check_names = {c['name'] for c in report['checks']}
        self.assertIn('python_runtime', check_names)
        self.assertIn('profile_compatibility', check_names)
        self.assertIn('native_entry', check_names)
        self.assertGreater(report['summary']['checks']['passed'], 0)
        text = render_text(report)
        self.assertIn('machines/', text)


class Phase1ProgressTests(unittest.TestCase):
    def test_fetch_baseline_emits_granular_progress(self):
        import shutil
        from test_profile_baseline import BaselineTests
        bf = BaselineTests()
        bf.setUp()
        self.addCleanup(bf.doCleanups)
        self.addCleanup(bf.tearDown)
        bf.prepared()

        stderr = io.StringIO()
        manager = bf.manager()
        record_bytes = json.dumps(bf.record).encode()
        def fake_prepare_git(root, repo, pin, **kwargs):
            root = Path(root)
            if not root.exists():
                root.parent.mkdir(parents=True, exist_ok=True)
                shutil.copytree(bf.candidate, root, symlinks=True)
                return True
            return False
        with patch('claude_config.fetch_baseline.release_record', return_value=record_bytes), \
             patch('claude_config.fetch_baseline.prepare_git', side_effect=fake_prepare_git), \
             progress_output(stderr):
            res = fetch_baseline_run(manager, 'test', 'baseline-' + bf.commit)
        self.assertTrue(res['ready'])
        lines = [line for line in stderr.getvalue().splitlines() if line.strip()]
        self.assertTrue(any('Release 记录' in line for line in lines))
        self.assertTrue(any('主目录仓库' in line for line in lines))
        self.assertTrue(any('校验受影响 Profile' in line for line in lines))


class Phase1ReportingAndFormatTests(unittest.TestCase):
    def test_logical_changes_and_verbose_preserve_collapsing_and_next_steps(self):
        raw = MergePlan({'settings': {'model': 'new'}}, {'settings': {'model': 'old'}}, source={}, destination={}).report()
        tpl = deepcopy(raw['decisions'][0])
        for pkg_idx in range(2):
            for file_idx in range(4):
                row = deepcopy(tpl)
                row['path'] = ['resources', 'skills', f'pkg{pkg_idx}@cat', f'/tmp/f{pkg_idx}_{file_idx}']
                raw['decisions'].append(row)
        for idx in range(5):
            raw['decisions'].append({
                'path': ['settings', f'extra_{idx}'],
                'before_exists': True, 'before': f'v{idx}',
                'incoming_exists': False, 'after_exists': True, 'after': f'v{idx}',
                'action': 'preserve', 'policy': 'keep_target',
                'policy_origin': 'program_default', 'reason': 'Source missing or unselected',
            })
        report = build_report(raw, operation='plan', context={'agent_type': 'codex'})
        report['plan_id'] = 'a' * 64
        self.assertEqual(report['summary']['logical_changes'], {'packages': 2, 'settings': 1, 'other': 0, 'files': 9})

        concise = render_text(report, verbose=False)
        self.assertIn('2 个资源包（展开 9 项），1 项设置', concise)
        self.assertIn('已省略 5 项保留本机值决策；添加 --verbose 查看明细。', concise)
        self.assertIn('下一步执行：claude-config apply --agent codex --expect-plan-id ' + ('a' * 12), concise)
        self.assertNotIn('\x1b[', concise)

        detailed = render_text(report, verbose=True)
        self.assertNotIn('已省略', detailed)
        self.assertIn('extra_4', detailed)

    def test_early_failure_suppresses_zero_boilerplate_and_sanitizes_ansi(self):
        report = build_report(
            {'targets': []},
            operation='plan',
            context={'agent_type': 'codex'},
            error={'code': 'setup_failed', 'phase': 'planning', 'reason': '坏了\x1b[31mINJECT', 'exit_code': 2},
        )
        plain = render_text(report, color=False)
        self.assertNotIn('检查：0 项通过', plain)
        self.assertNotIn('待更新：0 项', plain)
        self.assertNotIn('\x1b[', plain)
        self.assertIn('\\x1b[31mINJECT', plain)

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop('NO_COLOR', None)
            colored = render_text(report, color=True)
        self.assertIn('\x1b[', colored)
        # Untrusted content injection was still escaped BEFORE styling
        self.assertIn('\\x1b[31mINJECT', colored)

        with patch.dict(os.environ, {'NO_COLOR': '1'}):
            no_color = render_text(report, color=True)
        self.assertNotIn('\x1b[', no_color)

    def test_conflict_and_plan_stale_next_steps(self):
        raw = MergePlan({'settings': {'model': 'new'}}, {'settings': {'model': 'old'}},
                        baseline={'settings': {'model': 'base'}}, source={}, destination={}).report()
        report = build_report(raw, operation='plan', context={'agent_type': 'codex'})
        text = render_text(report)
        self.assertIn('下一步解决设置冲突（/settings/model）', text)
        self.assertIn('claude-config resolve --agent codex', text)
        self.assertIn('--choose /settings/model=source_wins', text)

        stale = build_report(
            {'checks': [{'name': 'native', 'status': 'passed'}]},
            operation='apply',
            context={'agent_type': 'codex'},
            error={'code': 'plan_stale', 'phase': 'recheck', 'reason': 'stale', 'exit_code': 1},
        )
        self.assertIn('下一步：重新运行 claude-config plan --agent codex 获取最新 plan_id 后再执行。', render_text(stale))

        # Backwards-compat: old saved reports lacking summary.logical_changes still display package counts
        legacy_raw = MergePlan({'settings': {'model': 'new'}}, {'settings': {'model': 'old'}}, source={}, destination={}).report()
        tpl = deepcopy(legacy_raw['decisions'][0])
        for file_idx in range(3):
            row = deepcopy(tpl)
            row['path'] = ['resources', 'skills', 'pkg0@cat', f'/tmp/old_{file_idx}']
            legacy_raw['decisions'].append(row)
        legacy_report = build_report(legacy_raw, operation='plan', context={'agent_type': 'codex'})
        legacy_report['summary'].pop('logical_changes', None)
        self.assertIn('1 个资源包（展开 4 项），1 项设置', render_text(legacy_report))

        # Scoped plan preserves selectors in next_steps_lines
        scoped_raw = MergePlan({'settings': {'model': 'new'}}, {'settings': {'model': 'old'}}, source={}, destination={}).report()
        scoped_report = build_report(scoped_raw, operation='plan', context={'agent_type': 'codex'})
        scoped_report['plan_id'] = 'b' * 64
        scoped_report['deployment_scope'] = {
            'requested': {'scope': 'all', 'resources': ['foo@cat'], 'resource_groups': [], 'fields': []},
            'expanded': {'resources': [], 'fields': []},
        }
        self.assertIn(
            '下一步执行：claude-config apply --agent codex --resource foo@cat --expect-plan-id ' + ('b' * 12),
            render_text(scoped_report),
        )
        scoped_report['deployment_scope'] = {
            'requested': {'scope': 'settings', 'resources': [], 'resource_groups': [], 'fields': []},
            'expanded': {'resources': [], 'fields': []},
        }
        self.assertIn(
            '下一步执行：claude-config apply --agent codex --scope settings --expect-plan-id ' + ('b' * 12),
            render_text(scoped_report),
        )

    def test_terminal_color_and_report_cmd_subcommand(self):
        tty, pipe = unittest.mock.Mock(), unittest.mock.Mock()
        tty.isatty.return_value = True
        pipe.isatty.return_value = False
        with patch.dict(os.environ, {'TERM': 'xterm-256color'}):
            os.environ.pop('NO_COLOR', None)
            self.assertTrue(terminal_color_enabled('auto', stdout=tty))
            self.assertFalse(terminal_color_enabled('text', stdout=tty))
            self.assertFalse(terminal_color_enabled('auto', stdout=pipe))
            self.assertTrue(terminal_interactive('tui', stdin=tty, stdout=tty))
        with patch.dict(os.environ, {'TERM': 'xterm-256color', 'NO_COLOR': '1'}):
            self.assertFalse(terminal_color_enabled('auto', stdout=tty))

        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / 'state'
            r1 = build_report(MergePlan({'settings': {'model': 'a'}}, {'settings': {'model': 'b'}}, source={}, destination={}).report(),
                              operation='plan', context={'agent_type': 'codex'})
            save_report(r1, state)
            out = io.StringIO()
            with redirect_stdout(out):
                code = cli_entry.main(['report', 'last', '--state-dir', str(state), '--format', 'text'])
            self.assertEqual(code, 0)
            self.assertIn('默认模型', out.getvalue())

            out_json = io.StringIO()
            with redirect_stdout(out_json):
                code = cli_entry.main(['report', r1['report_id'][:8], '--state-dir', str(state), '--format', 'json'])
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(out_json.getvalue())['report_id'], r1['report_id'])

            out_bad = io.StringIO()
            with patch('claude_config.report_cmd.fallback_state_root', return_value=Path(tmp) / 'fb'), redirect_stdout(out_bad):
                code = cli_entry.main(['report', '../../etc/passwd', '--state-dir', str(state), '--format', 'json'])
            self.assertEqual(code, 2)
            self.assertEqual(json.loads(out_bad.getvalue())['errors'][0]['code'], 'invalid_arguments')

            # Historical blocked/error report still returns exit code 0 when viewed successfully
            blocked = build_report(
                {'checks': [{'name': 'test', 'status': 'blocked', 'reason': 'blocked_demo'}]},
                operation='plan',
                context={'agent_type': 'codex'},
            )
            save_report(blocked, state)
            out_blocked = io.StringIO()
            with redirect_stdout(out_blocked):
                code = cli_entry.main(['report', 'last', '--state-dir', str(state), '--format', 'text'])
            self.assertEqual(code, 0)
            self.assertIn('blocked_demo', out_blocked.getvalue())

            # Malformed JSON object lacking required report keys fails safely with code=2
            bad_json = Path(tmp) / 'malformed.json'
            bad_json.write_text(json.dumps({'schema_version': 1, 'operation': 'plan'}), encoding='utf-8')
            out_malformed = io.StringIO()
            with patch('claude_config.report_cmd.fallback_state_root', return_value=Path(tmp) / 'fb'), redirect_stdout(out_malformed):
                code = cli_entry.main(['report', str(bad_json), '--format', 'json'])
            self.assertEqual(code, 2)
            malformed_err = json.loads(out_malformed.getvalue())['errors'][0]
            self.assertEqual(malformed_err['code'], 'report_load_failed')

            invalid_json = Path(tmp) / 'broken.json'
            invalid_json.write_text('{SECRET_RAW_DATA', encoding='utf-8')
            out_invalid = io.StringIO()
            with patch('claude_config.report_cmd.fallback_state_root', return_value=Path(tmp) / 'fb'), redirect_stdout(out_invalid):
                code = cli_entry.main(['report', str(invalid_json), '--format', 'json'])
            self.assertEqual(code, 2)
            self.assertEqual(json.loads(out_invalid.getvalue())['errors'][0]['code'], 'report_load_failed')
            self.assertNotIn('SECRET_RAW_DATA', out_invalid.getvalue())


if __name__ == '__main__':
    unittest.main()
