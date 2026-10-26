"""Opt-in installed App source audits and temporary native preference files."""
import json
import os
from pathlib import Path
import plistlib
import subprocess
import sys
import tempfile
import unittest

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from claude_config.core.app_manager import AppProfileManager
from claude_config.core.app_native import AppProbe, asar_entries
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
                (home / 'config.toml').write_text('model = "local-only-model"\n[desktop]\nunmanagedPreference = "preserve"\n')
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
            before_native = {p.relative_to(native): p.read_bytes() for p in native.rglob('*') if p.is_file()}
            plan = manager.plan()
            self.assertTrue(plan.report['ready'], plan.report['blockers'])
            self.assertNotIn('LOCAL_ONLY', json.dumps(plan.report))
            result = plan.apply(root / 'state')
            if app == 'claude-desktop':
                info = plistlib.loads((Path(application) / 'Contents/Info.plist').read_bytes())
                if info['CFBundleShortVersionString'] == '3.6556.2':
                    # Execute only selected, hash-pinned native readers/setters
                    # with in-memory dependencies, never the Electron entrypoint.
                    resources = Path(application) / 'Contents/Resources'
                    module = '.vite/build/index.chunk-jPKPzV-P.js'
                    source = asar_entries((resources / 'app.asar').read_bytes(), [module])[module]
                    source_path = root / 'source.js'
                    source_path.write_bytes(source)
                    files = {p.relative_to(native).as_posix(): p.read_text() for p in native.rglob('*.json')}
                    proc = subprocess.run(['node', str(Path(__file__).with_name('claude_desktop_36556_probe.cjs')), str(source_path)],
                                          input=json.dumps({'files': files, 'settings': settings,
                                                            'catalogs': [p.name for p in resources.glob('*.json')]}),
                                          capture_output=True, text=True, timeout=30)
                    self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
                    print(proc.stdout.strip())
            if backend:
                env = {'PATH': os.environ.get('PATH', ''), 'HOME': str(root), 'CODEX_HOME': str(home), 'TMPDIR': str(root)}
                readback = NativeProbe._codex_read(backend, home, env)
                self.assertIsInstance(readback, dict)
                self.assertEqual({key: readback['desktop'].get(key) for key in settings}, settings)
                self.assertEqual(readback['model'], 'local-only-model')
                self.assertEqual(readback['desktop']['unmanagedPreference'], 'preserve')
            selectors = ['/settings/' + '/'.join(path) for path, _ in leaves(settings)]
            export_name = f'profiles/apps/{app}/exported.yaml'
            exported = AppProfileManager(path, app, probe=probe).export(selectors, profile=export_name)
            self.assertTrue(exported.report['ready'], exported.report['blockers'])
            exported.apply(root / 'state')
            self.assertEqual(yaml.safe_load((root / 'data' / export_name).read_text())['settings'], settings)
            rollback_plan(AppProfileManager(path, app, probe=probe), Path(result['recovery']).parent.name).apply(root / 'state')
            self.assertEqual(manager.native_path.read_bytes() if manager.native_path.exists() else None, before)
            self.assertEqual({p.relative_to(native): p.read_bytes() for p in native.rglob('*') if p.is_file()}, before_native)

    def test_codex_app_preferences_backend_readback(self):
        backend = os.environ.get('CLAUDE_CONFIG_NATIVE_CODEX_APP')
        if not backend:
            self.skipTest('CLAUDE_CONFIG_NATIVE_CODEX_APP is required')
        self.exercise('codex-app', os.environ.get('CLAUDE_CONFIG_CODEX_APPLICATION', '/Applications/ChatGPT.app'),
                      {'followUpQueueMode': 'queue', 'git-branch-prefix': '', 'preventSleepWhileRunning': False,
                       'reviewDelivery': 'inline', 'appearanceLightCodeThemeId': 'vscode-plus',
                       'appearanceDarkCodeThemeId': 'nord',
                       'localeOverride': 'en-US', 'open-in-target-preferences': {'global': 'vscode'},
                       'appearanceLightChromeTheme': {'accent': '#123456', 'opaqueWindows': False,
                                                     'semanticColors': {'skill': '#abcdef'}}}, backend=backend)

    def test_codex_261021_native_schemas_store_and_failed_write_recovery(self):
        from claude_config.core.app_native import AUDITS
        from claude_config.core.app_fields import CODEX
        from claude_config.core.app_themes import CODE_THEMES
        application = Path(os.environ.get('CLAUDE_CONFIG_CODEX_APPLICATION', '/Applications/ChatGPT.app'))
        checks = AppProbe().check('codex-app', application, {'reviewDelivery': 'inline'}, {})
        self.assertEqual(checks[0]['status'], 'passed')
        if checks[0]['reviewed_version'] != '26.1021.31345':
            self.skipTest('This source probe requires the reviewed 26.1021.31345 interface')
        cases = []
        for key, (kind, domain) in CODEX.items():
            if kind == 'bool':
                valid, invalid = [False, True], ['false', 1]
            elif kind == 'enum':
                valid, invalid = list(domain), ['unregistered-test-value', False]
            elif kind in ('number', 'integer'):
                valid, invalid = [domain[0], domain[1] or domain[0] + 1], [domain[0] - 1, '12']
                if domain[1] is not None:
                    invalid.append(domain[1] + 1)
            elif kind in ('string', 'locale'):
                valid, invalid = (['', 'codex/'] if kind == 'string' else ['en-US', 'zh-CN']), [False, 1]
            elif kind == 'open-target':
                valid, invalid = [{}, {'global': 'vscode'}], [{'global': False}]
            else:
                self.assertEqual(kind, 'chrome-theme')
                theme = {'accent': '#123456', 'ink': '#000000', 'surface': '#ffffff',
                         'contrast': 45, 'opaqueWindows': False, 'fonts': {'code': None, 'content': None, 'ui': None},
                         'semanticColors': {'diffAdded': '#008000', 'diffRemoved': '#ff0000', 'skill': '#0000ff'}}
                valid, invalid = [theme], [{**theme, 'accent': 'invalid'}]
            cases.append({'key': key, 'valid': valid, 'invalid': invalid})
        with tempfile.TemporaryDirectory(prefix='codex-store-source-') as directory:
            root = Path(directory)
            entries = asar_entries((application / 'Contents/Resources/app.asar').read_bytes(), AUDITS['codex-app'][2])
            for name, content in entries.items():
                (root / Path(name).name).write_bytes(content)
            result = subprocess.run(['node', str(Path(__file__).with_name('codex_app_261021_probe.cjs')), str(root)],
                                    input=json.dumps({'cases': cases, 'themes': CODE_THEMES}),
                                    text=True, capture_output=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            evidence = json.loads(result.stdout)
            self.assertEqual(evidence['registered_fields'], len(CODEX))
            self.assertEqual(evidence['failed_write_recovery'], 'passed')

    def test_claude_official_appearance_and_language(self):
        self.exercise('claude-desktop', os.environ.get('CLAUDE_CONFIG_CLAUDE_APPLICATION', '/Applications/Claude.app'),
                      {'userThemeMode': 'dark', 'locale': 'en-US'})

    def test_claude_existing_third_party_feature_preferences(self):
        self.exercise('claude-desktop', os.environ.get('CLAUDE_CONFIG_CLAUDE_APPLICATION', '/Applications/Claude.app'),
                      {'chatTabEnabled': True, 'autoModeEnabled': False, 'builtinBrowserEnabled': True}, features=True)
