"""Repository selections are authoritative; local dependencies never enter them."""
from copy import deepcopy
from pathlib import Path
import json
import sys
import unittest
from types import SimpleNamespace

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from claude_config.core.machine_profiles import MachineProfiles
from claude_config.core.recovery import rollback_plan
from claude_config.core.reporting import build_report, render_text
from claude_config.core.schema import SchemaError
from claude_config.core.transaction import PlanStale
from claude_config.setup import Setup, doctor
import test_profile_manager as agent_cases
import test_profile_apps as app_cases
import test_profile_baseline as baseline_cases
import test_framework_cutover as cutover_cases


def externalize(fixture):
    local = deepcopy(fixture.machine)
    selection = {'schema_version': 1, 'machine_id': local['machine_id'], 'agents': {}, 'apps': {}}
    for group in ('agents', 'apps'):
        for name, entry in local[group].items():
            selection[group][name] = entry.pop('profile')
    local['profile_selection'] = 'machines/' + local['machine_id'] + '.yaml'
    path = fixture.data / local['profile_selection']
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(selection, sort_keys=False))
    fixture.machine_path.write_text(yaml.safe_dump(local, sort_keys=False))
    return local, selection, path


class MachineSelectionTests(unittest.TestCase):
    def fixture(self, kind=agent_cases.ManagerTests):
        fixture = kind()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.addCleanup(fixture.tearDown)
        return fixture

    def test_agent_switch_updates_repository_and_rolls_back_without_copying_local_paths(self):
        f = self.fixture()
        f.native(value={'model': 'selected', 'private': 'keep'})
        name = 'profiles/agents/claude-code/other.yaml'
        (f.data / name).write_text(yaml.safe_dump({'schema_version': 1, 'agent_type': 'claude-code', 'settings': {'model': 'other'}}))
        local, selection, path = externalize(f)
        local_bytes, selection_bytes = f.machine_path.read_bytes(), path.read_bytes()
        plan = f.manager().plan(profile=name)
        self.assertTrue(plan.report['ready'])
        self.assertEqual(path.read_bytes(), selection_bytes)
        result = plan.apply(f.state)
        self.assertEqual(yaml.safe_load(path.read_text())['agents']['claude-code'], name)
        self.assertEqual(json.loads(f.native().read_text()), {'model': 'other', 'private': 'keep'})
        self.assertEqual(f.machine_path.read_bytes(), local_bytes)
        self.assertNotIn(str(f.root), path.read_text())
        rollback_plan(f.manager(), Path(result['recovery']).parent.name).apply(f.state)
        self.assertEqual(path.read_bytes(), selection_bytes)
        self.assertEqual(json.loads(f.native().read_text())['model'], 'selected')

    def test_app_switch_and_report_identify_repository_source(self):
        f = self.fixture(app_cases.AppTests)
        f.native('claude-desktop', {'userThemeMode': 'light', 'private': True})
        name = 'profiles/apps/claude-desktop/dark.yaml'
        f.profile('claude-desktop', {'userThemeMode': 'dark'}, name=name)
        local, selection, path = externalize(f)
        before = f.machine_path.read_bytes()
        manager = f.manager('claude-desktop')
        plan = manager.plan(profile=name)
        report = build_report(plan.report, manager=manager, operation='switch')
        self.assertEqual(report['targets'][0]['profile_selection_file'], str(path))
        self.assertIn('机器的 Profile 选择：', render_text(report))
        result = plan.apply(f.state)
        self.assertEqual(yaml.safe_load(path.read_text())['apps']['claude-desktop'], name)
        self.assertEqual(f.machine_path.read_bytes(), before)
        rollback_plan(f.manager('claude-desktop'), Path(result['recovery']).parent.name).apply(f.state)
        self.assertEqual(yaml.safe_load(path.read_text()), selection)

    def test_pulled_choice_is_used_and_later_selection_edit_blocks_all_writes(self):
        f = self.fixture()
        f.native(value={'model': 'selected'})
        name = 'profiles/agents/claude-code/pulled.yaml'
        (f.data / name).write_text(yaml.safe_dump({'schema_version': 1, 'agent_type': 'claude-code', 'settings': {'model': 'pulled'}}))
        local, selection, path = externalize(f)
        selection['agents']['claude-code'] = name
        path.write_text(yaml.safe_dump(selection))
        self.assertEqual(f.manager().entry['profile'], name)
        plan = f.manager().plan()
        selection['agents']['claude-code'] = f.machine['agents']['claude-code']['profile']
        path.write_text(yaml.safe_dump(selection))
        with self.assertRaises(PlanStale):
            plan.apply(f.state)
        self.assertEqual(json.loads(f.native().read_text())['model'], 'selected')

    def test_bad_identity_unknown_fields_client_mismatch_and_profile_type_rejected(self):
        f = self.fixture()
        local, selection, path = externalize(f)
        bad = [dict(selection, machine_id='another'), dict(selection, secrets={'key': 'DO_NOT_SYNC'}),
               dict(selection, agents={}), dict(selection, agents={'claude-code': 'profiles/apps/claude-desktop/work.yaml', 'codex': selection['agents']['codex']}),
               dict(selection, agents={'claude-code': '../../outside.yaml', 'codex': selection['agents']['codex']})]
        for value in bad:
            with self.subTest(value=value):
                path.write_text(yaml.safe_dump(value))
                with self.assertRaises(SchemaError):
                    f.manager()
        path.write_text(yaml.safe_dump(selection))
        local['agents']['claude-code']['profile'] = selection['agents']['claude-code']
        f.machine_path.write_text(yaml.safe_dump(local))
        with self.assertRaises(SchemaError):
            f.manager()

    def test_missing_selection_no_fallback_and_symlink_escape_rejected(self):
        f = self.fixture()
        local, selection, path = externalize(f)
        path.unlink()
        with self.assertRaises(SchemaError):
            f.manager()
        outside = f.root / 'outside.yaml'
        outside.write_text(yaml.safe_dump(selection))
        path.symlink_to(outside)
        with self.assertRaises(SchemaError):
            f.manager()

    def test_missing_or_wrong_selected_profile_is_rejected(self):
        f = self.fixture()
        local, selection, path = externalize(f)
        profile = f.data / selection['agents']['claude-code']
        profile.unlink()
        with self.assertRaises(SchemaError):
            f.manager().plan()
        profile.write_text(yaml.safe_dump({'schema_version': 1, 'agent_type': 'codex'}))
        with self.assertRaises(SchemaError):
            f.manager().plan()

    def test_init_adopts_matching_selections_without_native_writes(self):
        f = self.fixture()
        local, selection, path = externalize(f)
        f.save_machine()  # Start with existing inline choices.
        before = f.machine_path.read_bytes()
        args = SimpleNamespace(machine=f.machine_path, from_machine=None, config_dir=None, skill=[],
                               retire_script_install=False, profile_selection=local['profile_selection'])
        setup = Setup(args)
        plan = setup.plan()
        self.assertEqual(f.machine_path.read_bytes(), before)
        self.assertEqual([c.path for c in plan.changes], [f.machine_path])
        plan.apply(f.state)
        self.assertEqual(yaml.safe_load(f.machine_path.read_text()), local)
        report, _ = doctor(f.machine_path)
        self.assertEqual(report['machine_id'], f.machine['machine_id'])
        self.assertFalse(f.native().exists())
        f.save_machine()
        selection['agents']['claude-code'] = 'profiles/agents/claude-code/unreviewed.yaml'
        path.write_text(yaml.safe_dump(selection))
        with self.assertRaises(SchemaError):
            Setup(args)

    def test_adopt_baseline_preserves_external_choice_and_local_only_shape(self):
        f = self.fixture(baseline_cases.BaselineTests)
        f.prepared()
        local, selection, path = externalize(f)
        before = path.read_bytes()
        f.plan().apply(f.state)
        after = yaml.safe_load(f.machine_path.read_text())
        self.assertEqual(after['profile_selection'], local['profile_selection'])
        self.assertNotIn('profile', after['agents']['claude-code'])
        self.assertEqual(after['catalog_roots']['test'], str(f.candidate.resolve()))
        self.assertEqual(path.read_bytes(), before)

    def test_cutover_copies_selections_and_keeps_local_profile_reference(self):
        f = self.fixture(cutover_cases.FrameworkCutoverTests)
        f.add_app()
        local, selection, path = externalize(f)
        plan = f.coordinator().plan()
        self.assertTrue(plan.report['ready'], plan.report['blockers'])
        plan.apply(f.state)
        target = yaml.safe_load((f.tool / 'machine.yaml').read_text())
        self.assertEqual(target['profile_selection'], local['profile_selection'])
        self.assertNotIn('profile', target['apps']['codex-app'])
        self.assertEqual(yaml.safe_load((f.target_data / local['profile_selection']).read_text()), selection)
        self.assertEqual(MachineProfiles(f.tool / 'machine.yaml').effective['apps']['codex-app']['profile'], selection['apps']['codex-app'])


if __name__ == '__main__':
    unittest.main()
