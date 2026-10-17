"""Read-only expandable terminal report; curses works inside tmux on POSIX."""
from dataclasses import dataclass, field
import os
import unicodedata

from .reporting import clean_text, overview_lines, outcome_lines, report_sections, check_lines, STATUS_LABELS


@dataclass
class Node:
    title: str
    lines: list = field(default_factory=list)
    children: list = field(default_factory=list)
    expanded: bool = False
    tone: str = 'heading'


def terminal_interactive(mode='auto', *, stdin=None, stdout=None):
    import sys
    stdin, stdout = stdin or sys.stdin, stdout or sys.stdout
    return (mode == 'auto' and os.name == 'posix'
            and os.environ.get('TERM', '').lower() not in ('', 'dumb', 'unknown')
            and stdin.isatty() and stdout.isatty())


def wrapped_lines(text, width):
    """Wrap by terminal cells, including Chinese and combining characters."""
    result = []
    for paragraph in clean_text(text).expandtabs(4).split('\n'):
        line, size = '', 0
        for char in paragraph:
            cells = 0 if unicodedata.combining(char) else 2 if unicodedata.east_asian_width(char) in ('W', 'F') else 1
            if size + cells > width and line:
                result.append(line)
                line, size = '', 0
            line += char
            size += cells
        result.append(line)
    return result


def nodes_for(report):
    overview = overview_lines(report)[2:]
    compact = []
    for line in overview:
        if not line.startswith(('对象：', '机器：', '默认模型：', '推理强度：')):
            continue
        if line.startswith('机器：'):
            line = line.split('；Profile：', 1)[0]
        label, _, value = line.partition('：')
        before, separator, after = value.partition(' → ')
        compact.append(label + '：' + before if separator and before == after else line)
    nodes = [Node('检查对象', compact,
                  [Node('配置位置与完整范围', overview, [Node('选定范围明细', report['scope']['selected'])])], expanded=True)]
    sections = report_sections(report)
    conflict_tones = {}
    for row in report['conflicts']:
        tone = 'error'
        if row.get('status') == 'resolved_adopt_source':
            tone = 'success'
        elif row.get('status', '').startswith('resolved_'):
            tone = 'warning'
        conflict_tones[row['id']] = tone
    for title, body, expanded in sections:
        if expanded:
            nodes.append(Node(title, body, expanded=True, tone=conflict_tones.get(title.split()[0], 'error')))
    attention = [row for row in report['checks'] if row['status'] in ('warning', 'not_verified', 'skipped')]
    if attention:
        unverified = check_lines([row for row in attention if row['status'] != 'warning'])
        lines = check_lines([row for row in attention if row['status'] == 'warning'])
        lines += [line.split('；', 1)[0] for line in unverified if not line.startswith('  涉及：')]
        nodes.append(Node(f'注意事项 · {len(attention)} 项', lines,
                          [Node('涉及对象与原因', check_lines(attention))], expanded=True, tone='warning'))
    for title, body, expanded in sections:
        if expanded:
            continue
        # Long file changes stay accessible without obscuring the action choices.
        details = [line for line in body if line.startswith('文件与变更范围：')]
        lines = [line for line in body if line not in details]
        nodes.append(Node(title, lines, [Node('文件与变更范围', details)] if details else []))
    if report['execution']['started'] and report['execution']['results']:
        rows = report['execution']['results']
        nodes.append(Node(f'逐文件执行与恢复明细（{len(rows)} 项）',
                          [STATUS_LABELS.get(row['status'], row['status']) + '：' + row.get('path', row.get('operation_id', '原生操作'))
                           for row in rows]))
    nodes.append(Node('执行结果与报告位置', outcome_lines(report)))
    return nodes


class ReportTree:
    """Pure view state, independently testable without terminal input."""
    def __init__(self, report):
        self.nodes = nodes_for(report)
        self.cursor = 0
        self.offset = 0

    def rows(self, width):
        rows = []
        def add(nodes, depth=0):
            for node in nodes:
                prefix = '  ' * depth + ('▼ ' if node.expanded else '▶ ')
                rows.extend((node, line) for line in wrapped_lines(prefix + node.title, width))
                if node.expanded:
                    for text in node.lines:
                        rows.extend((node, line) for line in wrapped_lines('  ' * (depth + 1) + text, width))
                    add(node.children, depth + 1)
        add(self.nodes)
        return rows

    def command(self, key, width, height):
        rows = self.rows(width)
        self.cursor = min(self.cursor, max(0, len(rows) - 1))
        if key in ('q', 'Q', '\x1b'):
            return False
        if key in ('down', 'j'):
            self.cursor = min(len(rows) - 1, self.cursor + 1)
        elif key in ('up', 'k'):
            self.cursor = max(0, self.cursor - 1)
        elif key == 'page_down':
            self.cursor = min(len(rows) - 1, self.cursor + max(1, height - 1))
        elif key == 'page_up':
            self.cursor = max(0, self.cursor - max(1, height - 1))
        elif key in (' ', '\n', '\r', 'left', 'right') and rows:
            node = rows[self.cursor][0]
            node.expanded = key == 'right' if key in ('left', 'right') else not node.expanded
            self.cursor = next(index for index, row in enumerate(self.rows(width)) if row[0] is node)
        return True

    def visible(self, width, height):
        rows = self.rows(width)
        self.cursor = min(self.cursor, max(0, len(rows) - 1))
        self.offset = min(self.offset, max(0, len(rows) - height))
        if self.cursor < self.offset:
            self.offset = self.cursor
        elif self.cursor >= self.offset + height:
            self.offset = self.cursor - height + 1
        return rows[self.offset:self.offset + height]


def terminal_styles(curses):
    """Use the terminal's background; colorless terminals retain visible hierarchy."""
    mono = {'title': curses.A_BOLD, 'heading': curses.A_BOLD, 'success': curses.A_BOLD,
            'warning': curses.A_BOLD, 'error': curses.A_BOLD, 'body': 0,
            'selected': curses.A_REVERSE | curses.A_BOLD}
    styles = dict(mono)
    try:
        if not curses.has_colors():
            return mono
        curses.start_color()
        curses.use_default_colors()
        if 'NO_COLOR' in os.environ:
            return mono
        for number, (tone, color) in enumerate((('title', curses.COLOR_CYAN), ('heading', curses.COLOR_CYAN),
                                               ('success', curses.COLOR_GREEN), ('warning', curses.COLOR_YELLOW),
                                               ('error', curses.COLOR_RED)), 1):
            curses.init_pair(number, color, -1)
            styles[tone] |= curses.color_pair(number)
    except (curses.error, AttributeError):
        return mono
    return styles


def header_rows(report, width, height):
    """Compact status labels for narrow panes, full labels when space permits."""
    counts = report['summary']
    checks = counts['checks']
    tone = 'error' if report['status'] in ('error', 'blocked') else 'heading'
    title = 'claude-config ' + report['operation'] + ' · 只读查看'
    if len(wrapped_lines(title, width)) > 1:
        title = 'claude-config 只读查看'
    rows = [(line, 'title') for line in wrapped_lines(title, width)]
    rows += [(line, tone) for line in wrapped_lines(outcome_lines(report)[0], width)]
    if height < 14:
        return rows[:2]
    gap, separator = ('', ' ') if width < 32 else (' ', '  ·  ')
    for text, role in (
        (f"通过{gap}{checks['passed']}{separator}不通过{gap}{checks['blocked'] + checks['failed']}",
         'error' if checks['blocked'] + checks['failed'] else 'success'),
        (f"警告{gap}{checks['warning']}{separator}未验证/未检查{gap}{checks['not_verified'] + checks['skipped']}",
         'warning' if checks['warning'] + checks['not_verified'] + checks['skipped'] else 'body'),
        (f"差异{gap}{counts['changes']}{separator}冲突{gap}{counts['conflicts']}{separator}阻断{gap}{counts['blockers']}",
         'error' if counts['blockers'] or counts['unresolved_conflicts'] else 'heading')):
        rows.extend((line, role) for line in wrapped_lines(text, width))
    return rows


def draw_report(screen, report, tree, styles):
    """Render one frame and return the exact viewport used by keyboard navigation."""
    height, width = screen.getmaxyx()
    screen.erase()
    def draw(y, text, style=0):
        if 0 <= y < height and width > 1:
            screen.addstr(y, 0, wrapped_lines(text, width - 1)[0], style)
    if height < 8 or width < 24:
        draw(0, '窗口太小，请放大', styles['warning'])
        draw(1, 'q 退出 · 放大后恢复', styles['body'])
        screen.refresh()
        return max(1, width - 3), 1
    header = header_rows(report, width - 1, height)
    # Reserve a separator and two footer rows; details remain scrollable.
    header = header[:max(2, height - 7)]
    for y, (text, tone) in enumerate(header):
        draw(y, text, styles[tone])
    draw(len(header), '─' * (width - 1), styles['heading'])
    top = len(header) + 1
    content_height = max(1, height - top - 3)
    content_width = width - 3  # Selection marker plus the unused last terminal cell.
    visible = tree.visible(content_width, content_height)
    for index, (node, text) in enumerate(visible):
        selected = tree.offset + index == tree.cursor
        style = styles['body']
        if selected:
            style = styles['selected']
        elif text.lstrip().startswith(('▶ ', '▼ ')):
            style = styles[node.tone]
        if selected:
            draw(top + index, ' ' * (width - 1), style)
        draw(top + index, ('> ' if selected else '  ') + text, style)
    end = top + len(visible)
    total = len(tree.rows(content_width))
    position = f'{tree.offset + 1}–{tree.offset + len(visible)} / {total}' if visible else '0 / 0'
    draw(end, '─ ' + position + ' ' + '─' * width, styles['heading'])
    if width >= 52:
        help_text = '↑↓ 选择 · 空格/Enter 展开 · PgUp/PgDn 翻页'
    elif width >= 38:
        help_text = '↑↓选择 空格/Enter展开 PgUp/PgDn'
    else:
        help_text = '↑↓选择 空格展开 q退出'
    draw(end + 1, help_text)
    draw(end + 2, 'q 退出 · 只读，不会修改配置' if width >= 38 else 'PgUp/PgDn 翻页')
    screen.refresh()
    return content_width, content_height


def show_report(report):
    """Return False on terminal setup/render failure so caller prints static text."""
    try:
        import curses
    except ImportError:
        return False
    tree = ReportTree(report)
    def run(screen):
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        screen.keypad(True)
        styles = terminal_styles(curses)
        key_names = {curses.KEY_DOWN: 'down', curses.KEY_UP: 'up',
                     curses.KEY_LEFT: 'left', curses.KEY_RIGHT: 'right',
                     curses.KEY_NPAGE: 'page_down', curses.KEY_PPAGE: 'page_up',
                     curses.KEY_ENTER: '\n'}
        while True:
            content_width, content_height = draw_report(screen, report, tree, styles)
            key = screen.get_wch()
            if not tree.command(key_names.get(key, key), content_width, content_height):
                break
    try:
        curses.wrapper(run)
        return True
    except (curses.error, OSError, ImportError):
        return False
    except KeyboardInterrupt:
        # The operation is already complete and persisted; leaving the reader
        # must not turn a successful deployment into an interrupted deployment.
        return True
