"""Real runtime checks around isolated resource review/restore/export; no dispatch."""
import os
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from claude_config.core.manager import ProfileManager
from claude_config.core.recovery import rollback_plan
from claude_config.core.schema import as_pointer
from test_profile_resources import ResourceFixture


@unittest.skipUnless(os.environ.get('CLAUDE_CONFIG_RUN_NATIVE_SMOKE') == '1', 'Opt-in native conflict workflow checks')
class NativeResourceConflictSmoke(unittest.TestCase):
    def exercise(self, agent, variable):
        binary = os.environ.get(variable)
        if not binary:
            self.skipTest(variable + ' is required')
        fixture = ResourceFixture()
        fixture.setUp()
        try:
            fixture.profile['agent_type'] = agent
            fixture.profile['settings'] = {'model': 'native-example'} if agent == 'claude-code' else {'model_reasoning_effort': 'high'}
            fixture.machine['agents'] = {agent: {'profile': f'profiles/agents/{agent}/work.yaml', 'binding': {
                'executable': binary, 'config_root': str(fixture.native)}}}
            entry = 'role.md' if agent == 'claude-code' else 'role.toml'
            content = '---\nname: reviewer\ndescription: isolated\n---\nOriginal body\n' if agent == 'claude-code' else 'name = "reviewer"\ndescription = "isolated"\ndeveloper_instructions = "Original body"\n'
            fixture.add('role', 'agent', {entry: content}, entry, options={'model': 'sonnet' if agent == 'claude-code' else 'example-model'})
            fixture.save()
            def manager():
                return ProfileManager(fixture.machine_path, agent)
            initial = manager().plan()
            self.assertTrue(initial.report['ready'], initial.report['blockers'])
            initial.apply(fixture.state)
            target = fixture.native / 'agents' / ('reviewer.md' if agent == 'claude-code' else 'reviewer.toml')
            target.write_text(target.read_text().replace('Original body', 'Reviewed local body'))
            local = target.read_bytes()
            plan = manager().plan()
            self.assertFalse(plan.report['ready'])
            row = plan.report['resource_conflicts'][0]
            choice = {as_pointer(tuple(row['path'])): {'conflict_id': row['conflict_id'], 'action': 'adopt_source'}}
            resolved = manager().plan(resolutions=choice)
            self.assertTrue(resolved.report['ready'], resolved.report['blockers'])
            result = resolved.apply(fixture.state)
            self.assertIn('Original body', target.read_text())
            exported = manager().export([], resource_refs=['role@test'])
            self.assertTrue(exported.report['ready'], exported.report['blockers'])
            rollback_plan(manager(), Path(result['recovery']).parent.name).apply(fixture.state)
            self.assertEqual(target.read_bytes(), local)
        finally:
            fixture.tearDown()

    def test_claude_cli_owned_resource_restore_workflow(self):
        self.exercise('claude-code', 'CLAUDE_CONFIG_NATIVE_CLAUDE')

    def test_codex_app_backend_owned_resource_restore_workflow(self):
        self.exercise('codex', 'CLAUDE_CONFIG_NATIVE_CODEX_APP')
