"""Registered portable resource options use the common merge policy protocol."""
from copy import deepcopy

from .merge import MISSING, MergePlan, get
from .schema import as_pointer, pointer, require


def option_kind(agent, group, path):
    if group == 'plugins':
        return 'bool' if path == ('enabled',) else None
    if group == 'skills':
        keys = ('enabled', 'allow_implicit_invocation') if agent == 'codex' else ('enabled', 'disable-model-invocation', 'user-invocable')
        return 'bool' if len(path) == 2 and path[0] == 'control' and path[1] in keys else None
    if group == 'mcp_servers':
        from .registrations import MCP_OPTIONS
        if len(path) == 3 and path[0] == 'tools' and path[1]:
            return {'approval_mode': 'string', 'output_token_limit': 'integer'}.get(path[2]) if agent == 'codex' else None
        if len(path) != 1 or path[0] not in MCP_OPTIONS[agent]:
            return None
        return 'array' if path[0] in ('enabled_tools', 'disabled_tools') else 'object' if path[0] == 'tools' else 'scalar'
    if group == 'hooks':
        return 'scalar' if path in (('timeout',), ('async',), ('statusMessage',)) else None
    if group == 'agents':
        from .agent_options import option_keys
        return 'string' if len(path) == 1 and path[0] in option_keys(agent) else None
    return None


def validate_resource_policy(agent, profile, path, action):
    parsed = pointer(path)
    require(len(parsed) >= 5 and parsed[0] == 'resources' and parsed[3] == 'options', 'Policy must target a resource option leaf')
    group, ref = parsed[1:3]
    require(ref in profile.get('resources', {}).get(group, {}), 'Resource policy targets an unselected resource')
    kind = option_kind(agent, group, parsed[4:])
    require(kind is not None and kind != 'object', 'Policy target is not a registered resource option leaf')
    require(action != 'append_unique', 'No resource option list is registered as an unordered set')
    require(action != 'replace' or kind == 'array', 'replace requires a resource array option')


def merge_options(resources, ref, incoming, current, *, baseline=None, report_prefix=None):
    package = resources.rendered[ref]['package']
    root = ('resources', package.group, ref, 'options')
    def wrap(value):
        return {'resources': {package.group: {ref: {'options': deepcopy(value)}}}}
    keys = set(incoming) | set(current)
    scope = [as_pointer((*root, key)) for key in sorted(keys)]
    policies = {}
    for label, rules in (('profile_policy', resources.profile.get('merge_policy', {})), ('run_policy', resources.run_policy)):
        policy = {key: value for key, value in rules.items() if key == 'default'}
        fields = {key: value for key, value in rules.get('fields', {}).items() if pointer(key)[:4] == root}
        if fields:
            policy['fields'] = fields
        policies[label] = policy
    plan = MergePlan(wrap(incoming), wrap(current), source={'kind': 'resource-usage', 'ref': ref, 'commit': package.commit},
                     destination={'kind': 'native-resource-options', 'agent_type': resources.manager.agent}, scope=scope,
                     machine_policy=resources.manager.machine.get('merge_policy'), baseline=wrap(baseline) if baseline is not None else None,
                     number_fields=[as_pointer((*root, key)) for key in ('startup_timeout_sec', 'tool_timeout_sec')], **policies)
    for row in plan.report()['decisions']:
        local = tuple(row['path'][4:])
        if get(package.selection.get('options', {}), local) is not MISSING:
            row['source'] = {'kind': 'profile-resource-options', 'ref': ref}
        row['target_fingerprint'] = plan.report()['snapshots']['target']
        if report_prefix:
            row['native_path'] = list(report_prefix)
        resources.decisions.append(row)
    for blocker in plan.blockers:
        resources.checks.append({'name': 'resource_option_merge', 'path': blocker['path'], 'status': 'blocked',
                                 'code': 'resource_option_conflict', 'reason': blocker['code']})
    return deepcopy(get(plan.result, root))
