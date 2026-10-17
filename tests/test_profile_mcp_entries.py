"""Named source templates select native connection modes without inference."""
import json
from pathlib import Path
import sys
import unittest

import tomlkit
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from claude_config.core.catalog_schema import validate_catalog
from claude_config.core.recovery import rollback_plan
from claude_config.core.transaction import PlanStale
from test_profile_resources import ResourceFixture


class McpEntryTests(ResourceFixture):
    def server(self, agent='codex', entry='http'):
        self.profile['agent_type'] = agent
        self.machine['bindings']['endpoints'] = {'docs': 'https://example.invalid/mcp'}
        self.add('server', 'mcp', {'stdio.json': json.dumps({'name': 'docs', 'server': {'command': {'binding': 'program'},
                            'args': [{'resource_file': 'server.py'}]}}), 'server.py': 'BUSINESS_MUST_NOT_RUN',
                            'http.json': json.dumps({'name': 'docs', 'server': {'url': {'binding': 'endpoint'}}})}, 'stdio.json',
                 options={'entry': entry, **({'enabled': False, 'tool_timeout_sec': 7.5} if agent == 'codex' else {'timeout': 12000})},
                 requires={'bindings': {'program': {'kind': 'executable', 'required': False},
                                         'endpoint': {'kind': 'endpoint', 'required': False}}},
                 bindings={'endpoint': 'endpoints.docs'} if entry == 'http' else {'program': 'executables.bash'})
        for compatible in self.catalog['resources']['server']['compatibility'].values():
            compatible['entries'] = {'stdio': 'stdio.json', 'http': 'http.json'}
        self.save()

    def deploy(self):
        plan = self.manager().plan()
        self.assertTrue(plan.report['ready'], plan.report['blockers'])
        plan.apply(self.state)
        return plan

    def native_server(self):
        if self.profile['agent_type'] == 'codex':
            return tomlkit.parse(self.manager().native_path.read_text()).unwrap()['mcp_servers']['docs']
        return json.loads((self.native / '.claude.json').read_text())['mcpServers']['docs']

    def test_select_http_preserves_usage_and_logical_export(self):
        self.server()
        self.deploy()
        self.assertEqual(self.native_server(), {'url': 'https://example.invalid/mcp', 'enabled': False, 'tool_timeout_sec': 7.5})
        self.assertTrue(self.manager().plan().report['ready'])
        plan = self.manager().export([], resource_refs=['server@test'])
        self.assertTrue(plan.report['ready'], plan.report['blockers'])
        plan.apply(self.state)
        profile = yaml.safe_load((self.data / self.machine['agents']['codex']['profile']).read_text())
        selected = profile['resources']['mcp_servers']['server@test']
        self.assertEqual(selected['options']['entry'], 'http')
        self.assertEqual(selected['bindings'], {'endpoint': 'endpoints.docs'})
        self.assertNotIn('https://example.invalid', json.dumps(profile))

    def test_switch_registered_entry_updates_owned_connection_and_can_rollback(self):
        self.server(entry='stdio')
        self.deploy()
        self.assertIn('command', self.native_server())
        self.profile['resources']['mcp_servers']['server@test']['options']['entry'] = 'http'
        self.profile['resources']['mcp_servers']['server@test']['bindings'] = {'endpoint': 'endpoints.docs'}
        self.save_profile()
        result = self.deploy()
        self.assertNotIn('command', self.native_server())
        self.assertEqual(self.native_server()['url'], 'https://example.invalid/mcp')
        self.assertTrue(any(row['path'][-1] == 'runtime_entry' and row['action'] == 'replace_entry' for row in result.report['decisions']))
        recovery = rollback_plan(self.manager(), Path(result.report['recovery']).parent.name)
        self.assertTrue(recovery.report['ready'], recovery.report['blockers'])
        recovery.apply(self.state)
        self.assertIn('command', self.native_server())

    def test_unknown_entry_and_missing_selected_binding_block_before_writes(self):
        self.server()
        selection = self.profile['resources']['mcp_servers']['server@test']
        for entry, bindings in [('other', {'endpoint': 'endpoints.docs'}), ('http', {})]:
            selection['options']['entry'], selection['bindings'] = entry, bindings
            self.save_profile()
            self.assertFalse(self.manager().plan().report['ready'])
            self.assertFalse(self.native.exists())

    def test_export_does_not_infer_entry_from_changed_connection(self):
        self.server()
        self.deploy()
        path = self.manager().native_path
        data = tomlkit.parse(path.read_text())
        data['mcp_servers']['docs']['url'] = 'https://example.invalid/changed'
        path.write_text(tomlkit.dumps(data))
        self.assertFalse(self.manager().export([], resource_refs=['server@test']).report['ready'])
        self.assertFalse(self.manager().plan().report['ready'])

    def test_claude_entry_switch_preserves_units(self):
        self.server('claude-code')
        self.deploy()
        self.assertEqual(self.native_server()['type'], 'http')
        self.assertEqual(self.native_server()['timeout'], 12000)

    def test_entry_change_after_preview_makes_plan_stale(self):
        self.server()
        plan = self.manager().plan()
        self.profile['resources']['mcp_servers']['server@test']['options']['entry'] = 'stdio'
        self.save_profile()
        with self.assertRaises(PlanStale):
            plan.apply(self.state)
        self.assertFalse(self.native.exists())

    def test_named_entries_reject_non_mcp_and_missing_files(self):
        self.server()
        item = self.catalog['resources']['server']
        item['kind'] = 'hook'
        with self.assertRaises(ValueError):
            validate_catalog(self.catalog)
        item['kind'] = 'mcp'
        item['compatibility']['codex']['entries']['missing'] = 'missing.json'
        self.save()
        self.assertFalse(self.manager().plan().report['ready'])

    def test_missing_default_entry_is_rejected_even_when_another_is_selected(self):
        self.server()
        self.catalog['resources']['server']['compatibility']['codex']['entry'] = 'absent.json'
        self.save()
        self.assertFalse(self.manager().plan().report['ready'])

    def test_entry_is_a_source_selection_not_a_native_merge_field(self):
        self.server()
        with self.assertRaisesRegex(ValueError, 'not a registered resource option leaf'):
            self.manager().plan(run_policy={'fields': {'/resources/mcp_servers/server@test/options/entry': 'keep_target'}})


if __name__ == '__main__':
    unittest.main()
