"""Audited Codex App theme leaves; native defaults and font files stay local."""
import re

from .schema import closed, require

CHROME_THEMES = ('appearanceLightChromeTheme', 'appearanceDarkChromeTheme')
CODE_THEMES = {
    'ayu': ('dark',), 'catppuccin': ('dark', 'light'), 'absolutely': ('dark', 'light'),
    'codex': ('dark', 'light'), 'dracula': ('dark',), 'everforest': ('dark', 'light'),
    'github': ('dark', 'light'), 'gruvbox': ('dark', 'light'), 'linear': ('dark', 'light'),
    'lobster': ('dark',), 'material': ('dark',), 'matrix': ('dark',), 'monokai': ('dark',),
    'night-owl': ('dark',), 'nord': ('dark',), 'notion': ('dark', 'light'), 'og': ('dark',),
    'oscurange': ('dark',), 'one': ('dark', 'light'), 'proof': ('light',),
    'raycast': ('dark', 'light'), 'rose-pine': ('dark', 'light'), 'sentry': ('dark',),
    'solarized': ('dark', 'light'), 'tokyo-night': ('dark',), 'temple': ('dark',),
    'vercel': ('dark', 'light'), 'vscode-plus': ('dark', 'light'), 'xcode': ('dark', 'light'),
}
COLOR_KEYS = ('accent', 'ink', 'surface')
SEMANTIC_KEYS = ('diffAdded', 'diffRemoved', 'skill')


def theme_paths(key):
    return [(key, name) for name in (*COLOR_KEYS, 'accentSource', 'contrast', 'opaqueWindows')] + [
        (key, 'semanticColors', name) for name in SEMANTIC_KEYS]


def validate_theme(value):
    closed(value, (*COLOR_KEYS, 'accentSource', 'contrast', 'opaqueWindows', 'semanticColors'), (), 'Chrome theme')
    def color(item):
        require(isinstance(item, str) and re.fullmatch(r'#[0-9a-fA-F]{6}', item), 'Theme colors must be six-digit hex strings')
    for key in COLOR_KEYS:
        if key in value:
            color(value[key])
    if 'accentSource' in value:
        require(value['accentSource'] in ('chatgpt', 'custom'), 'Invalid theme accent source')
    if 'contrast' in value:
        require(type(value['contrast']) is int and 0 <= value['contrast'] <= 100, 'Theme contrast must be an integer from 0 to 100')
    if 'opaqueWindows' in value:
        require(type(value['opaqueWindows']) is bool, 'Theme opaqueWindows must be Boolean')
    if 'semanticColors' in value:
        closed(value['semanticColors'], SEMANTIC_KEYS, (), 'Theme semantic colors')
        for item in value['semanticColors'].values():
            color(item)
