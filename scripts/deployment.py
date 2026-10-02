"""Native plugin deployment, source checks, operation journal and scoped rollback."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
from datetime import datetime, timezone

import tomlkit
from native_mcp import MCPMixin

ABSENT = {'__claude_config_absent__': True}
EXCLUDED = {'.git', '__pycache__', '.DS_Store'}


def fingerprint(root):
    root = Path(root)
    if not root.is_dir():
        return None
    digest = hashlib.sha256()
    for path in sorted(root.rglob('*')):
        relative = path.relative_to(root)
        if set(relative.parts) & EXCLUDED or not path.is_file():
            continue
        if not path.resolve().is_relative_to(root.resolve()):
            raise ValueError('Package symlink escapes its root: ' + str(relative))
        digest.update(str(relative).encode() + b'\0' + path.read_bytes())
    return digest.hexdigest()


def parse_config(path, text):
    return tomlkit.parse(text).unwrap() if Path(path).suffix == '.toml' else json.loads(text or '{}')


def changed_leaves(before, after, prefix=()):
    result = []
    for key in set(before) | set(after):
        old, new = before.get(key, ABSENT), after.get(key, ABSENT)
        if isinstance(old, dict) and isinstance(new, dict) and old != ABSENT and new != ABSENT:
            result.extend(changed_leaves(old, new, (*prefix, key)))
        elif old != new:
            result.append({'path': [*prefix, key], 'before': old, 'after': new})
    return result


def get_value(data, keys):
    for key in keys:
        if not isinstance(data, dict) or key not in data:
            return ABSENT
        data = data[key]
    return data


class DeploymentMixin(MCPMixin):
    def state_root(self):
        return Path(self.machine.get('state_dir', self.backups.parent / 'state')).expanduser().resolve()

    def journal_write(self, journal):
        directory = self.state_root() / 'runs'
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(directory, 0o700)
        path = directory / (journal['id'] + '.json')
        temporary = path.with_suffix('.tmp')
        temporary.write_text(json.dumps(journal, ensure_ascii=False, indent=2) + '\n')
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)

    def expected(self, host, catalog_name, entry):
        source = entry['source']
        if isinstance(source, str):
            relative = source
        elif source.get('source') == 'local':
            relative = source['path']
        else:
            raise ValueError('Native managed deployment requires a prepared local pinned source: ' + entry['name'])
        root = (self.catalog_root(catalog_name) / relative).resolve()
        if not root.is_relative_to(self.catalog_root(catalog_name).resolve()):
            raise ValueError('Plugin source escapes the catalog root')
        manifests = ['.codex-plugin/plugin.json', 'plugin.json', '.claude-plugin/plugin.json'] if host == 'codex' else ['.claude-plugin/plugin.json', 'plugin.json']
        path = next((root / name for name in manifests if (root / name).is_file()), None)
        if path is None:
            raise ValueError('Missing plugin manifest: ' + str(root))
        manifest = json.loads(path.read_text())
        if manifest['name'] != entry['name'] or not manifest.get('version'):
            raise ValueError('Plugin identity/version mismatch: ' + entry['name'])
        for field in ['skills', 'agents', 'hooks']:
            values = manifest.get(field, [])
            for value in values if isinstance(values, list) else [values]:
                if not isinstance(value, str):
                    continue
                referenced = (root / value).resolve()
                if not referenced.is_relative_to(root) or not referenced.exists():
                    raise ValueError('Missing/escaping component path: ' + value)
        index = self.catalog_root(catalog_name) / 'catalog.json'
        pin = None
        if index.exists():
            candidate = next((p for p in json.loads(index.read_text())['plugins'] if p['name'] == entry['name']), None)
            if not candidate or candidate['source'] != entry['source']:
                raise ValueError('Generated catalog differs from its canonical source')
            pin = candidate['upstream']['sha']
            if candidate.get('packaging') == 'upstream-submodule':
                actual = subprocess.check_output(['git', '-C', str(root), 'rev-parse', 'HEAD'], text=True).strip()
                dirty = subprocess.check_output(['git', '-C', str(root), 'status', '--porcelain', '--untracked-files=no'], text=True).strip()
                if actual != pin or dirty:
                    raise ValueError('Original submodule pin/content changed: ' + entry['name'])
            else:
                record = json.loads((root / 'UPSTREAM.json').read_text())
                if record['sha'] != pin:
                    raise ValueError('Imported source pin differs from catalog')
                for relative, info in record['files'].items():
                    actual = hashlib.sha256((root / relative).read_bytes()).hexdigest()
                    if actual != info['sha256']:
                        raise ValueError('Preserved upstream file changed: ' + relative)
        return {'root': str(root), 'version': manifest['version'], 'digest': fingerprint(root), 'pin': pin}

    def installed_path(self, host, selector, item):
        if item.get('installPath') or item.get('installedPath'):
            return Path(item.get('installPath') or item['installedPath'])
        plugin, catalog = selector.split('@')
        return self.host_root(host) / 'plugins/cache' / catalog / plugin / item.get('version', '')

    def marketplaces(self, host):
        data = json.loads(self.runner([host, 'plugin', 'marketplace', 'list', '--json']))
        return {m['name']: m for m in data if isinstance(m, dict)} if host == 'claude' else {m['name']: m for m in data.get('marketplaces', [])}

    def preflight(self):
        plan = self.plan()
        inventory = {}
        for host in self.hosts:
            if not self.selected(host):
                inventory[host] = {'installed': {}, 'marketplaces': {}}
                continue
            installed, marketplaces = self.installed(host), self.marketplaces(host)
            for selector, catalog, entry in self.selected(host):
                expected = self.expected(host, catalog, entry)
                old = marketplaces.get(catalog)
                old_root = old.get('installLocation') if host == 'claude' and old else old.get('root') if old else None
                if old_root and Path(old_root).resolve() != self.catalog_root(catalog).resolve():
                    raise ValueError(f'{host} marketplace {catalog} points elsewhere; reconcile before apply')
                item = installed.get(selector)
                if item and item.get('version') != expected['version']:
                    raise ValueError(f'Installed version drift: {selector}. Review the source update and use the native updater before applying this new pin.')
                if item and fingerprint(self.installed_path(host, selector, item)) != expected['digest']:
                    raise ValueError(f'Installed content drift: {selector}. Reconcile the source/cache explicitly; apply will not overwrite an unknown installed version.')
            inventory[host] = {'installed': installed, 'marketplaces': marketplaces}
        for host in self.hosts:
            inventory[host]['mcp'] = self.mcp_preflight(host)
        return plan, inventory

    def plan(self):
        rows = []
        for host in self.hosts:
            plugins = []
            for selector, catalog, entry in self.selected(host):
                expected = self.expected(host, catalog, entry)
                plugins.append({'selector': selector, **expected})
            updates = self.updates(host)
            for update in updates:
                root = self.host_root(host)
                if any(p.is_symlink() for p in (update['target'], *update['target'].parents) if p == root or p.is_relative_to(root)):
                    raise ValueError('Refusing a symlinked managed target: ' + str(update['target']))
            rows.append({'agent': host, 'plugins': [p['selector'] for p in plugins], 'sources': plugins,
                         'catalogs': sorted({s[1] for s in self.selected(host)}), 'mcp': list(self.mcp_definitions(host)),
                         'config': [{'target': str(u['target']), 'keys': u['keys'], 'changed': u['changed']} for u in updates]})
        return rows

    def status(self):
        rows = []
        for host in self.hosts:
            installed = self.installed(host) if self.selected(host) else {}
            for selector, catalog, entry in self.selected(host):
                expected = self.expected(host, catalog, entry)
                item = installed.get(selector, {})
                digest = fingerprint(self.installed_path(host, selector, item)) if item else None
                rows.append({'agent': host, 'plugin': selector, 'installed': bool(item),
                             'enabled': item.get('enabled', False), 'version': item.get('version'),
                             'expected_version': expected['version'], 'source_pin': expected['pin'],
                             'content_matches': bool(digest and digest == expected['digest'])})
            for update in self.updates(host):
                rows.append({'agent': host, 'target': str(update['target']), 'keys': update['keys'],
                             'config_matches': not update['changed']})
            for name in self.mcp_definitions(host):
                rows.append({'agent': host, 'mcp': name, 'registered': self.mcp_fingerprint(host, name) is not None,
                             'connection_verified': False})
        return rows

    def set_plugin_enabled(self, host, selector, enabled):
        if host == 'claude':
            self.runner(['claude', 'plugin', 'enable' if enabled else 'disable', selector, '--scope', 'user', '--json'])
        else:
            target = self.host_root(host) / 'config.toml'
            document = tomlkit.parse(target.read_text() if target.exists() else '')
            if 'plugins' not in document:
                document['plugins'] = tomlkit.table()
            if selector not in document['plugins']:
                document['plugins'][selector] = tomlkit.table()
            document['plugins'][selector]['enabled'] = enabled
            self.write(target, tomlkit.dumps(document))

    def apply(self):
        _, inventory = self.preflight()
        journal = {'id': datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ'),
                   'config_dir': str(self.config_dir), 'roots': {h: str(self.host_root(h).resolve()) for h in self.hosts},
                   'status': 'running', 'operations': []}
        def record(operation):
            journal['operations'].append(operation)
            self.journal_write(journal)
            return operation
        try:
            for host in self.hosts:
                for update in self.updates(host):
                    if not update['changed']:
                        continue
                    target = update['target']
                    before = target.read_text() if target.exists() else None
                    operation = record({'kind': update['kind'], 'agent': host, 'target': str(target),
                                        'before': before if update['kind'] != 'config' else None,
                                        'after': update['content'] if update['kind'] != 'config' else None,
                                        'before_exists': before is not None, 'state': 'pending'})
                    if update['kind'] == 'config':
                        operation['changes'] = changed_leaves(parse_config(target, before or ''), parse_config(target, update['content']))
                        self.journal_write(journal)
                    self.write(target, update['content'])
                    operation['state'] = 'done'
                    self.journal_write(journal)
                for catalog in sorted({s[1] for s in self.selected(host)}):
                    if catalog in inventory[host]['marketplaces']:
                        continue
                    operation = record({'kind': 'marketplace', 'agent': host, 'name': catalog,
                                        'root': str(self.catalog_root(catalog)), 'state': 'pending'})
                    self.runner([host, 'plugin', 'marketplace', 'add', str(self.catalog_root(catalog))])
                    operation['state'] = 'done'
                    self.journal_write(journal)
                for selector, catalog, entry in self.selected(host):
                    expected = self.expected(host, catalog, entry)
                    old = inventory[host]['installed'].get(selector)
                    if old and old.get('enabled'):
                        continue
                    operation = record({'kind': 'plugin', 'agent': host, 'selector': selector,
                                        'expected': expected, 'before_installed': bool(old),
                                        'before_enabled': old.get('enabled', False) if old else False, 'state': 'pending'})
                    if not old:
                        command = ['claude', 'plugin', 'install', selector, '--scope', 'user', '--json'] if host == 'claude' else ['codex', 'plugin', 'add', selector, '--json']
                        self.runner(command)
                        operation['state'] = 'installed'
                        self.journal_write(journal)
                    if not self.installed(host).get(selector, {}).get('enabled'):
                        self.set_plugin_enabled(host, selector, True)
                    operation['state'] = 'done'
                    self.journal_write(journal)
                for name, definition in self.mcp_definitions(host).items():
                    if inventory[host]['mcp'][name]['observed']:
                        continue
                    operation = record({'kind': 'mcp', 'agent': host, 'name': name, 'state': 'pending'})
                    self.mcp_add(host, name, definition)
                    observed = self.mcp_fingerprint(host, name)
                    if observed is None:
                        raise RuntimeError('MCP registration did not persist')
                    operation['observed'] = observed
                    self.mcp_remember(host, name, {'selected': inventory[host]['mcp'][name]['selected'], 'observed': observed})
                    operation['state'] = 'done';self.journal_write(journal)
            status = self.status()
            if any(not r.get('enabled') or not r.get('content_matches') for r in status if 'plugin' in r):
                raise RuntimeError('Native installation/content verification failed')
            if any(not r['config_matches'] for r in status if 'config_matches' in r):
                raise RuntimeError('Managed configuration verification failed')
            journal['status'] = 'complete'
            if journal['operations']:
                self.journal_write(journal)
            return {'run_id': journal['id'] if journal['operations'] else None, 'status': status,
                    'operations': [{k: op[k] for k in ('kind', 'agent', 'state')} for op in journal['operations']]}
        except Exception as exc:
            journal['status'] = 'failed'
            journal['error_type'] = type(exc).__name__
            journal['error'] = str(exc)[:1000]
            self.journal_write(journal)
            raise RuntimeError(f'Partial apply recorded as {journal["id"]}: {exc}. Run status and retry the affected host, or rollback this run.') from None

    def rollback(self, run_id, dry_run=False):
        if not run_id or any(c not in '0123456789TZ' for c in run_id):
            raise ValueError('Use a deployment run id')
        path = self.state_root() / 'runs' / (run_id + '.json')
        journal = json.loads(path.read_text())
        if journal['config_dir'] != str(self.config_dir):
            raise ValueError('Run belongs to another data repository')
        for host, root in journal['roots'].items():
            if str(self.host_root(host).resolve()) != root:
                raise ValueError('Run belongs to another runtime directory')
        result = []
        for operation in reversed(journal['operations']):
            if operation['agent'] not in self.hosts or operation['state'] == 'rolled-back':
                continue
            host, kind = operation['agent'], operation['kind']
            result.append({'agent': host, 'kind': kind, 'target': operation.get('target') or operation.get('selector') or operation.get('name')})
            if dry_run:
                continue
            if kind == 'mcp':
                observed = self.mcp_fingerprint(host, operation['name'])
                if observed:
                    if observed != operation.get('observed'):
                        raise ValueError('MCP changed since deployment; rollback stopped')
                    self.runner([host, 'mcp', 'remove', operation['name']] + (['--scope', 'user'] if host == 'claude' else []))
                self.mcp_remember(host, operation['name'], None)
            elif kind == 'plugin':
                current = self.installed(host).get(operation['selector'])
                if current:
                    if current['version'] != operation['expected']['version'] or fingerprint(self.installed_path(host, operation['selector'], current)) != operation['expected']['digest']:
                        raise ValueError('Plugin changed since deployment; rollback stopped: ' + operation['selector'])
                    if not operation['before_installed']:
                        command = ['claude', 'plugin', 'uninstall', operation['selector'], '--scope', 'user', '--keep-data', '--json'] if host == 'claude' else ['codex', 'plugin', 'remove', operation['selector'], '--json']
                        self.runner(command)
                    else:
                        self.set_plugin_enabled(host, operation['selector'], operation['before_enabled'])
            elif kind == 'marketplace':
                others = [s for s in self.installed(host) if s.endswith('@' + operation['name'])]
                if others:
                    raise ValueError('Marketplace now contains other installed plugins; leave it registered: ' + operation['name'])
                if operation['name'] in self.marketplaces(host):
                    self.runner([host, 'plugin', 'marketplace', 'remove', operation['name']])
            elif kind == 'config':
                target = Path(operation['target'])
                text = target.read_text() if target.exists() else ''
                current = parse_config(target, text)
                document = tomlkit.parse(text) if target.suffix == '.toml' else current
                for change in operation['changes']:
                    actual = get_value(current, change['path'])
                    if actual == change['before']:
                        continue
                    if actual != change['after']:
                        raise ValueError('Managed key changed since deployment; rollback stopped: ' + '.'.join(change['path']))
                    parent = document
                    for key in change['path'][:-1]:
                        parent = parent[key]
                    key = change['path'][-1]
                    if change['before'] == ABSENT:
                        del parent[key]
                    else:
                        parent[key] = change['before']
                if not document and not operation['before_exists']:
                    target.unlink(missing_ok=True)
                else:
                    self.write(target, tomlkit.dumps(document) if target.suffix == '.toml' else json.dumps(document, indent=2, ensure_ascii=False) + '\n')
            else:
                target = Path(operation['target'])
                current = target.read_text() if target.exists() else None
                if kind == 'instructions':
                    if current == operation['after']:
                        if operation['before'] is None:
                            target.unlink(missing_ok=True)
                        else:
                            self.write(target, operation['before'])
                        operation['state'] = 'rolled-back';self.journal_write(journal)
                        continue
                    before_block = self.instruction_block(operation['before'] or '')
                    after_block = self.instruction_block(operation['after'])
                    if self.instruction_block(current or '') != after_block:
                        raise ValueError('Managed instruction block changed since deployment')
                    restored = (current or '').replace(after_block, before_block, 1)
                    if operation['before'] is None and not restored.strip():
                        target.unlink(missing_ok=True)
                    else:
                        self.write(target, restored)
                elif current == operation['after']:
                    if operation['before'] is None:
                        target.unlink(missing_ok=True)
                    else:
                        self.write(target, operation['before'])
                elif current != operation['before']:
                    raise ValueError('Managed file changed since deployment: ' + str(target))
            operation['state'] = 'rolled-back'
            self.journal_write(journal)
        if not dry_run:
            journal['status'] = 'rolled-back' if all(op['state'] == 'rolled-back' for op in journal['operations']) else 'partially-rolled-back'
            self.journal_write(journal)
        return {'run_id': run_id, 'dry_run': dry_run, 'operations': result}
