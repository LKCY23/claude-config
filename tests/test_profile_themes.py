from copy import deepcopy
import json
from pathlib import Path
import sys
import unittest

import tomlkit

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from claude_config.core.app_fields import export_paths, validate_settings
from claude_config.core.recovery import rollback_plan
from claude_config.core.schema import SchemaError
import test_profile_apps as fixtures


class ThemeTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.AppTests()
        self.f.setUp()
    def tearDown(self): self.f.tearDown()

    def test_color_leaf_merge_export_and_rollback_preserve_fonts_and_local_state(self):
        self.f.profile(settings={'appearanceLightChromeTheme': {'accent': '#123456', 'opaqueWindows': False,
                                                                'semanticColors': {'skill': '#abcdef'}},
                                'appearanceLightCodeThemeId': 'vscode-plus'})
        path = self.f.codex / 'config.toml'
        initial = {'model': 'unchanged', 'desktop': {'appearanceLightChromeTheme': {'ink': '#000000',
                    'fonts': {'code': 'LOCAL_ONLY'}, 'semanticColors': {'diffAdded': '#112233'}},
                    'git-worktree-root': '/local/worktrees', 'custom_file_handlers': {'private': {'command': '/local/handler'}}}}
        path.write_text(tomlkit.dumps(initial))
        before = path.read_bytes()
        plan = self.f.manager().plan()
        self.assertTrue(plan.report['ready'], plan.report['blockers'])
        plan.apply(self.f.state)
        value = tomlkit.parse(path.read_text()).unwrap()
        theme = value['desktop']['appearanceLightChromeTheme']
        self.assertEqual(theme['accent'], '#123456')
        self.assertFalse(theme['opaqueWindows'])
        self.assertEqual(theme['fonts']['code'], 'LOCAL_ONLY')
        self.assertEqual(theme['semanticColors']['diffAdded'], '#112233')
        self.assertEqual(value['desktop']['custom_file_handlers'], initial['desktop']['custom_file_handlers'])
        exported = self.f.manager().export(['/settings/appearanceLightChromeTheme/accent'])
        self.assertTrue(exported.report['ready'])
        self.assertNotIn('LOCAL_ONLY', json.dumps(exported.report))
        rollback_plan(self.f.manager(), Path(plan.report['recovery']).parent.name).apply(self.f.state)
        self.assertEqual(path.read_bytes(), before)

    def test_builtin_code_themes_are_checked_for_light_and_dark_modes(self):
        validate_settings('codex-app', {'appearanceLightCodeThemeId': 'proof', 'appearanceDarkCodeThemeId': 'nord'})
        for key, value in (('appearanceLightCodeThemeId', 'nord'), ('appearanceDarkCodeThemeId', 'proof'),
                           ('appearanceDarkCodeThemeId', '/local/theme'), ('appearanceLightCodeThemeId', 'custom-id')):
            with self.assertRaises(SchemaError): validate_settings('codex-app', {key: value})

    def test_color_and_nested_types_reject_unsupported_paths(self):
        for value in ({'accent': 'red'}, {'accent': '#fff'}, {'contrast': True}, {'contrast': 101},
                      {'opaqueWindows': 0}, {'semanticColors': {'unknown': '#ffffff'}}, {'backgroundImage': '/local/file'}):
            with self.assertRaises(SchemaError): validate_settings('codex-app', {'appearanceLightChromeTheme': value})

    def test_export_and_policy_require_registered_atomic_leaves(self):
        self.assertEqual(export_paths('codex-app', ['/settings/appearanceLightChromeTheme/semanticColors/skill']),
                         [('settings', 'appearanceLightChromeTheme', 'semanticColors', 'skill')])
        for path in ('/settings/appearanceLightChromeTheme', '/settings/appearanceLightChromeTheme/semanticColors',
                     '/settings/appearanceLightChromeTheme/backgroundImage'):
            with self.assertRaises(SchemaError): export_paths('codex-app', [path])

    def test_font_selection_and_reset_are_excluded(self):
        for value in ('Some Font', None):
            self.f.profile(settings={'appearanceLightChromeTheme': {'fonts': {'code': value}, 'accent': '#123456'}})
            with self.assertRaises(SchemaError): self.f.manager().plan()

    def test_font_export_and_field_policy_are_rejected(self):
        (self.f.codex / 'config.toml').write_text(tomlkit.dumps({'desktop': {'appearanceLightChromeTheme': {'fonts': {'code': 'LOCAL_ONLY'}}}}))
        with self.assertRaises(SchemaError): self.f.manager().export(['/settings/appearanceLightChromeTheme/fonts/code'])
        self.f.profile(settings={'appearanceLightChromeTheme': {'accent': '#123456'}},
                       merge_policy={'fields': {'/settings/appearanceLightChromeTheme/fonts/code': 'keep_target'}})
        with self.assertRaises(SchemaError): self.f.manager().plan()

    def test_desktop_font_and_custom_sound_assets_are_outside_scope(self):
        for value in ('serif', None):
            with self.assertRaises(SchemaError): validate_settings('claude-desktop', {'chatFont': value})
            with self.assertRaises(SchemaError): export_paths('claude-desktop', ['/settings/chatFont'])
        for settings in ({'notifications-sound': '/local/custom.wav'}, {'notificationSoundFile': '/local/custom.wav'}):
            with self.assertRaises(SchemaError): validate_settings('codex-app', settings)
        with self.assertRaises(SchemaError): export_paths('codex-app', ['/settings/notificationSoundFile'])

    def test_theme_leaf_drift_and_per_field_preservation_use_common_merge(self):
        self.f.profile(settings={'appearanceDarkChromeTheme': {'contrast': 50}},
                       merge_policy={'fields': {'/settings/appearanceDarkChromeTheme/contrast': 'keep_target'}})
        path = self.f.codex / 'config.toml'
        path.write_text(tomlkit.dumps({'desktop': {'appearanceDarkChromeTheme': {'contrast': 75}}}))
        plan = self.f.manager().plan()
        self.assertTrue(plan.report['ready'])
        plan.apply(self.f.state)
        self.assertEqual(tomlkit.parse(path.read_text())['desktop']['appearanceDarkChromeTheme']['contrast'], 75)
        path.write_text(tomlkit.dumps({'desktop': {'appearanceDarkChromeTheme': {'contrast': 80}}}))
        self.assertFalse(self.f.manager().plan().report['ready'])


if __name__ == '__main__': unittest.main()
