"""Package-local Markdown references and native instruction layouts."""
from pathlib import PurePosixPath
import posixpath
import re
from urllib.parse import quote, unquote, urlsplit

from .schema import require

LINK = re.compile(r'!?\[[^\]\n]*\]\(\s*(<[^>\n]+>|[^\s)]+)(?:\s+["\'][^\n]*?["\'])?\s*\)')
IMPORT = re.compile(r'(?<![\w\\])@((?:\\ |[^\s`"\'<>])+)')


def prose(text):
    """Mask code without moving offsets used to resolve links in the source."""
    lines, fence = [], None
    for line in text.splitlines(keepends=True):
        match = re.match(r'^\s{0,3}(`{3,}|~{3,})', line)
        if fence:
            if match and match[1][0] == fence[0] and len(match[1]) >= len(fence):
                fence = None
            lines.append(re.sub(r'[^\n]', ' ', line))
        elif match:
            fence = match[1]
            lines.append(re.sub(r'[^\n]', ' ', line))
        else:
            lines.append(line)
    return re.sub(r'(?<!`)(`+)(?!`)(.*?)(?<!`)\1(?!`)',
                  lambda m: re.sub(r'[^\n]', ' ', m[0]), ''.join(lines), flags=re.S)


def local_target(source, target, files, sibling_files=frozenset()):
    parsed = urlsplit(target)
    if parsed.scheme in ('http', 'https', 'mailto') or target.startswith('#'):
        return None
    require(not parsed.scheme and not parsed.netloc and not parsed.query,
            'Resource reference must be a package-local path or a public link: ' + source)
    path = unquote(parsed.path)
    require(path and not path.startswith(('/', '~')) and '\\' not in path and not any(c in path for c in '\0\r\n'),
            'Absolute or machine-local resource reference requires an explicit binding: ' + source)
    resolved = posixpath.normpath(str(PurePosixPath(source).parent / path))
    if resolved != '..' and (resolved in sibling_files or any(name.startswith(resolved + '/') for name in sibling_files)):
        return resolved
    require(resolved != '..' and not resolved.startswith('../'), 'Resource reference escapes its package: ' + source)
    require(resolved in files or any(name.startswith(resolved + '/') for name in files),
            'Resource reference target is not a tracked package file or directory: ' + source + ' -> ' + resolved)
    return resolved


def markdown_links(source, content, files, sibling_files=frozenset()):
    text = content.decode()
    result = []
    for match in LINK.finditer(prose(text)):
        raw = match[1]
        value = raw[1:-1] if raw.startswith('<') else raw
        target = local_target(source, value, files, sibling_files)
        if target:
            result.append((match.start(1), match.end(1), target, urlsplit(value).fragment))
    return result


def package_references(files, entry, *, native_imports=False, sibling_files=frozenset()):
    links = {name: markdown_links(name, content, files, sibling_files) for name, content in files.items() if name.lower().endswith('.md')}
    imports = {}
    if native_imports:
        def visit(name, ancestors):
            require(name not in ancestors, 'Instruction import cycle: ' + name)
            # The managed CLAUDE.md wrapper consumes one of the four native hops.
            require(len(ancestors) <= 3, 'Instruction import exceeds native depth after deployment: ' + name)
            try:
                content = files[name].decode()
            except UnicodeDecodeError as exc:
                raise ValueError('Instruction import must be UTF-8 text: ' + name) from exc
            targets = []
            for match in IMPORT.finditer(prose(content)):
                raw = match[1].replace('\\ ', ' ')
                target = local_target(name, raw, files)
                require(target is not None and not urlsplit(raw).fragment, 'Native instruction import must reference a package file')
                require(target in files, 'Native instruction import must reference a file, not a directory')
                targets.append(target)
            imports[name] = targets
            for target in targets:
                visit(target, (*ancestors, name))
        visit(entry, ())
    return {'links': links, 'imports': imports,
            'count': sum(len(rows) for rows in links.values()) + sum(len(rows) for rows in imports.values())}


def instruction_body(item, agent):
    """Keep original package bytes; bind the native root to its stable layout."""
    entry = item['package'].entry
    text = item['files'][entry].decode().rstrip()
    refs = item['references']
    if agent == 'claude-code' and (refs['imports'].get(entry) or refs['links'].get(entry)):
        path = str(item['cache'] / entry)
        require(not any(c in path for c in '\0\r\n\\`"'), 'Instruction path cannot be represented as a native import')
        return '@' + path.replace(' ', '\\ ')
    if agent == 'codex':
        require(not IMPORT.search(prose(text)), 'Codex has no native Claude-style instruction import adapter')
        for start, end, target, fragment in reversed(refs['links'].get(entry, [])):
            path = str(item['cache'] / target)
            require(not any(c in path for c in '\0\r\n<>'), 'Instruction link cannot represent the deployed path')
            text = text[:start] + '<' + quote(path, safe='/') + ('#' + fragment if fragment else '') + '>' + text[end:]
    return text
