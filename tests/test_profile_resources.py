from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import tomlkit
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from claude_config.core.manager import ProfileManager
from claude_config.core.resources import START, END
from claude_config.core.catalog import CatalogResolver
from claude_config.core.transaction import PlanStale, atomic_write


def run_git(root, *args):
    result = subprocess.run(['git', '-C', str(root), *args], capture_output=True, text=True, check=True)
    return result.stdout.strip()


class ResourceFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        run_git(self.repo, 'init', '-q')
        run_git(self.repo, 'config', 'user.name', 'isolated-test')
        run_git(self.repo, 'config', 'user.email', 'isolated@example.invalid')
        run_git(self.repo, 'remote', 'add', 'origin', 'https://github.com/owner/catalog.git')
        self.data, self.native, self.state = (self.root / name for name in ('data', 'native', 'state'))
        self.data.mkdir()
        self.binary = self.root / 'native-cli'
        self.binary.write_text('test-only-binary')
        self.binary.chmod(0o700)
        self.machine_path = self.root / 'machine.yaml'
        self.catalog = {'schema_version': 1, 'name': 'test', 'resources': {}}
        self.profile = {'schema_version': 1, 'agent_type': 'claude-code', 'settings': {'model': 'selected'}}
        self.machine = {'schema_version': 1, 'machine_id': 'isolated', 'config_dir': str(self.data), 'catalog_roots': {'test': str(self.repo)},
                        'agents': {'claude-code': {'profile': 'profiles/agents/claude-code/work.yaml', 'binding': {'executable': str(self.binary), 'config_root': str(self.native)}}},
                        'apps': {}, 'bindings': {'executables': {'bash': str(self.binary)}}, 'runtime': {'state_dir': str(self.state)}}

    def tearDown(self):
        self.temp.cleanup()

    def add(self, name, kind, files, entry, *, requires=None, mode='native', options=None, bindings=None):
        root = self.repo / 'packages' / name
        root.mkdir(parents=True, exist_ok=True)
        for relative, content in files.items():
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            if isinstance(content, bytes):
                path.write_bytes(content)
            else:
                path.write_text(content)
        self.catalog['resources'][name] = {'kind': kind, 'description': 'Isolated fixture',
            'origin': {'type': 'self', 'author': 'test'}, 'maintenance': {'owner': 'ours', 'mode': 'original'},
            'source': {'kind': 'tracked', 'path': 'packages/' + name},
            'compatibility': {host: {'mode': mode, 'entry': entry, 'validation': {'status': 'not-evaluated'}} for host in ('claude-code', 'codex')},
            'requires': requires or {}, 'update': {'strategy': 'manual'}}
        group = {'plugin': 'plugins', 'skill': 'skills', 'mcp': 'mcp_servers', 'hook': 'hooks', 'instruction': 'instructions', 'rule': 'rules', 'agent': 'agents'}[kind]
        selection = {}
        if options is not None:
            selection['options'] = options
        if bindings is not None:
            selection['bindings'] = bindings
        self.profile.setdefault('resources', {}).setdefault(group, {})[name + '@test'] = selection
        return root

    def save(self):
        (self.repo / 'catalog.json').write_text(json.dumps(self.catalog, indent=2))
        run_git(self.repo, 'add', '.')
        run_git(self.repo, 'commit', '-qm', 'isolated fixed baseline')
        commit = run_git(self.repo, 'rev-parse', 'HEAD')
        (self.data / 'catalogs.yaml').write_text(yaml.safe_dump({'schema_version': 1, 'catalogs': {'test': {'repo': 'owner/catalog', 'commit': commit, 'path': '.'}}}))
        self.save_profile()
        return commit

    def save_profile(self):
        agent = self.profile['agent_type']
        if agent not in self.machine['agents']:
            self.machine['agents'][agent] = {'profile': f'profiles/agents/{agent}/work.yaml', 'binding': {'executable': str(self.binary), 'config_root': str(self.native)}}
        path = self.data / self.machine['agents'][agent]['profile']
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(yaml.safe_dump(self.profile, sort_keys=False))
        self.machine_path.write_text(yaml.safe_dump(self.machine, sort_keys=False))

    def manager(self):
        return ProfileManager(self.machine_path, self.profile['agent_type'], probe=lambda *args: [{'name': 'isolated-native', 'status': 'passed'}])

    def hook(self):
        self.add('status', 'hook', {'run.sh': '#!/bin/sh\nprintf safe\n', 'helpers/data.txt': 'body'}, 'run.sh', mode='status-line')
        self.profile['bindings'] = {'/settings/statusLine/command': {'kind': 'command', 'executable': 'executables.bash',
                                                                   'args': [{'resource': 'status@test', 'path': 'run.sh'}, '$(touch should-not-run)', 'space argument']}}


class ResourceTests(ResourceFixture):
    def test_fixed_hook_package_and_typed_command_are_planned_then_applied(self):
        self.hook()
        self.save()
        plan = self.manager().plan()
        self.assertTrue(plan.report['ready'], plan.report['blockers'])
        self.assertFalse(self.native.exists())
        self.assertFalse(self.state.exists())
        plan.apply(self.state)
        settings = json.loads((self.native / 'settings.json').read_text())
        self.assertEqual(settings['statusLine']['type'], 'command')
        self.assertIn("'$(touch should-not-run)'", settings['statusLine']['command'])
        self.assertIn("'space argument'", settings['statusLine']['command'])
        self.assertFalse((self.root / 'should-not-run').exists())
        self.assertEqual(len(list(self.native.glob('.claude-config-resources/test/status/*/helpers/data.txt'))), 1)
        repeated = self.manager().plan().apply(self.state)
        self.assertTrue(all(row['status'] == 'unchanged' for row in repeated['results']))

    def test_binding_reverse_export_preserves_logical_reference_or_reports_drift(self):
        self.hook()
        self.save()
        self.manager().plan().apply(self.state)
        profile = self.data / self.machine['agents']['claude-code']['profile']
        before = profile.read_bytes()
        plan = self.manager().export(['/settings/statusLine/command'])
        self.assertTrue(plan.report['ready'], plan.report['blockers'])
        self.assertTrue(any(row['action'] == 'preserve_binding' for row in plan.report['decisions']))
        plan.apply(self.state)
        self.assertEqual(profile.read_bytes(), before)
        native = self.native / 'settings.json'
        value = json.loads(native.read_text())
        value['statusLine']['command'] = 'manual LOCAL_PRIVATE_VALUE'
        native.write_text(json.dumps(value))
        plan = self.manager().export(['/settings/statusLine/command'])
        self.assertFalse(plan.report['ready'])
        self.assertNotIn('LOCAL_PRIVATE_VALUE', json.dumps(plan.report))
        self.assertTrue(any(row.get('code') == 'binding_drift' for row in plan.report['blockers']))
        self.assertEqual(profile.read_bytes(), before)

    def test_source_sha_repository_identity_and_dirty_checkout_block(self):
        self.hook()
        self.save()
        catalogs = self.data / 'catalogs.yaml'
        before = catalogs.read_bytes()
        value = yaml.safe_load(before)
        value['catalogs']['test']['commit'] = '0' * 40
        catalogs.write_text(yaml.safe_dump(value))
        self.assertFalse(self.manager().plan().report['ready'])
        catalogs.write_bytes(before)
        run_git(self.repo, 'remote', 'set-url', 'origin', 'https://github.com/other/repo.git')
        self.assertFalse(self.manager().plan().report['ready'])
        run_git(self.repo, 'remote', 'set-url', 'origin', 'https://github.com/owner/catalog.git')
        (self.repo / 'untracked').write_text('unexpected')
        self.assertFalse(self.manager().plan().report['ready'])
        self.assertFalse(self.native.exists())

    def test_assume_unchanged_cannot_hide_modified_catalog_or_body(self):
        self.hook()
        self.save()
        for relative in ('catalog.json', 'packages/status/run.sh'):
            path = self.repo / relative
            before = path.read_bytes()
            run_git(self.repo, 'update-index', '--assume-unchanged', relative)
            path.write_bytes(before + b'\n')
            self.assertEqual(run_git(self.repo, 'status', '--porcelain'), '')
            self.assertFalse(self.manager().plan().report['ready'])
            path.write_bytes(before)
            run_git(self.repo, 'update-index', '--no-assume-unchanged', relative)

    def test_missing_dependency_and_cycle_never_expand_selection(self):
        self.hook()
        self.catalog['resources']['status']['requires']['resources'] = ['unselected@test']
        self.save()
        self.assertFalse(self.manager().plan().report['ready'])
        self.catalog['resources']['status']['requires']['resources'] = ['status@test']
        self.save()
        self.assertFalse(self.manager().plan().report['ready'])
        self.assertFalse(self.native.exists())

    def test_kind_wrong_agent_and_escaping_entry_block(self):
        self.hook()
        self.catalog['resources']['status']['kind'] = 'skill'
        self.save()
        self.assertFalse(self.manager().plan().report['ready'])
        self.catalog['resources']['status']['kind'] = 'hook'
        self.catalog['resources']['status']['compatibility'].pop('claude-code')
        self.save()
        self.assertFalse(self.manager().plan().report['ready'])
        self.catalog['resources']['status']['compatibility']['claude-code'] = {'mode': 'status-line', 'entry': '../other', 'validation': {'status': 'not-evaluated'}}
        self.save()
        self.assertFalse(self.manager().plan().report['ready'])

    def test_complete_skill_directory_and_native_call_control(self):
        package = self.add('skill', 'skill', {'SKILL.md': '---\nname: original-name\ndescription: example\n---\nbody\n',
                                            'scripts/run.sh': '#!/bin/sh\nexit 0\n', 'references/guide.txt': 'guide', 'asset.bin': b'\0\1\2'},
                           'SKILL.md', options={'control': {'disable-model-invocation': True}})
        (package / 'scripts/run.sh').chmod(0o755)
        self.save()
        plan = self.manager().plan()
        self.assertTrue(plan.report['ready'], plan.report['blockers'])
        plan.apply(self.state)
        deployed = self.native / 'skills/original-name'
        self.assertEqual((deployed / 'asset.bin').read_bytes(), b'\0\1\2')
        self.assertEqual((deployed / 'references/guide.txt').read_text(), 'guide')
        self.assertEqual((deployed / 'scripts/run.sh').stat().st_mode & 0o777, 0o700)
        self.assertIn('disable-model-invocation: true', (deployed / 'SKILL.md').read_text())
        self.assertNotIn('disable-model-invocation:', (package / 'SKILL.md').read_text())

    def test_known_skill_upgrade_removes_only_owned_obsolete_files(self):
        package = self.add('skill', 'skill', {'SKILL.md': '---\nname: skill\ndescription: example\n---\nbody\n', 'obsolete.txt': 'old'}, 'SKILL.md')
        self.save()
        self.manager().plan().apply(self.state)
        (package / 'obsolete.txt').unlink()
        (package / 'new.txt').write_text('new')
        self.save()
        plan = self.manager().plan()
        self.assertTrue(plan.report['ready'], plan.report['blockers'])
        self.assertTrue(any(row['action'] == 'remove_obsolete_resource_file' for row in plan.report['decisions']))
        plan.apply(self.state)
        self.assertFalse((self.native / 'skills/skill/obsolete.txt').exists())
        self.assertEqual((self.native / 'skills/skill/new.txt').read_text(), 'new')
        self.assertEqual(len(list(self.native.glob('.claude-config-resources/test/skill/*/obsolete.txt'))), 1)

    def test_unknown_native_files_and_known_external_edits_block_all_writes(self):
        self.add('skill', 'skill', {'SKILL.md': '---\nname: skill\ndescription: example\n---\nbody\n'}, 'SKILL.md')
        self.save()
        root = self.native / 'skills/skill'
        root.mkdir(parents=True)
        (root / 'unexpected.txt').write_text('user data')
        plan = self.manager().plan()
        self.assertFalse(plan.report['ready'])
        self.assertFalse((self.native / 'settings.json').exists())
        (root / 'unexpected.txt').unlink()
        self.manager().plan().apply(self.state)
        (root / 'SKILL.md').write_text('manual edit')
        plan = self.manager().plan()
        self.assertFalse(plan.report['ready'])
        self.assertTrue(any(row['code'] == 'resource_drift' for row in plan.report['blockers']))

    def test_removing_selection_keeps_installed_resource_and_reselection_is_known(self):
        self.add('skill', 'skill', {'SKILL.md': '---\nname: skill\ndescription: example\n---\nbody\n'}, 'SKILL.md')
        self.save()
        self.manager().plan().apply(self.state)
        selected = deepcopy(self.profile['resources'])
        self.profile.pop('resources')
        self.save_profile()
        manager = self.manager()
        manager.plan().apply(self.state)
        self.assertTrue((self.native / 'skills/skill/SKILL.md').exists())
        self.assertFalse(json.loads(manager.state_path.read_text())['resources']['skill@test']['active'])
        self.profile['resources'] = selected
        self.save_profile()
        self.assertTrue(self.manager().plan().report['ready'])

    def test_codex_skill_install_root_and_implicit_invocation_metadata(self):
        self.profile['agent_type'] = 'codex'
        self.add('skill', 'skill', {'SKILL.md': '---\nname: skill\ndescription: example\n---\nbody\n'}, 'SKILL.md',
                 requires={'bindings': {'install_root': {'kind': 'path', 'required': True}}},
                 options={'control': {'allow_implicit_invocation': False}}, bindings={'install_root': 'paths.user.skills'})
        root = self.root / 'home/.agents/skills'
        self.machine['bindings']['paths'] = {'user.skills': str(root)}
        self.save()
        plan = self.manager().plan()
        self.assertTrue(plan.report['ready'], plan.report['blockers'])
        plan.apply(self.state)
        self.assertFalse(yaml.safe_load((root / 'skill/agents/openai.yaml').read_text())['policy']['allow_implicit_invocation'])

    def test_binding_kind_missing_slot_and_secret_value_never_copied(self):
        self.hook()
        entry = self.catalog['resources']['status']
        entry['requires']['bindings'] = {'interpreter': {'kind': 'executable', 'required': True}}
        self.save()
        self.assertFalse(self.manager().plan().report['ready'])
        selected = self.profile['resources']['hooks']['status@test']
        selected['bindings'] = {'interpreter': 'paths.wrong'}
        self.machine['bindings']['paths'] = {'wrong': str(self.root)}
        self.save_profile()
        self.assertFalse(self.manager().plan().report['ready'])
        selected['bindings'] = {'interpreter': 'executables.bash', 'credential': 'secrets.test-token'}
        entry['requires']['bindings']['credential'] = {'kind': 'secret', 'required': True}
        self.machine['bindings']['secrets'] = {'test-token': {'provider': 'env', 'variable': 'ISOLATED_TEST_TOKEN'}}
        self.save()
        with patch.dict(os.environ, {'ISOLATED_TEST_TOKEN': 'NEVER_COPY_ME'}):
            plan = self.manager().plan()
            self.assertTrue(plan.report['ready'], plan.report['blockers'])
            self.assertNotIn('NEVER_COPY_ME', json.dumps(plan.report))
            self.assertNotIn('NEVER_COPY_ME', str(plan.changes))

    def test_owned_instruction_block_preserves_unmanaged_text(self):
        package = self.add('guide', 'instruction', {'guide.md': 'managed guidance\n'}, 'guide.md')
        self.save()
        self.native.mkdir()
        path = self.native / 'CLAUDE.md'
        path.write_text('user preface\n')
        self.manager().plan().apply(self.state)
        self.assertIn('user preface', path.read_text())
        path.write_text('new unowned text\n' + path.read_text())
        (package / 'guide.md').write_text('new managed guidance\n')
        self.save()
        plan = self.manager().plan()
        self.assertTrue(plan.report['ready'], plan.report['blockers'])
        plan.apply(self.state)
        self.assertIn('new unowned text', path.read_text())
        self.assertIn('new managed guidance', path.read_text())
        self.assertEqual(path.read_text().count(START), 1)
        path.write_text(path.read_text().replace('new managed guidance', 'manual edit'))
        self.assertFalse(self.manager().plan().report['ready'])

    def test_unowned_instruction_marker_and_relative_import_are_blocked(self):
        self.add('guide', 'instruction', {'guide.md': 'managed guidance\n'}, 'guide.md')
        self.save()
        self.native.mkdir()
        (self.native / 'CLAUDE.md').write_text(START + '\nunowned\n' + END)
        self.assertFalse(self.manager().plan().report['ready'])
        (self.repo / 'packages/guide/guide.md').write_text('@./companion.md\n')
        self.save()
        self.assertFalse(self.manager().plan().report['ready'])

    def test_agent_body_and_model_override_native_rules(self):
        self.add('review', 'agent', {'role.md': '---\nname: reviewer\ndescription: review\nmodel: inherit\n---\nCheck code\n'}, 'role.md', options={'model': 'sonnet'})
        self.add('policy', 'rule', {'policy.md': '---\npaths:\n  - "src/**"\n---\nRespect conventions\n'}, 'policy.md')
        self.save()
        plan = self.manager().plan()
        self.assertTrue(plan.report['ready'], plan.report['blockers'])
        plan.apply(self.state)
        self.assertIn('model: sonnet', (self.native / 'agents/reviewer.md').read_text())
        self.assertTrue((self.native / 'rules/policy.md').exists())
        self.assertIn('model: inherit', (self.repo / 'packages/review/role.md').read_text())

    def test_symlink_is_contained_and_pinned_target_is_required(self):
        self.hook()
        root = self.repo / 'packages/status'
        (root / 'contained.sh').symlink_to('run.sh')
        self.save()
        self.assertTrue(self.manager().plan().report['ready'])
        outside = self.repo / 'outside.sh'
        outside.write_text('outside')
        (root / 'escape.sh').symlink_to('../../outside.sh')
        self.save()
        self.assertFalse(self.manager().plan().report['ready'])

    def test_external_source_must_be_prepared_at_exact_declared_commit(self):
        external = self.root / 'external'
        external.mkdir()
        run_git(external, 'init', '-q')
        run_git(external, 'config', 'user.name', 'isolated-test')
        run_git(external, 'config', 'user.email', 'isolated@example.invalid')
        run_git(external, 'remote', 'add', 'origin', 'https://github.com/owner/external.git')
        (external / 'guide.md').write_text('external guidance')
        run_git(external, 'add', '.')
        run_git(external, 'commit', '-qm', 'external fixture')
        commit = run_git(external, 'rev-parse', 'HEAD')
        self.add('guide', 'instruction', {'unused.md': 'unused'}, 'guide.md')
        self.catalog['resources']['guide']['source'] = {'kind': 'external', 'repo': 'owner/external', 'commit': commit, 'path': '.'}
        self.save()
        self.assertFalse(self.manager().plan().report['ready'])
        prepared = self.state / 'sources/owner--external' / commit
        prepared.parent.mkdir(parents=True)
        shutil.move(external, prepared)
        plan = self.manager().plan()
        self.assertTrue(plan.report['ready'], plan.report['blockers'])
        plan.apply(self.state)
        self.assertIn('external guidance', (self.native / 'CLAUDE.md').read_text())

    def test_submodule_head_and_gitlink_are_both_verified(self):
        external = self.root / 'submodule-source'
        external.mkdir()
        run_git(external, 'init', '-q')
        run_git(external, 'config', 'user.name', 'isolated-test')
        run_git(external, 'config', 'user.email', 'isolated@example.invalid')
        (external / 'guide.md').write_text('submodule guide')
        run_git(external, 'add', '.')
        run_git(external, 'commit', '-qm', 'submodule fixture')
        commit = run_git(external, 'rev-parse', 'HEAD')
        run_git(self.repo, '-c', 'protocol.file.allow=always', 'submodule', 'add', '-q', str(external), 'upstreams/child')
        run_git(self.repo / 'upstreams/child', 'remote', 'set-url', 'origin', 'https://github.com/owner/child.git')
        self.add('guide', 'instruction', {'unused.md': 'unused'}, 'guide.md')
        self.catalog['resources']['guide']['source'] = {'kind': 'submodule', 'repo': 'owner/child', 'commit': commit, 'path': 'upstreams/child'}
        self.save()
        self.assertTrue(self.manager().plan().report['ready'])
        self.catalog['resources']['guide']['source']['commit'] = '0' * 40
        self.save()
        self.assertFalse(self.manager().plan().report['ready'])

    def test_snapshot_hash_uses_complete_pinned_tree(self):
        self.add('guide', 'instruction', {'guide.md': 'snapshot guide'}, 'guide.md')
        self.save()
        manager = self.manager()
        package = CatalogResolver(manager).resolve(self.profile)['guide@test']
        item = self.catalog['resources']['guide']
        item['source'] = {'kind': 'snapshot', 'path': 'packages/guide', 'baseline': 'snapshot-1', 'sha256': package.source_fingerprint}
        self.save()
        self.assertTrue(self.manager().plan().report['ready'])
        item['source']['sha256'] = '0' * 64
        self.save()
        self.assertFalse(self.manager().plan().report['ready'])

    def test_codex_agent_native_body_is_separate_from_runtime_agents_settings(self):
        self.profile['agent_type'] = 'codex'
        self.add('review', 'agent', {'role.toml': '# keep source comment\nname = "reviewer"\ndescription = "Review"\ndeveloper_instructions = "Check code"\nmodel = "source"\n'},
                 'role.toml', options={'model': 'selected-role-model', 'model_reasoning_effort': 'high'})
        self.save()
        plan = self.manager().plan()
        self.assertTrue(plan.report['ready'], plan.report['blockers'])
        plan.apply(self.state)
        native = tomlkit.parse((self.native / 'agents/reviewer.toml').read_text()).unwrap()
        self.assertEqual(native['model'], 'selected-role-model')
        self.assertEqual(native['model_reasoning_effort'], 'high')
        self.assertIn('# keep source comment', (self.native / 'agents/reviewer.toml').read_text())
        self.assertNotIn('agents', tomlkit.parse((self.native / 'config.toml').read_text()).unwrap())

    def test_native_identity_rename_retires_only_previous_owned_definition(self):
        source = self.add('review', 'agent', {'role.md': '---\nname: before\ndescription: review\n---\nbody\n'}, 'role.md')
        self.save()
        self.manager().plan().apply(self.state)
        (source / 'role.md').write_text('---\nname: after\ndescription: review\n---\nbody\n')
        self.save()
        plan = self.manager().plan()
        self.assertTrue(plan.report['ready'], plan.report['blockers'])
        plan.apply(self.state)
        self.assertFalse((self.native / 'agents/before.md').exists())
        self.assertTrue((self.native / 'agents/after.md').exists())

    def test_unknown_options_binding_destination_and_native_identity_collision_block(self):
        self.hook()
        self.profile['resources']['hooks']['status@test']['options'] = {'enabled': True}
        self.save()
        self.assertFalse(self.manager().plan().report['ready'])
        self.profile['resources']['hooks']['status@test'].pop('options')
        declaration = self.profile['bindings'].pop('/settings/statusLine/command')
        self.profile['bindings']['/settings/unknown/command'] = declaration
        self.save_profile()
        self.assertFalse(self.manager().plan().report['ready'])
        self.profile.pop('bindings')
        self.add('one', 'agent', {'role.md': '---\nname: duplicate\ndescription: one\n---\nbody\n'}, 'role.md')
        self.add('two', 'agent', {'role.md': '---\nname: duplicate\ndescription: two\n---\nbody\n'}, 'role.md')
        self.save()
        self.assertFalse(self.manager().plan().report['ready'])

    def test_late_extra_file_during_write_is_not_adopted_by_directory_guard(self):
        self.hook()
        self.save()
        plan = self.manager().plan()
        root = next(iter(plan.directories))
        injected = False
        def inject_extra(path, content, mode):
            nonlocal injected
            atomic_write(path, content, mode)
            if not injected:
                (root / 'unowned-late.txt').write_text('manual file')
                injected = True
        with self.assertRaises(PlanStale):
            plan.apply(self.state, writer=inject_extra)
        self.assertFalse((self.native / 'settings.json').exists())
        self.assertTrue((root / 'unowned-late.txt').exists())

    def test_selected_plugin_component_overlap_is_blocked(self):
        self.add('parent', 'plugin', {'plugin.json': '{}', 'skills/child/SKILL.md': '---\nname: child\ndescription: child\n---\nbody\n'}, 'plugin.json')
        self.add('child', 'skill', {'SKILL.md': '---\nname: child\ndescription: child\n---\nbody\n'}, 'SKILL.md')
        self.catalog['resources']['child']['source']['path'] = 'packages/parent/skills/child'
        self.save()
        self.assertFalse(self.manager().plan().report['ready'])

    def test_preview_stale_on_source_or_extra_target_file(self):
        self.hook()
        self.save()
        plan = self.manager().plan()
        (self.repo / 'packages/status/run.sh').write_text('changed')
        with self.assertRaises(PlanStale):
            plan.apply(self.state)
        (self.repo / 'packages/status/run.sh').write_text('#!/bin/sh\nprintf safe\n')
        plan = self.manager().plan()
        root = next(iter(plan.directories))
        root.mkdir(parents=True, exist_ok=True)
        (root / 'late.txt').write_text('late addition')
        with self.assertRaises(PlanStale):
            plan.apply(self.state)
        self.assertFalse((self.native / 'settings.json').exists())

    def test_failure_restores_script_files_and_never_writes_native_config(self):
        self.hook()
        self.save()
        plan = self.manager().plan()
        def fail_native(path, content, mode):
            if path.name == 'settings.json':
                raise OSError('simulated failure')
            atomic_write(path, content, mode)
        with self.assertRaises(OSError):
            plan.apply(self.state, writer=fail_native)
        self.assertFalse((self.native / 'settings.json').exists())
        self.assertFalse(any(path.is_file() for path in self.native.rglob('*')))

    def test_invalid_native_plugin_manifest_blocks_entire_plan(self):
        self.add('plugin', 'plugin', {'plugin.json': '{}'}, 'plugin.json', options={'enabled': False})
        self.save()
        plan = self.manager().plan()
        self.assertFalse(plan.report['ready'])
        self.assertTrue(any(row.get('code') == 'resource_preflight_failed' for row in plan.report['blockers']))
        self.assertFalse(self.native.exists())


if __name__ == '__main__':
    unittest.main()
