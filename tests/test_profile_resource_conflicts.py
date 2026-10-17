"""Owned drift resolutions must be scoped, temporary, reviewable and reversible."""
from copy import deepcopy
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from claude_config.core.native import parse_json, parse_yaml
from claude_config.core.recovery import rollback_plan
from claude_config.core.resources import START, END
from claude_config.core.schema import SchemaError, as_pointer
from claude_config.core.transaction import PlanStale, atomic_write
from test_profile_resources import ResourceFixture


class ConflictTests(ResourceFixture):
    def role(self):
        self.add('review', 'agent', {'role.md': '---\nname: reviewer\ndescription: review\n---\nReview source\n'},
                 'role.md', options={'model': 'sonnet'})
        self.save()
        self.deploy()
        return (self.native / 'agents/reviewer.md').resolve()

    def skill(self):
        self.add('tiny', 'skill', {'SKILL.md': '---\nname: tiny\ndescription: test\n---\nOriginal skill\n',
                                  'run.sh': '#!/bin/sh\necho original\n'}, 'SKILL.md')
        self.save()
        self.deploy()
        return (self.native / 'skills/tiny').resolve()

    def mcp(self):
        self.add('server', 'mcp', {'server.json': json.dumps({'name': 'remote', 'server': {'type': 'http', 'url': 'https://example.invalid/mcp'}})}, 'server.json')
        self.save()
        self.deploy()
        return (self.native / '.claude.json').resolve()

    def hook_resource(self):
        self.add('check', 'hook', {'hooks.json': json.dumps({'hooks': {'PreToolUse': [{'matcher': 'Bash', 'hooks': [{'type': 'command',
            'command': {'executable': {'binding': 'program'}, 'args': ['literal']}}]}]}})}, 'hooks.json', mode='lifecycle',
            requires={'bindings': {'program': {'kind': 'executable', 'required': True}}}, bindings={'program': 'executables.bash'})
        self.save()
        self.deploy()
        return self.manager().native_path

    def deploy(self, **kwargs):
        plan = self.manager().plan(**kwargs)
        self.assertTrue(plan.report['ready'], plan.report['blockers'])
        plan.apply(self.state)
        return plan

    def review(self):
        plan = self.manager().plan()
        self.assertFalse(plan.report['ready'])
        self.assertTrue(plan.report['resource_conflicts'])
        return plan.report['resource_conflicts'][0]

    def choose(self, review, action):
        return {as_pointer(tuple(review['path'])): {'conflict_id': review['conflict_id'], 'action': action}}

    def record(self, ref):
        return parse_json(self.manager().state_path.read_bytes())['resources'][ref]

    def test_three_snapshots_line_changes_and_public_report_has_no_body(self):
        path = self.role()
        path.write_text(path.read_text().replace('Review source', 'LOCAL_PRIVATE_BODY'))
        review = self.review()
        self.assertEqual(review['eligible_actions'], ['adopt_source', 'keep_local', 'update_source'])
        self.assertEqual(len(review['conflict_id']), 64)
        row = next(row for row in review['file_changes'] if row['path'] == str(path))
        self.assertTrue(row['baseline_body_available'])
        self.assertTrue(row['line_changes'])
        self.assertNotIn('LOCAL_PRIVATE_BODY', json.dumps(review))

    def test_source_adoption_restores_owned_body_and_model(self):
        path = self.role()
        before = path.read_bytes()
        path.write_text(path.read_text().replace('sonnet', 'opus').replace('Review source', 'Local edit'))
        review = self.review()
        self.deploy(resolutions=self.choose(review, 'adopt_source'))
        self.assertEqual(path.read_bytes(), before)
        self.assertTrue(self.manager().plan().report['ready'])

    def test_declared_source_diff_is_distinct_from_local_diff(self):
        path = self.role()
        path.write_text(path.read_text() + '\nLocal edit\n')
        source = self.repo / 'packages/review/role.md'
        source.write_text(source.read_text() + '\nCommitted source update\n')
        self.save()
        review = self.review()
        self.assertTrue(review['file_changes'])
        self.assertTrue(review['declared_source_changes'])
        self.assertEqual(review['incoming']['stage'], 'declaration_before_optional_usage_preservation')

    def test_keep_local_does_not_update_baseline_or_profile(self):
        path = self.role()
        record = deepcopy(self.record('review@test'))
        profile = (self.data / self.machine['agents']['claude-code']['profile']).read_bytes()
        path.write_text(path.read_text().replace('Review source', 'Keep local'))
        after = path.read_bytes()
        plan = self.deploy(resolutions=self.choose(self.review(), 'keep_local'))
        self.assertEqual(path.read_bytes(), after)
        self.assertEqual(self.record('review@test'), record)
        self.assertEqual((self.data / self.machine['agents']['claude-code']['profile']).read_bytes(), profile)
        self.assertEqual(plan.report['synchronization'], 'local_changes_retained')
        self.assertEqual(plan.report['kept_resources'], ['review@test'])
        self.assertFalse(self.manager().plan().report['ready'])

    def test_source_update_choice_is_a_workflow_and_does_not_clear_conflict(self):
        path = self.role()
        path.write_text(path.read_text().replace('sonnet', 'opus'))
        before = path.read_bytes()
        plan = self.manager().plan(resolutions=self.choose(self.review(), 'update_source'))
        self.assertFalse(plan.report['ready'])
        row = plan.report['resource_conflicts'][0]
        self.assertEqual(row['status'], 'source_update_required')
        self.assertEqual(row['workflow']['options']['operation'], 'export')
        self.assertEqual(row['workflow']['body']['destination'], 'claudespace resource source')
        self.assertEqual(path.read_bytes(), before)

    def test_changed_local_after_review_invalidates_decision(self):
        path = self.role()
        path.write_text(path.read_text() + '\nfirst edit\n')
        choice = self.choose(self.review(), 'adopt_source')
        path.write_text(path.read_text() + 'another edit\n')
        plan = self.manager().plan(resolutions=choice)
        self.assertFalse(plan.report['ready'])
        self.assertIn('another edit', path.read_text())

    def test_changed_profile_policy_invalidates_review(self):
        path = self.role()
        path.write_text(path.read_text() + '\nedit\n')
        choice = self.choose(self.review(), 'keep_local')
        self.profile['merge_policy'] = {'default': 'keep_target'}
        self.save_profile()
        self.assertFalse(self.manager().plan(resolutions=choice).report['ready'])

    def test_changed_bound_program_invalidates_review(self):
        path = self.hook_resource()
        value = json.loads(path.read_text())
        value['hooks']['PreToolUse'][0]['hooks'][0]['command'] = 'local command'
        path.write_text(json.dumps(value))
        choice = self.choose(self.review(), 'adopt_source')
        self.binary.write_text('new reviewed-runtime-needed')
        self.assertFalse(self.manager().plan(resolutions=choice).report['ready'])

    def test_changed_fixed_source_invalidates_review(self):
        path = self.role()
        path.write_text(path.read_text() + '\nedit\n')
        choice = self.choose(self.review(), 'adopt_source')
        source = self.repo / 'packages/review/role.md'
        source.write_text(source.read_text() + '\nnew upstream body\n')
        self.save()
        self.assertFalse(self.manager().plan(resolutions=choice).report['ready'])

    def test_source_update_and_profile_export_then_new_review(self):
        path = self.role()
        path.write_text(path.read_text().replace('sonnet', 'opus'))
        old_review = self.review()
        export = self.manager().export([], resource_refs=['review@test'])
        self.assertTrue(export.report['ready'])
        export.apply(self.state)
        newer = self.review()
        self.assertNotEqual(old_review['conflict_id'], newer['conflict_id'])
        before = path.read_bytes()
        self.deploy(resolutions=self.choose(newer, 'adopt_source'))
        self.assertEqual(path.read_bytes(), before)
        self.assertTrue(self.manager().plan().report['ready'])

    def test_changed_native_after_resolved_plan_blocks_commit(self):
        path = self.role()
        path.write_text(path.read_text() + '\nedit\n')
        plan = self.manager().plan(resolutions=self.choose(self.review(), 'adopt_source'))
        self.assertTrue(plan.report['ready'])
        path.write_text(path.read_text() + 'late edit\n')
        with self.assertRaises(PlanStale):
            plan.apply(self.state)
        self.assertIn('late edit', path.read_text())

    def test_resolution_for_unselected_or_nonconflicting_resource_is_rejected(self):
        self.role()
        with self.assertRaises(SchemaError):
            self.manager().plan(resolutions={'/resources/agents/unknown@test': {'conflict_id': 'a' * 64, 'action': 'keep_local'}})
        plan = self.manager().plan(resolutions={'/resources/agents/review@test': {'conflict_id': 'a' * 64, 'action': 'keep_local'}})
        self.assertFalse(plan.report['ready'])

    def test_unknown_same_name_resource_cannot_be_adopted(self):
        self.add('review', 'agent', {'role.md': '---\nname: reviewer\ndescription: review\n---\nsource\n'}, 'role.md')
        self.save()
        target = self.native / 'agents/reviewer.md'
        target.parent.mkdir(parents=True)
        target.write_text('unknown local owner')
        plan = self.manager().plan()
        self.assertFalse(plan.report['ready'])
        self.assertEqual(plan.report.get('resource_conflicts'), [])
        self.assertEqual(target.read_text(), 'unknown local owner')

    def test_unknown_extra_skill_file_blocks_all_resolution_actions(self):
        root = self.skill()
        (root / 'unknown.sh').write_text('unknown')
        review = self.review()
        self.assertEqual(review['eligible_actions'], [])
        self.assertIn('unmanaged_resource_collision', review['issues'])
        self.assertFalse(self.manager().plan(resolutions=self.choose(review, 'keep_local')).report['ready'])

    def test_snapshot_tampering_cannot_be_resolved(self):
        path = self.role()
        record = self.record('review@test')
        snapshot = Path(record['native']['review']['files'][str(path)])
        snapshot.write_bytes(b'tampered base')
        path.write_text(path.read_text() + '\nedit\n')
        self.assertFalse(self.manager().plan().report['ready'])

    def test_deleted_owned_definition_can_restore_from_source(self):
        path = self.role()
        before = path.read_bytes()
        path.unlink()
        self.deploy(resolutions=self.choose(self.review(), 'adopt_source'))
        self.assertEqual(path.read_bytes(), before)

    def test_private_review_writes_only_private_files_and_keeps_native_state(self):
        path = self.role()
        path.write_text(path.read_text() + '\nPRIVATE_REVIEW_TEXT\n')
        native = path.read_bytes()
        state = self.manager().state_path.read_bytes()
        plan = self.manager().review_conflicts()
        self.assertTrue(plan.report['ready'])
        self.assertFalse(plan.report['deployment_ready'])
        self.assertNotIn('PRIVATE_REVIEW_TEXT', json.dumps(plan.report))
        plan.apply(self.state)
        directory = Path(plan.report['review_directories'][0])
        self.assertIn(b'PRIVATE_REVIEW_TEXT', (directory / '0-local.bin').read_bytes())
        self.assertTrue((directory / 'incoming/role.md').is_file())
        self.assertEqual((directory / '0-local.bin').stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.state.stat().st_mode & 0o777, 0o700)
        self.assertEqual(path.read_bytes(), native)
        self.assertEqual(self.manager().state_path.read_bytes(), state)

    def test_private_review_stale_and_rollback_are_scoped(self):
        path = self.role()
        path.write_text(path.read_text() + '\nlocal\n')
        plan = self.manager().review_conflicts()
        path.write_text(path.read_text() + '\nlate\n')
        with self.assertRaises(PlanStale):
            plan.apply(self.state)
        result = self.manager().review_conflicts().apply(self.state)
        before = path.read_bytes()
        rollback_plan(self.manager(), Path(result['recovery']).parent.name).apply(self.state)
        self.assertEqual(path.read_bytes(), before)

    def test_restore_run_can_rollback_to_the_reviewed_local_modification(self):
        path = self.role()
        path.write_text(path.read_text() + '\nlocal edit to recover\n')
        local = path.read_bytes()
        result = self.deploy(resolutions=self.choose(self.review(), 'adopt_source')).report
        rollback_plan(self.manager(), Path(result['recovery']).parent.name).apply(self.state)
        self.assertEqual(path.read_bytes(), local)

    def test_failed_restore_recovers_local_content(self):
        path = self.role()
        path.write_text(path.read_text() + '\nlocal edit\n')
        local = path.read_bytes()
        self.profile['settings']['model'] = 'changed-main-model'
        self.save_profile()
        plan = self.manager().plan(resolutions=self.choose(self.review(), 'adopt_source'))
        def fail(target, content, mode):
            if target == self.manager().state_path:
                raise OSError('isolated injected failure')
            atomic_write(target, content, mode)
        with self.assertRaises(OSError):
            plan.apply(self.state, writer=fail)
        self.assertEqual(path.read_bytes(), local)

    def test_restore_skill_invocation_and_script_use_baseline_not_local_options(self):
        root = self.skill()
        (root / 'SKILL.md').write_text((root / 'SKILL.md').read_text().replace('description: test', 'description: test\nuser-invocable: false'))
        (root / 'run.sh').write_text('local script')
        self.deploy(resolutions=self.choose(self.review(), 'adopt_source'))
        self.assertEqual((root / 'run.sh').read_text(), '#!/bin/sh\necho original\n')
        self.assertNotIn('user-invocable', (root / 'SKILL.md').read_text())

    def test_restore_changed_prior_cache_during_new_source_update(self):
        root = self.skill()
        record = self.record('tiny@test')
        cache = Path(record['cache'])
        (cache / 'run.sh').write_text('changed live prior cache')
        source = self.repo / 'packages/tiny/run.sh'
        source.write_text('new committed script')
        self.save()
        self.deploy(resolutions=self.choose(self.review(), 'adopt_source'))
        self.assertEqual((root / 'run.sh').read_text(), 'new committed script')
        self.assertEqual((cache / 'run.sh').read_text(), '#!/bin/sh\necho original\n')

    def test_keep_skipped_new_dependency_blocks_consumers(self):
        root = self.skill()
        (root / 'run.sh').write_text('local script')
        (self.repo / 'packages/tiny/run.sh').write_text('new source')
        self.add('consumer', 'instruction', {'guide.md': 'Use dependency'}, 'guide.md', requires={'resources': ['tiny@test']})
        self.save()
        review = next(row for row in self.manager().plan().report['resource_conflicts'] if row['ref'] == 'tiny@test')
        plan = self.manager().plan(resolutions=self.choose(review, 'keep_local'))
        self.assertFalse(plan.report['ready'])
        self.assertTrue(any(row.get('code') == 'kept_resource_dependency' for row in plan.report['blockers']))
        self.assertFalse((self.native / 'CLAUDE.md').exists())

    def test_rule_body_drift_uses_same_restore_protocol(self):
        self.add('rule', 'rule', {'rule.md': 'Original rule'}, 'rule.md')
        self.save()
        self.deploy()
        path = self.native / 'rules/rule.md'
        path.write_text('Changed rule')
        self.deploy(resolutions=self.choose(self.review(), 'adopt_source'))
        self.assertEqual(path.read_text(), 'Original rule')

    def test_codex_skill_activation_restore_and_keep_are_separate_from_body(self):
        import tomlkit
        self.profile['agent_type'] = 'codex'
        self.machine['bindings']['paths'] = {'skills': str(self.root / 'home/.agents/skills')}
        self.add('tiny', 'skill', {'SKILL.md': '---\nname: tiny\ndescription: test\n---\nbody\n'}, 'SKILL.md',
                 options={'control': {'enabled': False}}, requires={'bindings': {'install_root': {'kind': 'path', 'required': True}}},
                 bindings={'install_root': 'paths.skills'})
        self.save()
        self.deploy()
        path = self.manager().native_path
        value = tomlkit.parse(path.read_text()).unwrap()
        value['skills']['config'][0]['enabled'] = True
        path.write_text(tomlkit.dumps(value))
        baseline = self.record('tiny@test')
        self.deploy(resolutions=self.choose(self.review(), 'keep_local'))
        self.assertEqual(self.record('tiny@test'), baseline)
        self.assertTrue(tomlkit.parse(path.read_text())['skills']['config'][0]['enabled'])
        self.deploy(resolutions=self.choose(self.review(), 'adopt_source'))
        self.assertFalse(tomlkit.parse(path.read_text())['skills']['config'][0]['enabled'])

    def test_removed_codex_activation_can_be_restored_without_adding_defaults(self):
        import tomlkit
        self.profile['agent_type'] = 'codex'
        self.machine['bindings']['paths'] = {'skills': str(self.root / 'home/.agents/skills')}
        self.add('tiny', 'skill', {'SKILL.md': '---\nname: tiny\ndescription: test\n---\nbody\n'}, 'SKILL.md',
                 options={'control': {'enabled': False}}, requires={'bindings': {'install_root': {'kind': 'path', 'required': True}}},
                 bindings={'install_root': 'paths.skills'})
        self.save()
        self.deploy()
        path = self.manager().native_path
        value = tomlkit.parse(path.read_text()).unwrap()
        value['skills']['config'] = []
        path.write_text(tomlkit.dumps(value))
        self.deploy(resolutions=self.choose(self.review(), 'adopt_source'))
        self.assertFalse(tomlkit.parse(path.read_text())['skills']['config'][0]['enabled'])

    def test_plugin_enabled_drift_restores_via_native_operation_and_keeps_baseline_on_skip(self):
        from claude_config.core.native_plugins import PluginOperation
        from test_profile_plugins import FakeInstaller
        installer = FakeInstaller()
        self.add('tiny', 'plugin', {'.claude-plugin/plugin.json': json.dumps({'name': 'tiny', 'version': '1.0.0'})},
                 '.claude-plugin/plugin.json', options={'enabled': False})
        self.save()
        with patch.object(PluginOperation, '_call', lambda operation, args, work: installer(operation, args, work)):
            self.deploy()
            state = self.record('tiny@test')
            path = self.manager().native_path
            value = json.loads(path.read_text())
            selector = state['native']['plugin']['selector']
            value['enabledPlugins'][selector] = True
            path.write_text(json.dumps(value))
            self.deploy(resolutions=self.choose(self.review(), 'keep_local'))
            self.assertEqual(self.record('tiny@test'), state)
            self.assertTrue(json.loads(path.read_text())['enabledPlugins'][selector])
            self.deploy(resolutions=self.choose(self.review(), 'adopt_source'))
            self.assertFalse(json.loads(path.read_text())['enabledPlugins'][selector])

    def test_mcp_resolution_cannot_bypass_new_name_collision(self):
        path = self.mcp()
        value = json.loads(path.read_text())
        value['mcpServers']['remote']['url'] = 'https://local.invalid'
        value['mcpServers']['renamed'] = {'type': 'http', 'url': 'https://unmanaged.invalid'}
        path.write_text(json.dumps(value))
        source = self.repo / 'packages/server/server.json'
        source.write_text(json.dumps({'name': 'renamed', 'server': {'type': 'http', 'url': 'https://example.invalid/mcp'}}))
        self.save()
        review = self.review()
        self.assertIn('unmanaged_mcp_collision', review['issues'])
        self.assertEqual(review['eligible_actions'], [])
        self.assertFalse(self.manager().plan(resolutions=self.choose(review, 'adopt_source')).report['ready'])

    def test_mcp_restore_preserves_unrelated_entries_and_native_secrets(self):
        path = self.mcp()
        value = json.loads(path.read_text())
        value['mcpServers']['remote']['url'] = 'https://secret.invalid/LOCAL_SECRET'
        value['mcpServers']['other'] = {'url': 'https://other.invalid', 'headers': {'Authorization': 'PRIVATE_AUTH'}}
        path.write_text(json.dumps(value))
        review = self.review()
        self.assertNotIn('LOCAL_SECRET', json.dumps(review))
        self.deploy(resolutions=self.choose(review, 'adopt_source'))
        result = json.loads(path.read_text())['mcpServers']
        self.assertEqual(result['remote']['url'], 'https://example.invalid/mcp')
        self.assertEqual(result['other']['headers']['Authorization'], 'PRIVATE_AUTH')

    def test_mcp_keep_and_native_private_three_way_details(self):
        path = self.mcp()
        value = json.loads(path.read_text())
        value['mcpServers']['remote']['url'] = 'https://local.invalid/PRIVATE_URL'
        path.write_text(json.dumps(value))
        before = path.read_bytes()
        plan = self.manager().review_conflicts()
        self.assertNotIn('PRIVATE_URL', json.dumps(plan.report))
        plan.apply(self.state)
        root = Path(plan.report['review_directories'][0])
        self.assertEqual(json.loads((root / 'native-local.json').read_text())['url'], value['mcpServers']['remote']['url'])
        self.assertTrue((root / 'native-baseline.json').exists())
        self.deploy(resolutions=self.choose(self.review(), 'keep_local'))
        self.assertEqual(path.read_bytes(), before)
        self.assertFalse(self.manager().plan().report['ready'])

    def test_hook_restore_replaces_only_the_owned_changed_group(self):
        path = self.hook_resource()
        value = json.loads(path.read_text())
        value['hooks']['PreToolUse'][0]['hooks'][0]['command'] = 'LOCAL_COMMAND'
        unrelated = {'matcher': 'Read', 'hooks': [{'type': 'command', 'command': 'keep unrelated'}]}
        value['hooks']['PreToolUse'].append(unrelated)
        path.write_text(json.dumps(value))
        review = self.review()
        self.assertNotIn('LOCAL_COMMAND', json.dumps(review))
        self.deploy(resolutions=self.choose(review, 'adopt_source'))
        rows = json.loads(path.read_text())['hooks']['PreToolUse']
        self.assertEqual(rows[1], unrelated)
        self.assertNotEqual(rows[0]['hooks'][0]['command'], 'LOCAL_COMMAND')

    def test_hook_ambiguity_is_not_bypassed(self):
        path = self.hook_resource()
        value = json.loads(path.read_text())
        value['hooks']['PreToolUse'] *= 2
        path.write_text(json.dumps(value))
        review = self.review()
        self.assertEqual(review['eligible_actions'], [])
        self.assertFalse(self.manager().plan(resolutions=self.choose(review, 'adopt_source')).report['ready'])

    def test_hook_matcher_identity_change_is_not_treated_as_a_deleted_owned_group(self):
        path = self.hook_resource()
        value = json.loads(path.read_text())
        value['hooks']['PreToolUse'][0]['matcher'] = 'Read'
        path.write_text(json.dumps(value))
        review = self.review()
        self.assertIn('hook_identity_unknown', review['issues'])
        self.assertEqual(review['eligible_actions'], [])

    def test_known_hook_group_deletion_can_restore_without_touching_other_groups(self):
        path = self.hook_resource()
        value = json.loads(path.read_text())
        value['hooks']['PreToolUse'] = []
        path.write_text(json.dumps(value))
        self.deploy(resolutions=self.choose(self.review(), 'adopt_source'))
        self.assertEqual(json.loads(path.read_text())['hooks']['PreToolUse'][0]['matcher'], 'Bash')

    def test_instruction_restore_preserves_text_outside_owned_markers(self):
        self.add('guide', 'instruction', {'guide.md': 'Managed instructions'}, 'guide.md')
        self.save()
        self.deploy()
        path = self.native / 'CLAUDE.md'
        path.write_text('PRIVATE_SURROUNDING\n' + path.read_text().replace('Managed instructions', 'Local instruction edit'))
        review = self.review()
        self.assertNotIn('PRIVATE_SURROUNDING', json.dumps(review))
        self.manager().review_conflicts().apply(self.state)
        self.deploy(resolutions=self.choose(review, 'adopt_source'))
        self.assertIn('PRIVATE_SURROUNDING', path.read_text())
        self.assertIn('Managed instructions', path.read_text())
        self.assertNotIn('Local instruction edit', path.read_text())

    def test_settings_and_resource_resolutions_share_one_input(self):
        path = self.role()
        native = self.manager().native_path
        value = json.loads(native.read_text())
        value['model'] = 'local-main'
        native.write_text(json.dumps(value))
        path.write_text(path.read_text() + '\nlocal role\n')
        report = self.manager().plan().report
        choice = self.choose(report['resource_conflicts'][0], 'adopt_source')
        choice['/settings/model'] = {'target_fingerprint': report['snapshots']['target'], 'policy': 'keep_target'}
        self.deploy(resolutions=choice)
        self.assertEqual(json.loads(native.read_text())['model'], 'local-main')
        self.assertNotIn('local role', path.read_text())

    def test_cli_review_preview_and_write_never_apply_native(self):
        path = self.role()
        path.write_text(path.read_text() + '\nlocal\n')
        before = path.read_bytes()
        # Avoid invoking a fixture pseudo-binary by wrapping only the native
        # probe in this in-process CLI test; all planning/file behavior is real.
        from claude_config.profile import main
        from contextlib import redirect_stdout
        import io
        with patch('claude_config.core.manager.NativeProbe', return_value=lambda *args: [{'name': 'fixture', 'status': 'passed'}]):
            output = io.StringIO()
            with redirect_stdout(output):
                code = main(['review-conflicts', '--machine', str(self.machine_path), '--agent', 'claude-code', '--format', 'json'])
            self.assertEqual(code, 0)
            report = json.loads(output.getvalue())
            self.assertTrue(report['preview_only'])
            self.assertFalse(Path(report['review_directories'][0]).exists())
            with redirect_stdout(io.StringIO()):
                code = main(['review-conflicts', '--machine', str(self.machine_path), '--agent', 'claude-code', '--write', '--format', 'json'])
            self.assertEqual(code, 0)
        self.assertEqual(path.read_bytes(), before)


if __name__ == '__main__':
    unittest.main()
