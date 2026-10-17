"""Native registration ownership, portable templates and transactional recovery."""
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

import tomlkit

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from claude_config.core.merge import fingerprint
from claude_config.core.transaction import PlanStale, atomic_write
from test_profile_resources import ResourceFixture


class RegistrationTests(ResourceFixture):
    def mcp(self, *, name='test-server', agent=None, options=None):
        if agent:
            self.profile['agent_type'] = agent
        return self.add('server', 'mcp', {'server.json': json.dumps({'name': name, 'server': {
            'command': {'binding': 'program'}, 'args': [{'resource_file': 'server.py'}, 'literal argument'],
        }}), 'server.py': 'raise RuntimeError("BUSINESS_MUST_NOT_RUN")\n'}, 'server.json',
            options=options, requires={'bindings': {'program': {'kind': 'executable', 'required': True}}},
            bindings={'program': 'executables.bash'})

    def lifecycle(self, *, agent=None, event='PreToolUse', options=None):
        if agent:
            self.profile['agent_type'] = agent
        return self.add('lifecycle', 'hook', {'hook.json': json.dumps({'hooks': {event: [{
            'matcher': 'Bash', 'hooks': [{'type': 'command', 'command': {
                'executable': {'binding': 'program'}, 'args': [{'resource_file': 'run.sh'}, '$(touch SHOULD_NOT_RUN)']
            }}]
        }]}}), 'run.sh': '#!/bin/sh\nexit 99\n'}, 'hook.json', mode='lifecycle', options=options,
            requires={'bindings': {'program': {'kind': 'executable', 'required': True}}}, bindings={'program': 'executables.bash'})

    def read_native(self):
        return json.loads(self.manager().native_path.read_text()) if self.profile['agent_type'] == 'claude-code' else tomlkit.parse(self.manager().native_path.read_text()).unwrap()

    def record(self, ref):
        return json.loads(self.manager().state_path.read_text())['resources'][ref]['native']['registration']

    def test_claude_mcp_user_registry_preserves_machine_state_and_other_servers(self):
        self.mcp()
        self.save()
        self.native.mkdir()
        path = self.native / '.claude.json'
        original = {'machineID': 'existing-machine', 'projects': {'local': {'private': True}},
                    'mcpServers': {'unowned': {'type': 'http', 'url': 'https://example.invalid/other'}}}
        path.write_text(json.dumps(original))
        plan = self.manager().plan()
        self.assertTrue(plan.report['ready'], plan.report['blockers'])
        plan.apply(self.state)
        result = json.loads(path.read_text())
        self.assertEqual(result['machineID'], original['machineID'])
        self.assertEqual(result['projects'], original['projects'])
        self.assertEqual(result['mcpServers']['unowned'], original['mcpServers']['unowned'])
        self.assertEqual(result['mcpServers']['test-server']['type'], 'stdio')
        self.assertIn('.claude-config-resources', result['mcpServers']['test-server']['args'][0])
        self.assertNotIn('mcpServers', self.read_native())
        self.assertEqual(self.record('server@test')['sha256'], fingerprint(result['mcpServers']['test-server']))
        self.assertFalse((self.root / 'SHOULD_NOT_RUN').exists())
        self.assertTrue(all(row['status'] == 'unchanged' for row in self.manager().plan().apply(self.state)['results']))

    def test_codex_mcp_shared_toml_preserves_settings_comments_and_unowned_entries(self):
        self.mcp(agent='codex', options={'enabled': False, 'tool_timeout_sec': 7.5, 'enabled_tools': [],
                                       'tools': {'write': {'approval_mode': 'prompt', 'output_token_limit': 100}}})
        self.save()
        self.native.mkdir()
        self.manager().native_path.write_text('# retain this comment\nmodel = "before"\n[mcp_servers.unowned]\nurl = "https://example.invalid/other"\n')
        plan = self.manager().plan()
        self.assertTrue(plan.report['ready'], plan.report['blockers'])
        self.assertEqual(sum(change.path == self.manager().native_path for change in plan.changes), 1)
        plan.apply(self.state)
        value = self.read_native()
        self.assertEqual(value['model'], 'selected')
        self.assertFalse(value['mcp_servers']['test-server']['enabled'])
        self.assertEqual(value['mcp_servers']['test-server']['enabled_tools'], [])
        self.assertEqual(value['mcp_servers']['test-server']['tool_timeout_sec'], 7.5)
        self.assertEqual(value['mcp_servers']['unowned']['url'], 'https://example.invalid/other')
        self.assertIn('# retain this comment', self.manager().native_path.read_text())

    def test_mcp_without_ordinary_settings_still_writes_shared_native_file(self):
        self.mcp(agent='codex')
        self.profile.pop('settings')
        self.save()
        plan = self.manager().plan()
        self.assertTrue(plan.report['ready'], plan.report['blockers'])
        plan.apply(self.state)
        self.assertIn('test-server', self.read_native()['mcp_servers'])

    def test_mcp_unknown_identity_collision_blocks_even_when_identical(self):
        self.mcp()
        self.save()
        self.native.mkdir()
        (self.native / '.claude.json').write_text(json.dumps({'mcpServers': {'test-server': {'command': str(self.binary)}}}))
        plan = self.manager().plan()
        self.assertFalse(plan.report['ready'])
        self.assertTrue(any(row.get('code') == 'unmanaged_mcp_collision' for row in plan.report['blockers']))
        with self.assertRaises(ValueError):
            plan.apply(self.state)
        self.assertFalse(self.manager().native_path.exists())

    def test_mcp_managed_entry_changes_block_global_source_wins(self):
        self.mcp(agent='codex')
        self.save()
        self.manager().plan().apply(self.state)
        path = self.manager().native_path
        data = tomlkit.parse(path.read_text())
        data['mcp_servers']['test-server']['env'] = {'MANUAL': 'value'}
        path.write_text(tomlkit.dumps(data))
        plan = self.manager().plan(run_policy={'default': 'source_wins'})
        self.assertFalse(plan.report['ready'])
        self.assertTrue(any(row.get('code') == 'mcp_drift' for row in plan.report['blockers']))

    def test_mcp_source_rename_removes_only_known_previous_native_entry(self):
        root = self.mcp(agent='codex')
        self.save()
        self.manager().plan().apply(self.state)
        definition = json.loads((root / 'server.json').read_text())
        definition['name'] = 'renamed-server'
        (root / 'server.json').write_text(json.dumps(definition))
        self.save()
        plan = self.manager().plan()
        self.assertTrue(plan.report['ready'], plan.report['blockers'])
        plan.apply(self.state)
        self.assertEqual(set(self.read_native()['mcp_servers']), {'renamed-server'})
        self.assertTrue(any(row['action'] == 'retire_previous_native_identity' for row in plan.report['decisions']))

    def test_mcp_selection_removal_retains_registration_and_reselect_checks_ownership(self):
        self.mcp()
        self.save()
        self.manager().plan().apply(self.state)
        selection = self.profile['resources'].pop('mcp_servers')
        self.save_profile()
        result = self.manager().plan().apply(self.state)
        self.assertTrue(any(row['action'] == 'residual' for row in result['decisions']))
        self.assertIn('test-server', json.loads((self.native / '.claude.json').read_text())['mcpServers'])
        self.profile['resources']['mcp_servers'] = selection
        self.save_profile()
        self.assertTrue(self.manager().plan().report['ready'])

    def test_codex_http_credentials_are_references_never_values(self):
        self.profile['agent_type'] = 'codex'
        self.machine['bindings']['secrets'] = {'test': {'provider': 'env', 'variable': 'ISOLATED_MCP_TOKEN'}}
        self.add('server', 'mcp', {'server.json': json.dumps({'name': 'remote', 'server': {'url': 'https://example.invalid/mcp',
            'bearer_token_env_var': {'binding': 'credential'}, 'env_http_headers': {'X-Token': {'binding': 'credential'}}}})}, 'server.json',
            requires={'bindings': {'credential': {'kind': 'secret', 'required': True}}}, bindings={'credential': 'secrets.test'})
        self.save()
        with patch.dict('os.environ', {'ISOLATED_MCP_TOKEN': 'PRIVATE_DO_NOT_COPY'}):
            plan = self.manager().plan()
            self.assertTrue(plan.report['ready'], plan.report['blockers'])
            plan.apply(self.state)
            self.assertNotIn('PRIVATE_DO_NOT_COPY', json.dumps(plan.report))
            self.assertFalse(any(b'PRIVATE_DO_NOT_COPY' in path.read_bytes() for path in self.root.rglob('*') if path.is_file()))
        self.assertEqual(self.read_native()['mcp_servers']['remote']['bearer_token_env_var'], 'ISOLATED_MCP_TOKEN')

    def test_claude_header_interpolation_retains_only_declared_variable_reference(self):
        self.machine['bindings']['secrets'] = {'test': {'provider': 'env', 'variable': 'ISOLATED_MCP_TOKEN'}}
        self.add('server', 'mcp', {'server.json': json.dumps({'name': 'remote', 'server': {'url': 'https://example.invalid/mcp',
            'headers': {'Authorization': {'binding': 'credential', 'prefix': 'Bearer '}}}})}, 'server.json',
            requires={'bindings': {'credential': {'kind': 'secret', 'required': True}}}, bindings={'credential': 'secrets.test'})
        self.save()
        with patch.dict('os.environ', {'ISOLATED_MCP_TOKEN': 'PRIVATE_DO_NOT_COPY'}):
            plan = self.manager().plan()
            self.assertTrue(plan.report['ready'], plan.report['blockers'])
            plan.apply(self.state)
        server = json.loads((self.native / '.claude.json').read_text())['mcpServers']['remote']
        self.assertEqual(server['headers']['Authorization'], 'Bearer ${ISOLATED_MCP_TOKEN}')

    def test_undeclared_secret_literal_command_and_invalid_transport_are_blocked(self):
        root = self.mcp()
        self.save()
        original = json.loads((root / 'server.json').read_text())
        variants = [dict(original['server'], command='/bin/sh'),
                    dict(original['server'], env={'API_KEY': 'SECRET_LITERAL_MUST_NOT_LEAK'}),
                    dict(original['server'], url='https://example.invalid/mcp'),
                    {'url': 'https://user:SECRET_LITERAL_MUST_NOT_LEAK@example.invalid/mcp'},
                    {'url': 'https://example.invalid/mcp?token=SECRET_LITERAL_MUST_NOT_LEAK'},
                    dict(original['server'], args=['${UNDECLARED}'])]
        for server in variants:
            with self.subTest(server_shape=list(server)):
                (root / 'server.json').write_text(json.dumps({'name': 'server', 'server': server}))
                self.save()
                plan = self.manager().plan()
                self.assertFalse(plan.report['ready'])
                self.assertNotIn('SECRET_LITERAL_MUST_NOT_LEAK', json.dumps(plan.report))

    def test_hook_groups_and_settings_share_one_native_write_and_preserve_unmanaged_groups(self):
        self.lifecycle(options={'timeout': 5, 'async': False})
        self.save()
        self.native.mkdir()
        unrelated = {'matcher': 'Write', 'hooks': [{'type': 'command', 'command': 'user-owned-command'}]}
        self.manager().native_path.write_text(json.dumps({'hooks': {'PreToolUse': [unrelated]}, 'model': 'before'}))
        plan = self.manager().plan()
        self.assertTrue(plan.report['ready'], plan.report['blockers'])
        self.assertEqual(sum(change.path == self.manager().native_path for change in plan.changes), 1)
        plan.apply(self.state)
        value = self.read_native()
        self.assertEqual(value['hooks']['PreToolUse'][0], unrelated)
        handler = value['hooks']['PreToolUse'][1]['hooks'][0]
        self.assertEqual(handler['timeout'], 5)
        self.assertFalse(handler['async'])
        self.assertIn("'$(touch SHOULD_NOT_RUN)'", handler['command'])
        self.assertEqual(value['model'], 'selected')

    def test_codex_hooks_json_retains_inline_hook_state_and_trust_without_enabling(self):
        self.lifecycle(agent='codex')
        self.save()
        self.native.mkdir()
        self.manager().native_path.write_text('model = "before"\n[features]\nhooks = false\n[hooks.state.existing]\nenabled = true\ntrusted_hash = "USER_NATIVE_TRUST"\n')
        plan = self.manager().plan()
        self.assertTrue(plan.report['ready'], plan.report['blockers'])
        plan.apply(self.state)
        value = self.read_native()
        self.assertFalse(value['features']['hooks'])
        self.assertEqual(value['hooks']['state']['existing']['trusted_hash'], 'USER_NATIVE_TRUST')
        self.assertIn('PreToolUse', json.loads((self.native / 'hooks.json').read_text())['hooks'])
        self.assertTrue(any(row['name'] == 'hook_trust' and row['status'] == 'not_verified' for row in plan.report['checks']))

    def test_hook_update_preserves_new_unrelated_group_and_checks_only_owned_group(self):
        root = self.lifecycle()
        self.save()
        self.manager().plan().apply(self.state)
        path = self.manager().native_path
        value = self.read_native()
        unowned = {'matcher': 'Read', 'hooks': [{'type': 'command', 'command': 'manual-other'}]}
        value['hooks']['PreToolUse'].insert(0, unowned)
        path.write_text(json.dumps(value))
        (root / 'run.sh').write_text('#!/bin/sh\nexit 0\n')
        self.save()
        plan = self.manager().plan()
        self.assertTrue(plan.report['ready'], plan.report['blockers'])
        plan.apply(self.state)
        self.assertEqual(self.read_native()['hooks']['PreToolUse'][0], unowned)
        self.assertEqual(len(self.read_native()['hooks']['PreToolUse']), 2)
        value = self.read_native()
        value['hooks']['PreToolUse'][1]['hooks'][0]['timeout'] = 8
        path.write_text(json.dumps(value))
        self.assertFalse(self.manager().plan().report['ready'])

    def test_duplicate_owned_hook_groups_are_ambiguous_and_block(self):
        self.lifecycle()
        self.save()
        self.manager().plan().apply(self.state)
        value = self.read_native()
        value['hooks']['PreToolUse'] *= 2
        self.manager().native_path.write_text(json.dumps(value))
        plan = self.manager().plan()
        self.assertFalse(plan.report['ready'])
        self.assertTrue(any(row.get('code') == 'hook_drift' for row in plan.report['blockers']))

    def test_unknown_hook_event_raw_command_and_ignored_matcher_block(self):
        root = self.lifecycle()
        self.save()
        original = json.loads((root / 'hook.json').read_text())
        raw = json.loads(json.dumps(original))
        raw['hooks']['PreToolUse'][0]['hooks'][0]['command'] = '/bin/sh arbitrary'
        ignored = {'hooks': {'Stop': original['hooks']['PreToolUse']}}
        unknown = {'hooks': {'NewUnverifiedEvent': original['hooks']['PreToolUse']}}
        for value in (raw, ignored, unknown):
            (root / 'hook.json').write_text(json.dumps(value))
            self.save()
            self.assertFalse(self.manager().plan().report['ready'])

    def test_multiple_resource_registrations_do_not_overwrite_each_other(self):
        self.mcp()
        self.lifecycle()
        self.add('other', 'mcp', {'other.json': json.dumps({'name': 'other', 'server': {'url': 'https://example.invalid/mcp'}})}, 'other.json')
        self.save()
        plan = self.manager().plan()
        self.assertTrue(plan.report['ready'], plan.report['blockers'])
        plan.apply(self.state)
        self.assertEqual(set(json.loads((self.native / '.claude.json').read_text())['mcpServers']), {'test-server', 'other'})
        self.assertIn('PreToolUse', self.read_native()['hooks'])

    def test_register_files_roll_back_when_later_native_write_fails(self):
        self.mcp()
        self.lifecycle()
        self.save()
        self.native.mkdir()
        registry = self.native / '.claude.json'
        before = b'{"machineID":"retain"}\n'
        registry.write_bytes(before)
        plan = self.manager().plan()
        def writer(path, content, mode):
            if path == self.manager().native_path:
                raise OSError('isolated simulated failure')
            atomic_write(path, content, mode)
        with self.assertRaises(OSError):
            plan.apply(self.state, writer=writer)
        self.assertEqual(registry.read_bytes(), before)
        self.assertFalse(self.manager().native_path.exists())
        self.assertFalse(self.manager().state_path.exists())

    def test_native_registry_change_after_preview_invalidates_whole_plan(self):
        self.mcp()
        self.save()
        plan = self.manager().plan()
        self.native.mkdir()
        (self.native / '.claude.json').write_text('{}')
        with self.assertRaises(PlanStale):
            plan.apply(self.state)
        self.assertFalse(self.manager().native_path.exists())

    def test_native_registration_capability_checks_all_bound_codex_binaries(self):
        from claude_config.core.manager import ProfileManager
        self.mcp(agent='codex')
        self.machine['apps'] = {'codex-app': {'profile': 'profiles/apps/codex-app/daily.yaml',
                                            'binding': {'application': '/Applications/Test.app', 'data_root': str(self.root / 'app'), 'backend': str(self.binary)}}}
        self.save()
        manager = ProfileManager(self.machine_path, 'codex', probe=lambda *args: [{'name': 'runtime_version', 'status': 'passed', 'version': 'codex-cli 0.159.0'}])
        plan = manager.plan()
        self.assertFalse(plan.report['ready'])
        self.assertTrue(any(row.get('name') == 'registration_capability' for row in plan.report['blockers']))

    def test_hook_selection_removal_retains_event_and_reselect_detects_drift(self):
        self.lifecycle()
        self.save()
        self.manager().plan().apply(self.state)
        selection = self.profile['resources'].pop('hooks')
        self.save_profile()
        self.manager().plan().apply(self.state)
        self.assertIn('PreToolUse', self.read_native()['hooks'])
        value = self.read_native()
        value['hooks']['PreToolUse'][0]['hooks'][0]['command'] = 'manual edit'
        self.manager().native_path.write_text(json.dumps(value))
        self.profile['resources']['hooks'] = selection
        self.save_profile()
        self.assertFalse(self.manager().plan().report['ready'])

    def test_codex_stdio_credential_forwarding_uses_env_vars_names(self):
        root = self.mcp(agent='codex')
        self.machine['bindings']['secrets'] = {'test': {'provider': 'env', 'variable': 'ISOLATED_MCP_TOKEN'}}
        self.catalog['resources']['server']['requires']['bindings']['credential'] = {'kind': 'secret', 'required': True}
        self.profile['resources']['mcp_servers']['server@test']['bindings']['credential'] = 'secrets.test'
        value = json.loads((root / 'server.json').read_text())
        value['server']['env_vars'] = [{'binding': 'credential'}]
        (root / 'server.json').write_text(json.dumps(value))
        self.save()
        with patch.dict('os.environ', {'ISOLATED_MCP_TOKEN': 'PRIVATE_DO_NOT_COPY'}):
            self.manager().plan().apply(self.state)
        self.assertEqual(self.read_native()['mcp_servers']['test-server']['env_vars'], ['ISOLATED_MCP_TOKEN'])
        self.assertNotIn('PRIVATE_DO_NOT_COPY', self.manager().native_path.read_text())

    def test_invalid_native_option_types_and_thresholds_block(self):
        self.mcp(agent='codex', options={'tools': {'write': {'output_token_limit': 0}}})
        self.save()
        self.assertFalse(self.manager().plan().report['ready'])
        self.profile['resources']['mcp_servers']['server@test']['options'] = {'enabled': 'false'}
        self.save_profile()
        self.assertFalse(self.manager().plan().report['ready'])
        self.profile['resources']['mcp_servers']['server@test']['options'] = {}
        self.lifecycle(options={'timeout': 4}, event='SessionEnd')
        value = json.loads((self.repo / 'packages/lifecycle/hook.json').read_text())
        value['hooks']['SessionEnd'][0].pop('matcher')
        (self.repo / 'packages/lifecycle/hook.json').write_text(json.dumps(value))
        self.save()
        self.assertFalse(self.manager().plan().report['ready'])

    def test_claude_default_registry_is_outside_settings_directory(self):
        self.native = self.root / '.claude'
        self.machine['agents']['claude-code']['binding']['config_root'] = str(self.native)
        self.mcp()
        self.save()
        with patch('claude_config.core.registrations.Path.home', return_value=self.root):
            plan = self.manager().plan()
            self.assertTrue(plan.report['ready'], plan.report['blockers'])
            plan.apply(self.state)
        self.assertTrue((self.root / '.claude.json').exists())
        self.assertFalse((self.native / '.claude.json').exists())

    def test_claude_explicit_user_config_can_disambiguate_default_directory(self):
        self.native = self.root / '.claude'
        binding = self.machine['agents']['claude-code']['binding']
        binding['config_root'] = str(self.native)
        binding['user_config'] = str(self.native / '.claude.json')
        self.mcp()
        self.save()
        with patch('claude_config.core.registrations.Path.home', return_value=self.root):
            self.manager().plan().apply(self.state)
        self.assertTrue((self.native / '.claude.json').exists())
        self.assertFalse((self.root / '.claude.json').exists())
        binding['user_config'] = str(self.root / 'wrong-native-file.json')
        self.save_profile()
        with patch('claude_config.core.registrations.Path.home', return_value=self.root):
            self.assertFalse(self.manager().plan().report['ready'])

    def test_claude_registry_binding_change_requires_explicit_relocation(self):
        self.native = self.root / '.claude'
        binding = self.machine['agents']['claude-code']['binding']
        binding['config_root'] = str(self.native)
        self.mcp()
        self.save()
        with patch('claude_config.core.registrations.Path.home', return_value=self.root):
            self.manager().plan().apply(self.state)
        binding['user_config'] = str(self.native / '.claude.json')
        self.save_profile()
        with patch('claude_config.core.registrations.Path.home', return_value=self.root):
            plan = self.manager().plan()
        self.assertFalse(plan.report['ready'])
        self.assertTrue(any(row.get('code') == 'registration_binding_changed' for row in plan.report['blockers']))
        self.assertFalse((self.native / '.claude.json').exists())

    def test_mcp_omitted_usage_options_preserve_native_values_and_partial_tools(self):
        self.mcp(agent='codex', options={'enabled': False, 'tool_timeout_sec': 4,
                                       'tools': {'one': {'approval_mode': 'prompt', 'output_token_limit': 50}, 'two': {'approval_mode': 'writes'}}})
        self.save()
        self.manager().plan().apply(self.state)
        self.profile['resources']['mcp_servers']['server@test']['options'] = {'tools': {'one': {'output_token_limit': 100}}}
        self.save_profile()
        plan = self.manager().plan()
        self.assertTrue(plan.report['ready'], plan.report['blockers'])
        plan.apply(self.state)
        server = self.read_native()['mcp_servers']['test-server']
        self.assertFalse(server['enabled'])
        self.assertEqual(server['tool_timeout_sec'], 4)
        self.assertEqual(server['tools']['one'], {'approval_mode': 'prompt', 'output_token_limit': 100})
        self.assertEqual(server['tools']['two'], {'approval_mode': 'writes'})
        self.assertTrue(any(row['path'][-2:] == ['options', 'enabled'] and row['action'] == 'preserve' for row in plan.report['decisions']))

    def test_omitted_hook_options_preserve_usage_after_script_update(self):
        root = self.lifecycle(options={'timeout': 5, 'async': False})
        self.save()
        self.manager().plan().apply(self.state)
        self.profile['resources']['hooks']['lifecycle@test'].pop('options')
        (root / 'run.sh').write_text('#!/bin/sh\nexit 0\n')
        self.save()
        plan = self.manager().plan()
        self.assertTrue(plan.report['ready'], plan.report['blockers'])
        plan.apply(self.state)
        handler = self.read_native()['hooks']['PreToolUse'][0]['hooks'][0]
        self.assertEqual(handler['timeout'], 5)
        self.assertFalse(handler['async'])
        self.assertTrue(any(row['action'] == 'preserve' and row['path'][-1] == 'timeout' for row in plan.report['decisions']))

    def test_changed_handler_layout_cannot_guess_omitted_usage_options(self):
        root = self.lifecycle(options={'timeout': 5})
        self.save()
        self.manager().plan().apply(self.state)
        value = json.loads((root / 'hook.json').read_text())
        value['hooks']['PreToolUse'][0]['hooks'].append({'type': 'command', 'command': {
            'executable': {'binding': 'program'}, 'args': ['different command']}})
        (root / 'hook.json').write_text(json.dumps(value))
        self.profile['resources']['hooks']['lifecycle@test'].pop('options')
        self.save()
        plan = self.manager().plan()
        self.assertFalse(plan.report['ready'])
        self.assertTrue(any(row.get('code') == 'hook_options_ambiguous' for row in plan.report['blockers']))
        self.profile['resources']['hooks']['lifecycle@test']['options'] = {'timeout': 8}
        self.save_profile()
        self.assertTrue(self.manager().plan().report['ready'])

    def test_noop_hooks_preserve_owned_group_positions_among_unrelated_groups(self):
        root = self.lifecycle()
        value = json.loads((root / 'hook.json').read_text())
        other = json.loads(json.dumps(value['hooks']['PreToolUse'][0]))
        other['matcher'] = 'Read'
        other['hooks'][0]['command']['args'] = ['other handler']
        value['hooks']['PreToolUse'].append(other)
        (root / 'hook.json').write_text(json.dumps(value))
        self.save()
        self.manager().plan().apply(self.state)
        document = self.read_native()
        document['hooks']['PreToolUse'].insert(1, {'matcher': 'Write', 'hooks': [{'type': 'command', 'command': 'user owned'}]})
        original = json.dumps(document).encode()
        self.manager().native_path.write_bytes(original)
        plan = self.manager().plan()
        self.assertTrue(plan.report['ready'], plan.report['blockers'])
        plan.apply(self.state)
        self.assertEqual(self.manager().native_path.read_bytes(), original)


if __name__ == '__main__':
    unittest.main()
