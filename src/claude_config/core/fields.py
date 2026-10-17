"""Explicit portable Agent setting allowlist. Not a native-file import schema."""
from dataclasses import dataclass
import re

from .schema import SchemaError, as_pointer, closed, pointer, require
from .permissions import NETWORK_FLAGS, domain_rule, permission_dependencies, permission_name

CLAUDE_DOC = 'https://code.claude.com/docs/en/settings-reference'
CODEX_DOC = 'https://learn.chatgpt.com/docs/config-file/config-reference'


@dataclass(frozen=True)
class Field:
    path: tuple
    kind: str
    choices: tuple = ()
    minimum: int = None
    maximum: int = None
    since: str = None
    binding: bool = False

    def check(self, value, path):
        label = as_pointer(('settings', *path))
        kinds = {'string': str, 'bool': bool, 'int': int, 'strings': list, 'picker': list}
        if self.kind == 'window':
            legal = value == 'auto' if isinstance(value, str) else type(value) is int and 100000 <= value <= 1000000
            require(legal, label + ': expected auto or 100000–1000000 tokens')
            return
        if self.kind == 'number':
            require(type(value) in (int, float), label + ': expected a number')
        else:
            require(type(value) is kinds[self.kind], label + ': wrong value type')
        if self.choices:
            require(value in self.choices, label + ': unsupported enum value')
        if self.minimum is not None:
            require(value >= self.minimum, label + ': below minimum')
        if self.maximum is not None:
            require(value <= self.maximum, label + ': above maximum')
        if self.kind == 'strings':
            require(all(isinstance(item, str) and item for item in value), label + ': expected model names')
        if self.kind == 'picker':
            for item in value:
                closed(item, ('model', 'label', 'description', 'behavesAs'), ('model',), label + ' row')
                require(all(isinstance(child, str) for child in item.values()) and bool(item['model']), label + ': invalid picker row')


def f(path, kind, **kwargs):
    return Field(tuple(path.split('.')), kind, **kwargs)


CLAUDE = [
    f('model', 'string'), f('effortLevel', 'string', choices=('low', 'medium', 'high', 'xhigh')),
    f('modelSettings.*.effortLevel', 'string', choices=('low', 'medium', 'high', 'xhigh'), since='2.1.251'),
    f('language', 'string'), f('permissions.defaultMode', 'string',
        choices=('default', 'acceptEdits', 'plan', 'auto', 'dontAsk', 'bypassPermissions', 'manual')),
    f('autoMemoryEnabled', 'bool'), f('autoCompactEnabled', 'bool'),
    f('attribution.commit', 'string'), f('attribution.pr', 'string'), f('outputStyle', 'string'),
    f('fallbackModel', 'strings'), f('availableModels', 'strings'),
    f('modelPicker.options', 'picker', since='2.1.242'), f('modelPicker.replaceBuiltInOptions', 'bool', since='2.1.242'),
    f('autoCompactWindow', 'number', minimum=100000, maximum=1000000),
    f('modelSettings.*.autoCompactWindow', 'window', since='2.1.288'),
    f('statusLine.type', 'string', choices=('command',)), f('statusLine.command', 'string', binding=True),
    f('statusLine.padding', 'number', minimum=0), f('statusLine.refreshInterval', 'number', minimum=1),
    f('statusLine.hideVimModeIndicator', 'bool'),
    f('subagentStatusLine.type', 'string', choices=('command',)), f('subagentStatusLine.command', 'string', binding=True),
    f('teammateMode', 'string', choices=('in-process', 'auto', 'tmux', 'iterm2')), f('agent', 'string'),
]
for variable in ('CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS', 'CLAUDE_CODE_SUBAGENT_MODEL_FORCE', 'CLAUDE_CODE_FORK_SUBAGENT'):
    CLAUDE.append(Field(('env', variable), 'string', choices=('1', '0')))
CLAUDE.append(Field(('env', 'CLAUDE_CODE_SUBAGENT_MODEL'), 'string'))
for variable in ('CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS', 'CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH'):
    CLAUDE.append(Field(('env', variable), 'string'))

CODEX = [
    f('model', 'string'), f('model_reasoning_effort', 'string'),
    f('approval_policy', 'string', choices=('on-request', 'never')),
    f('sandbox_mode', 'string', choices=('read-only', 'workspace-write', 'danger-full-access')),
    f('memories.generate_memories', 'bool'), f('memories.use_memories', 'bool'),
    f('web_search', 'string', choices=('disabled', 'cached', 'indexed', 'live')),
    f('review_model', 'string'), f('model_context_window', 'int', minimum=1),
    f('model_auto_compact_token_limit', 'int', minimum=0),
    f('model_auto_compact_token_limit_scope', 'string', choices=('total', 'body_after_prefix')),
    f('model_post_turn_compact_threshold_percent', 'int', minimum=0, maximum=100),
    f('model_verbosity', 'string', choices=('low', 'medium', 'high')),
    f('model_reasoning_summary', 'string', choices=('auto', 'concise', 'detailed', 'none')),
    f('approvals_reviewer', 'string', choices=('user', 'auto_review')),
    f('auto_review.policy', 'string'), f('auto_review.extra_policy', 'string'),
    f('auto_review.circuit_break_action', 'string', choices=('default', 'strict')),
    f('auto_review.conversation_history_max_output_tokens', 'int', minimum=1),
    f('default_permissions', 'string'),
    f('features.network_proxy', 'bool', since='0.138.0'),
    f('permissions.*.description', 'string', since='0.138.0'),
    f('permissions.*.extends', 'string', since='0.138.0'),
    f('permissions.*.network.domains.*', 'string', choices=('allow', 'deny'), since='0.138.0'),
    f('agents.enabled', 'bool'), f('agents.max_concurrent_threads_per_session', 'int', minimum=1),
    f('agents.default_subagent_model', 'string'), f('agents.default_subagent_reasoning_effort', 'string'),
    f('agents.interrupt_message', 'bool'), f('agents.max_depth', 'int'),
    f('skills.include_instructions', 'bool'), f('skills.max_context_tokens', 'int', minimum=1),
    f('hide_agent_reasoning', 'bool'), f('show_raw_agent_reasoning', 'bool'),
]
for key in NETWORK_FLAGS:
    CODEX.append(Field(('permissions', '*', 'network', key), 'bool', since='0.138.0'))
for key in ('network_access', 'exclude_slash_tmp', 'exclude_tmpdir_env_var'):
    CODEX.append(Field(('sandbox_workspace_write', key), 'bool'))
for key in ('mcp_elicitations', 'request_permissions', 'rules', 'sandbox_approval', 'skill_approval'):
    CODEX.append(Field(('approval_policy', 'granular', key), 'bool'))

REGISTRY = {'claude-code': CLAUDE, 'codex': CODEX}


def matches(pattern, path):
    return len(pattern) == len(path) and all(a == '*' or a == b for a, b in zip(pattern, path))


def lookup(agent, path):
    return next((field for field in REGISTRY[agent] if matches(field.path, path)), None)


def leaves(value, prefix=()):
    for key, child in value.items():
        path = (*prefix, key)
        if isinstance(child, dict) and child:
            yield from leaves(child, path)
        elif not isinstance(child, dict):
            yield path, child
        # Empty objects select no leaves, but their parent must still be allowed.


def validate_settings(agent, value, *, generated=()):
    require(isinstance(value, dict), 'settings must be a mapping')
    generated = set(generated)
    def visit(node, prefix):
        for key, child in node.items():
            path = (*prefix, key)
            if agent == 'codex' and path[:1] == ('permissions',) and len(path) >= 2:
                from .schema import NAME
                require(NAME.fullmatch(path[1]), 'Invalid named permission policy')
            field = lookup(agent, path)
            if field is not None and not isinstance(child, dict):
                require(not field.binding or path in generated, as_pointer(('settings', *path)) + ': requires a command binding')
                field.check(child, path)
                if agent == 'codex' and path == ('default_permissions',):
                    permission_name(child)
                if agent == 'codex' and path[:1] == ('permissions',):
                    if path[2:] == ('extends',):
                        permission_name(child, parent=True)
                    elif path[2:4] == ('network', 'domains'):
                        domain_rule(path[4], child)
                if path[-1] in ('CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS', 'CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH'):
                    require(bool(re.fullmatch(r'[1-9][0-9]*', child)), 'Subagent limit must be a positive integer string')
                if path[-1] in ('model', 'model_reasoning_effort', 'default_subagent_model', 'default_subagent_reasoning_effort', 'default_permissions', 'agent'):
                    require(bool(child), as_pointer(('settings', *path)) + ': empty value is not allowed')
            elif isinstance(child, dict) and any(len(field.path) > len(path) and matches(field.path[:len(path)], path) for field in REGISTRY[agent]):
                visit(child, path)
            else:
                raise SchemaError('Field excluded or not registered: ' + as_pointer(('settings', *path)))
    visit(value, ())
    if 'default_permissions' in value and any(key in value for key in ('sandbox_mode', 'sandbox_workspace_write')):
        raise SchemaError('default_permissions cannot be combined with legacy sandbox settings')
    return value


def validate_native_result(agent, value, selected):
    """Check native object requirements after incremental leaf merging."""
    if agent == 'codex' and 'approval_policy' in selected and isinstance(value.get('approval_policy'), dict):
        require(set(value['approval_policy'].get('granular', {})) >= {'mcp_elicitations', 'rules', 'sandbox_approval'},
                'Merged granular approval needs mcp_elicitations, rules and sandbox_approval')
    if agent == 'codex' and 'default_permissions' in selected:
        require(not any(key in value for key in ('sandbox_mode', 'sandbox_workspace_write')),
                'Merged default_permissions conflicts with existing sandbox settings; clear them explicitly')
    if agent == 'codex' and ('default_permissions' in selected or 'permissions' in selected):
        permission_dependencies(selected, value)
    for group in ('statusLine', 'subagentStatusLine'):
        if agent == 'claude-code' and group in selected and selected[group]:
            current = value.get(group, {})
            require(current.get('type') == 'command' and isinstance(current.get('command'), str) and bool(current['command']),
                    'Merged ' + group + ' requires type=command and a native command or resolved binding')


def selected_paths(agent, selectors, bound=()):
    paths = []
    for selector in selectors:
        path = pointer(selector)
        require(len(path) > 1 and path[0] == 'settings', 'Select a concrete settings field')
        require(lookup(agent, path[1:]) is not None, 'Selection is not a registered atomic field: ' + selector)
        require(not lookup(agent, path[1:]).binding or selector in bound, 'Command fields export through logical bindings only')
        paths.append(path)
    require(len(paths) == len(set(paths)), 'Duplicate field selection')
    return paths


def version_tuple(version):
    match = re.search(r'(\d+)\.(\d+)\.(\d+)', version)
    require(match is not None, 'Cannot identify native version')
    return tuple(map(int, match.groups()))


def capability_checks(agent, settings, version, *, native_context=None):
    checks = []
    # Claude has no reliable public schema-check command. Claim only the documented
    # user-file interface verified against the inspected installed release.
    if agent == 'claude-code' and version_tuple(version) < (2, 1, 278):
        checks.append({'name': 'native_version', 'status': 'blocked', 'code': 'capability_unknown',
                       'reason': 'Claude settings adapter requires 2.1.278 or later'})
    for path, value in leaves(settings):
        field = lookup(agent, path)
        if field is None:
            continue
        row = {'name': 'field_capability', 'path': ['settings', *path], 'version': version,
               'status': 'passed', 'evidence': CLAUDE_DOC if agent == 'claude-code' else CODEX_DOC,
               'level': 'documented-config-interface'}
        if field.since and version_tuple(version) < version_tuple(field.since):
            row.update(status='blocked', code='unsupported', reason='Requires ' + field.since + ' or later')
        if agent == 'codex' and path == ('agents', 'max_depth'):
            features = (native_context or {}).get('features', {})
            v2 = features.get('multi_agent_v2')
            if v2 is not False:
                row.update(status='blocked', code='unsupported' if v2 else 'capability_unknown',
                           reason='max_depth applies only to V1; V2 or backend selection is not verified as V1')
        if agent == 'codex' and path == ('agents', 'enabled') and (native_context or {}).get('features', {}).get('multi_agent_v2'):
            row.update(status='warning', code='backend_override', reason='multi_agent_v2 takes precedence over agents.enabled')
        checks.append(row)
    return checks
