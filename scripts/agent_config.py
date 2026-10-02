#!/usr/bin/env python3
"""Apply explicit Claude Code/Codex plugin selections and host-specific config overlays."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

try:
    import yaml
    import tomlkit
except ImportError as exc:
    raise SystemExit('Use a Python environment with requirements.txt installed: ' + str(exc))

FRAMEWORK = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from deployment import DeploymentMixin
from native_hooks import validate_hooks

HOSTS = ('claude', 'codex')
SECRET_KEYS = {'api_key', 'apikey', 'access_token', 'refresh_token', 'auth_token',
               'password', 'client_secret', 'authorization', 'bearer_token'}


def read_yaml(path):
    value = yaml.safe_load(path.read_text()) or {}
    if not isinstance(value, dict):
        raise ValueError(f'Expected a mapping: {path}')
    return value


def within(root, relative):
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError('Source path escapes its repository: ' + str(relative))
    return path


def run(command):
    result = subprocess.run(command, text=True, capture_output=True, timeout=180)
    if result.returncode:
        raise RuntimeError(f'Command failed: {command[0:3]}\n{result.stderr.strip() or result.stdout.strip()}')
    return result.stdout


def reject_secrets(value, path=()):
    if isinstance(value, dict):
        for key, child in value.items():
            normalized = str(key).lower().replace('-', '_')
            if normalized in SECRET_KEYS or normalized.endswith(('_api_key', '_auth_token', '_access_token', '_refresh_token', '_password', '_secret')):
                raise ValueError('Secret value must remain local: ' + '.'.join((*path, str(key))))
            reject_secrets(child, (*path, str(key)))
    elif isinstance(value, list):
        for child in value:
            reject_secrets(child, path)


def merge_mapping(target, overlay, prefix=()):
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            merge_mapping(target[key], value, (*prefix, key))
        elif isinstance(value, list) and (prefix == ('permissions',) and key in ('allow', 'ask', 'deny') or prefix == ('hooks',)):
            existing = target.get(key, [])
            if not isinstance(existing, list):
                raise ValueError('Cannot merge a native list over a scalar: ' + key)
            target[key] = existing + [item for item in value if item not in existing]
        else:
            target[key] = value


def merge_toml(current, overlay):
    document = tomlkit.parse(current)
    def merge(target, values, prefix=()):
        for key, value in values.items():
            if isinstance(value, dict):
                if key not in target:
                    target[key] = tomlkit.table()
                if not isinstance(target[key], (dict, tomlkit.items.Table)):
                    raise ValueError('Cannot merge a table over an existing scalar: ' + key)
                merge(target[key], value, (*prefix, key))
            elif isinstance(value, list) and prefix == ('skills',) and key == 'config':
                items = [dict(item) for item in target.get(key, [])]
                for item in value:
                    old = next((row for row in items if row.get('path') == item.get('path')), None)
                    if old is None:
                        items.append(item)
                    else:
                        old.update(item)
                array = tomlkit.aot()
                for item in items:
                    table = tomlkit.table(); table.update(item); array.append(table)
                target[key] = array
            elif isinstance(value, list) and prefix == ('hooks',):
                existing = list(target.get(key, []))
                target[key] = existing + [item for item in value if item not in existing]
            else:
                target[key] = value
    merge(document, overlay)
    return tomlkit.dumps(document)


def private_write(target, content, backup_root):
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_symlink():
        raise ValueError('Refusing to replace a symlinked runtime config: ' + str(target))
    if target.exists() and target.read_text() == content:
        return 'unchanged'
    if target.exists():
        backup_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(backup_root, 0o700)
        stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
        backup = backup_root / (stamp + '-' + target.name)
        shutil.copy2(target, backup)
        os.chmod(backup, 0o600)
    mode = target.stat().st_mode & 0o777 if target.exists() else 0o600
    with tempfile.NamedTemporaryFile('w', dir=target.parent, delete=False) as stream:
        stream.write(content)
        temporary = Path(stream.name)
    try:
        os.chmod(temporary, mode)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return 'written'


class Manager(DeploymentMixin):
    def __init__(self, config_dir, host='both', machine=None, runner=run, home=None):
        self.config_dir = Path(config_dir).expanduser().resolve()
        self.machine = machine or {}
        self.declaration = read_yaml(self.config_dir / 'agents.yaml')
        if self.declaration.get('version') != 1:
            raise ValueError('agents.yaml version must be 1')
        if not isinstance(self.declaration.get('agents', {}), dict) or not isinstance(self.declaration.get('catalogs', {}), dict):
            raise ValueError('agents and catalogs must be mappings')
        for name, profile in self.declaration.get('agents', {}).items():
            if name not in HOSTS or not isinstance(profile, dict):
                raise ValueError('Profiles must name a supported native host')
        self.hosts = HOSTS if host == 'both' else (host,)
        self.runner = runner
        self.home = Path(home or Path.home())
        self.backups = Path(self.machine.get('backup_dir', FRAMEWORK / 'backups')).expanduser()

    def write(self, target, content):
        return private_write(target, content, self.backups)

    @staticmethod
    def instruction_block(text):
        start, end = '<!-- claude-config:begin -->', '<!-- claude-config:end -->'
        if start not in text:
            return ''
        return start + text.split(start, 1)[1].split(end, 1)[0] + end

    def updates(self, host):
        # Scripts/roles are prepared before config references or CLI lifecycle events.
        rows = self.file_updates(host)
        for row in (self.instruction_update(host), self.config_update(host)):
            if row:
                rows.append(row)
        return rows

    def file_updates(self, host):
        rows = []
        allowed = ('agents/', 'rules/', 'hooks/') if host == 'claude' else ('agents/', 'rules/', 'hooks/')
        targets = set()
        for item in self.declaration.get('agents', {}).get(host, {}).get('files', []):
            relative = item['target']
            if not relative.startswith(allowed) and not (host == 'codex' and relative == 'hooks.json'):
                raise ValueError('Only explicitly selected role/rule/hook files belong in files: ' + relative)
            target = self.host_root(host) / relative
            within(self.host_root(host), relative)
            if target in targets:
                raise ValueError('Multiple sources own the same target: ' + relative)
            targets.add(target)
            content = within(self.config_dir, item['source']).read_text()
            if target.suffix == '.json':
                value = json.loads(content); reject_secrets(value)
                if relative == 'hooks.json' or relative.startswith('hooks/'):
                    if 'hooks' not in value:
                        raise ValueError('Hook JSON must contain a hooks event mapping')
                    validate_hooks(host, value['hooks'])
                    if relative == 'hooks.json':
                        # Use scoped config rollback and preserve unmanaged events.
                        current = target.read_text() if target.exists() else '{}\n'
                        data = json.loads(current); merge_mapping(data, value)
                        content = current if data == json.loads(current) else json.dumps(data, indent=2) + '\n'
            if target.suffix == '.toml':
                value = tomlkit.parse(content).unwrap(); reject_secrets(value)
                if relative.startswith('agents/'):
                    if not all(isinstance(value.get(k), str) and value[k] for k in ('name', 'description', 'developer_instructions')):
                        raise ValueError('Codex agent requires name, description and developer_instructions')
            if host == 'claude' and relative.startswith('agents/'):
                if not content.startswith('---\n'):
                    raise ValueError('Claude agent requires YAML frontmatter')
                meta = yaml.safe_load(content.split('---', 2)[1]); reject_secrets(meta)
                if not meta.get('name') or not meta.get('description'):
                    raise ValueError('Claude agent requires name and description')
            before = target.read_text() if target.exists() else None
            rows.append({'kind': 'config' if relative == 'hooks.json' else 'file', 'target': target, 'content': content, 'keys': [relative], 'changed': content != before})
        return rows

    def host_root(self, host):
        env = 'CODEX_HOME' if host == 'codex' else 'CLAUDE_CONFIG_DIR'
        return Path(os.environ.get(env) or self.home / ('.codex' if host == 'codex' else '.claude'))

    def catalog_root(self, name):
        entry = self.declaration.get('catalogs', {}).get(name)
        if not entry:
            raise ValueError('Unknown catalog: ' + name)
        repo = entry['repo']
        override = self.machine.get('catalog_roots', {}).get(repo)
        base = Path(override).expanduser() if override else FRAMEWORK / 'catalogs' / repo.replace('/', '--')
        root = within(base, entry.get('path', '.'))
        return root

    def catalog(self, name, host):
        root = self.catalog_root(name)
        names = ['.agents/plugins/marketplace.json', '.claude-plugin/marketplace.json'] if host == 'codex' else ['.claude-plugin/marketplace.json']
        path = next((root / p for p in names if (root / p).is_file()), None)
        if path is None:
            raise ValueError('Catalog not available locally; prepare it first: ' + str(root))
        catalog = json.loads(path.read_text())
        if catalog.get('name') != name:
            raise ValueError('Catalog name differs from selection: ' + name)
        return catalog

    def selected(self, host):
        profile = self.declaration.get('agents', {}).get(host, {})
        result = []
        for selector in profile.get('plugins', []):
            if selector.count('@') != 1:
                raise ValueError('Use explicit plugin@catalog selectors: ' + selector)
            plugin, catalog_name = selector.split('@')
            catalog = self.catalog(catalog_name, host)
            item = next((p for p in catalog['plugins'] if p['name'] == plugin), None)
            if not item:
                raise ValueError('Selected plugin is not registered: ' + selector)
            policy = item.get('policy', {}).get('installation', 'AVAILABLE')
            if policy == 'NOT_AVAILABLE':
                raise ValueError('Selected plugin is unavailable: ' + selector)
            source = item['source']
            if isinstance(source, dict) and source.get('source') in ('url', 'git-subdir'):
                sha = source.get('sha', '')
                if len(sha) != 40 or any(c not in '0123456789abcdef' for c in sha):
                    raise ValueError('Remote selection must have an exact commit pin: ' + selector)
            elif isinstance(source, str):
                within(self.catalog_root(catalog_name), source)
            result.append((selector, catalog_name, item))
        if len({r[0] for r in result}) != len(result):
            raise ValueError('Duplicate plugin selection for ' + host)
        return result

    def config_update(self, host):
        profile = self.declaration.get('agents', {}).get(host, {})
        source = profile.get('config', {}).get('source')
        if not source:
            return None
        source = within(self.config_dir, source)
        if host == 'codex':
            desired = tomlkit.parse(source.read_text()).unwrap()
            target = self.host_root(host) / 'config.toml'
            reject_secrets(desired)
            # Authentication and marketplace state have dedicated runtime owners.
            if any(key in desired for key in ('marketplaces', 'plugins', 'mcp_servers')):
                raise ValueError('Use plugin selections instead of raw plugin/marketplace config')
            for key in ('model', 'model_reasoning_effort', 'model_provider', 'approval_policy', 'sandbox_mode'):
                if key in desired and not isinstance(desired[key], str):
                    raise ValueError('Codex setting must be a string: ' + key)
            current = target.read_text() if target.exists() else ''
            content = merge_toml(current, desired) if desired else current
        else:
            desired = json.loads(source.read_text())
            reject_secrets(desired)
            if any(key in desired for key in ('enabledPlugins', 'extraKnownMarketplaces', 'env', 'mcpServers')):
                raise ValueError('Use plugin selections; credential/connection env stays host-local')
            for key in ('model', 'effortLevel'):
                if key in desired and not isinstance(desired[key], str):
                    raise ValueError('Claude setting must be a string: ' + key)
            target = self.host_root(host) / 'settings.json'
            current = target.read_text() if target.exists() else '{}\n'
            data = json.loads(current)
            merge_mapping(data, desired)
            content = current if data == json.loads(current) else json.dumps(data, indent=2, ensure_ascii=False) + '\n'
        if 'hooks' in desired:
            validate_hooks(host, desired['hooks'])
        return {'kind': 'config', 'target': target, 'content': content, 'keys': list(desired), 'changed': content != current}

    def instruction_update(self, host):
        source = self.declaration.get('agents', {}).get(host, {}).get('instructions', {}).get('source')
        if not source:
            return None
        body = within(self.config_dir, source).read_text().rstrip()
        target = self.host_root(host) / ('AGENTS.md' if host == 'codex' else 'CLAUDE.md')
        current = target.read_text() if target.exists() else ''
        start, end = '<!-- claude-config:begin -->', '<!-- claude-config:end -->'
        if current.count(start) != current.count(end) or current.count(start) > 1:
            raise ValueError('Malformed managed instruction block: ' + str(target))
        block = start + '\n' + body + '\n' + end
        if start in current:
            before, rest = current.split(start, 1)
            _, after = rest.split(end, 1)
            content = before + block + after
        else:
            content = current.rstrip() + ('\n\n' if current.strip() else '') + block + '\n'
        return {'kind': 'instructions', 'target': target, 'content': content, 'keys': ['managed instruction block'], 'changed': content != current}

    def installed(self, host):
        if host == 'claude':
            entries = json.loads(self.runner(['claude', 'plugin', 'list', '--json']))
            return {p['id']: p for p in entries}
        data = json.loads(self.runner(['codex', 'plugin', 'list', '--json']))
        return {p['pluginId']: p for p in data.get('installed', [])}



def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('plan', 'validate', 'apply', 'status', 'diff', 'export', 'rollback'))
    parser.add_argument('--agent', choices=(*HOSTS, 'both'), default='claude')
    parser.add_argument('--config-dir')
    parser.add_argument('--machine-config', type=Path, default=Path.home() / '.claude-config-tool/config.yaml')
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--output', type=Path, help='Explicit output path for export')
    parser.add_argument('--run-id', help='Deployment run to roll back')
    args = parser.parse_args()
    machine = read_yaml(args.machine_config) if args.machine_config.is_file() else {}
    config_dir = args.config_dir or machine.get('config_dir') or '~/claude-config-data'
    try:
        manager = Manager(config_dir, args.agent, machine)
        if args.command == 'status':
            result = manager.status()
        elif args.command == 'rollback':
            result = manager.rollback(args.run_id, args.dry_run)
        elif args.command == 'validate':
            result = manager.preflight()[0]
        elif args.command == 'apply' and not args.dry_run:
            result = manager.apply()
        elif args.command == 'export':
            if args.output is None:
                raise ValueError('Export needs --output; never export a full runtime/auth file')
            result = {}
            for host in manager.hosts:
                update = manager.config_update(host)
                if update:
                    source = manager.declaration['agents'][host]['config']['source']
                    desired = tomlkit.parse(within(manager.config_dir, source).read_text()).unwrap() if host == 'codex' else json.loads(within(manager.config_dir, source).read_text())
                    target = update['target']
                    actual = tomlkit.parse(target.read_text()).unwrap() if host == 'codex' else json.loads(target.read_text())
                    def project(shape, value):
                        return {key: project(child, value.get(key, {})) if isinstance(child, dict) else value.get(key)
                                for key, child in shape.items()}
                    result[host] = project(desired, actual)
            reject_secrets(result)
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(yaml.safe_dump(result, allow_unicode=True, sort_keys=False))
            result = {'exported': str(args.output), 'agents': list(result)}
        else:
            result = manager.plan()
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (ValueError, RuntimeError, OSError) as exc:
        raise SystemExit(str(exc))


if __name__ == '__main__':
    main()
