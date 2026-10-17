"""Terminal theme fallback, cell bounds and compact report layout."""
from copy import deepcopy
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
import unicodedata

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from claude_config.core.reporting import build_report
from claude_config.core.report_viewer import ReportTree, draw_report, header_rows, terminal_styles


class Colors:
    A_BOLD, A_REVERSE = 1, 2
    COLOR_CYAN, COLOR_GREEN, COLOR_YELLOW, COLOR_RED = 6, 2, 3, 1
    class error(Exception):
        pass
    def __init__(self, supported=True, failure=False):
        self.supported, self.failure, self.pairs = supported, failure, []
    def has_colors(self): return self.supported
    def start_color(self): pass
    def use_default_colors(self):
        if self.failure:
            raise self.error('unsupported default background')
    def init_pair(self, *args): self.pairs.append(args)
    def color_pair(self, number): return number * 16


class Screen:
    def __init__(self, height, width):
        self.height, self.width, self.calls = height, width, []
    def getmaxyx(self): return self.height, self.width
    def erase(self): self.calls = []
    def refresh(self): pass
    def addstr(self, y, x, text, style):
        cells = sum(0 if unicodedata.combining(c) else 2 if unicodedata.east_asian_width(c) in ('W', 'F') else 1 for c in text)
        assert 0 <= y < self.height
        assert x == 0 and cells < self.width, (text, cells, self.width)
        self.calls.append((y, text, style))


def report():
    return build_report({'checks': [
        {'name': 'file_preflight', 'status': 'passed'},
        {'name': 'resource_runtime', 'status': 'not_verified', 'client': 'codex', 'ref': 'example@test', 'reason': '资源未实际运行'}],
        'scope': [['settings', 'model']]}, operation='plan', context={'agent_type': 'codex', 'profile': 'profiles/agents/codex/current.yaml'})


class ViewerStyleTests(unittest.TestCase):
    def test_default_background_and_visible_monochrome_selection(self):
        with patch.dict(os.environ, {}, clear=True):
            colors = Colors()
            styles = terminal_styles(colors)
            self.assertTrue(all(background == -1 for _, _, background in colors.pairs))
            self.assertNotEqual(styles['success'], styles['error'])
            for colors in (Colors(supported=False), Colors(failure=True)):
                mono = terminal_styles(colors)
                self.assertEqual(mono['selected'], Colors.A_REVERSE | Colors.A_BOLD)
                self.assertEqual(mono['title'], Colors.A_BOLD)
        with patch.dict(os.environ, {'NO_COLOR': '1'}):
            colors = Colors()
            self.assertEqual(terminal_styles(colors)['selected'], Colors.A_REVERSE | Colors.A_BOLD)
            self.assertEqual(colors.pairs, [])

    def test_zero_difference_initial_view_has_context_and_attention(self):
        tree = ReportTree(report())
        self.assertTrue(tree.nodes[0].expanded)
        self.assertTrue(any(node.expanded and node.tone == 'warning' for node in tree.nodes))
        initial = '\n'.join(text for _, text in tree.rows(77))
        self.assertIn('未验证', initial)
        self.assertNotIn('profiles/agents/codex/current.yaml', initial)
        tree.nodes[0].children[0].expanded = True
        self.assertIn('profiles/agents/codex/current.yaml', '\n'.join(text for _, text in tree.rows(77)))

    def test_styles_use_status_not_config_text_and_zero_failures_are_not_red(self):
        value = report()
        rows = header_rows(value, 79, 28)
        self.assertFalse(any(tone == 'error' for _, tone in rows))
        self.assertTrue(any(tone == 'warning' for _, tone in rows))
        self.assertTrue(any(tone == 'success' for _, tone in rows))
        value['status'] = 'blocked'
        self.assertTrue(any(tone == 'error' for _, tone in header_rows(value, 79, 28)))

    def test_cell_bounds_selection_bar_and_resize_leave_report_unchanged(self):
        value = report()
        original = deepcopy(value)
        tree = ReportTree(value)
        styles = terminal_styles(Colors(supported=False))
        for height, width in ((30, 80), (22, 40), (14, 24), (6, 18), (30, 80)):
            screen = Screen(height, width)
            content_width, content_height = draw_report(screen, value, tree, styles)
            self.assertGreater(content_width, 0)
            self.assertGreater(content_height, 0)
            if height < 8 or width < 24:
                self.assertTrue(any('窗口太小' in text for _, text, _ in screen.calls))
            else:
                self.assertTrue(any(text == ' ' * (width - 1) and style == styles['selected'] for _, text, style in screen.calls))
                self.assertTrue(any(text.startswith('> ') and style == styles['selected'] for _, text, style in screen.calls))
        self.assertEqual(value, original)

    def test_short_report_footer_follows_content(self):
        value = report()
        screen = Screen(60, 100)
        draw_report(screen, value, ReportTree(value), terminal_styles(Colors(supported=False)))
        self.assertLess(max(y for y, _, _ in screen.calls), 40)

    def test_resolved_conflicts_do_not_look_like_blocking_errors(self):
        for status, tone in (('resolved_adopt_source', 'success'), ('resolved_keep_local', 'warning'), ('unresolved', 'error')):
            value = build_report({'resource_conflicts': [{'path': ['resources', 'skills', 'example@test'], 'status': status}]}, operation='plan')
            node = next(node for node in ReportTree(value).nodes if node.title.startswith('C1'))
            self.assertEqual(node.tone, tone)


@unittest.skipUnless(shutil.which('tmux'), 'tmux is not installed')
class StyledTmuxTests(unittest.TestCase):
    def test_colors_selection_and_shrink_grow_in_real_tmux(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            scripts = Path(__file__).resolve().parents[1] / 'src'
            value = report()
            (root / 'report.json').write_text(json.dumps(value))
            script = root / 'viewer.py'
            script.write_text('import json, sys, time\nfrom pathlib import Path\n'
                              + f'sys.path.insert(0, {str(scripts)!r})\n'
                              + 'from claude_config.profile import output_report\nfrom claude_config.core.reporting import save_report\n'
                              + f'r=json.loads(Path({str(root / "report.json")!r}).read_text())\n'
                              + f'save_report(r, {str(root / "state")!r})\n'
                              + 'code=output_report(r,"auto")\n'
                              + f'Path({str(root / "done")!r}).write_text(str(code))\n'
                              + 'time.sleep(10)\n')
            command = ['tmux', '-L', 'cc-style-' + root.name, '-f', '/dev/null']
            def tmux(*args):
                return subprocess.run([*command, *args], capture_output=True, text=True, check=True).stdout
            def wait_for(predicate):
                deadline = time.monotonic() + 6
                while time.monotonic() < deadline:
                    if predicate():
                        return
                    time.sleep(.05)
                self.fail('Styled tmux viewer did not reach expected state:\n' + tmux('capture-pane', '-p', '-t', 'report'))
            try:
                tmux('new-session', '-d', '-s', 'report', '-x', '80', '-y', '30',
                     shlex.join(['env', '-u', 'NO_COLOR', sys.executable, str(script)]))
                wait_for(lambda: '注意事项' in tmux('capture-pane', '-p', '-t', 'report'))
                colored = tmux('capture-pane', '-p', '-e', '-t', 'report')
                parameters = [part for code in re.findall(r'\x1b\[([0-9;]*)m', colored) for part in code.split(';')]
                self.assertIn('7', parameters)  # Reverse-video selection, not just a marker.
                self.assertTrue({'32', '33', '36'} <= set(parameters), repr(colored))
                self.assertIn('> ▼ 检查对象', tmux('capture-pane', '-p', '-t', 'report'))
                for width, height in ((40, 22), (24, 14), (18, 6), (80, 30)):
                    tmux('resize-window', '-t', 'report', '-x', str(width), '-y', str(height))
                    text = '窗口太小' if width < 24 else '只读查看'
                    wait_for(lambda: text in tmux('capture-pane', '-p', '-t', 'report'))
                    self.assertFalse((root / 'done').exists())
                wait_for(lambda: '注意事项' in tmux('capture-pane', '-p', '-t', 'report'))
                tmux('send-keys', '-t', 'report', 'Left')
                wait_for(lambda: '> ▶ 检查对象' in tmux('capture-pane', '-p', '-t', 'report'))
                tmux('send-keys', '-t', 'report', 'Right')
                wait_for(lambda: '> ▼ 检查对象' in tmux('capture-pane', '-p', '-t', 'report'))
                tmux('send-keys', '-t', 'report', 'q')
                wait_for(lambda: (root / 'done').exists())
                self.assertEqual((root / 'done').read_text(), '0')
                self.assertEqual(len(list((root / 'state/reports').glob('*.json'))), 1)
                self.assertIn('完整 JSON', tmux('capture-pane', '-p', '-S', '-200', '-t', 'report'))
            finally:
                subprocess.run([*command, 'kill-server'], capture_output=True)


if __name__ == '__main__':
    unittest.main()
