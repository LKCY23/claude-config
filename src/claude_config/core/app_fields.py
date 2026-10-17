"""App-only preference whitelist. No Agent settings, defaults or local paths."""
import re

from .schema import closed, mapping, pointer, require
from .app_themes import CHROME_THEMES, CODE_THEMES, theme_paths, validate_theme


# Native key names are also the portable Profile keys. All entries are optional.
CODEX = {
    'appearanceTheme': ('enum', ('system', 'light', 'dark')),
    'appearanceDiffMarkerStyle': ('enum', ('color', 'symbols')),
    'sansFontSize': ('number', (11, 16)),
    'codeFontSize': ('number', (8, 24)),
    'useFontSmoothing': ('bool', None),
    'usePointerCursors': ('bool', None),
    'reduced-motion-preference': ('enum', ('system', 'on', 'off')),
    'followUpQueueMode': ('enum', ('queue', 'steer', 'interrupt')),
    'composerEnterBehavior': ('enum', ('enter', 'cmdIfMultiline', 'cmdAlways')),
    'composerPlainTextMode': ('bool', None),
    'confirm-before-close': ('enum', ('never', 'keyboardOnly', 'always')),
    'conversationDetailMode': ('enum', ('STEPS_PROSE', 'STEPS_COMMANDS', 'STEPS_EXECUTION')),
    'show-context-window-usage': ('bool', None),
    'show-educational-tips': ('bool', None),
    'default-workspace-full-view': ('bool', None),
    'sidebar-show-projects-in-recents': ('bool', None),
    'localeOverride': ('locale', None),
    'open-in-target-preferences': ('open-target', None),
    'open-link-in-target-preference': ('enum', ('in-app-browser', 'external-browser')),
    'open-local-url-in-target-preference': ('enum', ('in-app-browser', 'external-browser')),
    'git-branch-prefix': ('string', None),
    'git-always-force-push': ('bool', None),
    'git-create-pull-request-as-draft': ('bool', None),
    'git-pull-request-merge-method': ('enum', ('merge', 'squash')),
    'git-pr-poll-interval-seconds': ('integer', (15, 300)),
    'git-show-sidebar-pr-icons': ('bool', None),
    'worktree-auto-cleanup-enabled': ('bool', None),
    'worktree-keep-count': ('integer', (1, None)),
    'git-review-mode': ('enum', ('full', 'last-turn-only')),
    'reviewDelivery': ('enum', ('inline', 'detached')),
    'notifications-turn-mode': ('enum', ('off', 'unfocused', 'always')),
    'notifications-permissions-enabled': ('bool', None),
    'notifications-questions-enabled': ('bool', None),
    'notifications-dot-enabled': ('bool', None),
    'notifications-sound': ('enum', ('default', 'classic', 'none', 'Basso', 'Glass', 'Ping', 'Pop', 'Purr', 'Tink')),
    'preventSleepWhileRunning': ('bool', None),
    'keepRemoteControlAwakeWhilePluggedIn': ('bool', None),
}
for key in CHROME_THEMES:
    CODEX[key] = ('chrome-theme', None)
for key, mode in (('appearanceLightCodeThemeId', 'light'), ('appearanceDarkCodeThemeId', 'dark')):
    CODEX[key] = ('enum', tuple(name for name, variants in CODE_THEMES.items() if mode in variants))
CLAUDE = {
    'userThemeMode': ('enum', ('system', 'light', 'dark')),
    'locale': ('locale', None),
    'chatTabEnabled': ('bool', None),
    'autoModeEnabled': ('bool', None),
    'builtinBrowserEnabled': ('bool', None),
}
FEATURES = frozenset(('chatTabEnabled', 'autoModeEnabled', 'builtinBrowserEnabled'))

# Font selection/files/reset and imported notification sounds are outside scope.
# Do not reserve them as pending interfaces or accept placeholder declarations.


def registry(app):
    return CODEX if app == 'codex-app' else CLAUDE


def validate_settings(app, settings):
    mapping(settings, 'App settings')
    fields = registry(app)
    for key, value in settings.items():
        require(key in fields, 'Unregistered or excluded App preference: ' + key)
        kind, domain = fields[key]
        valid = False
        if kind == 'bool':
            valid = type(value) is bool
        elif kind == 'enum':
            valid = isinstance(value, str) and value in domain
        elif kind in ('number', 'integer'):
            valid = (type(value) in ((int, float) if kind == 'number' else (int,))
                     and value >= domain[0] and (domain[1] is None or value <= domain[1]))
        elif kind == 'string':
            valid = isinstance(value, str)
        elif kind == 'locale':
            valid = isinstance(value, str) and re.fullmatch(r'[a-z]{2,3}(?:-[A-Za-z0-9]{2,8})*', value) is not None
        elif kind == 'open-target':
            closed(value, ('global',), (), 'portable open target')
            valid = 'global' not in value or (isinstance(value['global'], str) and
                    re.fullmatch(r'[a-z][a-z0-9_-]*', value['global']) is not None)
        elif kind == 'chrome-theme':
            validate_theme(value)
            valid = True
        require(valid, 'Invalid App preference type or value: ' + key)
    return settings


def export_paths(app, selectors):
    result = []
    for selector in selectors:
        path = pointer(selector)
        require(len(path) >= 2 and path[0] == 'settings', 'Export requires an explicit App settings field')
        key = path[1]
        require(key in registry(app), 'Unregistered or excluded App export field')
        if key in CHROME_THEMES:
            require(path[1:] in theme_paths(key), 'Select an atomic registered theme field')
        else:
            expected = ('settings', key, 'global') if key == 'open-in-target-preferences' else ('settings', key)
            require(path == expected, 'Select an atomic portable App field; per-project paths are excluded')
        require(path not in result, 'Duplicate App export selector')
        result.append(path)
    return result


def native_paths(app):
    paths = []
    for key in registry(app):
        paths += theme_paths(key) if key in CHROME_THEMES else [(key, 'global')] if key == 'open-in-target-preferences' else [(key,)]
    return paths


def validate_policies(app, *policies):
    for policy in policies:
        for selector, action in (policy or {}).get('fields', {}).items():
            export_paths(app, [selector])
            require(action not in ('append_unique', 'replace'), 'Selected App fields are scalar preferences, not arrays')
