"""Tests for unified terminal UI primitives, static ANSI/input hygiene, and PTY progress/prompt behavior."""
import ast
import os
from pathlib import Path
import pty
import select
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from claude_config.core.terminal_ui import (
    badge,
    color_enabled,
    display_width,
    heading,
    meta_line,
    next_steps,
    pad_display,
    rule,
    section,
    strip_ansi,
    style,
    truncate_display,
)
from claude_config.data_cmd import render_data_repo_lines
from claude_config.plugin import summary as plugin_summary
from claude_config.profiles_cmd import render_matrix
from claude_config.status_cmd import render_status


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_PKG = REPO_ROOT / 'src' / 'claude_config'
ANSI_ALLOWLIST = {
    'core/terminal_ui.py',
    'core/reporting.py',
    'core/report_viewer.py',
    'core/execution_context.py',
}
INPUT_ALLOWLIST = {
    'core/execution_context.py',
}


def _read_pty_until(master_fd, needle, *, timeout=5.0):
    deadline = time.monotonic() + timeout
    buf = bytearray()
    needle_b = needle.encode('utf-8') if isinstance(needle, str) else needle
    while time.monotonic() < deadline:
        r, _, _ = select.select([master_fd], [], [], 0.05)
        if master_fd in r:
            try:
                chunk = os.read(master_fd, 4096)
            except OSError:
                break
            if not chunk:
                break
            buf.extend(chunk)
            if needle_b in buf:
                return buf.decode('utf-8', errors='replace')
    return buf.decode('utf-8', errors='replace')


def _drain_pty(master_fd, proc, *, timeout=5.0):
    deadline = time.monotonic() + timeout
    buf = bytearray()
    while time.monotonic() < deadline:
        r, _, _ = select.select([master_fd], [], [], 0.05)
        if master_fd in r:
            try:
                chunk = os.read(master_fd, 4096)
            except OSError:
                break
            if not chunk:
                break
            buf.extend(chunk)
        elif proc.poll() is not None:
            # Drain any trailing buffered bytes once child exits.
            while True:
                r2, _, _ = select.select([master_fd], [], [], 0.02)
                if master_fd not in r2:
                    break
                try:
                    chunk = os.read(master_fd, 4096)
                except OSError:
                    chunk = b''
                if not chunk:
                    break
                buf.extend(chunk)
            break
    return buf.decode('utf-8', errors='replace')


class TerminalUIContractTests(unittest.TestCase):
    def test_static_no_hardcoded_ansi_or_bare_input_outside_core(self):
        ansi_violations = []
        input_violations = []
        for py_path in sorted(SRC_PKG.rglob('*.py')):
            rel = py_path.relative_to(SRC_PKG).as_posix()
            text = py_path.read_text(encoding='utf-8')
            if rel not in ANSI_ALLOWLIST:
                for idx, line in enumerate(text.splitlines(), 1):
                    if any(tok in line for tok in ('_ANSI_', '\\033[', '\\x1b[', '\033[')):
                        ansi_violations.append(f'{rel}:{idx}: {line.strip()}')
            if rel not in INPUT_ALLOWLIST:
                tree = ast.parse(text, filename=str(py_path))
                for node in ast.walk(tree):
                    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == 'input':
                        input_violations.append(f'{rel}:{node.lineno}')
        self.assertEqual(ansi_violations, [], 'Found hardcoded ANSI outside core allowlist:\n' + '\n'.join(ansi_violations))
        self.assertEqual(input_violations, [], 'Found bare input() outside execution_context.py:\n' + '\n'.join(input_violations))

    def test_display_width_padding_and_truncation_with_cjk_and_ansi(self):
        plain = '配置A'
        colored = style(plain, 'title', color=True)
        self.assertEqual(strip_ansi(colored), plain)
        self.assertEqual(display_width(plain), 5)
        self.assertEqual(display_width(colored), 5)

        padded = pad_display(colored, 8)
        self.assertEqual(display_width(padded), 8)
        self.assertTrue(padded.endswith('   '))

        truncated = truncate_display('配置状态很长很长的文本', 8)
        self.assertLessEqual(display_width(truncated), 8)
        self.assertTrue(truncated.endswith('…'))

    def test_semantic_primitives_respect_color_and_no_color(self):
        with patch.dict(os.environ, {'NO_COLOR': '1'}):
            class FakeTTY:
                def isatty(self):
                    return True
            self.assertFalse(color_enabled('auto', stdout=FakeTTY()))
        self.assertFalse(color_enabled('never'))
        self.assertTrue(color_enabled('always'))

        self.assertEqual(badge('success', '已同步', color=False), '✓ 已同步')
        self.assertIn('\x1b[', badge('success', '已同步', color=True))
        self.assertEqual(heading('标题', color=False), '标题')
        self.assertEqual(section('区块：', color=False), '区块：')
        self.assertEqual(meta_line('Machine', '/tmp/m.yaml', color=False), '  Machine: /tmp/m.yaml')
        self.assertGreaterEqual(len(rule(20, color=False)), 20)
        self.assertEqual(
            next_steps(['claude-config status', 'claude-config status', 'claude-config sync'], color=False),
            ['', '建议下一步：', '  claude-config status', '  claude-config sync'],
        )

    def test_unified_renderers_produce_clean_output_plain_and_colored(self):
        status_report = {
            'machine_id': 'm1',
            'machine_file': '/tmp/machine.yaml',
            'mismatches': [],
            'data_repo': {'is_git': True, 'branch': 'main', 'upstream': 'origin/main', 'head': 'abc1234', 'state': 'clean'},
            'baselines': {'aligned': True, 'groups': [{'aliases': ['official'], 'catalog_commit': '12345678', 'cached_commit': '12345678', 'needs_sync': False}]},
            'targets': [{'name': 'claude-code', 'kind': 'agent', 'profile': 'profiles/agents/claude-code/a.yaml', 'state': 'synced', 'logical_changes': {}}],
        }
        plain = render_status(status_report, color=False)
        colored = render_status(status_report, color=True)
        self.assertIn('✓ 已同步', plain)
        self.assertEqual(strip_ansi(colored), plain)

        lines = render_data_repo_lines(status_report['data_repo'], color=False)
        self.assertEqual(len(lines), 1)

        matrix = render_matrix({'machine_id': 'm1', 'config_dir': '/tmp/cfg', 'agents': {}}, color=False)
        self.assertIn('Profile 矩阵总览 (m1)', matrix)

        plug = plugin_summary({
            'plugins': [{'resource': 'p@cat', 'installed_version': '1.0.0', 'selected_version': '1.0.0',
                         'pin_source': 'catalog', 'desired_enabled': True, 'enabled': True, 'status': '已同步'}],
        }, color=False)
        self.assertIn('p@cat', plug)

    def test_pty_single_line_progress_and_prompt_input_pauses_heartbeat(self):
        script = (
            "import sys, time\n"
            "from claude_config.core.execution_context import progress_output, progress, prompt_input\n"
            "with progress_output(sys.stderr, heartbeat_interval=0.05):\n"
            "    progress('扫描配置')\n"
            "    time.sleep(0.16)\n"
            "    progress('检查计划：50/200')\n"
            "    time.sleep(0.08)\n"
            "    ans = prompt_input('请选择 [1-4]: ')\n"
            "    print('CHOICE=' + ans)\n"
        )
        master_fd, slave_fd = pty.openpty()
        env = dict(os.environ)
        env['TERM'] = 'xterm-256color'
        env['PYTHONPATH'] = str(REPO_ROOT / 'src') + os.pathsep + env.get('PYTHONPATH', '')
        proc = subprocess.Popen(
            [sys.executable, '-c', script],
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            env=env,
            close_fds=True,
            start_new_session=True,
        )
        os.close(slave_fd)
        try:
            before_input = _read_pty_until(master_fd, '请选择 [1-4]: ', timeout=5.0)
            self.assertIn('\r\033[2K', before_input)
            self.assertIn('仍在处理', before_input)
            self.assertIn('请选择 [1-4]: ', before_input)
            # Wait long enough for several 0.05s heartbeats if pausing were broken.
            time.sleep(0.25)
            os.write(master_fd, b'2\n')
            after_input = _drain_pty(master_fd, proc, timeout=5.0)
            self.assertEqual(proc.wait(timeout=5.0), 0)
        finally:
            os.close(master_fd)
            if proc.poll() is None:
                proc.kill()
                proc.wait()

        # No heartbeat line may appear while awaiting user input.
        self.assertNotIn('仍在处理', after_input, f'Heartbeat leaked during prompt_input:\n{before_input + after_input!r}')
        self.assertNotIn('仍在运行', after_input, f'Heartbeat leaked during prompt_input:\n{before_input + after_input!r}')
        self.assertIn('CHOICE=2', before_input + after_input)

    def test_pty_resolve_interactive_pauses_heartbeat_end_to_end(self):
        isolated_home = tempfile.mkdtemp(prefix='cc-pty-home-')
        self.addCleanup(shutil.rmtree, isolated_home, True)
        script = """
import contextlib
import io
from pathlib import Path
from unittest.mock import patch
import claude_config.core.execution_context as ec

orig_po = ec.progress_output
def fast_po(stream, *, heartbeat_interval=5.0):
    return orig_po(stream, heartbeat_interval=0.05)
ec.progress_output = fast_po

import claude_config.resolve as resolve_mod
from claude_config import cli as cli_entry
import test_profile_manager as manager_fixtures

resolve_mod.progress_output = fast_po

fixture = manager_fixtures.ManagerTests()
fixture.setUp()
try:
    fixture.native('codex', value={'model': 'base'})
    with patch('claude_config.core.manager.NativeProbe',
               return_value=lambda agent, exe, settings, ctx: [{'name': 'runtime_version', 'status': 'passed', 'version': '99.0.0'}]):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc_apply = cli_entry.main(['apply', '--machine', str(fixture.machine_path), '--agent', 'codex', '--format', 'json'])
        assert rc_apply == 0, buf.getvalue()
        fixture.profile('codex', settings={'model': 'repo-model'})
        fixture.native('codex', value={'model': 'local-model'})
        rc = resolve_mod._entry(['--machine', str(fixture.machine_path), '--agent', 'codex'])
        print('EXIT=' + str(rc))
finally:
    fixture.doCleanups()
    fixture.tearDown()
"""
        master_fd, slave_fd = pty.openpty()
        env = dict(os.environ)
        env['TERM'] = 'xterm-256color'
        env['HOME'] = isolated_home
        env['PYTHONPATH'] = f"{REPO_ROOT / 'src'}{os.pathsep}{REPO_ROOT / 'tests'}{os.pathsep}{env.get('PYTHONPATH', '')}"
        proc = subprocess.Popen(
            [sys.executable, '-c', script],
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            env=env,
            close_fds=True,
            start_new_session=True,
        )
        os.close(slave_fd)
        try:
            prompt_out = _read_pty_until(master_fd, '请选择 [1-4] (默认 4): ', timeout=8.0)
            self.assertIn('请选择 [1-4] (默认 4): ', prompt_out)
            # Dwell >4x heartbeat interval (0.05s) without typing.
            time.sleep(0.25)
            os.write(master_fd, b'1\n')
            confirm_out = _read_pty_until(master_fd, '[y/N] ', timeout=8.0)
            self.assertIn('[y/N] ', confirm_out)
            time.sleep(0.20)
            os.write(master_fd, b'n\n')
            rest = _drain_pty(master_fd, proc, timeout=8.0)
            self.assertEqual(proc.wait(timeout=5.0), 0)
        finally:
            os.close(master_fd)
            if proc.poll() is None:
                proc.kill()
                proc.wait()

        self.assertIn('\r\033[2K', prompt_out)
        self.assertNotIn('仍在处理', confirm_out, f'Heartbeat leaked during conflict selection:\n{confirm_out!r}')
        self.assertNotIn('仍在运行', confirm_out, f'Heartbeat leaked during conflict selection:\n{confirm_out!r}')
        self.assertNotIn('仍在处理', rest, f'Heartbeat leaked during confirm prompt:\n{rest!r}')
        self.assertNotIn('仍在运行', rest, f'Heartbeat leaked during confirm prompt:\n{rest!r}')
        self.assertIn('EXIT=0', confirm_out + rest)


if __name__ == '__main__':
    unittest.main()
