"""Closed management schemas; native setting capability checks live in adapters."""
import json
import math
from pathlib import Path
import re
from urllib.parse import urlsplit

import yaml

AGENTS = ('claude-code', 'codex')
APPS = ('claude-desktop', 'codex-app')
RESOURCE_KINDS = dict(plugins='plugin', skills='skill', mcp_servers='mcp',
                      hooks='hook', instructions='instruction', rules='rule', agents='agent')
REF = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]*@[A-Za-z0-9][A-Za-z0-9_.-]*\Z')
NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]*\Z')
COMMIT = re.compile(r'[0-9a-f]{40}\Z')
POLICIES = ('source_wins', 'keep_target', 'fail_if_different')


class SchemaError(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise SchemaError(message)


def mapping(value, label):
    require(isinstance(value, dict), label + ' must be an object')
    require(all(isinstance(key, str) for key in value), label + ' keys must be strings')
    return value


def closed(value, allowed, required, label):
    mapping(value, label)
    require(not (set(value) - set(allowed)), label + ': unknown fields ' + ', '.join(sorted(set(value) - set(allowed))))
    require(not (set(required) - set(value)), label + ': missing fields ' + ', '.join(sorted(set(required) - set(value))))


def string(value, label, empty=False):
    require(isinstance(value, str) and (empty or bool(value)), label + ' must be a string')


def json_value(value):
    if isinstance(value, dict):
        mapping(value, 'object')
        for child in value.values():
            json_value(child)
    elif isinstance(value, list):
        for child in value:
            json_value(child)
    else:
        require(value is None or type(value) in (str, bool, int, float), 'Only JSON-compatible values are allowed')
        require(not isinstance(value, float) or math.isfinite(value), 'Non-finite numbers are not allowed')


class StrictLoader(yaml.SafeLoader):
    # No YAML 1.1 yes/on booleans or implicit dates. Only true/false are booleans.
    yaml_implicit_resolvers = {
        key: [(tag, pattern) for tag, pattern in rows
              if tag not in ('tag:yaml.org,2002:bool', 'tag:yaml.org,2002:timestamp')]
        for key, rows in yaml.SafeLoader.yaml_implicit_resolvers.items()
    }

    def compose_node(self, parent, index):
        require(not self.check_event(yaml.AliasEvent), 'YAML aliases are not allowed')
        return super().compose_node(parent, index)

    def construct_mapping(self, node, deep=False):
        result = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            require(isinstance(key, str), 'YAML keys must be strings')
            require(key not in result, 'Duplicate YAML key: ' + key)
            result[key] = self.construct_object(value_node, deep=deep)
        return result


StrictLoader.add_implicit_resolver('tag:yaml.org,2002:bool', re.compile(r'^(?:true|false)$'), ['t', 'f'])


def load_yaml(path):
    value = yaml.load(Path(path).read_text(), Loader=StrictLoader)
    json_value(value)
    return mapping(value, str(path))


def load_json(path):
    def pairs(rows):
        result = {}
        for key, value in rows:
            require(key not in result, 'Duplicate JSON key: ' + key)
            result[key] = value
        return result
    value = json.loads(Path(path).read_text(), object_pairs_hook=pairs)
    json_value(value)
    return mapping(value, str(path))


def version(value):
    require(type(value.get('schema_version')) is int and value['schema_version'] == 1,
            'schema_version must be 1; legacy declarations require the separate converter')


def pointer(value):
    require(isinstance(value, str) and value.startswith('/'), 'Field paths must be JSON Pointers')
    require(not re.search(r'~(?![01])', value), 'Invalid JSON Pointer escape')
    return tuple(part.replace('~1', '/').replace('~0', '~') for part in value[1:].split('/'))


def as_pointer(path):
    return '/' + '/'.join(str(part).replace('~', '~0').replace('/', '~1') for part in path)


def relative(value, label):
    string(value, label)
    path = Path(value)
    require(not path.is_absolute() and '..' not in path.parts and '\\' not in value, label + ' must stay inside its source root')
    return path


def within(root, value, must_exist=True):
    relative(value, 'source path')
    root = Path(root).resolve()
    path = (root / value).resolve()
    require(path.is_relative_to(root), 'Source path or symlink escapes its root')
    require(not must_exist or path.exists(), 'Missing source: ' + str(path))
    return path


def policy(value):
    closed(value, ('default', 'fields'), (), 'merge_policy')
    require(value.get('default', 'source_wins') in POLICIES, 'Invalid default merge policy')
    for path, action in mapping(value.get('fields', {}), 'merge_policy.fields').items():
        pointer(path)
        require(action in (*POLICIES, 'replace', 'append_unique'), 'Invalid field merge policy')


def binding_ref(value, group=None):
    string(value, 'binding reference')
    category, separator, name = value.partition('.')
    require(separator and category in ('executables', 'paths', 'endpoints', 'secrets') and NAME.fullmatch(name), 'Invalid binding reference')
    require(group is None or category == group, 'Binding reference has the wrong kind')
    return category, name


def validate_profile(value, kind='agent', expected=None):
    require(kind in ('agent', 'app'), 'Unknown Profile kind')
    json_value(value)
    field, choices = ('agent_type', AGENTS) if kind == 'agent' else ('app', APPS)
    allowed = ['schema_version', field, 'description', 'settings', 'bindings', 'merge_policy']
    if kind == 'agent':
        allowed.append('resources')
    closed(value, allowed, ('schema_version', field), kind + ' Profile')
    version(value)
    require(value[field] in choices, 'Unknown ' + field)
    require(expected is None or value[field] == expected, 'Profile type disagrees with its binding or directory')
    if 'description' in value:
        string(value['description'], 'description', empty=True)
    settings = mapping(value.get('settings', {}), 'settings')
    json_value(settings)
    resources = mapping(value.get('resources', {}), 'resources')
    require(not (set(resources) - set(RESOURCE_KINDS)), 'Unknown resource group')
    selected = set()
    for group, entries in resources.items():
        mapping(entries, 'resources.' + group)
        require(group != 'instructions' or len(entries) <= 1, 'Only one global instruction entry is allowed')
        for ref, entry in entries.items():
            require(REF.fullmatch(ref), 'Invalid resource reference: ' + ref)
            require(ref not in selected, 'A resource cannot belong to multiple groups: ' + ref)
            selected.add(ref)
            closed(entry, ('options', 'bindings'), (), 'resource ' + ref)
            mapping(entry.get('options', {}), 'resource options')
            for slot, reference in mapping(entry.get('bindings', {}), 'resource bindings').items():
                require(NAME.fullmatch(slot), 'Invalid binding slot')
                binding_ref(reference)
    targets = []
    for destination, entry in mapping(value.get('bindings', {}), 'bindings').items():
        path = pointer(destination)
        require(len(path) > 1 and path[0] == 'settings', 'Command bindings must target a settings field')
        require(not any(path[:len(old)] == old or old[:len(path)] == path for old in targets), 'Overlapping binding targets')
        targets.append(path)
        closed(entry, ('kind', 'executable', 'args'), ('kind', 'executable', 'args'), 'command binding')
        require(entry['kind'] == 'command', 'Unknown command binding kind')
        binding_ref(entry['executable'], 'executables')
        require(isinstance(entry['args'], list), 'Command args must be an array')
        for arg in entry['args']:
            if isinstance(arg, str):
                continue
            closed(arg, ('resource', 'path'), ('resource', 'path'), 'resource argument')
            require(arg['resource'] in selected, 'Command argument references an unselected resource')
            relative(arg['path'], 'resource argument path')
        current = settings
        for part in path[1:]:
            if not isinstance(current, dict) or part not in current:
                break
            current = current[part]
        else:
            raise SchemaError('Settings and binding both own ' + destination)
    if 'merge_policy' in value:
        policy(value['merge_policy'])
    if kind == 'app':
        # Apply the same App-only boundary in every schema consumer, including
        # converters and catalog impact checks, not just the runtime manager.
        from .app_fields import validate_policies, validate_settings
        validate_settings(value['app'], settings)
        validate_policies(value['app'], value.get('merge_policy'))
    return value


def validate_catalogs(value):
    closed(value, ('schema_version', 'catalogs'), ('schema_version', 'catalogs'), 'catalogs')
    version(value)
    for name, entry in mapping(value['catalogs'], 'catalogs').items():
        require(NAME.fullmatch(name), 'Invalid catalog alias')
        closed(entry, ('repo', 'commit', 'path'), ('repo', 'commit', 'path'), 'catalog source')
        require(isinstance(entry['repo'], str) and re.fullmatch(r'[\w.-]+/[\w.-]+', entry['repo']), 'Invalid repository')
        require(isinstance(entry['commit'], str) and COMMIT.fullmatch(entry['commit']), 'Catalog requires a full lowercase commit SHA')
        relative(entry['path'], 'catalog path')
    return value


def absolute(value, label):
    string(value, label)
    require(Path(value).is_absolute(), label + ' must be an absolute path')


def state_directory(machine, *, config_dir=None):
    runtime = machine.get('runtime', {})
    if 'state_dir' in runtime:
        return Path(runtime['state_dir'])
    root = machine['config_dir'] if config_dir is None else config_dir
    return Path(root) / '.state'


def validate_machine(value):
    required = ('schema_version', 'machine_id', 'config_dir', 'catalog_roots', 'agents', 'apps', 'bindings')
    closed(value, (*required, 'runtime', 'merge_policy', 'profile_selection'), required, 'Machine')
    version(value)
    string(value['machine_id'], 'machine_id')
    external = 'profile_selection' in value
    if external:
        require(NAME.fullmatch(value['machine_id']), 'Invalid machine_id for repository selection')
        require(value['profile_selection'] == 'machines/' + value['machine_id'] + '.yaml',
                'profile_selection must be machines/<machine_id>.yaml')
    absolute(value['config_dir'], 'config_dir')
    for name, root in mapping(value['catalog_roots'], 'catalog_roots').items():
        require(NAME.fullmatch(name), 'catalog_roots keys must be catalog aliases')
        absolute(root, 'catalog root')
    for group, kinds in (('agents', AGENTS), ('apps', APPS)):
        for name, entry in mapping(value[group], group).items():
            require(name in kinds, 'Unknown Machine entry: ' + name)
            keys = ('binding',) if external else ('profile', 'binding')
            closed(entry, keys, keys, 'Machine entry')
            if not external:
                profile_path(entry['profile'], group, name)
            allowed = ('executable', 'config_root') if group == 'agents' else ('application', 'data_root', 'backend')
            if group == 'agents' and name == 'claude-code':
                allowed += ('user_config',)
            closed(entry['binding'], allowed, allowed[:2], 'runtime binding')
            for key, location in entry['binding'].items():
                absolute(location, key)
    bindings = value['bindings']
    closed(bindings, ('executables', 'paths', 'endpoints', 'secrets'), (), 'Machine bindings')
    for group, entries in bindings.items():
        for name, entry in mapping(entries, 'bindings.' + group).items():
            require(NAME.fullmatch(name), 'Invalid Machine binding name')
            if group in ('executables', 'paths'):
                absolute(entry, group + '.' + name)
            elif group == 'endpoints':
                string(entry, 'endpoint')
                parsed = urlsplit(entry)
                require(parsed.scheme in ('http', 'https', 'ws', 'wss') and parsed.hostname is not None and not parsed.username and not parsed.password,
                        'Endpoint must be a URL without credentials')
                require(not parsed.query and not parsed.fragment, 'Endpoint query/fragment must stay local')
            else:
                require(isinstance(entry, dict), 'Secrets must be logical references')
                provider = entry.get('provider')
                key = 'variable' if provider == 'env' else 'ref'
                closed(entry, ('provider', key), ('provider', key), 'secret reference')
                require(provider in ('env', 'native'), 'Unknown secret provider')
                string(entry[key], 'secret reference')
                if provider == 'env':
                    require(re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', entry[key]), 'Invalid environment variable')
    if 'runtime' in value:
        runtime = value['runtime']
        closed(runtime, ('state_dir', 'backup_dir'), (), 'runtime')
        for key, entry in runtime.items():
            absolute(entry, 'runtime.' + key)
    if 'merge_policy' in value:
        policy(value['merge_policy'])
    return value


def profile_path(value, group, name):
    path = relative(value, 'Profile path')
    require(len(path.parts) == 4 and path.parts[:3] == ('profiles', group, name) and path.suffix == '.yaml',
            'Profile path must agree with its type and directory')


def validate_machine_profiles(value, machine_id):
    keys = ('schema_version', 'machine_id', 'agents', 'apps')
    closed(value, keys, keys, 'Machine Profile selection')
    version(value)
    require(value['machine_id'] == machine_id and NAME.fullmatch(machine_id), 'Machine selection identity mismatch')
    for group, kinds in (('agents', AGENTS), ('apps', APPS)):
        for name, profile in mapping(value[group], group).items():
            require(name in kinds, 'Unknown Machine selection: ' + name)
            profile_path(profile, group, name)
    return value
