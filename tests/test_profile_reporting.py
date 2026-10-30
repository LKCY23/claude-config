"""Report evidence, machine output, persistence and read-only terminal behavior."""
from contextlib import redirect_stdout
from copy import deepcopy
import io
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from claude_config import profile as profile_config
from claude_config.core.merge import MergePlan
from claude_config.core.reporting import build_report, encode_json, exit_code, render_text, save_report
from claude_config.core.report_viewer import ReportTree, show_report, terminal_interactive, wrapped_lines
from claude_config.core.transaction import FileChange, FilePlan, atomic_write
import test_profile_manager as fixtures


def sample(incoming=None, target=None, **kwargs):
    plan = MergePlan({'settings': incoming or {'model_reasoning_effort': 'medium'}},
                     {'settings': target or {'model_reasoning_effort': 'high'}},
                     source={'kind': 'profile'}, destination={'kind': 'native-config'})
    raw = plan.report()
    raw['ready'] = not raw['blockers']
    return build_report(raw, operation='plan', context={'agent_type': 'codex'}, **kwargs)


class ReportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
    def tearDown(self):
        self.temp.cleanup()

    def test_report_and_plan_id_have_distinct_lifetimes_and_input_is_unchanged(self):
        raw = MergePlan({'a': 1}, {'a': 2}, source={}, destination={}).report()
        before = deepcopy(raw)
        first = build_report(raw, operation='plan')
        second = build_report(raw, operation='validate')
        self.assertNotEqual(first['report_id'], second['report_id'])
        self.assertEqual(first['plan_id'], second['plan_id'])
        self.assertEqual(raw, before)

    def test_private_saved_json_is_exact_public_report(self):
        report = save_report(sample(), self.root / 'state')
        path = Path(report['artifacts']['json_path'])
        self.assertEqual(json.loads(path.read_text()), report)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)
        self.assertIn(str(path), render_text(report))
        self.assertEqual(exit_code(report), 0)

    def test_save_failure_does_not_hide_completed_write_or_fake_a_path(self):
        raw = {'ready': True, 'results': [{'path': '/isolated/settings.json', 'status': 'applied', 'owner': 'agent-settings'}]}
        report = build_report(raw, operation='apply', preview_only=False, execution_started=True)
        with patch('claude_config.core.reporting.atomic_write', side_effect=OSError('SENSITIVE')):
            save_report(report, self.root / 'state')
        self.assertIsNone(report['artifacts']['json_path'])
        self.assertFalse(report['artifacts']['saved'])
        self.assertEqual(report['execution']['status'], 'completed')
        self.assertEqual(report['summary']['applied'], 1)
        self.assertIn('执行完成', render_text(report))
        self.assertNotIn('SENSITIVE', json.dumps(report))
        self.assertEqual(exit_code(report), 2)

    def test_report_storage_refuses_symlink_ancestors_and_root(self):
        (self.root / 'elsewhere').mkdir()
        (self.root / 'link').symlink_to(self.root / 'elsewhere')
        for path in (self.root / 'link', self.root / 'link/child'):
            report = save_report(sample(), path)
            self.assertFalse(report['artifacts']['saved'])
        self.assertEqual(list((self.root / 'elsewhere').iterdir()), [])

    def test_structured_error_detail_renders_as_text(self):
        report = sample()
        report['errors'].append({'code': 'plan_stale', 'phase': 'recheck', 'reason': '请重新预览',
                                 'detail': {'phase': 'recheck', 'path': 'a/b', 'expected': 'x', 'actual': 'y'}})
        text = render_text(report)
        self.assertIn('检查详情', text)
        self.assertIn('"path": "a/b"', text)

    def test_diff_is_success_and_policy_origin_is_visible(self):
        report = sample()
        self.assertEqual(report['summary']['changes'], 1)
        self.assertEqual(exit_code(report), 0)
        text = render_text(report)
        for phrase in ('推理强度', '当前："high"', '结果："medium"', '程序默认策略', '未修改配置'):
            self.assertIn(phrase, text)

    def test_missing_null_false_and_zero_stay_distinct(self):
        raw = MergePlan({'settings': {'a': None, 'b': False, 'c': 0}}, {}, source={}, destination={}).report()
        report = build_report(raw, operation='plan')
        decisions = {row['path'][-1]: row for row in report['decisions']}
        for name, value in (('a', None), ('b', False), ('c', 0)):
            self.assertFalse(decisions[name]['before_exists'])
            self.assertTrue(decisions[name]['after_exists'])
            self.assertIs(decisions[name]['after'], value)
        self.assertEqual(report['summary']['changes'], 3)

    def test_array_replacement_reports_removed_items(self):
        text = render_text(sample({'list': ['new']}, {'list': ['old', 'keep']}))
        for word in ('整体替换列表', 'old', 'keep', 'new'):
            self.assertIn(word, text)

    def test_preserved_difference_is_explained_and_resolved_conflicts_are_counted(self):
        plan = MergePlan({'settings': {'model': 'source'}}, {'settings': {'model': 'local'}},
                         source={'kind': 'profile', 'file': 'profile.yaml'}, destination={'kind': 'native-config'},
                         profile_policy={'default': 'keep_target'})
        report = build_report(plan.report(), operation='plan')
        text = render_text(report)
        self.assertIn('当前："local"', text)
        self.assertIn('来源："source"', text)
        self.assertIn('Profile 的默认策略', text)
        self.assertNotIn('{"kind"', text)
        raw = {'ready': True, 'resource_conflicts': [{'path': ['resources', 'skills', 'x@y'],
                                                     'status': 'resolved_keep_local', 'eligible_actions': ['keep_local']}],
               'kept_resources': ['x@y']}
        report = build_report(raw, operation='plan')
        self.assertEqual(report['summary']['resolved_conflicts'], 1)
        self.assertEqual(report['summary']['unresolved_conflicts'], 0)
        self.assertIn('仍偏离同步配置', render_text(report))

    def test_failed_native_restoration_preserves_earlier_files(self):
        path = self.root / 'config'
        path.write_text('old')
        class NativeOperation:
            paths, directories = (), ()
            identifier = 'isolated-native'
            definition = {}
            root = self.root / 'native'
            def preflight(self): pass
            def capture(self, *args):
                return {'path': str(self.root), 'operation_id': self.identifier, 'kind': 'native-plugin', 'status': 'started'}
            def perform(self, *args): raise RuntimeError('fixture failure')
            def observe(self, *args): pass
            def restore(self, *args): raise RuntimeError('fixture recovery failure')
        plan = FilePlan([FileChange(path, b'new', 'config', lambda _: None)], native_operations=[NativeOperation()])
        with self.assertRaises(RuntimeError):
            plan.apply(self.root / 'state')
        self.assertEqual(path.read_text(), 'new')
        self.assertEqual([row['status'] for row in plan.report['results']], ['recovery_conflict', 'recovery_conflict'])

    def test_persistent_journal_save_failure_keeps_live_recovery_evidence(self):
        path = self.root / 'config'
        path.write_text('old')
        plan = FilePlan([FileChange(path, b'new', 'config', lambda _: None)])
        failed = False
        def journal_failure(location, data, mode=0o600):
            nonlocal failed
            if Path(location).name == 'journal.json':
                value = json.loads(data)
                failed |= any(row['status'] == 'applied' for row in value['operations'])
                if failed:
                    raise OSError('fixture journal failure')
            atomic_write(location, data, mode)
        with patch('claude_config.core.transaction.atomic_write', side_effect=journal_failure):
            with self.assertRaises(OSError):
                plan.apply(self.root / 'state')
        self.assertEqual(path.read_text(), 'old')
        self.assertEqual(plan.report['results'][0]['status'], 'rolled_back')

    def test_initial_journal_save_failure_never_claims_a_saved_recovery_path(self):
        path = self.root / 'config'
        path.write_text('old')
        plan = FilePlan([FileChange(path, b'new', 'config', lambda _: None)])
        with patch('claude_config.core.transaction.atomic_write', side_effect=OSError('fixture storage failure')):
            with self.assertRaises(OSError):
                plan.apply(self.root / 'state')
        self.assertIsNone(plan.report['recovery'])
        self.assertEqual(path.read_text(), 'old')

    def test_conflict_is_not_double_counted_by_related_blockers(self):
        merge = MergePlan({'x': 'source'}, {'x': 'local'}, source={}, destination={},
                          run_policy={'default': 'fail_if_different'})
        raw = merge.report()
        raw['blockers'].append(deepcopy(raw['blockers'][0]))
        raw['ready'] = False
        report = build_report(raw, operation='plan')
        self.assertEqual(report['summary']['conflicts'], 1)
        self.assertEqual(report['summary']['changes'], 0)
        self.assertTrue(all(row['conflict_ids'] == ['C1'] for row in report['blockers']))
        self.assertEqual(exit_code(report), 1)

    def test_review_success_does_not_mean_deployment_ready_or_material_generated(self):
        raw = {'ready': True, 'deployment_ready': False, 'deployment_blockers': [{'code': 'resource_drift'}],
               'review_directories': ['/private/not-created']}
        report = build_report(raw, operation='review-conflicts')
        self.assertTrue(report['readiness']['operation_ready'])
        self.assertFalse(report['readiness']['deployment_ready'])
        self.assertEqual(report['artifacts']['review_directories'], [])
        self.assertEqual(exit_code(report), 0)
        self.assertIn('部署仍被阻断', render_text(report))

    def test_partial_recovery_and_successful_recovery_are_distinct(self):
        error = {'code': 'operation_failed', 'phase': 'execution', 'reason': 'Failed', 'exit_code': 2}
        for state, expected in (('rolled_back', 'completed'), ('recovery_conflict', 'failed')):
            report = build_report({'ready': True, 'results': [{'path': '/isolated/config', 'status': state}],
                                   'recovery': '/isolated/journal.json'}, operation='apply', error=error,
                                  execution_started=True, preview_only=False)
            self.assertEqual(report['recovery']['status'], expected)
            self.assertEqual(exit_code(report), 2)
            self.assertNotIn('执行完成', render_text(report))
            self.assertIn('已恢复' if state == 'rolled_back' else '未恢复', render_text(report))

    def test_secrets_and_terminal_escape_injection_do_not_leak(self):
        report = build_report({'ready': True, 'decisions': [], 'checks': [],
                               'extra': {'api_key': 'PRIVATE', 'authorization': 'BEARER'},
                               'results': [{'path': '/test/\x1b[2J', 'status': 'applied', 'definition': {'secret': 'PRIVATE'}}]},
                              operation='apply', preview_only=False, execution_started=True)
        encoded = json.dumps(report)
        self.assertNotIn('PRIVATE', encoded)
        self.assertNotIn('BEARER', encoded)
        self.assertNotIn('\x1b', render_text(report))
        self.assertNotIn('definition', report['results'][0])

    def test_json_preserves_exact_control_character_paths_without_terminal_controls(self):
        path = '/isolated/\x1b[2J\x9bfile'
        report = build_report({'ready': True, 'results': [{'path': path, 'status': 'applied'}]}, operation='apply',
                              preview_only=False, execution_started=True)
        encoded = encode_json(report)
        self.assertNotIn('\x1b', encoded)
        self.assertNotIn('\x9b', encoded)
        self.assertEqual(json.loads(encoded)['results'][0]['path'], path)
        self.assertNotIn('\x1b', render_text(report))

    def test_viewer_navigation_and_wide_character_wrapping(self):
        report = sample()
        tree = ReportTree(report)
        self.assertFalse(tree.nodes[1].expanded)
        self.assertTrue(tree.nodes[0].expanded)
        tree.command('left', 30, 8)
        tree.command('down', 30, 8)
        tree.command(' ', 30, 8)
        self.assertTrue(tree.nodes[1].expanded)
        self.assertTrue(any('当前' in row[1] for row in tree.rows(30)))
        for _ in range(20):
            tree.command('down', 30, 4)
            self.assertLessEqual(len(tree.visible(30, 4)), 4)
        tree.visible(18, 2)
        self.assertFalse(tree.command('q', 18, 2))
        self.assertEqual(wrapped_lines('中文ab', 4), ['中文', 'ab'])
        self.assertEqual(wrapped_lines('e\u0301ab', 2), ['e\u0301a', 'b'])

    def test_noninteractive_and_explicit_formats_never_read_keys(self):
        tty, pipe = unittest.mock.Mock(), unittest.mock.Mock()
        tty.isatty.return_value = True
        pipe.isatty.return_value = False
        with patch.dict(os.environ, {'TERM': 'tmux-256color', 'TMUX': 'anything'}):
            self.assertTrue(terminal_interactive(stdin=tty, stdout=tty))
            for mode, source, target in (('json', tty, tty), ('text', tty, tty),
                                         ('auto', pipe, tty), ('auto', tty, pipe)):
                self.assertFalse(terminal_interactive(mode, stdin=source, stdout=target))
        with patch.dict(os.environ, {'TERM': 'dumb'}):
            self.assertFalse(terminal_interactive(stdin=tty, stdout=tty))

    def test_viewer_setup_failure_falls_back_and_ctrl_c_only_leaves_reader(self):
        import curses
        with patch('curses.wrapper', side_effect=curses.error('Unavailable')):
            self.assertFalse(show_report(sample()))
        with patch('curses.wrapper', side_effect=KeyboardInterrupt):
            self.assertTrue(show_report(sample()))


class ReportCliTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.ManagerTests()
        self.fixture.setUp()
        self.args = ['plan', '--machine', str(self.fixture.machine_path), '--agent', 'claude-code']
    def tearDown(self):
        self.fixture.tearDown()

    def invoke(self, args, manager=None, **kwargs):
        output = io.StringIO()
        with patch('claude_config.profile.ProfileManager', return_value=manager or self.fixture.manager()), \
             patch('claude_config.profile.fallback_state_root', return_value=self.fixture.root / 'fallback'), redirect_stdout(output):
            code = profile_config.main(args)
        return code, output.getvalue()

    def test_default_static_and_explicit_json_match_saved_report(self):
        self.fixture.native(value={'model': 'old'})
        for extra in ([], ['--format', 'text'], ['--format', 'json']):
            code, output = self.invoke(self.args + extra)
            self.assertEqual(code, 0, output)
            if extra == ['--format', 'json']:
                report = json.loads(output)
                self.assertEqual(json.loads(Path(report['artifacts']['json_path']).read_text()), report)
            else:
                self.assertIn('完整 JSON：', output)
                self.assertIn('默认模型', output)
        self.assertEqual(json.loads(self.fixture.native().read_text())['model'], 'old')
        self.assertFalse(self.fixture.manager().state_path.exists())

    def test_interactive_quit_leaves_static_summary_and_never_reexecutes(self):
        with patch('claude_config.profile.terminal_interactive', return_value=True), patch('claude_config.profile.show_report', return_value=True) as viewer:
            code, output = self.invoke(self.args)
        self.assertEqual(code, 0)
        viewer.assert_called_once()
        self.assertIn('完整 JSON：', output)
        self.assertNotIn('D1  ', output)

    def test_schema_error_and_yaml_parser_error_have_saved_reports(self):
        path = self.fixture.profile(settings={'not_a_field': 'SECRET_VALUE'})
        code, output = self.invoke(self.args + ['--format', 'json'])
        report = json.loads(output)
        self.assertEqual(code, 1)
        self.assertIn('not_a_field', report['errors'][0]['detail'])
        self.assertNotIn('SECRET_VALUE', output)
        self.assertTrue(Path(report['artifacts']['json_path']).exists())
        path.write_text('settings: [\nSECRET_VALUE:')
        code, output = self.invoke(self.args + ['--format', 'json'])
        self.assertEqual(code, 1)
        self.assertNotIn('SECRET_VALUE', output)
        self.assertTrue(json.loads(output)['artifacts']['saved'])

    def test_argument_errors_use_machine_json_and_isolated_fallback(self):
        code, output = self.invoke(['apply', '--format=json', '--unexpected-secret=PRIVATE'])
        report = json.loads(output)
        self.assertEqual(code, 2)
        self.assertIsNone(report['plan_id'])
        self.assertNotIn('PRIVATE', output)
        self.assertTrue(report['artifacts']['saved'])
        self.assertIn('fallback', report['artifacts']['json_path'])

    def test_apply_result_and_report_save_failure_do_not_undo_success(self):
        self.fixture.native(value={'model': 'old'})
        with patch('claude_config.core.reporting.atomic_write', side_effect=OSError('full disk')):
            code, output = self.invoke(['apply', *self.args[1:], '--format', 'json'])
        report = json.loads(output)
        self.assertEqual(code, 2)
        self.assertEqual(report['execution']['status'], 'completed')
        self.assertFalse(report['artifacts']['saved'])
        self.assertEqual(json.loads(self.fixture.native().read_text())['model'], 'selected')

    def test_interrupt_during_write_restores_completed_write_and_saves_report(self):
        first, second = self.fixture.root / 'first', self.fixture.root / 'second'
        first.write_text('old')
        second.write_text('old')
        plan = FilePlan([FileChange(path, b'new', 'config', lambda _: None) for path in (first, second)],
                        report={'checks': [], 'decisions': [], 'blockers': [], 'ready': True})
        manager = self.fixture.manager()
        manager.plan = lambda **kwargs: plan
        def interrupted_write(path, data, mode=0o600):
            if Path(path) == second:
                raise KeyboardInterrupt
            return atomic_write(path, data, mode)
        plan_apply = plan.apply
        plan.apply = lambda state: plan_apply(state, writer=interrupted_write)
        code, output = self.invoke(['apply', *self.args[1:], '--format', 'json'], manager=manager)
        report = json.loads(output)
        self.assertEqual(code, 130)
        self.assertEqual(first.read_text(), 'old')
        self.assertEqual(second.read_text(), 'old')
        self.assertEqual(report['recovery']['status'], 'completed')
        self.assertTrue(report['artifacts']['saved'])


@unittest.skipUnless(shutil.which('tmux'), 'tmux is not installed')
class TmuxReportTests(unittest.TestCase):
    def test_actual_tmux_expand_resize_quit_and_saved_summary(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            script = root / 'viewer.py'
            scripts = Path(__file__).resolve().parents[1] / 'src'
            script.write_text('import sys, time\nfrom pathlib import Path\n'
                              + f'sys.path.insert(0, {str(scripts)!r})\n'
                              + 'from claude_config.core.merge import MergePlan\n'
                              + 'from claude_config.core.reporting import build_report, save_report\n'
                              + 'from claude_config.profile import output_report\n'
                              + 'p=MergePlan({"settings":{"model":"new"}}, {"settings":{"model":"old"}}, source={}, destination={})\n'
                              + 'r=build_report(p.report(), operation="plan", context={"agent_type":"codex"})\n'
                              + f'save_report(r, {str(root / "state")!r})\n'
                              + 'code=output_report(r,"auto")\n'
                              + f'Path({str(root / "done.tmp")!r}).write_text(str(code))\n'
                              + f'Path({str(root / "done.tmp")!r}).replace({str(root / "done")!r})\n'
                              + 'time.sleep(10)\n')
            socket = 'cc-report-' + root.name
            command = ['tmux', '-L', socket, '-f', '/dev/null']
            def tmux(*args):
                return subprocess.run([*command, *args], capture_output=True, text=True, check=True).stdout
            def wait_for(predicate):
                deadline = time.monotonic() + 6
                while time.monotonic() < deadline:
                    if predicate():
                        return
                    time.sleep(.05)
                self.fail('tmux viewer did not reach expected state')
            try:
                tmux('new-session', '-d', '-s', 'report', '-x', '90', '-y', '25', shlex.join([sys.executable, str(script)]))
                wait_for(lambda: '只读查看' in tmux('capture-pane', '-p', '-t', 'report'))
                tmux('send-keys', '-t', 'report', 'Left', 'Down', 'Space')
                wait_for(lambda: '当前：' in tmux('capture-pane', '-p', '-t', 'report'))
                tmux('resize-window', '-t', 'report', '-x', '34', '-y', '14')
                wait_for(lambda: '只读查看' in tmux('capture-pane', '-p', '-t', 'report'))
                tmux('send-keys', '-t', 'report', 'q')
                wait_for(lambda: (root / 'done').exists())
                self.assertEqual((root / 'done').read_text(), '0')
                self.assertIn('完整 JSON', tmux('capture-pane', '-p', '-S', '-200', '-t', 'report'))
                self.assertEqual(len(list((root / 'state/reports').glob('*.json'))), 1)
            finally:
                subprocess.run([*command, 'kill-server'], capture_output=True)


if __name__ == '__main__':
    unittest.main()
