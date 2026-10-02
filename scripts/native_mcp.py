"""Optional native MCP registration; credentials stay with the host."""
import hashlib
import json
import re


class MCPMixin:
    def mcp_definitions(self, host):
        values = self.declaration.get('agents', {}).get(host, {}).get('mcp_servers', {})
        if not isinstance(values, dict):
            raise ValueError('mcp_servers must be a mapping')
        for name, value in values.items():
            if not re.fullmatch(r'[a-zA-Z0-9_-]+', name) or not isinstance(value, dict):
                raise ValueError('Invalid MCP declaration')
            allowed = {'type', 'command', 'args', 'url'} if host == 'claude' else {'command', 'args', 'url', 'bearer_token_env_var'}
            if set(value) - allowed:
                raise ValueError('MCP declarations contain only native connection definitions and credential variable names')
            if bool(value.get('command')) == bool(value.get('url')):
                raise ValueError('MCP needs exactly one native command or URL')
            if value.get('url') and value.get('args'):
                raise ValueError('Remote MCP cannot have stdio args')
            if host == 'claude':
                expected = 'stdio' if value.get('command') else 'http'
                if value.get('type', expected) not in ({'stdio'} if expected == 'stdio' else {'http', 'sse'}):
                    raise ValueError('MCP type differs from native transport')
            if 'bearer_token_env_var' in value and (not isinstance(value['bearer_token_env_var'], str) or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', value['bearer_token_env_var'])):
                raise ValueError('MCP credential reference must be a variable name')
            if value.get('args') is not None and (not isinstance(value['args'], list) or not all(isinstance(x, str) for x in value['args'])):
                raise ValueError('MCP args must be a list of strings')
            for key in ('url', 'command', 'bearer_token_env_var'):
                if key in value and not isinstance(value[key], str):
                    raise ValueError('MCP field must be a string: ' + key)
        return values

    def mcp_fingerprint(self, host, name):
        command = [host, 'mcp', 'get', name] + (['--json'] if host == 'codex' else [])
        try:
            output = self.runner(command)
        except RuntimeError as exc:
            if 'No MCP server named' in str(exc):
                return None
            raise
        if host == 'codex':
            value = json.loads(output)
            normalized = json.dumps(value.get('transport', value), sort_keys=True)
        else:
            output = re.sub(r'\x1b\[[0-9;]*m', '', output)
            # Exclude health checks and auth/header output. Only store a digest.
            normalized = '\n'.join(line.strip() for line in output.splitlines()
                                   if re.match(r'\s*(Type|URL|Command|Args|Scope):', line))
            if not normalized:
                raise ValueError('Cannot verify this Claude MCP metadata format; no mutation performed')
        return hashlib.sha256(normalized.encode()).hexdigest()

    def mcp_registry_path(self):
        return self.state_root() / 'managed-mcp.json'

    def mcp_preflight(self, host):
        path = self.mcp_registry_path()
        registry = json.loads(path.read_text()) if path.exists() else {}
        result = {}
        for name, definition in self.mcp_definitions(host).items():
            digest = self.mcp_fingerprint(host, name)
            selected_hash = hashlib.sha256(json.dumps(definition, sort_keys=True).encode()).hexdigest()
            owned = registry.get(host, {}).get(name, {})
            if digest and (owned.get('observed') != digest or owned.get('selected') != selected_hash):
                raise ValueError('Existing MCP differs or is unmanaged; reconcile with the native host: ' + name)
            result[name] = {'observed': digest, 'selected': selected_hash}
        return result

    def mcp_add(self, host, name, definition):
        if host == 'claude':
            native = {'type': 'stdio' if definition.get('command') else 'http', **definition}
            self.runner(['claude', 'mcp', 'add-json', name, json.dumps(native), '--scope', 'user'])
        elif definition.get('url'):
            command = ['codex', 'mcp', 'add', name, '--url', definition['url']]
            if definition.get('bearer_token_env_var'):
                command += ['--bearer-token-env-var', definition['bearer_token_env_var']]
            self.runner(command)
        else:
            self.runner(['codex', 'mcp', 'add', name, '--', definition['command'], *definition.get('args', [])])

    def mcp_remember(self, host, name, value):
        path = self.mcp_registry_path()
        registry = json.loads(path.read_text()) if path.exists() else {}
        registry.setdefault(host, {})
        if value is None:
            registry[host].pop(name, None)
        else:
            registry[host][name] = value
        self.write(path, json.dumps(registry, indent=2) + '\n')
