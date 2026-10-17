"""Owned native MCP entries and Hook groups; no service or handler execution."""
from copy import deepcopy
import json
from pathlib import Path
import re
import shlex
from urllib.parse import urlsplit

from .merge import fingerprint, get, same
from .native import parse_json, put, snapshot_read
from .resource_options import merge_options
from .schema import NAME, closed, mapping, relative, require, string
from .transaction import FileChange


MCP_FIELDS = {
    'claude-code': ('type', 'command', 'args', 'env', 'url', 'headers', 'timeout'),
    'codex': ('command', 'args', 'env', 'env_vars', 'cwd', 'url', 'bearer_token_env_var',
              'http_headers', 'env_http_headers', 'startup_timeout_sec', 'tool_timeout_sec',
              'enabled', 'required', 'enabled_tools', 'disabled_tools', 'default_tools_approval_mode',
              'tools', 'supports_parallel_tool_calls', 'tool_input_schema_max_bytes'),
}
MCP_OPTIONS = {
    'claude-code': ('timeout',),
    'codex': ('startup_timeout_sec', 'tool_timeout_sec', 'enabled', 'required', 'enabled_tools',
              'disabled_tools', 'default_tools_approval_mode', 'tools', 'supports_parallel_tool_calls',
              'tool_input_schema_max_bytes'),
}
EVENTS = {
    'claude-code': frozenset(('SessionStart', 'SessionEnd', 'UserPromptSubmit', 'PreToolUse',
                             'PermissionRequest', 'PostToolUse', 'PostToolUseFailure', 'Notification',
                             'Stop', 'SubagentStart', 'SubagentStop', 'PreCompact', 'PostCompact',
                             'TeammateIdle', 'TaskCompleted')),
    'codex': frozenset(('SessionStart', 'SessionEnd', 'UserPromptSubmit', 'PreToolUse', 'PermissionRequest',
                       'PostToolUse', 'PreCompact', 'PostCompact', 'SubagentStart', 'SubagentStop', 'Stop', 'Interrupt')),
}
HOOK_OPTIONS = ('timeout', 'async', 'statusMessage')
SENSITIVE = re.compile(r'(?:token|password|secret|authorization|api[-_]?key)', re.I)


def claude_user_config(binding):
    root = Path(binding['config_root']).absolute()
    default = root == Path.home() / '.claude'
    ordinary = Path.home() / '.claude.json' if default else root / '.claude.json'
    chosen = Path(binding.get('user_config', str(ordinary)))
    allowed = {root / '.claude.json', ordinary}
    require(chosen in allowed, 'Claude user_config must match its default or explicit-directory native layout')
    return chosen


def validate_registration(value):
    closed(value, ('path', 'kind', 'name', 'sha256', 'groups'), ('path', 'kind'), 'native registration state')
    require(isinstance(value['path'], str) and Path(value['path']).is_absolute(), 'Registration path must be absolute')
    require(value['kind'] in ('mcp', 'hook'), 'Unknown registration kind')
    if value['kind'] == 'mcp':
        require(set(value) == {'path', 'kind', 'name', 'sha256'}, 'Invalid MCP state shape')
        require(isinstance(value['name'], str) and NAME.fullmatch(value['name']), 'Invalid managed MCP name')
        require(isinstance(value['sha256'], str) and re.fullmatch(r'[0-9a-f]{64}', value['sha256']), 'Invalid MCP state fingerprint')
    else:
        require(set(value) == {'path', 'kind', 'groups'} and isinstance(value['groups'], list), 'Invalid Hook state shape')
        seen = set()
        for group in value['groups']:
            closed(group, ('event', 'sha256'), ('event', 'sha256'), 'Hook group state')
            require(isinstance(group['event'], str) and isinstance(group['sha256'], str)
                    and re.fullmatch(r'[0-9a-f]{64}', group['sha256']), 'Invalid Hook group fingerprint')
            key = (group['event'], group['sha256'])
            require(key not in seen, 'Duplicate owned Hook group')
            seen.add(key)


class Templates:
    def __init__(self, item):
        self.item = item
        self.slots = item['slots']
        self.requirements = item['package'].item['requires'].get('bindings', {})

    def slot(self, value, kind):
        closed(value, ('binding',), ('binding',), 'resource binding placeholder')
        name = value['binding']
        require(isinstance(name, str) and name in self.slots and self.requirements[name]['kind'] == kind,
                'Resource placeholder requires a selected binding of kind ' + kind)
        return self.slots[name]

    def credential(self, value):
        reference = self.slot(value, 'secret')
        require(reference['provider'] == 'env', 'This native field requires an environment credential reference')
        return reference['variable']

    def argument(self, value):
        if isinstance(value, str):
            require('\0' not in value and not value.startswith(('/', '~')) and '${' not in value,
                    'Local argument paths and environment interpolation require logical bindings')
            return value
        if isinstance(value, dict) and 'resource_file' in value:
            closed(value, ('resource_file',), ('resource_file',), 'package file argument')
            path = relative(value['resource_file'], 'package file argument').as_posix()
            require(path in self.item['files'], 'Argument is not a tracked package file')
            return str(self.item['cache'] / path)
        return self.slot(value, 'path')

    def command(self, value):
        closed(value, ('executable', 'args'), ('executable', 'args'), 'Hook command template')
        require(isinstance(value['args'], list), 'Hook command args must be a list')
        return shlex.join([self.slot(value['executable'], 'executable'), *[self.argument(arg) for arg in value['args']]])

    def url(self, value):
        value = self.slot(value, 'endpoint') if isinstance(value, dict) else value
        string(value, 'MCP URL')
        parsed = urlsplit(value)
        require(parsed.scheme in ('http', 'https') and parsed.hostname and not parsed.username and not parsed.password
                and not parsed.query and not parsed.fragment and '${' not in value,
                'MCP URL must be an HTTP endpoint without embedded credentials, interpolation, query or fragment')
        return value

    def public_values(self, value, *, interpolation=False, headers=False):
        result = {}
        for name, entry in mapping(value, 'native environment or headers').items():
            require(name and '\n' not in name and '\r' not in name, 'Invalid native environment/header name')
            if isinstance(entry, dict):
                require(interpolation, 'This native field cannot interpolate credentials; use env_vars or env_http_headers')
                closed(entry, ('binding', 'prefix'), ('binding',), 'credential interpolation')
                variable = self.credential({'binding': entry['binding']})
                if headers:
                    require(variable not in ('ANTHROPIC_API_KEY', 'ANTHROPIC_AUTH_TOKEN', 'AWS_BEARER_TOKEN_BEDROCK', 'HTTPS_PROXY', 'NPM_TOKEN'),
                            'Claude remote headers suppress this native credential variable; select a service-specific variable')
                prefix = entry.get('prefix', '')
                require(prefix in ('', 'Bearer '), 'Credential prefix must be empty or Bearer')
                result[name] = prefix + '${' + variable + '}'
            else:
                require(isinstance(entry, str) and '${' not in entry and not SENSITIVE.search(name),
                        'Credential-bearing environment/header values require logical references')
                require(not headers or '\r' not in entry and '\n' not in entry, 'Header values cannot contain line breaks')
                result[name] = entry
        return result


def mcp_definition(item, agent):
    template = Templates(item)
    value = parse_json(item['files'][item['package'].entry])
    closed(value, ('name', 'server'), ('name', 'server'), 'MCP resource template')
    require(isinstance(value['name'], str) and NAME.fullmatch(value['name']), 'Invalid MCP native name')
    server = deepcopy(value['server'])
    closed(server, MCP_FIELDS[agent], (), 'native MCP server')
    selection = item['package'].selection.get('options', {})
    closed(selection, (*MCP_OPTIONS[agent], 'entry'), (), 'native MCP options')
    options = {key: value for key, value in selection.items() if key != 'entry'}
    def merge_options(target, incoming):
        for key, value in incoming.items():
            if isinstance(value, dict) and isinstance(target.get(key), dict):
                merge_options(target[key], value)
            else:
                target[key] = deepcopy(value)
    merge_options(server, options)
    require(('command' in server) != ('url' in server), 'MCP needs exactly one command or URL')
    stdio = 'command' in server
    if agent == 'claude-code':
        transport = server.get('type', 'stdio' if stdio else 'http')
        require(transport in (('stdio',) if stdio else ('http', 'sse')), 'MCP type differs from transport')
        server['type'] = transport
    if stdio:
        require(not set(server).intersection(('headers', 'http_headers', 'env_http_headers', 'bearer_token_env_var')), 'Stdio MCP cannot have HTTP options')
        server['command'] = template.slot(server['command'], 'executable')
        if 'args' in server:
            require(isinstance(server['args'], list), 'MCP args must be a list')
            server['args'] = [template.argument(arg) for arg in server['args']]
        if 'cwd' in server:
            server['cwd'] = template.slot(server['cwd'], 'path')
            require(Path(server['cwd']).is_dir(), 'MCP working directory must be a directory')
        if 'env' in server:
            server['env'] = template.public_values(server['env'], interpolation=agent == 'claude-code')
        if 'env_vars' in server:
            require(isinstance(server['env_vars'], list), 'MCP env_vars must be a list of credential bindings')
            server['env_vars'] = [template.credential(ref) for ref in server['env_vars']]
            require(len(server['env_vars']) == len(set(server['env_vars'])), 'Duplicate forwarded credential variable')
    else:
        require(not set(server).intersection(('args', 'cwd', 'env', 'env_vars')), 'HTTP MCP cannot have stdio options')
        server['url'] = template.url(server['url'])
        for field in ('headers', 'http_headers'):
            if field in server:
                server[field] = template.public_values(server[field], interpolation=agent == 'claude-code', headers=True)
        if 'bearer_token_env_var' in server:
            server['bearer_token_env_var'] = template.credential(server['bearer_token_env_var'])
        if 'env_http_headers' in server:
            server['env_http_headers'] = {name: template.credential(ref) for name, ref in mapping(server['env_http_headers'], 'credential headers').items()}
    for field in ('enabled', 'required', 'supports_parallel_tool_calls'):
        if field in server:
            require(type(server[field]) is bool, 'MCP ' + field + ' must be Boolean')
    for field in ('startup_timeout_sec', 'tool_timeout_sec'):
        if field in server:
            require(type(server[field]) in (int, float) and server[field] > 0, 'MCP timeout must be positive')
    if 'timeout' in server:
        require(type(server['timeout']) is int and server['timeout'] >= 1000, 'Claude MCP timeout is milliseconds and must be at least 1000')
    if 'tool_input_schema_max_bytes' in server:
        require(type(server['tool_input_schema_max_bytes']) is int and server['tool_input_schema_max_bytes'] > 0, 'Invalid MCP schema budget')
    for field in ('enabled_tools', 'disabled_tools'):
        if field in server:
            require(isinstance(server[field], list) and all(isinstance(name, str) and name for name in server[field]), 'MCP tool selection must be a list of names')
    approvals = ('auto', 'prompt', 'writes', 'approve')
    if 'default_tools_approval_mode' in server:
        require(server['default_tools_approval_mode'] in approvals, 'Invalid MCP default tool approval')
    for tool in mapping(server.get('tools', {}), 'MCP tools').values():
        closed(tool, ('approval_mode', 'output_token_limit'), (), 'MCP tool options')
        if 'approval_mode' in tool:
            require(tool['approval_mode'] in approvals, 'Invalid MCP tool approval')
        if 'output_token_limit' in tool:
            require(type(tool['output_token_limit']) is int and tool['output_token_limit'] > 0, 'Invalid MCP output token limit')
    return value['name'], server


def hook_definition(item, agent):
    template = Templates(item)
    value = parse_json(item['files'][item['package'].entry])
    closed(value, ('hooks',), ('hooks',), 'Hook resource template')
    hooks = mapping(value['hooks'], 'native Hook events')
    require(bool(hooks), 'Hook template must declare at least one event')
    options = item['package'].selection.get('options', {})
    closed(options, HOOK_OPTIONS, (), 'native Hook options')
    result = {}
    for event, groups in hooks.items():
        require(event in EVENTS[agent], 'Hook event has no verified adapter: ' + event)
        require(isinstance(groups, list) and bool(groups), 'Hook event requires matcher groups')
        result[event] = []
        for group in groups:
            closed(group, ('matcher', 'hooks'), ('hooks',), 'Hook matcher group')
            if 'matcher' in group:
                string(group['matcher'], 'Hook matcher', empty=True)
                require(event not in ('UserPromptSubmit', 'Stop', 'Interrupt', 'TeammateIdle', 'TaskCompleted'),
                        'This Hook event ignores matchers; omit matcher')
            require(isinstance(group['hooks'], list) and bool(group['hooks']), 'Hook group needs handlers')
            rendered = {key: deepcopy(value) for key, value in group.items() if key != 'hooks'}
            rendered['hooks'] = []
            allowed = ('type', 'command', *HOOK_OPTIONS, 'additionalContextLimit') if agent == 'codex' else ('type', 'command', *HOOK_OPTIONS)
            for original in group['hooks']:
                closed(original, allowed, ('type', 'command'), 'native Hook handler')
                handler = {**deepcopy(original), **deepcopy(options)}
                require(handler['type'] == 'command', 'Only command Hook handlers are connected in this adapter')
                handler['command'] = template.command(handler['command'])
                if 'timeout' in handler:
                    require(type(handler['timeout']) is int and handler['timeout'] > 0, 'Hook timeout must be a positive integer')
                    require(agent != 'codex' or event not in ('SessionEnd', 'Interrupt') or handler['timeout'] <= 3,
                            'Codex SessionEnd/Interrupt timeout is limited to 3 seconds')
                if 'async' in handler:
                    require(type(handler['async']) is bool, 'Hook async must be Boolean')
                if 'statusMessage' in handler:
                    string(handler['statusMessage'], 'Hook status message', empty=True)
                if 'additionalContextLimit' in handler:
                    require(type(handler['additionalContextLimit']) is int and handler['additionalContextLimit'] >= 0, 'Invalid Hook context budget')
                rendered['hooks'].append(handler)
            result[event].append(rendered)
    return result


class Registrations:
    def __init__(self, resources, native, original):
        self.resources = resources
        self.manager = resources.manager
        self.main_path = self.manager.native_path.resolve()
        self.documents = {self.main_path: {'value': deepcopy(native), 'original': original,
                                                   'expected': self.manager.sources[self.manager.native_path]}}
        self.main_roots = set()
        self.main_fields = set()

    def document(self, path):
        require(not path.is_symlink(), 'Native registration file cannot be a symlink')
        path = self.resources._target(path.parent, path.name)
        if path not in self.documents:
            value, content, expected = self.manager._read(path, parse_json)
            self.documents[path] = {'value': value, 'original': content, 'expected': expected}
        return path, self.documents[path]['value']

    def _blocked(self, ref, code, reason, before=None, incoming=None):
        self.resources._block(ref, code, reason)
        self.resources._decision(ref, ['registration'], before, incoming, 'conflict', reason)

    def add_mcp(self, ref, item):
        agent = self.manager.agent
        name, server = mcp_definition(item, agent)
        path = claude_user_config(self.manager.entry['binding']) if agent == 'claude-code' else self.manager.native_path
        path, document = self.document(path)
        key = 'mcpServers' if agent == 'claude-code' else 'mcp_servers'
        entries = mapping(document.get(key, {}), 'native MCP entries')
        old = self.resources.previous.get(ref, {}).get('native', {}).get('registration')
        if old is not None and old['path'] != str(path):
            self._blocked(ref, 'registration_binding_changed', 'Native registration file binding changed; review explicit relocation and ownership before applying')
            return
        owned = old is not None and old['path'] == str(path)
        if owned:
            require(old['kind'] == 'mcp', 'Registration changed kind')
            if (old['name'] not in entries or fingerprint(entries[old['name']]) != old['sha256']) and ref not in self.resources.conflicts.restore:
                self._blocked(ref, 'mcp_drift', 'Managed MCP entry was externally changed or removed',
                              {'sha256': fingerprint(entries[old['name']])} if old['name'] in entries else None,
                              {'sha256': fingerprint(server)})
                return
        if name in entries and (not owned or old['name'] != name):
            self._blocked(ref, 'unmanaged_mcp_collision', 'Native MCP name already exists outside this resource ownership',
                          {'sha256': fingerprint(entries[name])}, {'sha256': fingerprint(server)})
            return
        controls = MCP_OPTIONS[agent]
        current = (self.resources.conflicts.baseline_registration(ref) if ref in self.resources.conflicts.restore else entries[old['name']]) if owned else {}
        server.update(merge_options(self.resources, ref, {key: server[key] for key in controls if key in server},
                                    {key: current[key] for key in controls if key in current}, report_prefix=(str(path), key, name)))
        before = {'sha256': fingerprint(entries[name])} if name in entries else None
        document.setdefault(key, {})[name] = server
        if owned and old['name'] != name:
            document[key].pop(old['name'], None)
            self.resources._decision(ref, ['registration', old['name']], {'sha256': old['sha256']}, None,
                                     'retire_previous_native_identity', 'Resource rename removes only the previous unchanged owned entry')
        after = {'sha256': fingerprint(server)}
        self.resources._decision(ref, ['registration', name], before, after, 'unchanged' if before == after else 'replace_entry' if before else 'add',
                                 'One resource owns this native service; other services and native fields are retained')
        self.resources.records[ref]['native']['registration'] = {'path': str(path), 'kind': 'mcp', 'name': name, **after}
        previous_entry = self.resources.previous.get(ref, {}).get('native', {}).get('mcp_entry')
        self.resources.records[ref]['native']['mcp_entry'] = item['package'].entry
        self.resources._decision(ref, ['runtime_entry'], previous_entry, item['package'].entry,
                                 'unchanged' if previous_entry == item['package'].entry else 'add' if previous_entry is None else 'replace_entry',
                                 'Selected fixed MCP template determines transport; connection settings are not inferred or converted')
        self.resources.checks.append({'name': 'native_mcp_definition', 'ref': ref, 'status': 'passed', 'level': 'documented-config-interface',
                                     'reason': 'Closed transport, binding and option schema; connection and authentication are not tested'})
        if path == self.main_path:
            self.main_roots.add(key)

    def add_hooks(self, ref, item):
        agent = self.manager.agent
        wanted = hook_definition(item, agent)
        path = self.manager.native_path if agent == 'claude-code' else self.manager.native_path.parent / 'hooks.json'
        path, document = self.document(path)
        events = mapping(document.get('hooks', {}), 'native Hook events')
        old = self.resources.previous.get(ref, {}).get('native', {}).get('registration')
        owned = old is not None and old['path'] == str(path)
        remaining = deepcopy(events)
        positions = {}
        owned_groups = {}
        if owned:
            require(old['kind'] == 'hook', 'Registration changed kind')
            if ref in self.resources.conflicts.restore:
                base = self.resources.conflicts.baseline_registration(ref)
                for event, index in sorted(self.resources.conflicts.locations[ref], key=lambda row: (row[0], -row[1])):
                    positions.setdefault(event, index)
                    del remaining[event][index]
                for row in base['groups']:
                    owned_groups.setdefault(row['event'], []).append(row['body'])
            for group in (() if ref in self.resources.conflicts.restore else old['groups']):
                rows = remaining.get(group['event'], [])
                require(isinstance(rows, list), 'Native Hook event must be a list')
                matches = [i for i, row in enumerate(rows) if fingerprint(row) == group['sha256']]
                if len(matches) != 1:
                    self._blocked(ref, 'hook_drift', 'Managed Hook group changed, disappeared or has ambiguous duplicates')
                    return
                positions.setdefault(group['event'], matches[0])
                owned_groups.setdefault(group['event'], []).append(deepcopy(rows[matches[0]]))
                del rows[matches[0]]
        controls = (*HOOK_OPTIONS, 'additionalContextLimit') if agent == 'codex' else HOOK_OPTIONS
        for event, groups in wanted.items():
            for index, group in enumerate(groups):
                candidates = [old_group for old_group in owned_groups.get(event, []) if old_group.get('matcher') == group.get('matcher')]
                peers = [peer for peer in groups if peer.get('matcher') == group.get('matcher')]
                if not candidates:
                    for handler_index, handler in enumerate(group['hooks']):
                        handler.update(merge_options(self.resources, ref, {key: handler[key] for key in controls if key in handler}, {},
                                                     report_prefix=(str(path), 'hooks', event, str(index), 'hooks', str(handler_index))))
                    continue
                old_controls = {key for old_group in candidates for handler in old_group['hooks'] for key in controls if key in handler}
                needs_preserve = any(key not in handler for key in old_controls for handler in group['hooks'])
                if len(candidates) != 1 or len(peers) != 1 or len(candidates[0]['hooks']) != len(group['hooks']):
                    if needs_preserve:
                        self._blocked(ref, 'hook_options_ambiguous', 'Changed Hook group/handler layout cannot preserve omitted usage options; select explicit options and replan')
                        return
                    continue
                for handler_index, (previous, handler) in enumerate(zip(candidates[0]['hooks'], group['hooks'])):
                    handler.update(merge_options(self.resources, ref, {key: handler[key] for key in controls if key in handler},
                                                 {key: previous[key] for key in controls if key in previous},
                                                 report_prefix=(str(path), 'hooks', event, str(index), 'hooks', str(handler_index))))
        state_groups = []
        for event, groups in wanted.items():
            rows = remaining.setdefault(event, [])
            require(isinstance(rows, list), 'Native Hook event must be a list')
            digests = [fingerprint(group) for group in groups]
            if len(set(digests)) != len(digests) or any(fingerprint(row) in digests for row in rows):
                self._blocked(ref, 'unmanaged_hook_collision', 'Hook group duplicates an existing group outside this resource ownership')
                return
            position = min(positions.get(event, len(rows)), len(rows))
            rows[position:position] = groups
            state_groups.extend({'event': event, 'sha256': digest} for digest in digests)
        before = {'groups': old['groups']} if owned else None
        if ref in self.resources.conflicts.restore:
            review = next(row for row in self.resources.conflicts.reviews if row['ref'] == ref)
            before = {'groups': [{key: group[key] for key in ('event', 'sha256')} for group in review['local']['native']['hooks'] if group['exists']]}
        after = {'groups': state_groups}
        # A no-op must retain the original placement of owned groups among
        # unrelated groups, rather than quietly clustering them together.
        document['hooks'] = events if before == after and ref not in self.resources.conflicts.restore else remaining
        self.resources._decision(ref, ['registration'], before, after, 'unchanged' if before == after else 'replace_owned_groups' if before else 'add',
                                 'Only exactly identified owned matcher groups are replaced; unrelated groups and order are retained')
        self.resources.records[ref]['native']['registration'] = {'path': str(path), 'kind': 'hook', **after}
        self.resources.checks.append({'name': 'native_hook_definition', 'ref': ref, 'status': 'passed', 'level': 'documented-config-interface',
                                     'reason': 'Closed native event/group/command schema; handlers are not executed'})
        if path == self.main_path:
            self.main_roots.add('hooks')
        if agent == 'codex':
            main = self.documents[self.main_path]['value']
            has_inline = bool(main.get('hooks', {}))
            self.resources.checks.append({'name': 'hook_layer', 'ref': ref, 'status': 'warning' if has_inline else 'passed',
                                         'reason': 'Native inline Hooks and hooks.json both load' if has_inline else 'User hooks.json registration'})
            self.resources.checks.append({'name': 'hook_trust', 'ref': ref, 'status': 'not_verified',
                                         'reason': 'Native feature enablement and exact-content trust remain native; registration does not grant trust'})

    def native_result(self, value):
        result = deepcopy(value)
        for key in sorted(self.main_roots):
            result[key] = deepcopy(self.documents[self.main_path]['value'][key])
        for path in sorted(self.main_fields):
            put(result, path, get(self.documents[self.main_path]['value'], path))
        return result

    def changes(self):
        changes = []
        for path, document in self.documents.items():
            if path == self.main_path:
                continue
            original = document['original']
            content = original if same(parse_json(original), document['value']) else (json.dumps(document['value'], indent=2, ensure_ascii=False) + '\n').encode()
            changes.append(FileChange(path, content, 'native-resource-registrations', parse_json, document['expected']))
        return changes
