"""Opt-in actual source/body checks. All roots are isolated; never run a skill."""
import json
import os
from pathlib import Path
import sys
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from claude_config.core.manager import ProfileManager
from claude_config.core.native import NativeProbe
from test_profile_resources import run_git


@unittest.skipUnless(os.environ.get('CLAUDE_CONFIG_RUN_NATIVE_SMOKE') == '1', 'Opt-in native resource checks')
class NativeResourceSmokeTests(unittest.TestCase):
    def fixture(self, directory, agent, binary, kind, files, entry, mode='native'):
        root = Path(directory)
        repo, data = root / 'repo', root / 'data'
        repo.mkdir()
        package = repo / 'package'
        package.mkdir()
        for name, content in files.items():
            path = package / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        requires = {'bindings': {'install_root': {'kind': 'path', 'required': True}}} if kind == 'skill' else {}
        item = {'kind': kind, 'description': 'Isolated smoke', 'origin': {'type': 'self', 'author': 'test'},
                'maintenance': {'owner': 'ours', 'mode': 'original'}, 'source': {'kind': 'tracked', 'path': 'package'},
                'compatibility': {agent: {'mode': mode, 'entry': entry, 'validation': {'status': 'not-evaluated'}}},
                'requires': requires, 'update': {'strategy': 'manual'}}
        (repo / 'catalog.json').write_text(json.dumps({'schema_version': 1, 'name': 'smoke', 'resources': {'native-smoke': item}}))
        run_git(repo, 'init', '-q')
        run_git(repo, 'config', 'user.name', 'isolated-test')
        run_git(repo, 'config', 'user.email', 'isolated@example.invalid')
        run_git(repo, 'remote', 'add', 'origin', 'https://github.com/owner/smoke.git')
        run_git(repo, 'add', '.')
        run_git(repo, 'commit', '-qm', 'isolated smoke')
        group = {'skill': 'skills', 'hook': 'hooks', 'rule': 'rules', 'mcp': 'mcp_servers'}[kind]
        selection = {'bindings': {'install_root': 'paths.skill_root'}} if kind == 'skill' else {}
        if kind == 'hook' and mode == 'lifecycle':
            requires = {'bindings': {'program': {'kind': 'executable', 'required': True}}}
            item['requires'] = requires
            (repo / 'catalog.json').write_text(json.dumps({'schema_version': 1, 'name': 'smoke', 'resources': {'native-smoke': item}}))
            run_git(repo, 'add', '.')
            run_git(repo, 'commit', '-qm', 'declare isolated command binding')
            selection = {'bindings': {'program': 'executables.sh'}}
        profile = {'schema_version': 1, 'agent_type': agent, 'resources': {group: {'native-smoke@smoke': selection}}}
        if kind == 'hook' and mode == 'status-line':
            profile['bindings'] = {'/settings/statusLine/command': {'kind': 'command', 'executable': 'executables.sh',
                                                                  'args': [{'resource': 'native-smoke@smoke', 'path': entry}]}}
        name = f'profiles/agents/{agent}/work.yaml'
        path = data / name
        path.parent.mkdir(parents=True)
        path.write_text(yaml.safe_dump(profile))
        (data / 'catalogs.yaml').write_text(yaml.safe_dump({'schema_version': 1, 'catalogs': {'smoke': {'repo': 'owner/smoke', 'commit': run_git(repo, 'rev-parse', 'HEAD'), 'path': '.'}}}))
        machine = {'schema_version': 1, 'machine_id': 'isolated', 'config_dir': str(data), 'catalog_roots': {'smoke': str(repo)},
                   'agents': {agent: {'profile': name, 'binding': {'executable': binary, 'config_root': str(root / 'native')}}},
                   'apps': {}, 'bindings': {'executables': {'sh': '/bin/sh'}, 'paths': {'skill_root': str(root / 'home/.agents/skills')}},
                   'runtime': {'state_dir': str(root / 'state')}}
        (root / 'machine.yaml').write_text(yaml.safe_dump(machine))
        return ProfileManager(root / 'machine.yaml', agent)

    def binary(self, variable):
        binary = os.environ.get(variable)
        if not binary:
            self.skipTest(variable + ' is required')
        return binary

    def test_claude_statusline_is_installed_but_never_executed(self):
        binary = self.binary('CLAUDE_CONFIG_NATIVE_CLAUDE')
        with tempfile.TemporaryDirectory(prefix='resource-native-smoke-') as directory:
            marker = Path(directory) / 'BUSINESS_MUST_NOT_RUN'
            script = '#!/bin/sh\ntouch ' + str(marker) + '\nprintf unsafe-to-run-in-test\n'
            manager = self.fixture(directory, 'claude-code', binary, 'hook', {'status.sh': script}, 'status.sh', 'status-line')
            plan = manager.plan()
            self.assertTrue(plan.report['ready'], plan.report['blockers'])
            plan.apply(manager.state_root)
            self.assertFalse(marker.exists())
            self.assertIn('statusLine', json.loads(manager.native_path.read_text()))
            exported = ProfileManager(manager.machine_path, 'claude-code').export(['/settings/statusLine/command'])
            self.assertTrue(exported.report['ready'])
            self.assertTrue(any(row['action'] == 'preserve_binding' for row in exported.report['decisions']))

    def test_codex_skill_is_discovered_in_explicit_user_root(self):
        binary = self.binary('CLAUDE_CONFIG_NATIVE_CODEX')
        with tempfile.TemporaryDirectory(prefix='resource-native-smoke-') as directory:
            manager = self.fixture(directory, 'codex', binary, 'skill', {'SKILL.md': '---\nname: native-smoke\ndescription: Isolated discovery check\n---\nDo not execute business logic.\n'}, 'SKILL.md')
            plan = manager.plan()
            self.assertTrue(plan.report['ready'], plan.report['blockers'])
            plan.apply(manager.state_root)
            root = Path(directory)
            env = {'PATH': os.environ.get('PATH', ''), 'HOME': str(root / 'home'), 'CODEX_HOME': str(root / 'native'), 'TMPDIR': directory}
            result = NativeProbe._codex_request(binary, root, env, 'skills/list', {'cwds': [str(root)], 'forceReload': True})
            self.assertIsInstance(result, dict)
            self.assertIn('native-smoke', json.dumps(result))
            self.assertIn(str(root / 'home/.agents/skills/native-smoke/SKILL.md'), json.dumps(result))

    def test_codex_skill_activation_and_invocation_controls(self):
        self.assert_skill_controls(self.binary('CLAUDE_CONFIG_NATIVE_CODEX'))

    def test_codex_app_backend_skill_activation_and_invocation_controls(self):
        self.assert_skill_controls(self.binary('CLAUDE_CONFIG_NATIVE_CODEX_APP'))

    def test_codex_named_mcp_entry_is_native_config_not_profile_metadata(self):
        self.assert_mcp_entry(self.binary('CLAUDE_CONFIG_NATIVE_CODEX'))

    def test_codex_app_backend_named_mcp_entry_is_native_config(self):
        self.assert_mcp_entry(self.binary('CLAUDE_CONFIG_NATIVE_CODEX_APP'))

    def assert_mcp_entry(self, binary):
        with tempfile.TemporaryDirectory(prefix='mcp-entry-native-smoke-') as directory:
            root = Path(directory)
            manager = self.fixture(directory, 'codex', binary, 'mcp', {
                'stdio.json': json.dumps({'name': 'native-smoke', 'server': {'command': {'binding': 'program'}}}),
                'http.json': json.dumps({'name': 'native-smoke', 'server': {'url': 'https://example.invalid/mcp'}})}, 'stdio.json')
            path = root / 'repo/catalog.json'
            catalog = json.loads(path.read_text())
            item = catalog['resources']['native-smoke']
            item['compatibility']['codex']['entries'] = {'stdio': 'stdio.json', 'http': 'http.json'}
            item['requires'] = {'bindings': {'program': {'kind': 'executable', 'required': False}}}
            path.write_text(json.dumps(catalog))
            run_git(root / 'repo', 'add', '.')
            run_git(root / 'repo', 'commit', '-qm', 'declare fixed native entries')
            declared = root / 'data/catalogs.yaml'
            value = yaml.safe_load(declared.read_text())
            value['catalogs']['smoke']['commit'] = run_git(root / 'repo', 'rev-parse', 'HEAD')
            declared.write_text(yaml.safe_dump(value))
            profile_path = manager.root / manager.entry['profile']
            profile = yaml.safe_load(profile_path.read_text())
            profile['resources']['mcp_servers']['native-smoke@smoke']['options'] = {'entry': 'http', 'enabled': False, 'tool_timeout_sec': 7.5}
            profile_path.write_text(yaml.safe_dump(profile))
            manager = ProfileManager(manager.machine_path, 'codex')
            plan = manager.plan()
            self.assertTrue(plan.report['ready'], plan.report['blockers'])
            plan.apply(manager.state_root)
            env = {'PATH': os.environ.get('PATH', ''), 'HOME': str(root / 'home'), 'CODEX_HOME': str(root / 'native'), 'TMPDIR': directory}
            response = NativeProbe._codex_request(binary, root, env, 'config/read', {'includeLayers': False})
            server = response['config']['mcp_servers']['native-smoke']
            self.assertEqual(server['url'], 'https://example.invalid/mcp')
            self.assertFalse(server['enabled'])
            self.assertEqual(server['tool_timeout_sec'], 7.5)
            self.assertNotIn('entry', server)
            exported = ProfileManager(manager.machine_path, 'codex').export([], resource_refs=['native-smoke@smoke'])
            self.assertTrue(exported.report['ready'], exported.report['blockers'])

    def assert_skill_controls(self, binary):
        with tempfile.TemporaryDirectory(prefix='skill-controls-native-smoke-') as directory:
            root = Path(directory)
            manager = self.fixture(directory, 'codex', binary, 'skill', {'SKILL.md': '---\nname: native-smoke\ndescription: Isolated controls\n---\nDo not execute business logic.\n'}, 'SKILL.md')
            profile_path = manager.root / manager.entry['profile']
            profile = yaml.safe_load(profile_path.read_text())
            profile['resources']['skills']['native-smoke@smoke']['options'] = {'control': {'enabled': False, 'allow_implicit_invocation': False}}
            profile['settings'] = {'skills': {'include_instructions': True, 'max_context_tokens': 600}}
            profile_path.write_text(yaml.safe_dump(profile))
            plan = ProfileManager(manager.machine_path, 'codex').plan()
            self.assertTrue(plan.report['ready'], plan.report['blockers'])
            plan.apply(manager.state_root)
            env = {'PATH': os.environ.get('PATH', ''), 'HOME': str(root / 'home'), 'CODEX_HOME': str(root / 'native'), 'TMPDIR': directory}
            def skill_metadata():
                result = NativeProbe._codex_request(binary, root, env, 'skills/list', {'cwds': [directory], 'forceReload': True})
                def find(value):
                    if isinstance(value, dict):
                        if value.get('name') == 'native-smoke':
                            return value
                        for child in value.values():
                            match = find(child)
                            if match:
                                return match
                    if isinstance(value, list):
                        for child in value:
                            match = find(child)
                            if match:
                                return match
                return find(result)
            metadata = skill_metadata()
            self.assertIsInstance(metadata, dict)
            self.assertFalse(metadata['enabled'])
            skill_file = Path(metadata['path'])
            self.assertTrue(skill_file.exists())
            self.assertFalse(yaml.safe_load((skill_file.parent / 'agents/openai.yaml').read_text())['policy']['allow_implicit_invocation'])
            profile['resources']['skills']['native-smoke@smoke']['options']['control']['enabled'] = True
            profile_path.write_text(yaml.safe_dump(profile))
            ProfileManager(manager.machine_path, 'codex').plan().apply(manager.state_root)
            self.assertTrue(skill_metadata()['enabled'])
            profile['resources']['skills']['native-smoke@smoke']['options'] = {}
            profile_path.write_text(yaml.safe_dump(profile))
            ProfileManager(manager.machine_path, 'codex').plan().apply(manager.state_root)
            self.assertTrue(skill_metadata()['enabled'])
            self.assertFalse(yaml.safe_load((skill_file.parent / 'agents/openai.yaml').read_text())['policy']['allow_implicit_invocation'])

    def test_codex_rule_syntax_uses_native_parser_without_running_a_command(self):
        binary = self.binary('CLAUDE_CONFIG_NATIVE_CODEX')
        with tempfile.TemporaryDirectory(prefix='resource-native-smoke-') as directory:
            manager = self.fixture(directory, 'codex', binary, 'rule', {'policy.rules': 'prefix_rule(pattern = ["dangerous-example"], decision = "forbidden")\n'}, 'policy.rules')
            plan = manager.plan()
            self.assertTrue(plan.report['ready'], plan.report['blockers'])
            self.assertTrue(any(row['name'] == 'native_rule_syntax' and row['status'] == 'passed' for row in plan.report['checks']))
            plan.apply(manager.state_root)
            self.assertTrue((manager.native_path.parent / 'rules/native-smoke.rules').exists())

    def test_claude_mcp_registration_matches_native_user_registry_writer(self):
        binary = self.binary('CLAUDE_CONFIG_NATIVE_CLAUDE')
        with tempfile.TemporaryDirectory(prefix='resource-native-smoke-') as directory:
            definition = {'type': 'http', 'url': 'https://example.invalid/mcp', 'timeout': 2000}
            manager = self.fixture(directory, 'claude-code', binary, 'mcp',
                                   {'server.json': json.dumps({'name': 'native-smoke', 'server': definition})}, 'server.json')
            plan = manager.plan()
            self.assertTrue(plan.report['ready'], plan.report['blockers'])
            plan.apply(manager.state_root)
            actual = json.loads((manager.native_path.parent / '.claude.json').read_text())['mcpServers']['native-smoke']
            reference = Path(directory) / 'native-reference'
            reference.mkdir()
            env = {'PATH': os.environ.get('PATH', ''), 'HOME': str(reference), 'CLAUDE_CONFIG_DIR': str(reference), 'TMPDIR': directory,
                   'DISABLE_AUTOUPDATER': '1', 'CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC': '1'}
            result = subprocess.run([binary, 'mcp', 'add-json', 'native-smoke', json.dumps(definition), '--scope', 'user'],
                                    cwd=reference, env=env, capture_output=True, timeout=20)
            self.assertEqual(result.returncode, 0)
            native = json.loads((reference / '.claude.json').read_text())['mcpServers']['native-smoke']
            self.assertEqual(actual, native)

    def test_codex_mcp_metadata_is_read_without_connecting_a_service(self):
        self.assert_mcp_metadata(self.binary('CLAUDE_CONFIG_NATIVE_CODEX'))

    def test_claude_default_user_layout_matches_native_without_config_dir(self):
        binary = self.binary('CLAUDE_CONFIG_NATIVE_CLAUDE')
        with tempfile.TemporaryDirectory(prefix='resource-native-smoke-') as directory:
            root = Path(directory)
            definition = {'type': 'http', 'url': 'https://example.invalid/mcp'}
            fixture = self.fixture(directory, 'claude-code', binary, 'mcp',
                                   {'server.json': json.dumps({'name': 'native-smoke', 'server': definition})}, 'server.json')
            machine = yaml.safe_load(fixture.machine_path.read_text())
            machine['agents']['claude-code']['binding']['config_root'] = str(root / '.claude')
            fixture.machine_path.write_text(yaml.safe_dump(machine))
            with patch('claude_config.core.registrations.Path.home', return_value=root):
                manager = ProfileManager(fixture.machine_path, 'claude-code')
                plan = manager.plan()
                self.assertTrue(plan.report['ready'], plan.report['blockers'])
                plan.apply(manager.state_root)
            self.assertTrue((root / '.claude.json').exists())
            self.assertFalse((root / '.claude/.claude.json').exists())
            reference = root / 'reference-home'
            reference.mkdir()
            env = {'PATH': os.environ.get('PATH', ''), 'HOME': str(reference), 'TMPDIR': directory,
                   'DISABLE_AUTOUPDATER': '1', 'CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC': '1'}
            result = subprocess.run([binary, 'mcp', 'add-json', 'native-smoke', json.dumps(definition), '--scope', 'user'],
                                    cwd=reference, env=env, capture_output=True, timeout=20)
            self.assertEqual(result.returncode, 0)
            self.assertEqual(json.loads((root / '.claude.json').read_text())['mcpServers']['native-smoke'],
                             json.loads((reference / '.claude.json').read_text())['mcpServers']['native-smoke'])

    def test_codex_app_backend_mcp_metadata(self):
        self.assert_mcp_metadata(self.binary('CLAUDE_CONFIG_NATIVE_CODEX_APP'))

    def assert_mcp_metadata(self, binary):
        with tempfile.TemporaryDirectory(prefix='resource-native-smoke-') as directory:
            definition = {'url': 'https://example.invalid/mcp', 'enabled': False, 'tool_timeout_sec': 7.5}
            manager = self.fixture(directory, 'codex', binary, 'mcp',
                                   {'server.json': json.dumps({'name': 'native-smoke', 'server': definition})}, 'server.json')
            plan = manager.plan()
            self.assertTrue(plan.report['ready'], plan.report['blockers'])
            plan.apply(manager.state_root)
            env = {'PATH': os.environ.get('PATH', ''), 'HOME': directory, 'CODEX_HOME': str(manager.native_path.parent), 'TMPDIR': directory}
            result = subprocess.run([binary, 'mcp', 'get', 'native-smoke', '--json'], cwd=directory, env=env, capture_output=True, timeout=20)
            self.assertEqual(result.returncode, 0)
            data = json.loads(result.stdout)
            self.assertEqual(data['name'], 'native-smoke')
            self.assertFalse(data['enabled'])
            self.assertEqual(data['transport']['url'], definition['url'])
            self.assertEqual(data['tool_timeout_sec'], 7.5)

    def test_codex_hook_discovery_does_not_execute_or_trust_the_script(self):
        self.assert_hook_discovery(self.binary('CLAUDE_CONFIG_NATIVE_CODEX'))

    def test_codex_app_backend_hook_discovery(self):
        self.assert_hook_discovery(self.binary('CLAUDE_CONFIG_NATIVE_CODEX_APP'))

    def assert_hook_discovery(self, binary):
        with tempfile.TemporaryDirectory(prefix='resource-native-smoke-') as directory:
            marker = Path(directory) / 'BUSINESS_MUST_NOT_RUN'
            definition = {'hooks': {'PreToolUse': [{'matcher': 'Bash', 'hooks': [{'type': 'command', 'command': {
                'executable': {'binding': 'program'}, 'args': [{'resource_file': 'run.sh'}]}}]}]}}
            manager = self.fixture(directory, 'codex', binary, 'hook',
                                   {'hooks.json': json.dumps(definition), 'run.sh': '#!/bin/sh\ntouch ' + str(marker) + '\n'}, 'hooks.json', 'lifecycle')
            plan = manager.plan()
            self.assertTrue(plan.report['ready'], plan.report['blockers'])
            plan.apply(manager.state_root)
            root = Path(directory)
            env = {'PATH': os.environ.get('PATH', ''), 'HOME': directory, 'CODEX_HOME': str(manager.native_path.parent), 'TMPDIR': directory}
            result = NativeProbe._codex_request(binary, root, env, 'hooks/list', {'cwds': [directory]})
            self.assertIsInstance(result, dict)
            self.assertIn('run.sh', json.dumps(result))
            self.assertFalse(marker.exists())
            self.assertFalse(manager.native_path.exists())


if __name__ == '__main__':
    unittest.main()
