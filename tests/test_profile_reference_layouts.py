"""Package references retain source bytes and participate in export/recovery."""
import json
from pathlib import Path
import sys
import unittest

import tomlkit

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from claude_config.core.resource_references import package_references
from claude_config.core.recovery import rollback_plan
from claude_config.core.transaction import PlanStale
from test_profile_resources import ResourceFixture


class ReferenceSyntaxTests(unittest.TestCase):
    def test_nested_local_links_fragments_images_and_code_examples(self):
        files = {'guide.md': b'[child](./docs/child.md#part) ![image](assets/icon.png)\n'
                             b'`[example](absent.md)`\n```md\n@/private/example\n[example](absent.md)\n```\n',
                 'docs/child.md': b'[parent](../guide.md)\n', 'assets/icon.png': b'\x00'}
        result = package_references(files, 'guide.md', native_imports=True)
        self.assertEqual(result['count'], 3)
        self.assertEqual(result['links']['guide.md'][0][2:], ('docs/child.md', 'part'))

    def test_missing_escaping_absolute_and_encoded_references_rejected(self):
        for target in ('missing.md', '../outside.md', '%2e%2e/outside.md', '/private/foo', 'file:///private/foo', '//other/secret'):
            with self.subTest(target=target), self.assertRaises(ValueError):
                package_references({'guide.md': ('[x](' + target + ')').encode()}, 'guide.md')

    def test_native_import_spaces_plain_names_and_cycles(self):
        files = {'guide.md': b'@README\n@docs/space\\ name.md\n', 'README': b'plain\n', 'docs/space name.md': b'body\n'}
        result = package_references(files, 'guide.md', native_imports=True)
        self.assertEqual(result['imports']['guide.md'], ['README', 'docs/space name.md'])
        files['README'] = b'@guide.md\n'
        with self.assertRaisesRegex(ValueError, 'cycle'):
            package_references(files, 'guide.md', native_imports=True)

    def test_import_depth_accounts_for_native_wrapper(self):
        files = {f'{n}.md': (f'@{n + 1}.md\n' if n < 3 else 'last\n').encode() for n in range(4)}
        package_references(files, '0.md', native_imports=True)
        files['3.md'], files['4.md'] = b'@4.md\n', b'too deep\n'
        with self.assertRaisesRegex(ValueError, 'depth'):
            package_references(files, '0.md', native_imports=True)


class InstructionLayoutTests(ResourceFixture):
    def guide(self, agent='claude-code'):
        self.profile['agent_type'] = agent
        text = '@./parts/child.md\n[asset](./assets/data.txt)\n' if agent == 'claude-code' else '[child](./parts/child.md#part)\n'
        self.package = self.add('guide', 'instruction', {'guide.md': text, 'parts/child.md': '[asset](../assets/data.txt)\n',
                                                       'assets/data.txt': 'reference data\n'}, 'guide.md')
        self.save()

    def deploy(self):
        plan = self.manager().plan()
        self.assertTrue(plan.report['ready'], plan.report['blockers'])
        plan.apply(self.state)
        return plan

    def cache(self):
        state = json.loads(self.manager().state_path.read_text())
        return Path(state['resources']['guide@test']['cache'])

    def test_claude_layout_preserves_package_source_and_surrounding_text(self):
        self.guide()
        self.native.mkdir()
        (self.native / 'CLAUDE.md').write_text('existing unrelated instruction\n')
        plan = self.deploy()
        root = self.cache()
        text = (self.native / 'CLAUDE.md').read_text()
        self.assertIn('@' + str(root / 'guide.md'), text)
        self.assertIn('existing unrelated instruction', text)
        self.assertEqual((root / 'guide.md').read_bytes(), (self.package / 'guide.md').read_bytes())
        self.assertTrue((root / 'assets/data.txt').exists())
        self.assertTrue(any(row['name'] == 'package_references' and row['reference_count'] == 3 for row in plan.report['checks']))
        self.assertTrue(self.manager().plan().report['ready'])
        export = self.manager().export([], resource_refs=['guide@test'])
        self.assertTrue(export.report['ready'], export.report['blockers'])

    def test_referenced_file_drift_blocks_apply_and_export(self):
        self.guide()
        self.deploy()
        (self.cache() / 'assets/data.txt').write_text('external edit')
        self.assertFalse(self.manager().plan().report['ready'])
        self.assertFalse(self.manager().export([], resource_refs=['guide@test']).report['ready'])

    def test_codex_links_relocated_without_claude_import_semantics(self):
        self.guide('codex')
        self.deploy()
        self.assertIn('<' + str(self.cache() / 'parts/child.md') + '#part>', (self.native / 'AGENTS.md').read_text())
        self.assertEqual((self.cache() / 'guide.md').read_bytes(), (self.package / 'guide.md').read_bytes())
        self.assertTrue(self.manager().export([], resource_refs=['guide@test']).report['ready'])

    def test_codex_native_import_is_rejected_before_writes(self):
        self.profile['agent_type'] = 'codex'
        self.add('guide', 'instruction', {'guide.md': '@./child.md\n', 'child.md': 'child'}, 'guide.md')
        self.save()
        self.assertFalse(self.manager().plan().report['ready'])
        self.assertFalse(self.native.exists())

    def test_source_change_makes_preview_stale_and_rollback_restores_layout(self):
        self.guide()
        plan = self.manager().plan()
        (self.package / 'parts/child.md').write_text('late edit')
        with self.assertRaises(PlanStale):
            plan.apply(self.state)
        self.assertFalse(self.native.exists())
        (self.package / 'parts/child.md').write_text('[asset](../assets/data.txt)\n')
        result = self.deploy()
        recovery = rollback_plan(self.manager(), Path(result.report['recovery']).parent.name)
        self.assertTrue(recovery.report['ready'], recovery.report['blockers'])
        recovery.apply(self.state)
        self.assertFalse((self.native / 'CLAUDE.md').exists())

    def test_skill_missing_link_blocks_complete_plan(self):
        self.add('skill', 'skill', {'SKILL.md': '---\nname: test\ndescription: test\n---\n[doc](references/missing.md)\n'}, 'SKILL.md')
        self.save()
        self.assertFalse(self.manager().plan().report['ready'])
        self.assertFalse(self.native.exists())

    def test_claude_wrapper_escapes_spaces_in_machine_root_and_imported_filename(self):
        self.native = self.root / 'native space'
        self.machine['agents']['claude-code']['binding']['config_root'] = str(self.native)
        self.add('guide', 'instruction', {'guide space.md': '@docs/space\\ name.md\n', 'docs/space name.md': 'body'}, 'guide space.md')
        self.save()
        self.deploy()
        root = self.cache()
        self.assertIn('@' + str(root / 'guide space.md').replace(' ', '\\ '), (self.native / 'CLAUDE.md').read_text())
        self.assertTrue(self.manager().export([], resource_refs=['guide@test']).report['ready'])

    def test_codex_link_encodes_filename_delimiters(self):
        self.profile['agent_type'] = 'codex'
        self.add('guide', 'instruction', {'guide.md': '[child](<docs/hash%23%3F%20name.md>)\n', 'docs/hash#? name.md': 'body'}, 'guide.md')
        self.save()
        self.deploy()
        self.assertIn('hash%23%3F%20name.md>', (self.native / 'AGENTS.md').read_text())

    def test_switch_instruction_resource_uses_prior_block_owner(self):
        self.guide()
        self.deploy()
        self.add('other', 'instruction', {'guide.md': '@child.md\n', 'child.md': 'new child'}, 'guide.md')
        del self.profile['resources']['instructions']['guide@test']
        self.save()
        self.deploy()
        self.assertIn('/other/', (self.native / 'CLAUDE.md').read_text())


if __name__ == '__main__':
    unittest.main()
