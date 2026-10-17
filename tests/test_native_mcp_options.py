"""Opt-in typed MCP configuration checks; no server connection or model request."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

import tomlkit

from claude_config.core.native import NativeProbe


@unittest.skipUnless(os.environ.get('CLAUDE_CONFIG_RUN_NATIVE_SMOKE') == '1', 'Opt-in native MCP checks')
class NativeMcpOptionTests(unittest.TestCase):
    def verify(self, variable):
        binary = os.environ.get(variable)
        if not binary:
            self.skipTest('No explicit native executable')
        with tempfile.TemporaryDirectory(prefix='mcp-option-evidence-') as directory:
            root = Path(directory)
            env = {'PATH': os.environ.get('PATH', ''), 'HOME': directory,
                   'CODEX_HOME': directory, 'TMPDIR': directory}
            base = {'url': 'https://example.invalid/mcp', 'enabled': False}
            def write(server):
                (root / 'config.toml').write_text(tomlkit.dumps({'mcp_servers': {'evidence': server}}))
            def read():
                return NativeProbe._codex_request(binary, root, env, 'config/read', {'includeLayers': False})
            options = {'startup_timeout_sec': 1.5, 'tool_timeout_sec': 7.5, 'required': True,
                       'enabled_tools': [], 'disabled_tools': ['write'], 'default_tools_approval_mode': 'prompt',
                       'tools': {'read': {'approval_mode': 'auto', 'output_token_limit': 128}},
                       'supports_parallel_tool_calls': True, 'tool_input_schema_max_bytes': 5000}
            for transport in ({'url': base['url']}, {'command': '/bin/false', 'args': [], 'cwd': directory}):
                with self.subTest(transport=next(iter(transport))):
                    write({**transport, 'enabled': False, **options})
                    response = read()
                    self.assertIsInstance(response, dict)
                    actual = response['config']['mcp_servers']['evidence']
                    for key, value in options.items():
                        self.assertEqual(actual[key], value, key)
                    self.assertFalse(actual['enabled'])
            # These resolved boolean defaults are omitted by config/read.
            for field in ('required', 'supports_parallel_tool_calls'):
                write({**base, field: False})
                actual = read()['config']['mcp_servers']['evidence']
                self.assertFalse(actual.get(field, False))
            for field, values in {'supports_parallel_tool_calls': ['invalid', 1],
                                  'tool_input_schema_max_bytes': ['invalid', 0, -1]}.items():
                for value in values:
                    with self.subTest(field=field, value=value):
                        write({**base, field: value})
                        result = subprocess.run([binary, 'features', 'list'], cwd=root, env=env,
                                                capture_output=True, text=True, timeout=15)
                        self.assertNotEqual(result.returncode, 0)
                        self.assertIn(field, result.stderr)
            # A generic TOML echo could appear to support arbitrary fields.
            write({**base, 'audit_unknown_probe': True})
            self.assertIsNone(read())

    def test_cli_mcp_options_are_typed_and_round_trip(self):
        self.verify('CLAUDE_CONFIG_NATIVE_CODEX')

    def test_app_mcp_options_are_typed_and_round_trip(self):
        self.verify('CLAUDE_CONFIG_NATIVE_CODEX_APP')

    def test_claude_mcp_timeout_is_validated_and_preserved(self):
        binary = os.environ.get('CLAUDE_CONFIG_NATIVE_CLAUDE')
        if not binary:
            self.skipTest('No explicit native executable')
        for value, valid in ((1000, True), ('invalid', False)):
            with self.subTest(timeout=value), tempfile.TemporaryDirectory(prefix='claude-mcp-evidence-') as directory:
                root = Path(directory)
                env = {'PATH': os.environ.get('PATH', ''), 'HOME': directory,
                       'CLAUDE_CONFIG_DIR': directory, 'TMPDIR': directory,
                       'DISABLE_AUTOUPDATER': '1', 'DISABLE_TELEMETRY': '1',
                       'CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC': '1'}
                payload = {'type': 'stdio', 'command': '/bin/false', 'timeout': value}
                result = subprocess.run([binary, 'mcp', 'add-json', '--scope', 'user', 'evidence', json.dumps(payload)],
                                        cwd=root, env=env, capture_output=True, text=True, timeout=20)
                if valid:
                    self.assertEqual(result.returncode, 0, result.stderr)
                    native = json.loads((root / '.claude.json').read_text())
                    self.assertEqual(native['mcpServers']['evidence']['timeout'], value)
                else:
                    self.assertNotEqual(result.returncode, 0)


if __name__ == '__main__':
    unittest.main()
