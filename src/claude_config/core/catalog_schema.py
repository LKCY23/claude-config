"""Strict new resource registry. Historical catalogs are converter inputs only."""
import json
from pathlib import PurePosixPath
import re

NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]*\Z')
REPO = re.compile(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z')
SHA = re.compile(r'[0-9a-f]{40}\Z')
REF = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]*@[A-Za-z0-9][A-Za-z0-9_.-]*\Z')


def require(test, message):
    if not test:
        raise ValueError(message)


def obj(value, allowed, required, label):
    require(isinstance(value, dict), label + ' must be an object')
    require(not set(value) - set(allowed), label + ': unknown fields')
    require(not set(required) - set(value), label + ': missing fields')


def text(value, label):
    require(isinstance(value, str) and bool(value), label + ' must be a nonempty string')


def path(value):
    text(value, 'resource path')
    require(not PurePosixPath(value).is_absolute() and '..' not in PurePosixPath(value).parts and '\\' not in value,
            'Resource path escapes its root')


def origin_pin(value):
    obj(value, ('repo', 'commit', 'path'), ('repo', 'commit', 'path'), 'upstream')
    require(isinstance(value['repo'], str) and REPO.fullmatch(value['repo']), 'Invalid upstream repository')
    require(isinstance(value['commit'], str) and SHA.fullmatch(value['commit']), 'Upstream requires a full commit SHA')
    path(value['path'])


def validate_catalog(value):
    obj(value, ('schema_version', 'name', 'resources'), ('schema_version', 'name', 'resources'), 'catalog')
    require(type(value['schema_version']) is int and value['schema_version'] == 1,
            'schema_version must be 1; use the separate converter for historical catalogs')
    require(isinstance(value['name'], str) and NAME.fullmatch(value['name']), 'Invalid catalog name')
    require(isinstance(value['resources'], dict), 'resources must be keyed by identifier')
    for name, item in value['resources'].items():
        require(isinstance(name, str) and NAME.fullmatch(name), 'Invalid resource identifier')
        required = ('kind', 'description', 'origin', 'maintenance', 'source', 'compatibility', 'requires', 'update')
        obj(item, (*required, 'packaging_revision'), required, 'resource ' + name)
        require(item['kind'] in ('plugin', 'skill', 'mcp', 'hook', 'instruction', 'rule', 'agent'), 'Invalid resource kind')
        text(item['description'], 'description')
        origin = item['origin']
        obj(origin, ('type', 'author', 'upstream', 'baseline'), ('type', 'author'), 'origin')
        require(origin['type'] in ('self', 'third-party'), 'Invalid origin type')
        text(origin['author'], 'origin author')
        if 'upstream' in origin:
            origin_pin(origin['upstream'])
        if 'baseline' in origin:
            text(origin['baseline'], 'origin baseline')
        require(origin['type'] != 'third-party' or 'upstream' in origin or 'baseline' in origin,
                'Third-party origin must retain upstream or publishing baseline')
        maintenance = item['maintenance']
        obj(maintenance, ('owner', 'mode'), ('owner', 'mode'), 'maintenance')
        require(maintenance['owner'] in ('ours', 'upstream') and maintenance['mode'] in ('original', 'adapter', 'fork'),
                'Invalid maintenance classification')
        source = item['source']
        obj(source, ('kind', 'path', 'repo', 'commit', 'imports', 'dependencies', 'baseline', 'sha256'), ('kind', 'path'), 'source')
        require(source['kind'] in ('tracked', 'submodule', 'external', 'snapshot'), 'Invalid source kind')
        path(source['path'])
        if source['kind'] in ('submodule', 'external'):
            origin_pin({key: source.get(key) for key in ('repo', 'commit', 'path')})
        elif 'repo' in source or 'commit' in source:
            raise ValueError('Tracked/snapshot sources use the catalog baseline, not duplicate Git pins')
        if source['kind'] == 'snapshot':
            text(source.get('baseline'), 'snapshot baseline')
            require(isinstance(source.get('sha256'), str) and re.fullmatch(r'[0-9a-f]{64}', source['sha256']), 'Snapshot requires a SHA256')
        elif 'baseline' in source or 'sha256' in source:
            raise ValueError('Snapshot metadata on a non-snapshot source')
        imports = source.get('imports', {})
        require(isinstance(imports, dict), 'imports must map upstream paths to package paths')
        for upstream_path, package_path in imports.items():
            path(upstream_path)
            path(package_path)
        dependencies = source.get('dependencies', [])
        require(isinstance(dependencies, list), 'dependencies must be upstream paths')
        for dependency in dependencies:
            path(dependency)
        if 'packaging_revision' in item:
            require(type(item['packaging_revision']) is int and item['packaging_revision'] >= 1, 'Invalid packaging revision')
        compatibility = item['compatibility']
        require(isinstance(compatibility, dict) and not set(compatibility) - {'claude-code', 'codex'}, 'Unknown compatible agent')
        for host, entry in compatibility.items():
            obj(entry, ('mode', 'entry', 'entries', 'validation'), ('mode', 'entry', 'validation'), 'compatibility.' + host)
            text(entry['mode'], 'compatibility mode')
            path(entry['entry'])
            entries = entry.get('entries', {})
            require(isinstance(entries, dict), 'MCP entries must be a name-to-path object')
            require(not entries or item['kind'] == 'mcp', 'Named runtime entries are supported only for MCP resources')
            for label, location in entries.items():
                require(isinstance(label, str) and NAME.fullmatch(label), 'Invalid MCP entry name')
                path(location)
            validation = entry['validation']
            obj(validation, ('status', 'level', 'evidence'), ('status',), 'validation')
            require(validation['status'] in ('not-evaluated', 'passed', 'failed'), 'Invalid validation status')
            if validation['status'] in ('passed', 'failed'):
                require(validation.get('level') in ('structural', 'native', 'runtime'), 'Validation must record its level')
                path(validation.get('evidence'))
        requires = item['requires']
        obj(requires, ('resources', 'bindings'), (), 'requires')
        refs = requires.get('resources', [])
        require(isinstance(refs, list) and all(isinstance(ref, str) and REF.fullmatch(ref) for ref in refs), 'Invalid dependency references')
        require(len(set(refs)) == len(refs), 'Duplicate dependency reference')
        bindings = requires.get('bindings', {})
        require(isinstance(bindings, dict), 'Binding requirements must be an object')
        for slot, entry in bindings.items():
            require(isinstance(slot, str) and NAME.fullmatch(slot), 'Invalid binding slot')
            obj(entry, ('kind', 'required'), ('kind', 'required'), 'required binding')
            require(entry['kind'] in ('executable', 'path', 'endpoint', 'secret') and type(entry['required']) is bool,
                    'Invalid binding requirement')
        update = item['update']
        obj(update, ('strategy', 'target'), ('strategy',), 'update')
        require(update['strategy'] in ('branch', 'release', 'manual'), 'Unknown update strategy')
        if update['strategy'] != 'manual':
            text(update.get('target'), 'update target')
            require('upstream' in origin or source['kind'] in ('external', 'submodule'), 'Trackable updates require a pinned Git source')
        else:
            require('target' not in update, 'Manual update has no remote tracking target')
    return value


def read_catalog(file):
    def pairs(rows):
        result = {}
        for key, value in rows:
            require(key not in result, 'Duplicate JSON key: ' + key)
            result[key] = value
        return result
    with open(file) as stream:
        return validate_catalog(json.load(stream, object_pairs_hook=pairs))
