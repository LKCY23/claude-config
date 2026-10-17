"""Actual native usage readback exports only into isolated Profile files."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

import tomlkit
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from claude_config.core.manager import ProfileManager
from claude_config.core.native import NativeProbe
import test_native_resource_smoke as native_fixture


@unittest.skipUnless(os.environ.get('CLAUDE_CONFIG_RUN_NATIVE_SMOKE') == '1', 'Opt-in native resource export checks')
class NativeResourceExportSmokeTests(unittest.TestCase):
    def assert_export(self, variable):
        binary = os.environ.get(variable)
        if not binary:
            self.skipTest(variable + ' is required')
        with tempfile.TemporaryDirectory(prefix='resource-export-native-smoke-') as directory:
            root = Path(directory)
            manager = native_fixture.NativeResourceSmokeTests().fixture(directory, 'codex', binary, 'skill',
                       {'SKILL.md': '---\nname: native-smoke\ndescription: Isolated export\n---\nDo not execute business logic\n'}, 'SKILL.md')
            profile_path = manager.root / manager.entry['profile']
            profile = yaml.safe_load(profile_path.read_text())
            profile['resources']['skills']['native-smoke@smoke']['options'] = {'control': {'enabled': False}}
            profile_path.write_text(yaml.safe_dump(profile))
            plan = ProfileManager(manager.machine_path, 'codex').plan()
            self.assertTrue(plan.report['ready'], plan.report['blockers'])
            plan.apply(manager.state_root)
            value = tomlkit.parse(manager.native_path.read_text())
            value['skills']['config'][0]['enabled'] = True
            manager.native_path.write_text(tomlkit.dumps(value))
            env = {'PATH': os.environ.get('PATH', ''), 'HOME': str(root / 'home'), 'CODEX_HOME': str(root / 'native'), 'TMPDIR': directory}
            result = NativeProbe._codex_request(binary, root, env, 'skills/list', {'cwds': [directory], 'forceReload': True})
            self.assertIn('native-smoke', json.dumps(result))
            native_bytes = manager.native_path.read_bytes()
            state_bytes = manager.state_path.read_bytes()
            export = ProfileManager(manager.machine_path, 'codex').export([], resource_refs=['native-smoke@smoke'])
            self.assertTrue(export.report['ready'], export.report['blockers'])
            export.apply(manager.state_root)
            self.assertTrue(yaml.safe_load(profile_path.read_text())['resources']['skills']['native-smoke@smoke']['options']['control']['enabled'])
            self.assertEqual(manager.native_path.read_bytes(), native_bytes)
            self.assertEqual(manager.state_path.read_bytes(), state_bytes)
            self.assertNotIn(str(root), profile_path.read_text())

    def test_codex_cli_resource_export(self):
        self.assert_export('CLAUDE_CONFIG_NATIVE_CODEX')

    def test_codex_app_backend_resource_export(self):
        self.assert_export('CLAUDE_CONFIG_NATIVE_CODEX_APP')


if __name__ == '__main__':
    unittest.main()
