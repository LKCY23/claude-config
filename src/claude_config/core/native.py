"""Native file codecs and isolated parser checks. No model or Hook execution."""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import selectors
import stat
import subprocess
import tempfile
import time
import tomllib

import tomlkit

from .fields import capability_checks, leaves
from .merge import MISSING, get, same
from .schema import StrictLoader, json_value, mapping
from .transaction import PlanStale, atomic_write
import yaml


def snapshot_read(path):
    path = Path(path).absolute()
    if path.is_symlink():
        raise ValueError('Refusing a symlinked target/source: ' + str(path))
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        if path.exists() or path.is_symlink():
            raise PlanStale('plan_stale while reading ' + str(path))
        return b'', {'exists': False}
    with os.fdopen(descriptor, 'rb') as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode):
            raise ValueError('Expected a regular file: ' + str(path))
        content = stream.read()
        after = os.fstat(stream.fileno())
    try:
        current = path.lstat()
    except FileNotFoundError as exc:
        raise PlanStale('plan_stale while reading ' + str(path)) from exc
    def identity(value):
        return (value.st_dev, value.st_ino, value.st_mode, value.st_size, value.st_mtime_ns, value.st_ctime_ns)
    if identity(before) != identity(after) or identity(before) != identity(current):
        raise PlanStale('plan_stale while reading ' + str(path))
    return content, {'exists': True, 'sha256': hashlib.sha256(content).hexdigest(), 'mode': before.st_mode & 0o777}


def parse_json(content):
    def pairs(rows):
        value = {}
        for key, child in rows:
            if key in value:
                raise ValueError('Duplicate JSON key')
            value[key] = child
        return value
    value = json.loads(content, object_pairs_hook=pairs) if content else {}
    json_value(value)
    return mapping(value, 'native JSON')


def parse_native(agent, content):
    try:
        return parse_json(content) if agent == 'claude-code' else mapping(tomllib.loads(content.decode()), 'native TOML')
    except (ValueError, TypeError, UnicodeError) as exc:
        raise ValueError('Native configuration cannot be parsed; no writes are allowed') from exc


def parse_yaml(content):
    value = yaml.load(content, Loader=StrictLoader)
    json_value(value)
    return mapping(value, 'management file')


def put(mapping, path, value):
    node = mapping
    for key in path[:-1]:
        node = node.setdefault(key, {})
    node[path[-1]] = deepcopy(value)


def projection(value, paths):
    result = {}
    for path in paths:
        child = get(value, path)
        if child is not MISSING:
            put(result, path, child)
    return result


def render_native(agent, before, result):
    old = parse_native(agent, before)
    if same(old, result):
        return before
    if agent == 'claude-code':
        return (json.dumps(result, indent=2, ensure_ascii=False) + '\n').encode()
    document = tomlkit.parse(before.decode())
    def update(node, original, wanted):
        for key in original.keys() - wanted.keys():
            del node[key]
        for key, value in wanted.items():
            if key in original and same(original[key], value):
                continue
            if isinstance(value, dict):
                if key not in node:
                    node[key] = tomlkit.table()
                update(node[key], original.get(key, {}), value)
            else:
                node[key] = value
    update(document, old, result)
    return tomlkit.dumps(document).encode()


class NativeProbe:
    def __call__(self, agent, executable, settings, context):
        executable = str(executable)
        with tempfile.TemporaryDirectory(prefix='claude-config-native-check-') as directory:
            root = Path(directory)
            env = {'PATH': os.environ.get('PATH', ''), 'HOME': directory, 'TMPDIR': directory,
                   'CODEX_HOME': directory, 'CLAUDE_CONFIG_DIR': directory,
                   'DISABLE_AUTOUPDATER': '1', 'DISABLE_TELEMETRY': '1',
                   'CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC': '1'}
            version = subprocess.run([executable, '--version'], cwd=root, env=env, capture_output=True, text=True, timeout=15)
            if version.returncode:
                return [{'name': 'native_version', 'status': 'blocked', 'code': 'capability_unknown'}]
            checks = capability_checks(agent, settings, version.stdout.strip(), native_context=context)
            checks.insert(0, {'name': 'runtime_version', 'status': 'passed', 'version': version.stdout.strip(),
                              'reason': 'Native binary release identified; does not prove session behavior'})
            if agent == 'codex':
                from .permissions import enforcement_checks
                checks += enforcement_checks(settings, context)
                # Only portable selected settings plus the backend choice are passed
                # to this process. Never copy native credentials, hooks or MCP.
                payload = deepcopy(settings)
                for key in ('multi_agent_v2', 'network_proxy'):
                    if key in context.get('features', {}):
                        payload.setdefault('features', {})[key] = context['features'][key]
                atomic_write(root / 'config.toml', tomlkit.dumps(payload).encode())
                parsed = self._codex_read(executable, root, env)
                if parsed is None:
                    checks.append({'name': 'native_parser', 'status': 'blocked', 'code': 'native_rejected',
                                   'reason': 'Isolated config/read did not return a valid configuration'})
                else:
                    for path, value in leaves(settings):
                        actual = get(parsed, path)
                        checks.append({'name': 'native_field_readback', 'path': ['settings', *path],
                                       'status': 'passed' if actual is not MISSING and same(value, actual) else 'blocked',
                                       'code': 'native_readback', 'level': 'native-parser'})
                effective = subprocess.run([executable, 'features', 'list'], cwd=root, env=env,
                                           capture_output=True, timeout=15)
                checks.append({'name': 'native_config_resolution', 'status': 'passed' if effective.returncode == 0 else 'blocked',
                               'code': 'native_config_resolution', 'level': 'native-config',
                               'reason': 'Isolated configuration and referenced permission profile must resolve'})
            # Claude --version/plugin list accept malformed settings too. Do not
            # misrepresent them as schema validation or a session-effect check.
            return checks

    @staticmethod
    def _codex_read(executable, root, env):
        result = NativeProbe._codex_request(executable, root, env, 'config/read', {'includeLayers': False})
        value = result.get('config') if isinstance(result, dict) else None
        return value if isinstance(value, dict) else None

    @staticmethod
    def _codex_request(executable, root, env, method, params):
        process = subprocess.Popen([executable, '--strict-config', 'app-server', '--listen', 'stdio://'],
                                   cwd=root, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL, bufsize=0)
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ)
        buffer = b''
        def send(value):
            process.stdin.write((json.dumps(value) + '\n').encode())
            process.stdin.flush()
        def receive(identifier):
            nonlocal buffer
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                while b'\n' in buffer:
                    line, buffer = buffer.split(b'\n', 1)
                    message = json.loads(line)
                    if message.get('id') == identifier:
                        return message
                if not selector.select(max(0, deadline - time.monotonic())):
                    break
                chunk = os.read(process.stdout.fileno(), 65536)
                if not chunk:
                    break
                buffer += chunk
            return {}
        try:
            send({'id': 1, 'method': 'initialize', 'params': {'clientInfo': {'name': 'claude-config-probe', 'version': '1'}}})
            if 'result' not in receive(1):
                return None
            send({'method': 'initialized'})
            send({'id': 2, 'method': method, 'params': params})
            value = receive(2).get('result')
            return value if isinstance(value, dict) else None
        except (ValueError, OSError):
            return None
        finally:
            selector.close()
            process.stdin.close()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            process.stdout.close()
