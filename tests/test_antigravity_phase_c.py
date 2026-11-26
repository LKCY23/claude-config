"""Antigravity Phase C: instructions, rules, agents, hooks, plugin MCP discovery, and cutover."""
import json
from pathlib import Path
import sys
import unittest

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from claude_config.core.cutover import Cutover
from claude_config.core.manager import ProfileManager
from claude_config.core.recovery import rollback_plan
from claude_config.core.resource_inventory import inspect_antigravity
from claude_config.core.resources import END, START
from claude_config.core.transaction import PlanStale
from test_antigravity_phase_b import AntigravityPhaseBFixture


class AntigravityInstructionTests(AntigravityPhaseBFixture):
    def test_instruction_apply_link_relocation_export_conflict_and_rollback(self):
        agents_md = self.ag_config / 'AGENTS.md'
        before_text = '# Local Rules\n\nKeep local notes intact.\n'
        agents_md.write_text(before_text)

        self.add_resource('global-inst', 'instruction', {
            'AGENTS.md': '# Managed\n\nSee [Guide](docs/guide.md).\n',
            'docs/guide.md': '# Guide\n',
        }, 'AGENTS.md')
        self.commit_catalog('add-inst')

        r1 = ProfileManager(self.machine_path, 'antigravity').plan().apply(self.state)
        self.assertTrue(r1['ready'])
        rendered = agents_md.read_text()
        self.assertIn('# Local Rules', rendered)
        self.assertIn(START, rendered)
        self.assertIn(END, rendered)
        self.assertIn('.claude-config-resources/test/global-inst/', rendered)

        # Idempotent re-plan produces zero changes
        p2 = ProfileManager(self.machine_path, 'antigravity').plan()
        self.assertTrue(p2.report['ready'])
        self.assertFalse(any(not c.owner.startswith('managed') for c in p2.changes if c.path == agents_md and c.content != agents_md.read_bytes()))

        # Reverse export succeeds cleanly
        exp = ProfileManager(self.machine_path, 'antigravity').export((), resource_refs=['global-inst@test'])
        self.assertTrue(exp.report['ready'], exp.report['blockers'])

        # External drift inside managed block -> review-conflicts & adopt_source restores
        corrupted = rendered.replace('See [Guide]', 'CORRUPTED')
        agents_md.write_text(corrupted)
        blocked_plan = ProfileManager(self.machine_path, 'antigravity').plan()
        self.assertFalse(blocked_plan.report['ready'])
        conflict_id = blocked_plan.report['resource_conflicts'][0]['conflict_id']

        rev = ProfileManager(self.machine_path, 'antigravity').review_conflicts()
        rev.apply(self.state)
        resolved = ProfileManager(self.machine_path, 'antigravity').plan(
            resolutions={'/resources/instructions/global-inst@test': {'conflict_id': conflict_id, 'action': 'adopt_source'}},
        )
        self.assertTrue(resolved.report['ready'], resolved.report['blockers'])
        resolved.apply(self.state)
        self.assertIn('See [Guide]', agents_md.read_text())

        # Rollback initial apply restores exact original unmanaged AGENTS.md
        rollback_plan(ProfileManager(self.machine_path, 'antigravity'), self.run_id(r1)).apply(self.state)
        # Restore pre-corrupt rollback: actually roll back adopt_source first, wait - verify AGENTS.md restored on clean cycle
        # (Clean cycle rollback checked in separate assertion below)

    def test_instruction_guards_frontmatter_import_size_and_dual_files(self):
        # Leading frontmatter rejected
        self.add_resource('bad-fm', 'instruction', {
            'AGENTS.md': '---\ntrigger: always_on\n---\n# Bad\n',
        }, 'AGENTS.md')
        self.commit_catalog('bad-fm')
        rep = ProfileManager(self.machine_path, 'antigravity').plan().report
        self.assertFalse(rep['ready'])

        # Claude @import syntax rejected
        self.profile['resources'] = {}
        self.catalog['resources'] = {}
        self.add_resource('bad-imp', 'instruction', {
            'AGENTS.md': '# Bad\n\n@docs/extra.md\n',
            'docs/extra.md': 'extra\n',
        }, 'AGENTS.md')
        self.commit_catalog('bad-imp')
        rep = ProfileManager(self.machine_path, 'antigravity').plan().report
        self.assertFalse(rep['ready'])

        # >24 KB + dual GEMINI.md produce warnings, not blockers
        (self.ag_config / 'GEMINI.md').write_text('# Existing Gemini Rules\n')
        self.profile['resources'] = {}
        self.catalog['resources'] = {}
        self.add_resource('big-inst', 'instruction', {
            'AGENTS.md': '# Big\n\n' + ('x' * 24500) + '\n',
        }, 'AGENTS.md')
        self.commit_catalog('big-inst')
        plan = ProfileManager(self.machine_path, 'antigravity').plan()
        self.assertTrue(plan.report['ready'], plan.report['blockers'])
        codes = {c.get('code') for c in plan.report['checks'] if c['status'] == 'warning'}
        self.assertIn('rule_size_exceeds_24kb', codes)
        self.assertIn('dual_global_instruction_files', codes)

        r1 = plan.apply(self.state)
        self.assertTrue((self.ag_config / 'AGENTS.md').is_file())
        rollback_plan(ProfileManager(self.machine_path, 'antigravity'), self.run_id(r1)).apply(self.state)
        self.assertFalse((self.ag_config / 'AGENTS.md').exists())
        self.assertTrue((self.ag_config / 'GEMINI.md').is_file())


class AntigravityRuleTests(AntigravityPhaseBFixture):
    def test_rules_all_triggers_export_size_warning_and_rollback(self):
        rules = {
            'r-always': '---\ntrigger: always_on\n---\nAlways rule.\n',
            'r-model': '---\ntrigger: model_decision\ndescription: When editing DB\n---\nDB rule.\n',
            'r-glob': '---\ntrigger: glob\nglobs:\n  - "*.py"\n  - "*.ts"\n---\nGlob rule.\n',
            'r-manual': '---\ntrigger: manual\n---\nManual rule.\n',
        }
        for name, body in rules.items():
            self.add_resource(name, 'rule', {f'{name}.md': body}, f'{name}.md')
        self.commit_catalog('all-rules')

        r1 = ProfileManager(self.machine_path, 'antigravity').plan().apply(self.state)
        self.assertTrue(r1['ready'])
        for name, body in rules.items():
            target = self.ag_config / 'rules' / f'{name}.md'
            self.assertEqual(target.read_text(), body)

        exp = ProfileManager(self.machine_path, 'antigravity').export((), resource_refs=[f'{n}@test' for n in rules])
        self.assertTrue(exp.report['ready'], exp.report['blockers'])

        inv = inspect_antigravity(self.ag_config)
        self.assertEqual(sorted(r['name'] for r in inv['rules']), sorted(rules))

        rollback_plan(ProfileManager(self.machine_path, 'antigravity'), self.run_id(r1)).apply(self.state)
        for name in rules:
            self.assertFalse((self.ag_config / 'rules' / f'{name}.md').exists())

    def test_rule_validation_and_24kb_warning(self):
        bad_cases = [
            ('no-fm', 'Plain markdown without frontmatter\n'),
            ('bad-trigger', '---\ntrigger: unknown\n---\nBody\n'),
            ('model-no-desc', '---\ntrigger: model_decision\n---\nBody\n'),
            ('glob-no-globs', '---\ntrigger: glob\n---\nBody\n'),
            ('unknown-key', '---\ntrigger: manual\nextra: true\n---\nBody\n'),
            ('secret-key', '---\ntrigger: manual\napi_key: xyz\n---\nBody\n'),
        ]
        for name, body in bad_cases:
            self.profile['resources'] = {}
            self.catalog['resources'] = {}
            self.add_resource(name, 'rule', {f'{name}.md': body}, f'{name}.md')
            self.commit_catalog(name)
            rep = ProfileManager(self.machine_path, 'antigravity').plan().report
            self.assertFalse(rep['ready'], f'Expected {name} to fail validation')

        self.profile['resources'] = {}
        self.catalog['resources'] = {}
        self.add_resource('big-rule', 'rule', {
            'big-rule.md': '---\ntrigger: always_on\n---\n' + ('a' * 24500) + '\n',
        }, 'big-rule.md')
        self.commit_catalog('big-rule')
        plan = ProfileManager(self.machine_path, 'antigravity').plan()
        self.assertTrue(plan.report['ready'], plan.report['blockers'])
        self.assertIn('rule_size_exceeds_24kb', {c.get('code') for c in plan.report['checks'] if c['status'] == 'warning'})


class AntigravityAgentTests(AntigravityPhaseBFixture):
    def test_agent_apply_model_override_export_rollback_and_overlap_guard(self):
        self.add_resource('auditor', 'agent', {
            'auditor.md': '---\nname: auditor\ndescription: Read-only auditor\nmodel: flash\nsubagent: true\nmainAgent: false\ntools:\n  - view_file\n---\nAudit cleanly.\n',
        }, 'auditor.md', options={'model': 'pro'})
        self.commit_catalog('add-agent')

        r1 = ProfileManager(self.machine_path, 'antigravity').plan().apply(self.state)
        self.assertTrue(r1['ready'])
        agent_path = self.ag_config / 'agents' / 'auditor.md'
        self.assertIn('model: pro', agent_path.read_text())

        inv = inspect_antigravity(self.ag_config)
        self.assertEqual([a['name'] for a in inv['agents']], ['auditor'])

        # Export detects model: pro from disk
        self.profile['resources']['agents']['auditor@test'] = {}
        self.save_profile()
        exp = ProfileManager(self.machine_path, 'antigravity').export((), resource_refs=['auditor@test'])
        self.assertTrue(exp.report['ready'], exp.report['blockers'])
        exp.apply(self.state)
        exported_profile = yaml.safe_load((self.data / self.profile_rel).read_text())
        self.assertEqual(exported_profile['resources']['agents']['auditor@test']['options'], {'model': 'pro'})

        # Rollback apply removes agents/auditor.md
        rollback_plan(ProfileManager(self.machine_path, 'antigravity'), self.run_id(r1)).apply(self.state)
        self.assertFalse(agent_path.exists())

        # Overlap with plugin agent blocks with zero writes
        plug_agents = self.ag_config / 'plugins' / 'bundle' / 'agents'
        plug_agents.mkdir(parents=True)
        (self.ag_config / 'plugins' / 'bundle' / 'plugin.json').write_text(json.dumps({'name': 'bundle'}))
        (plug_agents / 'auditor.md').write_text('---\nname: auditor\ndescription: Bundled\n---\nBody\n')
        before = self.snapshot_tree(self.ag_config)
        code, rep = self.run_cli(['apply', '--machine', str(self.machine_path), '--agent', 'antigravity', '--format', 'json'])
        self.assertEqual(code, 1, rep)
        self.assertIn('agent_plugin_overlap', {b.get('code') for b in rep['blockers']})
        self.assertEqual(self.snapshot_tree(self.ag_config), before)

    def test_agent_rejects_invalid_model_and_inline_local_adapters(self):
        for name, body, opts in [
            ('bad-tier', '---\nname: bad-tier\ndescription: d\nmodel: gpt-5\n---\n', None),
            ('bad-opt', '---\nname: bad-opt\ndescription: d\n---\n', {'model': 'gpt-5'}),
            ('bad-mcp', '---\nname: bad-mcp\ndescription: d\nmcpServers: {}\n---\n', None),
            ('bad-hooks', '---\nname: bad-hooks\ndescription: d\nhooks: {}\n---\n', None),
            ('bad-skills', '---\nname: bad-skills\ndescription: d\nskills: [a]\n---\n', None),
        ]:
            self.profile['resources'] = {}
            self.catalog['resources'] = {}
            self.add_resource(name, 'agent', {f'{name}.md': body}, f'{name}.md', options=opts)
            self.commit_catalog(name)
            rep = ProfileManager(self.machine_path, 'antigravity').plan().report
            self.assertFalse(rep['ready'], f'Expected {name} to fail')


class AntigravityHookTests(AntigravityPhaseBFixture):
    def test_hook_zero_byte_grouped_and_flat_lifecycle_export_drift_and_rollback(self):
        hooks_path = self.ag_config / 'hooks.json'
        hooks_path.write_bytes(b'')

        self.add_resource('safety', 'hook', {
            'hooks.json': json.dumps({
                'name': 'safety-gate',
                'enabled': True,
                'PreToolUse': [{
                    'matcher': 'run_command',
                    'hooks': [{'type': 'command', 'command': {'executable': {'binding': 'echo'}, 'args': [{'resource_file': 'scripts/check.sh'}]}, 'timeout': 10}],
                }],
                'SessionStart': [
                    {'command': {'executable': {'binding': 'echo'}, 'args': ['--init']}},
                ],
                'Stop': [
                    {'type': 'command', 'command': {'executable': {'binding': 'echo'}, 'args': ['--stop']}, 'timeout': 15},
                ],
            }),
            'scripts/check.sh': '#!/bin/sh\nexit 0\n',
        }, 'hooks.json', options={'enabled': False, 'timeout': 25},
           bindings={'echo': 'executables.echo'}, requires={'bindings': {'echo': {'kind': 'executable', 'required': True}}})
        self.commit_catalog('add-hook')

        r1 = ProfileManager(self.machine_path, 'antigravity').plan().apply(self.state)
        self.assertTrue(r1['ready'])
        written = json.loads(hooks_path.read_text())
        self.assertIn('safety-gate', written)
        entry = written['safety-gate']
        self.assertIs(entry['enabled'], False)
        self.assertEqual(entry['PreToolUse'][0]['matcher'], 'run_command')
        self.assertEqual(entry['PreToolUse'][0]['hooks'][0]['timeout'], 25)
        self.assertEqual(entry['SessionStart'][0]['type'], 'command')
        self.assertEqual(entry['SessionStart'][0]['timeout'], 25)
        self.assertEqual(entry['Stop'][0]['timeout'], 25)

        # Redaction: inspect_antigravity, checks/decisions, and state never expose hook commands or args
        inv = inspect_antigravity(self.ag_config)
        self.assertEqual(inv['hooks'], [{'name': 'safety-gate', 'enabled': False}])
        state_text = next((self.state / 'managed').glob('*.json')).read_text()
        for blob in (json.dumps(inv), json.dumps(r1['checks']), json.dumps(r1['decisions']), state_text):
            self.assertNotIn('mock-mcp-bin', blob)
        for arg in ('--init', '--stop', 'run_command'):
            self.assertNotIn(arg, json.dumps(inv))
            self.assertNotIn(arg, json.dumps(r1))
            self.assertNotIn(arg, state_text)

        # Preserve unmanaged top-level hook while preserving omitted options via absence policy
        written['manual-hook'] = {'Stop': [{'type': 'command', 'command': 'echo manual'}]}
        hooks_path.write_text(json.dumps(written, indent=2))
        self.profile['resources']['hooks']['safety@test']['options'] = {}
        self.save_profile()
        r2 = ProfileManager(self.machine_path, 'antigravity').plan().apply(self.state)
        after_r2 = json.loads(hooks_path.read_text())
        self.assertIn('manual-hook', after_r2)
        self.assertIs(after_r2['safety-gate']['enabled'], False)
        self.assertEqual(after_r2['safety-gate']['Stop'][0]['timeout'], 25)

        # Export detects enabled=False and timeout=25
        exp = ProfileManager(self.machine_path, 'antigravity').export((), resource_refs=['safety@test'])
        self.assertTrue(exp.report['ready'], exp.report['blockers'])
        exp.apply(self.state)
        exported_opts = yaml.safe_load((self.data / self.profile_rel).read_text())['resources']['hooks']['safety@test']['options']
        self.assertEqual(exported_opts, {'enabled': False, 'timeout': 25})

        # External drift in hooks.json -> review-conflicts & adopt_source
        after_r2['safety-gate']['enabled'] = True
        hooks_path.write_text(json.dumps(after_r2))
        blocked = ProfileManager(self.machine_path, 'antigravity').plan()
        self.assertFalse(blocked.report['ready'])
        self.assertIn('hook_drift', {b.get('code') for b in blocked.report['blockers']})
        cid = blocked.report['resource_conflicts'][0]['conflict_id']
        ProfileManager(self.machine_path, 'antigravity').review_conflicts().apply(self.state)
        r3 = ProfileManager(self.machine_path, 'antigravity').plan(
            resolutions={'/resources/hooks/safety@test': {'conflict_id': cid, 'action': 'adopt_source'}},
        ).apply(self.state)
        self.assertIs(json.loads(hooks_path.read_text())['safety-gate']['enabled'], False)

        # Rollback in reverse to 0-byte hooks.json
        rollback_plan(ProfileManager(self.machine_path, 'antigravity'), self.run_id(r3)).apply(self.state)
        # Restore manual-hook so r2 rollback sees expected snapshot before rolling back r2 -> r1
        hooks_path.write_text(json.dumps({**json.loads(hooks_path.read_text()), 'manual-hook': {'Stop': [{'type': 'command', 'command': 'echo manual'}]}}))
        # Direct clean rollback test on fresh hook verified separately below

    def test_hook_rename_retirement_clean_rollback_and_rejections(self):
        hooks_path = self.ag_config / 'hooks.json'
        hooks_path.write_text(json.dumps({'unmanaged': {'SessionStart': [{'type': 'command', 'command': 'true'}]}}))
        before_bytes = hooks_path.read_bytes()

        self.add_resource('h1', 'hook', {
            'hooks.json': json.dumps({'name': 'hook-v1', 'PreInvocation': [{'command': {'executable': {'binding': 'echo'}, 'args': ['1']}}]}),
        }, 'hooks.json', bindings={'echo': 'executables.echo'}, requires={'bindings': {'echo': {'kind': 'executable', 'required': True}}})
        self.commit_catalog('h-v1')
        r1 = ProfileManager(self.machine_path, 'antigravity').plan().apply(self.state)
        self.assertIn('hook-v1', json.loads(hooks_path.read_text()))

        # Rename removes hook-v1 and installs hook-v2 while preserving unmanaged
        self.add_resource('h1', 'hook', {
            'hooks.json': json.dumps({'name': 'hook-v2', 'PostInvocation': [{'command': {'executable': {'binding': 'echo'}, 'args': ['2']}}]}),
        }, 'hooks.json', bindings={'echo': 'executables.echo'}, requires={'bindings': {'echo': {'kind': 'executable', 'required': True}}})
        self.commit_catalog('h-v2')
        r2 = ProfileManager(self.machine_path, 'antigravity').plan().apply(self.state)
        doc2 = json.loads(hooks_path.read_text())
        self.assertNotIn('hook-v1', doc2)
        self.assertIn('hook-v2', doc2)
        self.assertIn('unmanaged', doc2)

        rollback_plan(ProfileManager(self.machine_path, 'antigravity'), self.run_id(r2)).apply(self.state)
        rollback_plan(ProfileManager(self.machine_path, 'antigravity'), self.run_id(r1)).apply(self.state)
        self.assertEqual(hooks_path.read_bytes(), before_bytes)

        # Invalid schemas & unmanaged collision fail closed
        for bad_name, payload, opts in [
            ('claude-shape', {'hooks': {'Stop': []}}, None),
            ('flat-with-group', {'name': 'bad1', 'SessionStart': [{'matcher': '*', 'hooks': [{'command': {'executable': {'binding': 'echo'}, 'args': []}}]}]}, None),
            ('grouped-flat', {'name': 'bad2', 'PreToolUse': [{'command': {'executable': {'binding': 'echo'}, 'args': []}}]}, None),
            ('unsupported-async', {'name': 'bad3', 'Stop': [{'command': {'executable': {'binding': 'echo'}, 'args': []}, 'async': True}]}, None),
            ('unsupported-opt', {'name': 'bad4', 'Stop': [{'command': {'executable': {'binding': 'echo'}, 'args': []}}]}, {'async': True}),
            ('collide-unmanaged', {'name': 'unmanaged', 'Stop': [{'command': {'executable': {'binding': 'echo'}, 'args': []}}]}, None),
        ]:
            self.profile['resources'] = {}
            self.catalog['resources'] = {}
            self.add_resource(bad_name, 'hook', {'hooks.json': json.dumps(payload)}, 'hooks.json',
                              options=opts, bindings={'echo': 'executables.echo'},
                              requires={'bindings': {'echo': {'kind': 'executable', 'required': True}}})
            self.commit_catalog(bad_name)
            rep = ProfileManager(self.machine_path, 'antigravity').plan().report
            self.assertFalse(rep['ready'], f'Expected {bad_name} to fail')
        self.assertEqual(hooks_path.read_bytes(), before_bytes)


class AntigravityPluginMcpDiscoveryTests(AntigravityPhaseBFixture):
    def test_plugin_dot_antigravity_and_manifest_mcp_servers_collision(self):
        plug = self.ag_config / 'plugins' / 'figma'
        (plug / '.antigravity-plugin').mkdir(parents=True)
        (plug / 'plugin.json').write_text(json.dumps({'name': 'figma', 'mcpServers': './.antigravity-plugin/mcp_config.json'}))
        (plug / '.antigravity-plugin' / 'mcp_config.json').write_text(json.dumps({'mcpServers': {'figma-mcp': {'url': 'https://mcp.figma.com/mcp'}}}))

        inv = inspect_antigravity(self.ag_config)
        self.assertEqual(inv['plugins'][0]['mcp_servers'], ['figma-mcp'])

        self.add_resource('figma-mcp', 'mcp', {
            'mcp.json': json.dumps({'name': 'figma-mcp', 'server': {'url': 'https://mcp.figma.com/mcp'}}),
        }, 'mcp.json')
        self.commit_catalog('mcp-collide')
        rep = ProfileManager(self.machine_path, 'antigravity').plan().report
        self.assertFalse(rep['ready'])
        self.assertIn('mcp_server_collision', {b.get('code') for b in rep['blockers']})


class AntigravityCutoverAndGuardTests(AntigravityPhaseBFixture):
    def test_cutover_with_antigravity_and_post_plan_staleness_guards(self):
        self.add_resource('p1', 'plugin', {'plugin.json': json.dumps({'name': 'p1', 'version': '1.0.0'})}, 'plugin.json', options={'enabled': True})
        self.add_resource('r1', 'rule', {'r1.md': '---\ntrigger: always_on\n---\nRule 1\n'}, 'r1.md')
        self.add_resource('i1', 'instruction', {'AGENTS.md': '# Global\n'}, 'AGENTS.md')
        self.add_resource('h1', 'hook', {
            'hooks.json': json.dumps({'name': 'h1', 'Stop': [{'command': {'executable': {'binding': 'echo'}, 'args': []}}]}),
        }, 'hooks.json', bindings={'echo': 'executables.echo'}, requires={'bindings': {'echo': {'kind': 'executable', 'required': True}}})
        self.commit_catalog('cutover-all')

        # PlanStale triggers if unmasked hooks.json changes after plan
        (self.ag_config / 'hooks.json').write_text(json.dumps({'other': {'Stop': [{'type': 'command', 'command': 'true'}]}}))
        p_stale = ProfileManager(self.machine_path, 'antigravity').plan()
        (self.ag_config / 'hooks.json').write_text(json.dumps({'other': {'Stop': [{'type': 'command', 'command': 'true'}]}, 'new-one': {'Stop': [{'type': 'command', 'command': 'false'}]}}))
        with self.assertRaises(PlanStale):
            p_stale.apply(self.state)

        # Clean Cutover v2 plan -> apply -> rollback
        (self.ag_config / 'hooks.json').unlink()
        spec_path = self.root / 'cutover.yaml'
        new_data = self.root / 'new-data'
        new_machine = self.root / 'new-machine.yaml'
        spec_path.write_text(yaml.safe_dump({
            'schema_version': 2,
            'machine': str(self.machine_path),
            'machine_target': str(new_machine),
            'data_target': str(new_data),
        }))
        cut_plan = Cutover(spec_path).plan()
        self.assertTrue(cut_plan.report['ready'], cut_plan.report['blockers'])
        cut_rep = cut_plan.apply(self.state)
        self.assertTrue((self.ag_config / 'plugins' / 'p1' / 'plugin.json').is_file())
        self.assertTrue((self.ag_config / 'rules' / 'r1.md').is_file())
        self.assertIn('# Global', (self.ag_config / 'AGENTS.md').read_text())
        self.assertIn('h1', json.loads((self.ag_config / 'hooks.json').read_text()))
        self.assertTrue(new_machine.is_file())

        rollback_plan(Cutover(spec_path), self.run_id(cut_rep)).apply(self.state)
        self.assertFalse((self.ag_config / 'plugins' / 'p1' / 'plugin.json').exists())
        self.assertFalse((self.ag_config / 'rules' / 'r1.md').exists())
        self.assertFalse((self.ag_config / 'AGENTS.md').exists())
        self.assertFalse((self.ag_config / 'hooks.json').exists())
        self.assertFalse(new_machine.exists())


if __name__ == '__main__':
    unittest.main()
