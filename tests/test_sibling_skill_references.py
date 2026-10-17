"""Selected sibling skills may link to pinned files at the same install root."""
import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).parent))
from test_profile_resources import ResourceFixture
from claude_config.core.resource_references import package_references


class ReferenceParserTests(unittest.TestCase):
    def test_literal_backticks_do_not_expose_later_code_examples(self):
        text = 'A `` ` `` literal; `![example](@./missing.png)`\n[real](data.txt)'
        result = package_references({'SKILL.md': text.encode(), 'data.txt': b'ok'}, 'SKILL.md')
        self.assertEqual(result['count'], 1)

    def test_directory_links_require_tracked_descendants_and_imports_require_files(self):
        files = {'SKILL.md': b'[templates](assets/)\n', 'assets/one.html': b'ok'}
        self.assertEqual(package_references(files, 'SKILL.md')['count'], 1)
        files['SKILL.md'] = b'@assets/\n'
        with self.assertRaisesRegex(ValueError, 'file, not a directory'):
            package_references(files, 'SKILL.md', native_imports=True)
        with self.assertRaises(ValueError):
            package_references({'SKILL.md': b'[parent](../)\n'}, 'SKILL.md',
                               sibling_files=frozenset({'../beta/SKILL.md'}))


class SiblingSkillTests(ResourceFixture):
    def prepare(self, agent='claude-code', separate_roots=False):
        self.profile['agent_type'] = agent
        self.machine['bindings']['paths'] = {
            'one': str(self.root / 'one/.agents/skills'),
            'two': str(self.root / 'two/.agents/skills'),
        }
        for name, body in (
            ('alpha', '[beta](../beta/references/info.md)\n'),
            ('beta', '[alpha](../alpha/SKILL.md)\n'),
        ):
            self.add(name, 'skill', {
                'SKILL.md': f'---\nname: {name}\ndescription: test\n---\n{body}',
                'references/info.md': 'Local reference\n',
            }, 'SKILL.md',
                requires={'bindings': {'install_root': {'kind': 'path', 'required': False}}},
                bindings={'install_root': 'paths.two' if separate_roots and name == 'beta' else 'paths.one'} if agent == 'codex' else {})
        self.save()

    def test_both_agents_preserve_links_and_export(self):
        for agent in ('claude-code', 'codex'):
            with self.subTest(agent=agent):
                # Each host uses a separate temporary fixture.
                if agent == 'codex':
                    self.tearDown()
                    self.setUp()
                self.prepare(agent)
                plan = self.manager().plan()
                self.assertTrue(plan.report['ready'], plan.report['blockers'])
                plan.apply(self.state)
                root = self.native / 'skills' if agent == 'claude-code' else self.root / 'one/.agents/skills'
                self.assertIn('../beta/references/info.md', (root / 'alpha/SKILL.md').read_text())
                self.assertTrue((root / 'alpha/../beta/references/info.md').is_file())
                self.assertTrue(self.manager().export([], resource_refs=['alpha@test', 'beta@test']).report['ready'])
                export = self.manager().export([], resource_refs=['alpha@test'])
                self.assertTrue(export.report['ready'], export.report['blockers'])
                del self.profile['resources']['skills']['beta@test']
                self.save_profile()
                self.assertFalse(self.manager().plan().report['ready'])

    def test_different_install_roots_do_not_authorize_sibling_link(self):
        self.prepare('codex', separate_roots=True)
        self.assertFalse(self.manager().plan().report['ready'])

    def test_missing_sibling_file_blocks(self):
        self.prepare()
        (self.repo / 'packages/beta/references/info.md').unlink()
        self.save()
        self.assertFalse(self.manager().plan().report['ready'])


if __name__ == '__main__':
    unittest.main()
