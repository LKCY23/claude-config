"""Phase 2 UX improvements tests: I-07 (resolve), I-08 (plan_stale attribution & isolation), I-09 (short plan_id)."""
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
from claude_config import setup as setup_config
from claude_config.core.native import parse_native
from claude_config.core.schema import SchemaError
from claude_config.core.transaction import plan_id_matches
import test_profile_manager as manager_fixtures
import test_profile_resource_conflicts as conflict_fixtures


class Phase2ShortPlanIdTests(unittest.TestCase):
    def test_plan_id_matches_short_prefix_and_rejects_too_short_or_non_hex(self):
        full = 'AbCdEf0123456789' * 4
        self.assertTrue(plan_id_matches(full, 'abcdef01'))
        self.assertTrue(plan_id_matches(full, 'ABCDEF012345'))
        self.assertTrue(plan_id_matches(full, full))
        self.assertFalse(plan_id_matches(full, '0' * 64))
        for bad in ('abc', '1234567', 'nothex!!', 'g' * 8):
            with self.assertRaises(SchemaError):
                plan_id_matches(full, bad)

    def test_apply_and_init_accept_short_plan_id_via_alias(self):
        fixture = manager_fixtures.ManagerTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.addCleanup(fixture.tearDown)

        fixture.native('codex', value={'model': 'old'})
        out = io.StringIO()
        with patch('claude_config.profile.ProfileManager', return_value=fixture.manager('codex')), redirect_stdout(out):
            code = cli_entry.main(['plan', '--machine', str(fixture.machine_path), '--agent', 'codex', '--format', 'json'])
        self.assertEqual(code, 0)
        plan_report = json.loads(out.getvalue())
        short_id = plan_report['plan_id'][:10].upper()

        out_apply = io.StringIO()
        with patch('claude_config.profile.ProfileManager', return_value=fixture.manager('codex')), redirect_stdout(out_apply):
            code = cli_entry.main(['apply', '--machine', str(fixture.machine_path), '--agent', 'codex',
                                   '--plan-id', short_id, '--format', 'json'])
        self.assertEqual(code, 0, out_apply.getvalue())
        self.assertEqual(parse_native('codex', fixture.native('codex').read_bytes())['model'], 'selected')

        # Invalid short prefix <8 chars is rejected cleanly without touching native config
        out_bad = io.StringIO()
        with patch('claude_config.profile.ProfileManager', return_value=fixture.manager('codex')), redirect_stdout(out_bad):
            code = cli_entry.main(['apply', '--machine', str(fixture.machine_path), '--agent', 'codex',
                                   '--plan-id', 'abc', '--format', 'json'])
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out_bad.getvalue())['errors'][0]['code'], 'invalid_configuration')

        # init also supports --plan-id short prefix
        out_init = io.StringIO()
        new_machine = fixture.root / 'init-machine.yaml'
        with redirect_stdout(out_init):
            code = setup_config.main(['init', '--machine', str(new_machine), '--config-dir', str(fixture.data), '--format', 'json'])
        self.assertEqual(code, 0)
        init_short = json.loads(out_init.getvalue())['plan_id'][:8]
        out_init_write = io.StringIO()
        with redirect_stdout(out_init_write):
            code = setup_config.main(['init', '--machine', str(new_machine), '--config-dir', str(fixture.data),
                                      '--write', '--plan-id', init_short, '--format', 'json'])
        self.assertEqual(code, 0)
        self.assertTrue(new_machine.is_file())


class Phase2PlanStaleAttributionTests(unittest.TestCase):
    def test_plan_stale_reports_native_and_profile_differences_and_cross_agent_isolation(self):
        fixture = manager_fixtures.ManagerTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.addCleanup(fixture.tearDown)

        # Configure clean antigravity + codex + claude-code bindings in the isolated fixture
        ag_root = fixture.root / '.gemini/config'
        ag_root.mkdir(parents=True)
        ag_app = fixture.root / 'Antigravity.app'
        ag_app.mkdir()
        ag_profile_path = fixture.data / 'profiles/agents/antigravity/current.yaml'
        ag_profile_path.parent.mkdir(parents=True, exist_ok=True)
        ag_profile_path.write_text(yaml.safe_dump({'schema_version': 1, 'agent_type': 'antigravity'}, sort_keys=False))
        fixture.machine['agents']['antigravity'] = {
            'profile': 'profiles/agents/antigravity/current.yaml',
            'binding': {'config_root': str(ag_root), 'application': str(ag_app)},
        }
        fixture.machine_path.write_text(yaml.safe_dump(fixture.machine, sort_keys=False))

        fixture.native('claude-code', value={'model': 'old'})
        fixture.native('codex', value={'model': 'old'})

        def run_real(argv):
            buf = io.StringIO()
            with patch('claude_config.core.manager.NativeProbe', return_value=lambda agent, exe, settings, ctx: [{'name': 'runtime_version', 'status': 'passed', 'version': '99.0.0'}]), \
                 redirect_stdout(buf):
                code = cli_entry.main(argv)
            return code, buf.getvalue()

        # Preview all three agents
        _, cc_out = run_real(['plan', '--machine', str(fixture.machine_path), '--agent', 'claude-code', '--format', 'json'])
        cc_plan_id = json.loads(cc_out)['plan_id']
        _, codex_out = run_real(['plan', '--machine', str(fixture.machine_path), '--agent', 'codex', '--format', 'json'])
        codex_plan_id = json.loads(codex_out)['plan_id']

        # Applying antigravity must NOT invalidate claude-code or codex plans
        ag_apply_code, _ = run_real(['apply', '--machine', str(fixture.machine_path), '--agent', 'antigravity', '--format', 'json'])
        self.assertEqual(ag_apply_code, 0)
        cc_apply_code, cc_apply_out = run_real(['apply', '--machine', str(fixture.machine_path), '--agent', 'claude-code',
                                                '--expect-plan-id', cc_plan_id[:12], '--format', 'json'])
        self.assertEqual(cc_apply_code, 0, cc_apply_out)

        # Mutate codex native file directly -> plan_stale explains native change in JSON and text!
        fixture.native('codex', value={'model': 'drifted-outside', 'SECRET_KEY': 'DO_NOT_LEAK_STALE'})
        stale_code, stale_text = run_real(['apply', '--machine', str(fixture.machine_path), '--agent', 'codex',
                                           '--expect-plan-id', codex_plan_id[:12], '--format', 'text'])
        self.assertEqual(stale_code, 1)
        self.assertIn('本机原生配置已变化', stale_text)
        self.assertIn('预期计划 ID：' + codex_plan_id[:12], stale_text)
        self.assertNotIn('DO_NOT_LEAK_STALE', stale_text)
        self.assertNotIn('{', stale_text.split('错误：plan_stale')[1].split('完整 JSON：')[0])

        # Re-preview codex, then mutate profile YAML -> explains source change
        _, codex_out2 = run_real(['plan', '--machine', str(fixture.machine_path), '--agent', 'codex', '--format', 'json'])
        codex_plan_id2 = json.loads(codex_out2)['plan_id']
        fixture.profile('codex', settings={'model': 'updated-in-repo'})
        stale_code2, stale_json2 = run_real(['apply', '--machine', str(fixture.machine_path), '--agent', 'codex',
                                             '--expect-plan-id', codex_plan_id2[:12], '--format', 'json'])
        self.assertEqual(stale_code2, 1)
        detail = json.loads(stale_json2)['errors'][0]['detail']
        self.assertIn('配置库声明（Profile / 资源来源）已变化', detail['differences'])


class Phase2ResolveTests(unittest.TestCase):
    def _setup_settings_drift(self):
        fixture = manager_fixtures.ManagerTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.addCleanup(fixture.tearDown)

        fixture.native('codex', value={'model': 'base'})
        probe_patch = patch('claude_config.core.manager.NativeProbe',
                            return_value=lambda agent, exe, settings, ctx: [{'name': 'runtime_version', 'status': 'passed', 'version': '99.0.0'}])
        probe_patch.start()
        self.addCleanup(probe_patch.stop)

        buf = io.StringIO()
        with redirect_stdout(buf):
            code = cli_entry.main(['apply', '--machine', str(fixture.machine_path), '--agent', 'codex', '--format', 'json'])
        self.assertEqual(code, 0)
        # Modify both profile and native differently so external drift triggers
        fixture.profile('codex', settings={'model': 'repo-model'})
        fixture.native('codex', value={'model': 'local-model', 'token': 'TOP_SECRET_LOCAL'})
        return fixture

    def _run_cli(self, argv):
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = cli_entry.main(argv)
        return code, buf.getvalue()

    def test_resolve_settings_drift_source_wins_keep_target_and_export(self):
        # 1) Preview only (--choose without --write) -> does not mutate native
        f1 = self._setup_settings_drift()
        code, prev_out = self._run_cli(['resolve', '--machine', str(f1.machine_path), '--agent', 'codex',
                                        '--choose', '/settings/model=source_wins', '--format', 'json'])
        self.assertEqual(code, 0, prev_out)
        self.assertTrue(json.loads(prev_out)['preview_only'])
        self.assertEqual(parse_native('codex',f1.native('codex').read_bytes())['model'], 'local-model')

        # 2) Execute source_wins (--write)
        code, sw_out = self._run_cli(['resolve', '--machine', str(f1.machine_path), '--agent', 'codex',
                                      '--choose', '/settings/model=source_wins', '--write', '--format', 'json'])
        self.assertEqual(code, 0, sw_out)
        self.assertFalse(json.loads(sw_out)['preview_only'])
        self.assertEqual(parse_native('codex',f1.native('codex').read_bytes())['model'], 'repo-model')
        self.assertEqual(parse_native('codex',f1.native('codex').read_bytes())['token'], 'TOP_SECRET_LOCAL')
        # Zero conflicts remain on next plan
        plan_code, plan_out = self._run_cli(['plan', '--machine', str(f1.machine_path), '--agent', 'codex', '--format', 'json'])
        self.assertEqual(plan_code, 0, plan_out)
        self.assertTrue(json.loads(plan_out)['ready'])

        # 3) Execute keep_target (--write) -> preserves local native model and updates managed baseline cleanly
        f2 = self._setup_settings_drift()
        code, kt_out = self._run_cli(['resolve', '--machine', str(f2.machine_path), '--agent', 'codex',
                                      '--choose', '/settings/model=keep_target', '--write', '--format', 'json'])
        self.assertEqual(code, 0, kt_out)
        self.assertEqual(parse_native('codex',f2.native('codex').read_bytes())['model'], 'local-model')

        # 4) Execute export (--write) -> writes local native model back into profile YAML
        f3 = self._setup_settings_drift()
        code, ex_out = self._run_cli(['resolve', '--machine', str(f3.machine_path), '--agent', 'codex',
                                      '--choose', '/settings/model=export', '--write', '--format', 'json'])
        self.assertEqual(code, 0, ex_out)
        profile_data = yaml.safe_load((f3.data / f3.machine['agents']['codex']['profile']).read_text())
        self.assertEqual(profile_data['settings']['model'], 'local-model')
        self.assertNotIn('token', profile_data['settings'])
        # Plan is now clean after export
        plan_code, plan_out = self._run_cli(['plan', '--machine', str(f3.machine_path), '--agent', 'codex', '--format', 'json'])
        self.assertEqual(plan_code, 0, plan_out)
        self.assertTrue(json.loads(plan_out)['ready'])

    def test_resolve_non_tty_without_choose_blocks_and_invalid_choose_rejected(self):
        fixture = self._setup_settings_drift()
        code, out = self._run_cli(['resolve', '--machine', str(fixture.machine_path), '--agent', 'codex', '--format', 'json'])
        self.assertEqual(code, 1)
        rep = json.loads(out)
        self.assertEqual(rep['errors'][0]['code'], 'unresolved_conflicts')
        self.assertIn('--choose /settings/model=source_wins', rep['errors'][0]['reason'])

        code_bad, out_bad = self._run_cli(['resolve', '--machine', str(fixture.machine_path), '--agent', 'codex',
                                           '--choose', '/settings/model=SECRET_ACTION', '--format', 'json'])
        self.assertEqual(code_bad, 1)
        self.assertEqual(json.loads(out_bad)['errors'][0]['code'], 'invalid_configuration')

    def test_resolve_interactive_tty_prompt_confirm_and_abort(self):
        fixture = self._setup_settings_drift()
        mock_tty = unittest.mock.Mock()
        mock_tty.isatty.return_value = True
        with patch('sys.stdin', mock_tty), patch('sys.stdout', io.StringIO()), \
             patch('builtins.input', side_effect=['1', 'n']):
            # Make stdout look like tty too
            with patch.object(sys.stdout, 'isatty', return_value=True):
                code = cli_entry.main(['resolve', '--machine', str(fixture.machine_path), '--agent', 'codex', '--format', 'text'])
        self.assertEqual(code, 0)
        # Aborted at y/N prompt -> native unchanged!
        self.assertEqual(parse_native('codex',fixture.native('codex').read_bytes())['model'], 'local-model')

        with patch('sys.stdin', mock_tty), patch('sys.stdout', io.StringIO()), \
             patch('builtins.input', side_effect=['1', 'y']):
            with patch.object(sys.stdout, 'isatty', return_value=True):
                code = cli_entry.main(['resolve', '--machine', str(fixture.machine_path), '--agent', 'codex', '--format', 'text'])
        self.assertEqual(code, 0)
        self.assertEqual(parse_native('codex',fixture.native('codex').read_bytes())['model'], 'repo-model')

    def test_resolve_resource_conflict_adopt_source_and_zero_conflict(self):
        fixture = conflict_fixtures.ConflictTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.addCleanup(fixture.tearDown)

        probe_patch = patch('claude_config.core.manager.NativeProbe',
                            return_value=lambda agent, exe, settings, ctx: [{'name': 'runtime_version', 'status': 'passed', 'version': '99.0.0'}])
        probe_patch.start()
        self.addCleanup(probe_patch.stop)

        skill_dir = fixture.skill()
        target = skill_dir / 'SKILL.md'
        target.write_text('local-edit')

        # 1) keep_local preserves local file and succeeds
        code_keep, out_keep = self._run_cli(['resolve', '--machine', str(fixture.machine_path), '--agent', 'claude-code',
                                             '--choose', '/resources/skills/tiny@test=keep_local',
                                             '--write', '--format', 'json'])
        self.assertEqual(code_keep, 0, out_keep)
        self.assertEqual(target.read_text(), 'local-edit')

        # 2) adopt_source restores managed body cleanly
        code, out = self._run_cli(['resolve', '--machine', str(fixture.machine_path), '--agent', 'claude-code',
                                   '--choose', '/resources/skills/tiny@test=adopt_source',
                                   '--write', '--format', 'json'])
        self.assertEqual(code, 0, out)
        self.assertIn('Original skill', target.read_text())

        # Zero-conflict run now returns clean exit 0
        code_clean, out_clean = self._run_cli(['resolve', '--machine', str(fixture.machine_path), '--agent', 'claude-code', '--format', 'json'])
        self.assertEqual(code_clean, 0, out_clean)
        self.assertEqual(json.loads(out_clean)['checks'][0]['name'], 'conflict_discovery')


if __name__ == '__main__':
    unittest.main()
