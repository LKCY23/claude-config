"""Human summaries stay compact without losing decisions or machine evidence."""
from contextlib import redirect_stdout
from copy import deepcopy
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from claude_config import profile as profile_config
from claude_config import cutover as cutover_config
from claude_config import migration as migration_config
from claude_config.core.merge import MergePlan
from claude_config.core.reporting import build_report, render_text, report_sections
from claude_config.core.report_viewer import ReportTree


class OutputContractTests(unittest.TestCase):
    def test_all_entrypoints_reject_removed_flag_and_honor_error_format(self):
        entries = [(profile_config, ['plan', '--machine', '/fixture/machine', '--agent', 'codex']),
                   (cutover_config, ['plan', '--spec', '/fixture/spec']),
                   (migration_config, ['inventory', '--data-dir', '/fixture/data'])]
        with tempfile.TemporaryDirectory() as directory:
            for module, argv in entries:
                for mode in ('text', 'json'):
                    with self.subTest(entry=module.__name__, mode=mode), \
                         patch.object(module, 'fallback_state_root', return_value=Path(directory)), \
                         patch('claude_config.profile.terminal_interactive', side_effect=lambda mode: mode == 'auto'), \
                         patch('claude_config.profile.show_report', side_effect=AssertionError('Unexpected viewer')):
                        output = io.StringIO()
                        with redirect_stdout(output):
                            code = module.main([*argv, '--no-input', '--format', mode])
                        self.assertEqual(code, 2)
                        if mode == 'json':
                            report = json.loads(output.getvalue())
                            self.assertEqual(report['errors'][0]['code'], 'invalid_arguments')
                        else:
                            self.assertIn('命令参数', output.getvalue())
                output = io.StringIO()
                with redirect_stdout(output), self.assertRaises(SystemExit) as result:
                    module.main(['--help'])
                self.assertEqual(result.exception.code, 0)
                self.assertNotIn('--no-input', output.getvalue())
                self.assertIn('--format', output.getvalue())

    def test_large_success_summary_keeps_exact_file_details_out_of_default_view(self):
        rows = [{'path': f'/fixture/resource/{index}', 'status': 'applied', 'owner': 'resource:example@test'} for index in range(1000)]
        report = build_report({'results': rows}, operation='apply', preview_only=False, execution_started=True)
        before = deepcopy(report)
        for brief in (False, True):
            text = render_text(report, brief=brief)
            self.assertLess(len(text.splitlines()), 25)
            self.assertIn('已修改 1000 项', text)
            self.assertNotIn('/fixture/resource/0', text)
        tree = ReportTree(report)
        details = next(node for node in tree.nodes if node.title.startswith('逐文件执行'))
        self.assertFalse(details.expanded)
        self.assertEqual(len(details.lines), 1000)
        self.assertEqual(report, before)

    def test_brief_failure_keeps_reason_and_every_unrecovered_path(self):
        report = build_report({'results': [
            {'path': '/fixture/restored', 'status': 'rolled_back', 'owner': 'agent-settings'},
            {'path': '/fixture/remaining', 'status': 'applied', 'owner': 'agent-settings'},
            {'path': '/fixture/interrupted', 'status': 'started', 'owner': 'native-plugins'}],
            'recovery': '/fixture/journal.json'}, operation='apply', preview_only=False, execution_started=True,
            error={'code': 'operation_failed', 'phase': 'execution', 'reason': '具体错误原因', 'exit_code': 2})
        text = render_text(report, brief=True)
        self.assertIn('具体错误原因', text)
        self.assertIn('未恢复：/fixture/remaining', text)
        self.assertIn('未恢复：/fixture/interrupted', text)
        self.assertIn('已恢复 1 项', text)
        self.assertNotIn('已修改', text)
        self.assertLess(text.index('执行失败'), text.index('检查：'))

    def test_repeated_unverified_checks_keep_subjects_ids_and_status_boundaries(self):
        report = build_report({'checks': [
            {'name': 'resource_runtime', 'status': 'not_verified', 'reason': '未运行', 'client': 'codex', 'ref': 'one@test'},
            {'name': 'resource_runtime', 'status': 'not_verified', 'reason': '未运行', 'client': 'claude-code', 'ref': 'two@test'},
            {'name': 'resource_runtime', 'status': 'warning', 'reason': '未运行', 'client': 'codex', 'ref': 'three@test'}]}, operation='plan')
        text = render_text(report)
        self.assertIn('K1、K2', text)
        self.assertIn('未验证（2 项）', text)
        self.assertIn('K3  资源实际运行：警告', text)
        for ref in ('one@test', 'two@test', 'three@test'):
            self.assertIn(ref, text)
        self.assertEqual(len(report['checks']), 3)
        brief = render_text(report, brief=True)
        self.assertIn('three@test', brief)
        self.assertIn('警告', brief)

    def test_file_grouping_preserves_runtime_fields_and_distinct_policies(self):
        raw = MergePlan({'settings': {'model': 'new'}}, {'settings': {'model': 'old'}}, source={}, destination={}).report()
        template = deepcopy(next(row for row in raw['decisions'] if row['path'] == ['settings', 'model']))
        for path, policy in [(['/tmp/a'], 'source_wins'), (['/tmp/b'], 'source_wins'),
                             (['/tmp/c'], 'keep_target'), (['options', 'enabled'], 'source_wins')]:
            row = {**deepcopy(template), 'path': ['clients', 'codex', 'resources', 'plugins', 'example@test', *path], 'policy': policy}
            raw['decisions'].append(row)
        report = build_report(raw, operation='plan')
        sections = report_sections(report)
        files = [s for s in sections if s[0].startswith('文件变更：')]
        self.assertEqual(len(files), 2)
        self.assertTrue(any('启用状态' in s[0] for s in sections))
        text = render_text(report)
        self.assertNotIn('/tmp/a', text)
        self.assertIn('当前："old" → 结果："new"', text)
        self.assertIn('保留本机值', text)
        tree = ReportTree(report)
        grouped = [node for node in tree.nodes if node.title.startswith('文件变更：')]
        self.assertTrue(any('/tmp/a' in line for node in grouped for child in node.children for line in child.lines))

    def test_multiple_decisions_at_one_path_are_all_explained(self):
        raw = MergePlan({'settings': {'model': 'new'}}, {'settings': {'model': 'old'}}, source={}, destination={}).report()
        extra = {**deepcopy(next(row for row in raw['decisions'] if row['path'] == ['settings', 'model'])), 'reason': '第二条处理原因', 'policy_origin': 'run_field'}
        raw['decisions'].append(extra)
        report = build_report(raw, operation='plan')
        text = render_text(report)
        self.assertIn('第二条处理原因', text)
        self.assertIn('本次命令的字段策略', text)

    def test_unconfigured_resource_options_are_concise_but_scope_and_values_remain(self):
        raw = MergePlan({}, {'enabled': True}, source={}, destination={}, scope=['/enabled']).report()
        for row in raw['decisions']:
            row['path'] = ['resources', 'plugins', 'example@test', *row['path']]
        raw['scope'] = [['settings', 'model']]
        raw['checks'] = [{'name': 'resource_runtime', 'ref': 'example@test', 'status': 'not_verified', 'reason': '未运行'}]
        report = build_report(raw, operation='plan')
        text = render_text(report)
        self.assertIn('检查范围：配置、资源 / example@test', text)
        self.assertIn('保留本机值 true', text)
        self.assertIn('同步配置未声明', text)


if __name__ == '__main__':
    unittest.main()
