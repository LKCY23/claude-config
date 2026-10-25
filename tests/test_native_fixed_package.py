"""Opt-in exact published package install, no-op, and scoped rollback."""
import json
import os
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from claude_config.core.manager import ProfileManager
from claude_config.core.recovery import rollback_plan
from test_profile_resources import ResourceFixture


@unittest.skipUnless(os.environ.get('CLAUDE_CONFIG_RUN_NATIVE_SMOKE') == '1' and os.environ.get('CLAUDE_CONFIG_FIXED_PACKAGE'),
                     'Opt-in fixed published package test')
class FixedPackageNativeTests(ResourceFixture):
    def test_five_existing_plus_published_package_and_exact_rollback(self):
        binary = os.environ['CLAUDE_CONFIG_NATIVE_CLAUDE']
        self.machine['agents']['claude-code']['binding']['executable'] = binary
        for n in range(5):
            name = 'existing-' + str(n)
            self.add(name, 'plugin', {'.claude-plugin/plugin.json': json.dumps({'name': name, 'version': '1.0.0'}),
                                      'body.txt': 'existing'}, '.claude-plugin/plugin.json')
        self.save()
        manager = lambda: ProfileManager(self.machine_path, 'claude-code')
        manager().plan().apply(self.state)
        package = Path(os.environ['CLAUDE_CONFIG_FIXED_PACKAGE'])
        manifest = json.loads((package / '.claude-plugin/plugin.json').read_text())
        files = {}
        for path in package.rglob('*'):
            self.assertFalse(path.is_symlink(), str(path))
            if path.is_file():
                files[path.relative_to(package).as_posix()] = path.read_bytes()
        root = self.add(manifest['name'], 'plugin', files, '.claude-plugin/plugin.json')
        for path in package.rglob('*'):
            if path.is_file():
                (root / path.relative_to(package)).chmod(path.stat().st_mode & 0o777)
        self.save()
        plan = manager().plan()
        self.assertTrue(plan.report['ready'], plan.report['blockers'])
        operation = plan.native_operations[0]
        before = operation.world()
        result = plan.apply(self.state)
        selected = next(x['desired'] for x in operation.plugins if x['desired']['selector'].startswith(manifest['name'] + '@'))
        self.assertEqual(operation.native(selected)['image'], selected['image'])
        repeated = manager().plan()
        self.assertTrue(all(row['status'] == 'unchanged' for row in repeated.report['results']))
        restore = rollback_plan(manager(), Path(result['recovery']).parent.name)
        self.assertTrue(restore.report['ready'], restore.report['blockers'])
        restore.apply(self.state)
        self.assertEqual(operation.world(), before)


if __name__ == '__main__':
    unittest.main()
