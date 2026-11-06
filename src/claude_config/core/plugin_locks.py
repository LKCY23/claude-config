"""Portable, per-Agent-Profile plugin pins; ordinary deployments use them too."""
from copy import deepcopy
from pathlib import Path, PurePosixPath
import re

from .native import parse_json, parse_yaml
from .schema import COMMIT, REF, closed, require, within


def lock_path(manager, name):
    relative = str(Path(name).with_suffix('.plugins.lock.yaml'))
    require(not (manager.root / relative).is_symlink(), 'Plugin lock must not be a symlink')
    return within(manager.root, relative, must_exist=False)


def empty_locks(agent, name):
    return {'schema_version': 1, 'agent_type': agent, 'profile': name, 'plugins': {}}


def validate_locks(value, agent, name):
    closed(value, ('schema_version', 'agent_type', 'profile', 'plugins'),
           ('schema_version', 'agent_type', 'profile', 'plugins'), 'plugin lock')
    require(type(value['schema_version']) is int and value['schema_version'] == 1 and value['agent_type'] == agent and value['profile'] == name,
            'Plugin lock belongs to another Profile or schema version')
    require(isinstance(value['plugins'], dict), 'Plugin lock entries must be an object')
    for ref, pin in value['plugins'].items():
        require(isinstance(ref, str) and REF.fullmatch(ref), 'Invalid locked resource reference')
        closed(pin, ('catalog', 'source', 'version', 'source_fingerprint', 'resolution'),
               ('catalog', 'source', 'version', 'source_fingerprint', 'resolution'), 'plugin pin')
        for key in ('catalog', 'source'):
            loc = pin[key]
            closed(loc, ('repo', 'commit', 'path'), ('repo', 'commit', 'path'), 'plugin ' + key)
            require(isinstance(loc['repo'], str) and re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', loc['repo'])
                    and all(x not in ('.', '..') for x in loc['repo'].split('/')), 'Invalid plugin repository')
            require(isinstance(loc['commit'], str) and COMMIT.fullmatch(loc['commit']), 'Plugin pin requires full commit')
            require(isinstance(loc['path'], str) and loc['path'] and not PurePosixPath(loc['path']).is_absolute()
                    and '..' not in PurePosixPath(loc['path']).parts and '\\' not in loc['path'], 'Invalid plugin source path')
        require(isinstance(pin['version'], str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._+-]*', pin['version']), 'Invalid plugin version')
        require(isinstance(pin['source_fingerprint'], str) and re.fullmatch(r'[a-f0-9]{64}', pin['source_fingerprint']), 'Invalid plugin content fingerprint')
        closed(pin['resolution'], ('kind', 'requested'), ('kind', 'requested'), 'plugin resolution')
        require(pin['resolution']['kind'] in ('catalog', 'commit', 'release')
                and isinstance(pin['resolution']['requested'], str), 'Invalid plugin resolution provenance')
        expected = {'commit': pin['source']['commit'], 'release': pin['version'], 'catalog': pin['catalog']['commit']}
        require(pin['resolution']['requested'] == expected[pin['resolution']['kind']], 'Plugin resolution provenance disagrees with its pin')
    return value


def read_locks(manager, name):
    path = lock_path(manager, name)
    value, raw, expected = manager._read(path, lambda raw: parse_yaml(raw) if raw else {})
    if not expected['exists']:
        value = empty_locks(manager.agent, name)
    validate_locks(value, manager.agent, name)
    return path, value, raw, expected


def locator(item, declared):
    source = item['source']
    if source['kind'] == 'external':
        return {key: source[key] for key in ('repo', 'commit', 'path')}
    if source['kind'] == 'submodule':
        return {'repo': source['repo'], 'commit': source['commit'], 'path': '.'}
    return {'repo': declared['repo'], 'commit': declared['commit'],
            'path': str(PurePosixPath(declared['path']) / source['path'])}


def apply_pin(item, declared, pin):
    """Only choose a revision in the registered repository/path, not a new identity."""
    source = locator(item, declared)
    require(pin['catalog']['repo'] == declared['repo'] and pin['catalog']['path'] == declared['path'],
            'Plugin lock catalog identity changed; explicit source migration is required')
    require(all(pin['source'][k] == source[k] for k in ('repo', 'path')),
            'Plugin lock cannot change the registered repository or package path')
    selected = deepcopy(item)
    selected['source'] = {'kind': 'external', **pin['source']}
    return selected


def validate_package_pin(package, pin):
    manifest = parse_json(package.files[package.entry])
    require(manifest.get('version') == pin['version'], 'Plugin manifest version differs from its lock')
    require(package.source_fingerprint == pin['source_fingerprint'], 'Plugin content differs from its lock')
