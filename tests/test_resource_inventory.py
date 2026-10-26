"""Ownership handoff, drift categories and read-only diagnostic boundaries."""
import io
import json
import plistlib
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from test_profile_resources import ResourceFixture
from claude_config.core.diagnostics import app_instances, storage_summary
from claude_config.core.resource_inventory import external_claims
from claude_config.core.schema import SchemaError, validate_profile
from claude_config.core.transaction import PlanStale
from claude_config.setup import doctor, main


class InventoryTests(ResourceFixture):
    def setUp(self):
        super().setUp()
        self.body = '---\nname: tiny\ndescription: fixture\n---\nPRIVATE_SKILL_BODY\n'
        self.add('tiny', 'skill', {'SKILL.md': self.body}, 'SKILL.md', options={'control': {'enabled': False}})
        self.cc = self.root / 'cc-switch'
        self.machine['bindings']['paths'] = {'cc-switch-data': str(self.cc)}
        self.profile.pop('settings', None)
        self.save()

    def database(self, claude=0, codex=0):
        self.cc.mkdir(exist_ok=True)
        with sqlite3.connect(self.cc / 'cc-switch.db') as db:
            db.executescript('CREATE TABLE skills (name, directory, enabled_claude, enabled_codex);'
                             'CREATE TABLE proxy_request_logs (secret);')
            db.execute('INSERT INTO skills VALUES (?,?,?,?)', ('tiny', 'tiny', claude, codex))
            db.execute('INSERT INTO proxy_request_logs VALUES (?)', ('PRIVATE_CREDENTIAL',))

    def deploy(self):
        plan = self.manager().plan()
        self.assertTrue(plan.report['ready'], plan.report['blockers'])
        plan.apply(self.state)
        return plan

    def test_selected_disabled_skill_still_requires_exclusive_writer(self):
        self.database(claude=1)
        plan = self.manager().plan()
        self.assertFalse(plan.report['ready'])
        row = plan.report['resource_inventory']['skills'][0]
        self.assertFalse(row['desired_enabled'])
        self.assertEqual(row['external_managers'], ['cc-switch'])
        self.assertIn('external_skill_manager_conflict', [r['code'] for r in plan.report['blockers']])
        self.assertFalse((self.native / 'settings.json').exists())
        self.assertNotIn('PRIVATE_CREDENTIAL', json.dumps(plan.report))

    def test_other_client_and_request_history_do_not_block_or_stale_plan(self):
        self.database(codex=1)
        plan = self.manager().plan()
        self.assertTrue(plan.report['ready'])
        with sqlite3.connect(self.cc / 'cc-switch.db') as db:
            db.execute('INSERT INTO proxy_request_logs VALUES (?)', ('NEW_PRIVATE_ROW',))
        plan.apply(self.state)
        self.assertTrue((self.native / 'skills/tiny/SKILL.md').is_file())

    def test_ownership_change_after_preview_is_stale(self):
        self.database()
        plan = self.manager().plan()
        with sqlite3.connect(self.cc / 'cc-switch.db') as db:
            db.execute('UPDATE skills SET enabled_claude=1')
        with self.assertRaises(PlanStale):
            plan.apply(self.state)
        self.assertFalse((self.native / 'settings.json').exists())

    def test_unreadable_existing_manager_is_not_reported_absent(self):
        self.cc.mkdir()
        (self.cc / 'cc-switch.db').write_bytes(b'not sqlite')
        plan = self.manager().plan()
        self.assertFalse(plan.report['ready'])
        self.assertEqual(plan.report['resource_inventory']['external_manager_status'], 'unreadable')

    def test_unknown_manager_schema_is_blocked_without_reading_private_tables(self):
        self.cc.mkdir()
        with sqlite3.connect(self.cc / 'cc-switch.db') as db:
            db.execute('CREATE TABLE private_credentials (value)')
        self.assertEqual(external_claims(self.cc, 'claude-code', {'tiny'})['status'], 'unreadable')

    def test_categories_and_discovery_do_not_claim_runtime_success(self):
        self.deploy()
        entry = self.native / 'skills/tiny/SKILL.md'
        entry.chmod(0o644)
        plan = self.manager().plan()
        row = plan.report['resource_inventory']['skills'][0]
        self.assertEqual(row['change_counts'], {'mode_changed': 1})
        self.assertEqual(row['native_discovery']['status'], 'not_verified')
        self.assertFalse(row['native_enabled'])
        self.assertEqual(plan.report['resource_conflicts'][0]['file_changes'][0]['kind'], 'mode_changed')
        entry.write_text(self.body + 'manual change')
        plan = self.manager().plan()
        self.assertEqual(plan.report['resource_inventory']['skills'][0]['change_counts'], {'content_changed': 1})
        entry.unlink()
        plan = self.manager().plan()
        self.assertEqual(plan.report['resource_inventory']['skills'][0]['change_counts'], {'missing': 1})
        self.assertNotIn('PRIVATE_SKILL_BODY', json.dumps(plan.report['resource_inventory']))

    def test_other_location_is_candidate_not_automatic_adoption(self):
        self.deploy()
        entry = self.native / 'skills/tiny/SKILL.md'
        alternate = self.root / '.agents/skills/tiny/SKILL.md'
        alternate.parent.mkdir(parents=True)
        alternate.write_bytes(entry.read_bytes())
        entry.unlink()
        with patch('pathlib.Path.home', return_value=self.root):
            plan = self.manager().plan()
        self.assertFalse(plan.report['ready'])
        row = plan.report['resource_inventory']['skills'][0]
        self.assertFalse(row['entry']['exists'])
        self.assertEqual(row['alternate_entries'][0]['path'], str(alternate))

    def codex(self):
        self.profile['agent_type'] = 'codex'
        self.machine['agents'] = {'codex': {'profile': 'profiles/agents/codex/work.yaml',
                                          'binding': {'executable': str(self.binary), 'config_root': str(self.native)}}}
        self.machine['bindings']['paths']['skills-root'] = str(self.root / '.agents/skills')
        self.catalog['resources']['tiny']['requires'] = {'bindings': {'install_root': {'kind': 'path', 'required': True}}}
        self.profile['resources']['skills']['tiny@test']['bindings'] = {'install_root': 'paths.skills-root'}
        self.save()

    def test_legacy_codex_alias_blocks_only_the_selected_skill(self):
        self.codex()
        legacy = self.native / 'skills/tiny'
        old = self.root / 'old-tiny'
        old.mkdir(); (old / 'SKILL.md').write_text(self.body)
        legacy.parent.mkdir(parents=True); legacy.symlink_to(old)
        plan = self.manager().plan()
        self.assertFalse(plan.report['ready'])
        self.assertIn('duplicate_skill_entry', [r['code'] for r in plan.report['blockers']])
        legacy.unlink()
        # Other legacy/system skills are not selected and must not be adopted or removed.
        unrelated = self.native / 'skills/other'; unrelated.symlink_to(old)
        self.deploy()
        self.assertTrue(unrelated.is_symlink())

    def test_legacy_entry_appearing_after_plan_is_stale(self):
        self.codex()
        plan = self.manager().plan()
        entry = self.native / 'skills/tiny/SKILL.md'
        entry.parent.mkdir(parents=True); entry.write_text(self.body)
        with self.assertRaises(PlanStale):
            plan.apply(self.state)

    def test_unselected_residual_is_distinct_from_uninstall(self):
        self.deploy()
        self.profile['resources']['skills'].clear(); self.save_profile()
        self.database(claude=1)
        plan = self.manager().plan()
        self.assertTrue(plan.report['ready'])
        self.assertEqual(plan.report['resource_inventory']['skills'][0]['lifecycle'], 'residual')
        plan.apply(self.state)
        self.assertTrue((self.native / 'skills/tiny/SKILL.md').exists())

    def test_default_custom_instance_does_not_read_global_manager(self):
        self.machine['bindings']['paths'].pop('cc-switch-data'); self.save_profile()
        with patch('claude_config.core.resource_inventory.sqlite3.connect', side_effect=AssertionError('must not read')):
            self.assertTrue(self.manager().plan().report['ready'])

    def test_doctor_resources_does_not_create_state_or_install_files(self):
        with patch('claude_config.core.manager.NativeProbe') as probe:
            probe.return_value.side_effect = lambda *args: [{'name': 'fixture', 'status': 'passed'}]
            report, _ = doctor(self.machine_path, resources=True)
        self.assertTrue(report['resource_inventories'])
        self.assertFalse(self.state.exists())
        self.assertFalse(self.native.exists())


class DiagnosticTests(unittest.TestCase):
    def test_minimum_tool_version_has_actionable_error(self):
        base = {'schema_version': 1, 'agent_type': 'claude-code', 'minimum_tool_version': '0.6.0'}
        validate_profile(base)
        with patch('claude_config.__version__', '0.5.2'), self.assertRaisesRegex(SchemaError, 'requires claude-config >= 0.6.0'):
            validate_profile(base)
        for value in (1, True, 'latest', '0.6'):
            with self.assertRaises(SchemaError):
                validate_profile(dict(base, minimum_tool_version=value))

    def test_doctor_no_save_is_not_persistent(self):
        raw = {'ready': True, 'checks': [], 'decisions': [], 'blockers': []}
        with patch('claude_config.setup.doctor', return_value=(raw, Path('/not-created'))), \
             patch('claude_config.setup.save_report') as save, redirect_stdout(io.StringIO()):
            self.assertEqual(main(['doctor', '--no-save', '--format', 'json']), 0)
        save.assert_not_called()

    def test_process_arguments_do_not_expose_credentials(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = Path(tmp) / 'Claude.app'; (app / 'Contents').mkdir(parents=True)
            (app / 'Contents/Info.plist').write_bytes(plistlib.dumps({'CFBundleExecutable': 'Claude'}))
            executable = str(app / 'Contents/MacOS/Claude')
            machine = {'apps': {'claude-desktop': {'binding': {'application': str(app), 'data_root': '/bound'}}}}
            def run(args, **kwargs):
                value = '41 ' + executable if 'ax' in args else executable + ' --user-data-dir=/other profile --api-key PRIVATE'
                return subprocess.CompletedProcess(args, 0, value, '')
            with patch('sys.platform', 'darwin'):
                result = app_instances(machine, runner=run)
            self.assertFalse(result['instances'][0]['binding_matches'])
            self.assertEqual(result['instances'][0]['observed_data_root'], '/other profile')
            self.assertNotIn('PRIVATE', json.dumps(result))

    def test_storage_keeps_required_evidence_and_never_follows_links(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); (root / 'reports').mkdir(); (root / 'resource-baselines').mkdir()
            (root / 'reports/r.json').write_text('{}'); (root / 'resource-baselines/a.bin').write_bytes(b'required')
            (root / 'reports/link').symlink_to(root / 'resource-baselines', target_is_directory=True)
            result = storage_summary(root)
            self.assertFalse(result['automatic_deletion'])
            rows = {row['category']: row for row in result['categories']}
            self.assertEqual(rows['reports']['files'], 1)
            self.assertEqual(rows['resource-baselines']['logical_bytes'], 8)


if __name__ == '__main__':
    unittest.main()
