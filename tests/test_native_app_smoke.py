"""Opt-in installed App source audits and temporary native preference files."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from claude_config.core.app_manager import AppProfileManager
from claude_config.core.app_native import AppProbe
from claude_config.core.native import NativeProbe
from claude_config.core.fields import leaves
from claude_config.core.recovery import rollback_plan


class IsolatedAppProbe(AppProbe):
    def __init__(self, fixture):
        self.fixture = fixture

    def assert_stopped(self, application):
        # Only these temporary native data roots are used by the test, so the
        # real GUI cannot retain or overwrite them. We never launch that GUI.
        assert self.fixture.is_dir()


@unittest.skipUnless(os.environ.get('CLAUDE_CONFIG_RUN_NATIVE_SMOKE') == '1', 'Opt-in installed App source checks')
class NativeAppSmoke(unittest.TestCase):
    def exercise(self, app, application, settings, *, features=False, backend=None):
        with tempfile.TemporaryDirectory(prefix='app-profile-smoke-') as directory:
            root = Path(directory)
            name = f'profiles/apps/{app}/work.yaml'
            profile = root / 'data' / name
            profile.parent.mkdir(parents=True)
            profile.write_text(yaml.safe_dump({'schema_version': 1, 'app': app, 'settings': settings}))
            native = root / 'native'
            native.mkdir()
            machine = {'schema_version': 1, 'machine_id': 'isolated-app', 'config_dir': str(root / 'data'),
                       'catalog_roots': {}, 'agents': {}, 'apps': {app: {'profile': name, 'binding': {
                           'application': application, 'data_root': str(native)}}}, 'bindings': {},
                       'runtime': {'state_dir': str(root / 'state')}}
            if app == 'codex-app':
                home = root / 'codex-home'
                home.mkdir()
                machine['agents']['codex'] = {'profile': 'profiles/agents/codex/work.yaml', 'binding': {
                    'executable': backend or '/fixture/unused', 'config_root': str(home)}}
            elif features:
                (native / 'config.json').write_text('{"token":"LOCAL_ONLY"}')
                (native / 'claude_desktop_config.json').write_text('{"deploymentMode":"3p"}')
                library = native / 'configLibrary'
                library.mkdir()
                uid = '11111111-2222-3333-4444-555555555555'
                (library / '_meta.json').write_text(json.dumps({'appliedId': uid}))
                (library / (uid + '.json')).write_text('{"inferenceGatewayApiKey":"LOCAL_ONLY"}')
            path = root / 'machine.yaml'
            path.write_text(yaml.safe_dump(machine))
            probe = IsolatedAppProbe(root)
            manager = AppProfileManager(path, app, probe=probe)
            before = manager.native_path.read_bytes() if manager.native_path.exists() else None
            plan = manager.plan()
            self.assertTrue(plan.report['ready'], plan.report['blockers'])
            self.assertNotIn('LOCAL_ONLY', json.dumps(plan.report))
            result = plan.apply(root / 'state')
            if backend:
                env = {'PATH': os.environ.get('PATH', ''), 'HOME': str(root), 'CODEX_HOME': str(home), 'TMPDIR': str(root)}
                readback = NativeProbe._codex_read(backend, home, env)
                self.assertIsInstance(readback, dict)
                self.assertEqual({key: readback['desktop'].get(key) for key in settings}, settings)
            selectors = ['/settings/' + '/'.join(path) for path, _ in leaves(settings)]
            exported = AppProfileManager(path, app, probe=probe).export(selectors)
            self.assertTrue(exported.report['ready'], exported.report['blockers'])
            rollback_plan(AppProfileManager(path, app, probe=probe), Path(result['recovery']).parent.name).apply(root / 'state')
            self.assertEqual(manager.native_path.read_bytes() if manager.native_path.exists() else None, before)

    def test_codex_app_preferences_backend_readback(self):
        backend = os.environ.get('CLAUDE_CONFIG_NATIVE_CODEX_APP')
        if not backend:
            self.skipTest('CLAUDE_CONFIG_NATIVE_CODEX_APP is required')
        self.exercise('codex-app', os.environ.get('CLAUDE_CONFIG_CODEX_APPLICATION', '/Applications/ChatGPT.app'),
                      {'followUpQueueMode': 'queue', 'git-branch-prefix': '', 'preventSleepWhileRunning': False,
                       'reviewDelivery': 'inline', 'appearanceLightCodeThemeId': 'vscode-plus',
                       'appearanceDarkCodeThemeId': 'nord',
                       'appearanceLightChromeTheme': {'accent': '#123456', 'opaqueWindows': False,
                                                     'semanticColors': {'skill': '#abcdef'}}}, backend=backend)

    def test_claude_official_appearance_and_language(self):
        self.exercise('claude-desktop', os.environ.get('CLAUDE_CONFIG_CLAUDE_APPLICATION', '/Applications/Claude.app'),
                      {'userThemeMode': 'dark', 'locale': 'en-US'})

    def test_claude_existing_third_party_feature_preferences(self):
        self.exercise('claude-desktop', os.environ.get('CLAUDE_CONFIG_CLAUDE_APPLICATION', '/Applications/Claude.app'),
                      {'chatTabEnabled': True, 'autoModeEnabled': False, 'builtinBrowserEnabled': True}, features=True)
