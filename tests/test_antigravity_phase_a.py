"""Antigravity Phase A: read-only discovery, doctor, schema, and plan-only guards."""
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from claude_config.cli import main as cli_main
from claude_config.core.catalog_schema import validate_catalog
from claude_config.core.cutover import Cutover
from claude_config.core.diagnostics import agent_processes, app_instances
from claude_config.core.manager import ProfileManager
from claude_config.core.native import parse_native
from claude_config.core.resource_inventory import inspect_antigravity
from claude_config.core.schema import SchemaError, validate_machine, validate_profile
from claude_config.setup import doctor


class AntigravitySchemaTests(unittest.TestCase):
    def _base_machine(self):
        return {
            'schema_version': 1,
            'machine_id': 'ag-test',
            'config_dir': '/tmp/ag-data',
            'catalog_roots': {},
            'agents': {},
            'apps': {},
            'bindings': {},
        }

    def test_machine_antigravity_binding_variants(self):
        base = self._base_machine()
        # application only (GUI-only machine without agy CLI)
        gui_only = {**base, 'agents': {'antigravity': {
            'profile': 'profiles/agents/antigravity/work.yaml',
            'binding': {'config_root': '/tmp/gemini-config', 'application': '/Applications/Antigravity.app'},
        }}}
        validate_machine(gui_only)

        # executable only (CLI-only)
        cli_only = {**base, 'agents': {'antigravity': {
            'profile': 'profiles/agents/antigravity/work.yaml',
            'binding': {'config_root': '/tmp/gemini-config', 'executable': '/usr/local/bin/agy'},
        }}}
        validate_machine(cli_only)

        # both + optional cli_settings
        both = {**base, 'agents': {'antigravity': {
            'profile': 'profiles/agents/antigravity/work.yaml',
            'binding': {
                'config_root': '/tmp/gemini-config',
                'executable': '/usr/local/bin/agy',
                'application': '/Applications/Antigravity.app',
                'cli_settings': '/tmp/gemini-cli/settings.json',
            },
        }}}
        validate_machine(both)

        # neither executable nor application -> rejected
        neither = {**base, 'agents': {'antigravity': {
            'profile': 'profiles/agents/antigravity/work.yaml',
            'binding': {'config_root': '/tmp/gemini-config', 'cli_settings': '/tmp/gemini-cli/settings.json'},
        }}}
        with self.assertRaises(SchemaError):
            validate_machine(neither)

        # relative path or unknown key -> rejected
        bad_key = {**base, 'agents': {'antigravity': {
            'profile': 'profiles/agents/antigravity/work.yaml',
            'binding': {'config_root': '/tmp/gemini-config', 'executable': '/usr/local/bin/agy', 'user_config': '/tmp/bad'},
        }}}
        with self.assertRaises(SchemaError):
            validate_machine(bad_key)

        rel_path = {**base, 'agents': {'antigravity': {
            'profile': 'profiles/agents/antigravity/work.yaml',
            'binding': {'config_root': 'relative/path', 'executable': '/usr/local/bin/agy'},
        }}}
        with self.assertRaises(SchemaError):
            validate_machine(rel_path)

    def test_existing_claude_and_codex_machine_rules_unchanged(self):
        base = self._base_machine()
        # claude-code still requires executable + config_root (application alone is rejected)
        for bad_agent, binding in (
            ('claude-code', {'config_root': '/tmp/claude', 'application': '/Applications/Claude.app'}),
            ('codex', {'config_root': '/tmp/codex', 'executable': '/usr/local/bin/codex', 'cli_settings': '/tmp/s.json'}),
        ):
            machine = {**base, 'agents': {bad_agent: {
                'profile': f'profiles/agents/{bad_agent}/work.yaml',
                'binding': binding,
            }}}
            with self.assertRaises(SchemaError):
                validate_machine(machine)
        validate_profile({'schema_version': 1, 'agent_type': 'antigravity'})

    def test_catalog_schema_accepts_antigravity_entries(self):
        catalog = {
            'schema_version': 1,
            'name': 'ag-cat',
            'resources': {
                'my-plugin': {
                    'kind': 'plugin',
                    'description': 'Antigravity plugin',
                    'origin': {'type': 'self', 'author': 'tester'},
                    'maintenance': {'owner': 'ours', 'mode': 'original'},
                    'source': {'kind': 'tracked', 'path': 'packages/my-plugin'},
                    'compatibility': {
                        'antigravity': {'mode': 'native', 'entry': 'plugin.json', 'validation': {'status': 'not-evaluated'}},
                    },
                    'requires': {},
                    'update': {'strategy': 'manual'},
                },
                'dot-plugin': {
                    'kind': 'plugin',
                    'description': 'Antigravity dot-plugin',
                    'origin': {'type': 'self', 'author': 'tester'},
                    'maintenance': {'owner': 'ours', 'mode': 'original'},
                    'source': {'kind': 'tracked', 'path': 'packages/dot-plugin'},
                    'compatibility': {
                        'antigravity': {'mode': 'native', 'entry': '.antigravity-plugin/plugin.json', 'validation': {'status': 'passed', 'level': 'structural', 'evidence': 'CHECK.md'}},
                    },
                    'requires': {},
                    'update': {'strategy': 'manual'},
                },
                'my-skill': {
                    'kind': 'skill',
                    'description': 'Antigravity skill',
                    'origin': {'type': 'self', 'author': 'tester'},
                    'maintenance': {'owner': 'ours', 'mode': 'original'},
                    'source': {'kind': 'tracked', 'path': 'packages/my-skill'},
                    'compatibility': {
                        'antigravity': {'mode': 'native', 'entry': 'SKILL.md', 'validation': {'status': 'not-evaluated'}},
                    },
                    'requires': {},
                    'update': {'strategy': 'manual'},
                },
                'my-hooks': {
                    'kind': 'hook',
                    'description': 'Antigravity hooks',
                    'origin': {'type': 'self', 'author': 'tester'},
                    'maintenance': {'owner': 'ours', 'mode': 'original'},
                    'source': {'kind': 'tracked', 'path': 'packages/my-hooks'},
                    'compatibility': {
                        'antigravity': {'mode': 'native', 'entry': 'hooks.json', 'validation': {'status': 'not-evaluated'}},
                    },
                    'requires': {},
                    'update': {'strategy': 'manual'},
                },
                'my-mcp': {
                    'kind': 'mcp',
                    'description': 'Antigravity MCP',
                    'origin': {'type': 'self', 'author': 'tester'},
                    'maintenance': {'owner': 'ours', 'mode': 'original'},
                    'source': {'kind': 'tracked', 'path': 'packages/my-mcp'},
                    'compatibility': {
                        'antigravity': {
                            'mode': 'native',
                            'entry': 'mcp_config.json',
                            'entries': {'stdio': 'mcp_config.json', 'http': 'http_mcp.json'},
                            'validation': {'status': 'not-evaluated'},
                        },
                    },
                    'requires': {},
                    'update': {'strategy': 'manual'},
                },
            },
        }
        validate_catalog(catalog)

        # Escaping path and unknown host still rejected
        bad_path = json.loads(json.dumps(catalog))
        bad_path['resources']['my-skill']['compatibility']['antigravity']['entry'] = '../SKILL.md'
        with self.assertRaises(ValueError):
            validate_catalog(bad_path)

        bad_host = json.loads(json.dumps(catalog))
        bad_host['resources']['my-skill']['compatibility']['unknown-agent'] = {'mode': 'native', 'entry': 'SKILL.md', 'validation': {'status': 'not-evaluated'}}
        with self.assertRaises(ValueError):
            validate_catalog(bad_host)


class AntigravityPhaseAFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='ag-phase-a-')
        self.root = Path(self.temp.name)
        self.data = self.root / 'data'
        self.state = self.root / 'state'
        self.ag_config = self.root / 'gemini-config'
        self.ag_config.mkdir(parents=True)
        self.app_bundle = self.root / 'Antigravity.app'
        (self.app_bundle / 'Contents').mkdir(parents=True)
        self.profile_rel = 'profiles/agents/antigravity/work.yaml'
        profile_path = self.data / self.profile_rel
        profile_path.parent.mkdir(parents=True)
        profile_path.write_text(yaml.safe_dump({'schema_version': 1, 'agent_type': 'antigravity'}))
        self.machine_path = self.root / 'machine.yaml'
        self.machine = {
            'schema_version': 1,
            'machine_id': 'ag-isolated',
            'config_dir': str(self.data),
            'catalog_roots': {},
            'agents': {
                'antigravity': {
                    'profile': self.profile_rel,
                    'binding': {
                        'config_root': str(self.ag_config),
                        'application': str(self.app_bundle),
                    },
                },
            },
            'apps': {},
            'bindings': {},
            'runtime': {'state_dir': str(self.state)},
        }
        self.save_machine()

    def tearDown(self):
        self.temp.cleanup()

    def save_machine(self):
        self.machine_path.write_text(yaml.safe_dump(self.machine, sort_keys=False))

    def snapshot_tree(self, root):
        if not root.exists():
            return {}
        return {
            str(p.relative_to(root)): p.read_bytes()
            for p in sorted(root.rglob('*'))
            if p.is_file()
        }


class AntigravityDiscoveryAndGuardTests(AntigravityPhaseAFixture):
    def test_config_json_redacts_sensitive_fields_and_reads_plugin_enabled_only(self):
        config_json = self.ag_config / 'config.json'
        config_payload = {
            'userSettings': {'email': 'PRIVATE_USER_SETTING', 'theme': 'dark'},
            'globalPermissionGrants': ['PRIVATE_GRANT_BASH'],
            'sidecars': {'secretSidecar': 'PRIVATE_SIDECAR'},
            'auth_token': 'PRIVATE_AUTH_TOKEN',
            'apiKey': 'PRIVATE_API_KEY',
            'plugins': {
                'alpha': {'enabled': False, 'auth_token': 'PRIVATE_PLUGIN_TOKEN'},
                '-187073': {'enabled': True},
            },
        }
        config_json.write_text(json.dumps(config_payload))
        before = config_json.read_bytes()

        # Standalone plugin + dot-antigravity plugin + standalone skill + MCP
        (self.ag_config / 'plugins/alpha').mkdir(parents=True)
        (self.ag_config / 'plugins/alpha/plugin.json').write_text(json.dumps({'name': 'alpha'}))
        (self.ag_config / 'plugins/alpha/hooks.json').write_text(json.dumps({'h': {}}))
        (self.ag_config / 'plugins/alpha/mcp_config.json').write_text(json.dumps({
            'mcpServers': {'alpha-srv': {'command': 'npx', 'env': {'TOKEN': 'PRIVATE_MCP_ENV'}}},
        }))
        (self.ag_config / 'plugins/-187073/.antigravity-plugin').mkdir(parents=True)
        (self.ag_config / 'plugins/-187073/.antigravity-plugin/plugin.json').write_text(json.dumps({'name': 'marketplace-solo'}))
        (self.ag_config / 'skills/solo-skill').mkdir(parents=True)
        (self.ag_config / 'skills/solo-skill/SKILL.md').write_text('---\nname: solo-skill\ndescription: fixture\n---\nPRIVATE_SKILL_BODY\n')
        (self.ag_config / 'mcp_config.json').write_text(json.dumps({
            'mcpServers': {'global-srv': {'url': 'https://example.invalid/mcp', 'headers': {'Authorization': 'PRIVATE_HEADER'}}},
        }))

        sanitized = parse_native('antigravity', config_json.read_bytes())
        self.assertEqual(sanitized, {'plugins': {'alpha': {'enabled': False}, '-187073': {'enabled': True}}})

        inv = inspect_antigravity(self.ag_config)
        self.assertEqual(inv['config_status'], 'read')
        self.assertEqual(inv['plugins_enabled'], {'alpha': False, '-187073': True})
        self.assertEqual([p['dir'] for p in inv['plugins']], ['-187073', 'alpha'])
        by_dir = {p['dir']: p for p in inv['plugins']}
        self.assertEqual(by_dir['-187073']['name'], 'marketplace-solo')
        self.assertEqual(by_dir['-187073']['manifest_kind'], 'antigravity-plugin')
        self.assertTrue(by_dir['-187073']['dir_name_mismatch'])
        self.assertTrue(by_dir['-187073']['enabled'])
        self.assertFalse(by_dir['alpha']['enabled'])
        self.assertTrue(by_dir['alpha']['has_hooks'])
        self.assertEqual(by_dir['alpha']['mcp_servers'], ['alpha-srv'])
        self.assertEqual(inv['mcp_servers'], ['global-srv'])
        self.assertEqual([s['dir'] for s in inv['skills']], ['solo-skill'])

        doc_report, _ = doctor(self.machine_path, resources=True)
        self.assertTrue(doc_report['ready'], doc_report)
        plan = ProfileManager(self.machine_path, 'antigravity').plan()
        self.assertTrue(plan.report['ready'], plan.report)

        for blob in (json.dumps(inv), json.dumps(doc_report), json.dumps(plan.report)):
            for secret in ('PRIVATE_USER_SETTING', 'PRIVATE_GRANT_BASH', 'PRIVATE_SIDECAR',
                           'PRIVATE_AUTH_TOKEN', 'PRIVATE_API_KEY', 'PRIVATE_PLUGIN_TOKEN',
                           'PRIVATE_MCP_ENV', 'PRIVATE_SKILL_BODY', 'PRIVATE_HEADER'):
                self.assertNotIn(secret, blob)
        self.assertEqual(config_json.read_bytes(), before)

    def test_duplicate_plugin_name_and_dir_vs_name_collision_detected(self):
        # Case 1: Two plugin directories declare same plugin.json.name (including omitted default)
        (self.ag_config / 'plugins/shared').mkdir(parents=True)
        (self.ag_config / 'plugins/shared/plugin.json').write_text(json.dumps({'description': 'defaults name to shared'}))
        (self.ag_config / 'plugins/other').mkdir(parents=True)
        (self.ag_config / 'plugins/other/plugin.json').write_text(json.dumps({'name': 'shared'}))

        # Case 2: dir_a has manifest name 'dir_b', while dir_b exists with manifest name 'distinct'
        (self.ag_config / 'plugins/dir_a').mkdir(parents=True)
        (self.ag_config / 'plugins/dir_a/plugin.json').write_text(json.dumps({'name': 'dir_b'}))
        (self.ag_config / 'plugins/dir_b').mkdir(parents=True)
        (self.ag_config / 'plugins/dir_b/plugin.json').write_text(json.dumps({'name': 'distinct'}))

        doc_plain, _ = doctor(self.machine_path)
        doc_res, _ = doctor(self.machine_path, resources=True)
        plan = ProfileManager(self.machine_path, 'antigravity').plan()

        for report in (doc_plain, doc_res, plan.report):
            self.assertFalse(report['ready'])
            codes = {row.get('code') for row in report['checks'] if row['status'] == 'blocked'}
            self.assertIn('duplicate_plugin_name', codes)
            self.assertIn('plugin_dir_name_collision', codes)

    def test_cross_kind_skill_vs_plugin_overlap_detected(self):
        (self.ag_config / 'skills/overlap-skill').mkdir(parents=True)
        (self.ag_config / 'skills/overlap-skill/SKILL.md').write_text('---\nname: overlap-skill\ndescription: global\n---\n')
        (self.ag_config / 'plugins/bundle/skills/overlap-skill').mkdir(parents=True)
        (self.ag_config / 'plugins/bundle/plugin.json').write_text(json.dumps({'name': 'bundle'}))
        (self.ag_config / 'plugins/bundle/skills/overlap-skill/SKILL.md').write_text('---\nname: overlap-skill\ndescription: bundled\n---\n')

        doc_report, _ = doctor(self.machine_path)
        plan = ProfileManager(self.machine_path, 'antigravity').plan()
        for report in (doc_report, plan.report):
            self.assertFalse(report['ready'])
            self.assertIn('skill_plugin_overlap', {row.get('code') for row in report['checks'] if row['status'] == 'blocked'})

    def test_global_vs_plugin_mcp_collision_detected(self):
        (self.ag_config / 'mcp_config.json').write_text(json.dumps({'mcpServers': {'shared-mcp': {'url': 'https://example.invalid/mcp'}}}))
        (self.ag_config / 'plugins/bundle').mkdir(parents=True)
        (self.ag_config / 'plugins/bundle/plugin.json').write_text(json.dumps({'name': 'bundle'}))
        (self.ag_config / 'plugins/bundle/mcp_config.json').write_text(json.dumps({'mcpServers': {'shared-mcp': {'command': 'npx'}}}))

        doc_report, _ = doctor(self.machine_path)
        plan = ProfileManager(self.machine_path, 'antigravity').plan()
        for report in (doc_report, plan.report):
            self.assertFalse(report['ready'])
            self.assertIn('mcp_server_collision', {row.get('code') for row in report['checks'] if row['status'] == 'blocked'})

    def test_process_diagnostics_detects_antigravity_processes(self):
        self.machine['agents']['antigravity']['binding']['executable'] = str(self.root / 'agy')
        self.save_machine()
        ps_out = '\n'.join([
            '101 /Applications/Antigravity.app/Contents/MacOS/Antigravity',
            '102 /Applications/Antigravity.app/Contents/Resources/app/extensions/antigravity/bin/language_server_macos_arm',
            f'103 {self.root}/agy',
            '104 /usr/bin/unrelated',
        ])
        def runner(args, **kwargs):
            return subprocess.CompletedProcess(args, 0, ps_out, '')
        observed = agent_processes(self.machine, runner=runner)
        self.assertEqual(observed['status'], 'observed')
        kinds = {row['kind'] for row in observed['instances']}
        self.assertEqual(kinds, {'application', 'language_server', 'cli'})

        # app_instances still works cleanly even if Antigravity.app has no Info.plist yet
        with patch('sys.platform', 'darwin'):
            apps_obs = app_instances(self.machine, runner=runner)
        self.assertEqual(apps_obs['status'], 'observed')

        with patch('claude_config.core.diagnostics.agent_processes', return_value=observed), \
             patch('claude_config.core.diagnostics.app_instances', return_value={'status': 'observed', 'instances': []}):
            doc_report, _ = doctor(self.machine_path, processes=True)
        self.assertEqual(doc_report['agent_processes'], observed)

    def test_plan_and_validate_read_only_and_apply_blocked_with_zero_writes(self):
        (self.ag_config / 'config.json').write_text(json.dumps({'userSettings': {'keep': True}, 'plugins': {'p1': {'enabled': True}}}))
        before = self.snapshot_tree(self.ag_config)

        # Direct Python API: plan succeeds, apply raises phase_a_read_only
        manager = ProfileManager(self.machine_path, 'antigravity')
        plan = manager.plan()
        self.assertTrue(plan.report['ready'], plan.report['blockers'])
        self.assertEqual(plan.changes, ())
        with self.assertRaisesRegex(SchemaError, 'phase_a_read_only'):
            plan.apply(self.state)
        self.assertEqual(self.snapshot_tree(self.ag_config), before)
        self.assertFalse((self.state / 'runs').exists())

        # CLI validate, plan, apply --dry-run succeed read-only
        for subcmd in ('validate', 'plan'):
            out = io.StringIO()
            with redirect_stdout(out):
                code = cli_main([subcmd, '--machine', str(self.machine_path), '--agent', 'antigravity', '--format', 'json'])
            self.assertEqual(code, 0, out.getvalue())
            report = json.loads(out.getvalue())
            self.assertTrue(report['ready'])
            self.assertTrue(report['preview_only'])

        out = io.StringIO()
        with redirect_stdout(out):
            code = cli_main(['plan', '--machine', str(self.machine_path), '--agent', 'antigravity', '--format', 'text'])
        self.assertEqual(code, 0, out.getvalue())
        self.assertIn('Antigravity', out.getvalue())

        out = io.StringIO()
        with redirect_stdout(out):
            code = cli_main(['apply', '--dry-run', '--machine', str(self.machine_path), '--agent', 'antigravity', '--format', 'json'])
        self.assertEqual(code, 0, out.getvalue())
        self.assertTrue(json.loads(out.getvalue())['preview_only'])

        # CLI apply (without --dry-run) and switch are explicitly blocked with phase_a_read_only
        other_profile = self.data / 'profiles/agents/antigravity/other.yaml'
        other_profile.write_text(yaml.safe_dump({'schema_version': 1, 'agent_type': 'antigravity'}))
        for argv in (
            ['apply', '--machine', str(self.machine_path), '--agent', 'antigravity', '--format', 'json'],
            ['switch', '--machine', str(self.machine_path), '--agent', 'antigravity', '--profile', 'profiles/agents/antigravity/other.yaml', '--format', 'json'],
        ):
            out = io.StringIO()
            with redirect_stdout(out):
                code = cli_main(argv)
            self.assertEqual(code, 1, out.getvalue())
            report = json.loads(out.getvalue())
            self.assertFalse(report['ready'])
            self.assertFalse(report['execution']['started'])
            self.assertIn('phase_a_read_only', [b.get('code') for b in report['blockers']])

        self.assertEqual(self.snapshot_tree(self.ag_config), before)
        self.assertFalse((self.state / 'runs').exists())

        # Cutover with antigravity is also blocked
        spec_path = self.root / 'cutover.yaml'
        spec_path.write_text(yaml.safe_dump({
            'schema_version': 2,
            'machine': str(self.machine_path),
            'machine_target': str(self.root / 'new-machine.yaml'),
            'data_target': str(self.root / 'new-data'),
        }))
        with self.assertRaisesRegex(SchemaError, 'phase_a_read_only'):
            Cutover(spec_path)


if __name__ == '__main__':
    unittest.main()
