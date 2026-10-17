"""Portable named Codex permission leaves and isolated native dependencies.

Filesystem roots, socket paths, proxy addresses and certificates stay local.
No policy is selected or network proxy enabled implicitly.
"""
from copy import deepcopy
import ipaddress
import re

from .schema import NAME, require

BUILTINS = (':read-only', ':workspace', ':danger-full-access')
NETWORK_FLAGS = ('enabled', 'enable_socks5', 'enable_socks5_udp', 'allow_upstream_proxy',
                 'allow_local_binding', 'dangerously_allow_non_loopback_proxy',
                 'dangerously_allow_all_unix_sockets')


def permission_name(value, *, parent=False):
    require(isinstance(value, str) and (value in BUILTINS or NAME.fullmatch(value)), 'Permission reference must be a built-in or named policy')
    require(not parent or value != ':danger-full-access', 'Permission inheritance cannot extend :danger-full-access')


def domain_rule(pattern, action):
    require(isinstance(pattern, str) and pattern and not any(char.isspace() for char in pattern), 'Invalid network domain rule')
    require(action in ('allow', 'deny'), 'Network domain action must be allow or deny')
    if pattern == '*':
        require(action == 'allow', 'Global network wildcard is valid only for allow')
        return
    host = pattern[3:] if pattern.startswith('**.') else pattern[2:] if pattern.startswith('*.') else pattern
    require('*' not in host and not any(char in host for char in ('/', '@', '\\', '?', '#')), 'Network rules use hosts, not URLs or local paths')
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        require(host == pattern, 'IP network rules cannot use domain wildcards')
        return
    try:
        ascii_host = host.encode('idna').decode('ascii')
    except UnicodeError:
        ascii_host = ''
    require(bool(ascii_host) and len(ascii_host) <= 253 and
            all(re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?', part) for part in ascii_host.rstrip('.').split('.')),
            'Invalid domain rule; ports and credentials stay in Machine bindings')


def permission_dependencies(selected, native):
    """Validate referenced parents and return only portable parser inputs.

    Final native policies can contain local roots/endpoints. Those survive writes
    but are not copied into an isolated probe or a synchronized Profile.
    """
    policies = native.get('permissions', {})
    required = set(selected.get('permissions', {}))
    if 'default_permissions' in selected:
        name = native.get('default_permissions')
        permission_name(name)
        if name not in BUILTINS:
            required.add(name)
    result, active = {}, set()
    def visit(name):
        require(isinstance(name, str) and NAME.fullmatch(name), 'Invalid named permission policy')
        require(name not in active, 'Named permission inheritance contains a cycle')
        if name in result:
            return
        require(isinstance(policies, dict) and name in policies and isinstance(policies[name], dict), 'Referenced named permission policy is missing: ' + name)
        active.add(name)
        value = policies[name]
        portable = {key: deepcopy(value[key]) for key in ('description', 'extends') if key in value}
        if 'extends' in portable:
            permission_name(portable['extends'], parent=True)
            if portable['extends'] not in BUILTINS:
                visit(portable['extends'])
        network = value.get('network', {})
        require(isinstance(network, dict), 'Permission network rules must be an object')
        chosen = {key: network[key] for key in NETWORK_FLAGS if key in network}
        require(all(type(item) is bool for item in chosen.values()), 'Permission network switches must be Boolean')
        if 'domains' in network:
            require(isinstance(network['domains'], dict), 'Permission network domains must be an object')
            for pattern, action in network['domains'].items():
                domain_rule(pattern, action)
            chosen['domains'] = deepcopy(network['domains'])
        if chosen:
            portable['network'] = chosen
        active.remove(name)
        result[name] = portable
    for name in sorted(required):
        visit(name)
    return result


def enforcement_checks(settings, context):
    """Explain configured domain rules without claiming command execution."""
    checks = []
    proxy = settings.get('features', {}).get('network_proxy', context.get('features', {}).get('network_proxy', False))
    for name, policy in settings.get('permissions', {}).items():
        network = policy.get('network', {})
        if network.get('domains') and not proxy:
            checks.append({'name': 'permission_domain_enforcement', 'path': ['settings', 'permissions', name, 'network', 'domains'],
                           'status': 'warning', 'code': 'network_proxy_inactive', 'level': 'documented-config-interface',
                           'reason': 'Domain rules are configured but network_proxy is not enabled in the checked user configuration; managed requirements and live enforcement are not verified'})
    return checks
