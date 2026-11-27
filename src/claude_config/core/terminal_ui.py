"""Shared terminal UI primitives: unified ANSI SGR palette, CJK width alignment, badges, and sections."""
import os
import re
import sys
import unicodedata

_ANSI_RE = re.compile(r'\x1b\[[0-9;]*[A-Za-z]')

SGR = {
    'title': '1;36',
    'heading': '1;36',
    'cyan': '36',
    'cmd': '36',
    'pending': '36',
    'hint': '36',
    'success': '32',
    'warning': '33',
    'error': '1;31',
    'bold': '1',
    'muted': '2',
}

_BADGES = {
    'success': ('✓', 'success'),
    'pending': ('→', 'pending'),
    'warning': ('!', 'warning'),
    'error': ('✗', 'error'),
    'muted': ('·', 'muted'),
}


def color_enabled(mode='auto', *, stdout=None):
    if 'NO_COLOR' in os.environ or mode == 'never':
        return False
    if mode == 'always':
        return True
    stdout = stdout or sys.stdout
    return (mode == 'auto' and os.name == 'posix'
            and os.environ.get('TERM', '').lower() not in ('', 'dumb', 'unknown')
            and bool(getattr(stdout, 'isatty', lambda: False)()))


def strip_ansi(text):
    return _ANSI_RE.sub('', text or '')


def display_width(text):
    """Return terminal cell width after stripping ANSI SGR sequences."""
    width = 0
    for char in strip_ansi(str(text)):
        width += 0 if unicodedata.combining(char) else 2 if unicodedata.east_asian_width(char) in ('W', 'F') else 1
    return width


def pad_display(text, width):
    """Right-pad text with spaces to target terminal column width."""
    text = str(text)
    gap = width - display_width(text)
    return text + (' ' * gap if gap > 0 else '')


def truncate_display(text, max_width):
    """Truncate plain text to fit within max_width terminal cells."""
    text = str(text)
    if max_width <= 0:
        return ''
    if display_width(text) <= max_width:
        return text
    if max_width == 1:
        return '…'
    acc = []
    size = 0
    limit = max_width - 1
    for char in text:
        cells = 0 if unicodedata.combining(char) else 2 if unicodedata.east_asian_width(char) in ('W', 'F') else 1
        if size + cells > limit:
            break
        acc.append(char)
        size += cells
    return ''.join(acc) + '…'


def style(text, role, *, color=False):
    text = str(text)
    if not color or 'NO_COLOR' in os.environ or not role:
        return text
    code = SGR.get(role)
    if not code:
        return text
    return '\n'.join(f'\x1b[{code}m{part}\x1b[0m' if part else '' for part in text.split('\n'))


def badge(kind, label='', *, color=False):
    symbol, role = _BADGES[kind]
    text = f'{symbol} {label}' if label else symbol
    return style(text, role, color=color)


def heading(text, *, color=False):
    return style(text, 'title', color=color)


def section(text, *, color=False):
    return style(text, 'heading', color=color)


def meta_line(label, value, *, color=False, indent='  '):
    return style(f'{indent}{label}: {value}', 'muted', color=color)


def rule(width=70, *, color=False):
    return style('─' * width, 'muted', color=color)


def next_steps(commands, *, title='建议下一步：', color=False):
    unique = [cmd for cmd in dict.fromkeys(commands) if cmd]
    if not unique:
        return []
    lines = ['', section(title, color=color)]
    lines.extend(f"  {style(cmd, 'cmd', color=color)}" for cmd in unique)
    return lines
