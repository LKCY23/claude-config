"""One-shot replacement inputs pin a reconstructable prior native package."""
from pathlib import Path

from .native import parse_json, snapshot_read
from .native_plugins import VERSION, image_fingerprint
from .schema import NAME, closed, require, version
from .transaction import directory_fingerprint


def replacement_source(manager, declaration):
    path = Path(declaration)
    require(path.is_absolute(), 'Plugin replacement declaration must be an absolute local file')
    value, _, _ = manager._read(path, parse_json)
    keys = ('schema_version', 'selector', 'version', 'image', 'source')
    closed(value, keys, keys, 'plugin replacement declaration')
    version(value)
    selector = value['selector']
    require(isinstance(selector, str) and len(selector.split('@')) == 2 and all(NAME.fullmatch(part) for part in selector.split('@')),
            'Replacement requires an exact native selector')
    require(isinstance(value['version'], str) and VERSION.fullmatch(value['version']), 'Replacement requires the exact prior native version')
    require(isinstance(value['source'], str) and Path(value['source']).is_absolute(), 'Replacement recovery source must be an absolute local directory')
    root = Path(value['source'])
    require(root.is_dir() and not root.is_symlink(), 'Replacement recovery source must be a regular directory')
    native_roots = [Path(entry['binding']['config_root']) / 'plugins' for entry in manager.machine['agents'].values()]
    require(not any(root.resolve().is_relative_to(path.resolve()) for path in native_roots),
            'A native plugin cache cannot be used as a replacement installation source')
    require(not (root / '.git').is_dir(), 'Recovery source cannot contain a Git database')
    image = image_fingerprint(root)
    require(value['image'] == image, 'Replacement recovery source differs from its fixed image pin')
    files, modes = {}, {}
    for name, snapshot in directory_fingerprint(root)['entries'].items():
        parts = Path(name).parts
        require(not set(parts).intersection(('.orphaned_at', '.in_use')) and ('.git' not in parts or name == '.git'),
                'Recovery source cannot contain runtime metadata or a Git database')
        data, expected = snapshot_read(root / name)
        require(expected == snapshot, 'Replacement source changed during planning')
        manager.sources[root / name] = expected
        files[name], modes[name] = data, expected['mode']
    entries = ('.claude-plugin/plugin.json',) if manager.agent == 'claude-code' else ('.codex-plugin/plugin.json', '.claude-plugin/plugin.json')
    entry = next((name for name in entries if name in files), None)
    require(entry is not None, 'Recovery source needs a native manifest readable by the selected installer')
    manifest = parse_json(files[entry])
    require(manifest.get('name') == selector.split('@')[0] and manifest.get('version') == value['version'],
            'Recovery manifest differs from the declared native identity/version')
    return {**value, 'root': root, 'files': files, 'modes': modes}
