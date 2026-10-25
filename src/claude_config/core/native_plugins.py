"""Fixed local plugin sources and native installer side effects, without sessions."""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import signal
import socket
import time

from .merge import fingerprint, same
from .native import parse_json, parse_native, render_native, snapshot_read
from .registrations import claude_user_config
from .resource_options import merge_options
from .schema import NAME, SchemaError, closed, mapping, require
from .transaction import atomic_write, directory_fingerprint, file_fingerprint, private_dir
from .execution_context import progress
from .journal import process_identity, owner_status

VERSION = re.compile(r'[0-9]+\.[0-9]+\.[0-9]+(?:-[A-Za-z0-9.-]+)?(?:\+[A-Za-z0-9.-]+)?\Z')
STARTUP_KEYS = ('firstStartTime', 'firstStartVersion', 'machineID', 'opusProMigrationComplete', 'sonnet1m45MigrationComplete',
                'seenNotifications', 'hasResetAutoModeOptInForDefaultOffer', 'migrationVersion', 'userID')


def image_in_use(root):
    # Claude leaves the marker directory after removing the final session lease.
    return any(marker.is_symlink() or not marker.is_dir() or any(marker.iterdir())
               for marker in Path(root).rglob('.in_use'))


class ImageContentMismatch(SchemaError):
    """Installed image carries content the fixed package does not declare."""


def require_image(condition, message):
    """Reject unexpected image content, while marking it safe to unwind."""
    if not condition:
        raise ImageContentMismatch(message)


def image_fingerprint(path):
    value = directory_fingerprint(path)
    require_image(value['exists'] and bool(value['entries']), 'Native plugin image is missing')
    require_image(all('sha256' in entry for entry in value['entries'].values()), 'Native plugin image contains symlinks')
    return fingerprint({name: {'sha256': entry['sha256'], 'executable': bool(entry['mode'] & 0o111)} for name, entry in value['entries'].items()
                        if '.orphaned_at' not in Path(name).parts})


def validate_receipt(value):
    keys = ('selector', 'version', 'image', 'source', 'marketplace', 'market_root', 'manifest', 'manifest_snapshot', 'enabled', 'history')
    closed(value, (*keys, 'retained_versions'), keys, 'native plugin receipt')
    retained = value.get('retained_versions', [])
    require(isinstance(retained, list) and all(isinstance(version, str) and VERSION.fullmatch(version) for version in retained)
            and len(set(retained)) == len(retained), 'Invalid retained native version directories')
    require(isinstance(value['selector'], str) and len(value['selector'].split('@')) == 2
            and all(NAME.fullmatch(part) for part in value['selector'].split('@')), 'Invalid native plugin selector')
    require(value['selector'].split('@')[1] == value['marketplace'], 'Plugin marketplace identity differs')
    require(isinstance(value['version'], str) and VERSION.fullmatch(value['version']) and type(value['enabled']) is bool, 'Invalid plugin version or enabled state')
    require(isinstance(value['image'], str) and re.fullmatch(r'[0-9a-f]{64}', value['image']), 'Invalid plugin content pin')
    for key in ('source', 'market_root', 'manifest'):
        require(isinstance(value[key], str) and Path(value[key]).is_absolute(), 'Plugin source identity must be absolute')
    require(isinstance(value['history'], dict) and value['version'] in value['history'], 'Plugin receipt requires source history')
    for version, source in value['history'].items():
        require(isinstance(version, str) and VERSION.fullmatch(version), 'Invalid historical plugin version')
        closed(source, ('source', 'image'), ('source', 'image'), 'historical plugin source')
        require(isinstance(source['source'], str) and Path(source['source']).is_absolute()
                and isinstance(source['image'], str) and re.fullmatch(r'[0-9a-f]{64}', source['image']), 'Invalid historical source pin')
    require(value['history'][value['version']] == {key: value[key] for key in ('source', 'image')}, 'Active plugin source differs from history')
    from .recovery import snapshot
    snapshot(value['manifest_snapshot'])


def marketplace_name(alias):
    safe = re.sub(r'[^a-z0-9-]', '-', alias.lower())[:40]
    return 'claude-config-' + safe + '-' + hashlib.sha256(alias.encode()).hexdigest()[:8]


def cache_version(agent, version):
    # Claude keeps the original SemVer in inventory, but encodes '+' in paths.
    return version.replace('+', '-') if agent == 'claude-code' else version


def validate_native_row(row, operation):
    from .recovery import snapshot
    closed(row, ('kind', 'path', 'owner', 'action', 'operation_id', 'definition', 'before', 'after', 'status', 'backups', 'safe_recovery', 'recovery_error', 'cleanup_safe', 'step', 'plugin_actions', 'child'),
           ('kind', 'path', 'owner', 'action', 'operation_id', 'definition', 'before', 'status', 'backups', 'safe_recovery'), 'native operation journal')
    if 'cleanup_safe' in row:
        require(type(row['cleanup_safe']) is bool, 'Invalid native cleanup classification')
    if 'recovery_error' in row:
        closed(row['recovery_error'], ('code', 'error_type', 'reason'), ('code', 'error_type', 'reason'), 'native recovery error')
        require(row['recovery_error']['code'] == 'native_recovery_blocked' and all(isinstance(value, str) for value in row['recovery_error'].values()),
                'Invalid native recovery error')
    require(row['kind'] == 'native-plugin' and row['path'] == str(operation.root) and row['owner'] == 'native-plugins'
            and row['action'] in ('native_plugin', 'native_plugin_restore') and type(row['safe_recovery']) is bool, 'Invalid native journal identity')
    require(isinstance(row['operation_id'], str) and re.fullmatch(r'[0-9a-f]{64}', row['operation_id'])
            and row['status'] in ('started', 'applied', 'rolled_back', 'recovery_conflict'), 'Invalid native journal state')
    for world in (row['before'], *([row['after']] if 'after' in row else [])):
        closed(world, ('files', 'directories'), ('files', 'directories'), 'native snapshot')
        require(set(world['files']) == {str(path) for path in operation.paths}
                and set(world['directories']) == {str(path) for path in operation.directories}, 'Native snapshot scope differs from the operation')
        for value in world['files'].values():
            snapshot(value)
        for path, value in world['directories'].items():
            closed(value, ('exists', 'resolved', 'entries'), ('exists', 'resolved', 'entries'), 'native directory snapshot')
            require(type(value['exists']) is bool and value['resolved'] == str(Path(path).resolve()), 'Invalid native directory identity')
            for name, entry in mapping(value['entries'], 'native directory entries').items():
                require(not Path(name).is_absolute() and '..' not in Path(name).parts, 'Native snapshot entry escapes its root')
                mapping(entry, 'native directory entry')
                if entry.get('kind') == 'symlink':
                    closed(entry, ('kind', 'target'), ('kind', 'target'), 'native symlink snapshot')
                    require(isinstance(entry['target'], str), 'Invalid native symlink target')
                else:
                    snapshot(entry)
    required = {path for path, value in row['before']['files'].items() if value['exists']}
    allowed = required | set(historical_backup_paths(row))
    require(required <= set(mapping(row['backups'], 'native backups')) <= allowed,
            'Native backup coverage differs from the recorded files')


def historical_backup_paths(row):
    if row['definition']['agent'] != 'claude-code':
        return {}
    root = Path(row['definition']['root']) / 'backups'
    entries = row['before']['directories'][str(root)]['entries']
    return {str(root / name): value for name, value in entries.items()
            if re.fullmatch(r'\.claude\.json\.backup\.[0-9]+', name) and 'sha256' in value}


def backup_snapshot(row, path):
    path = str(path)
    return row['before']['files'][path] if path in row['before']['files'] else historical_backup_paths(row)[path]


def masked(value, paths):
    result = deepcopy(value)
    for path in paths:
        node = result
        parents = []
        for key in path[:-1]:
            if not isinstance(node, dict) or key not in node:
                break
            parents.append((node, key))
            node = node[key]
        else:
            if isinstance(node, dict):
                node.pop(path[-1], None)
                for parent, key in reversed(parents):
                    if parent[key] == {}:
                        del parent[key]
    return result


class PluginOperation:
    def __init__(self, definition, runner=None):
        closed(definition, ('agent', 'root', 'user_config', 'executable', 'executable_snapshot', 'plugins'),
               ('agent', 'root', 'user_config', 'executable', 'executable_snapshot', 'plugins'), 'native plugin operation')
        require(definition['agent'] in ('claude-code', 'codex') and isinstance(definition['plugins'], list) and definition['plugins'], 'Invalid plugin operation agent or selection')
        self.definition = deepcopy(definition)
        self.agent = definition['agent']
        self.root = Path(definition['root'])
        require(self.root.is_absolute() and Path(definition['executable']).is_absolute(), 'Native plugin binding must be absolute')
        self.config = self.root / ('settings.json' if self.agent == 'claude-code' else 'config.toml')
        self.user_config = Path(definition['user_config']) if definition['user_config'] else None
        self.plugins = definition['plugins']
        for item in self.plugins:
            closed(item, ('ref', 'desired', 'previous', 'adoption', 'replacement'), ('ref', 'desired', 'previous'), 'native plugin action')
            validate_receipt(item['desired'])
            if item['previous']:
                validate_receipt(item['previous'])
            if 'adoption' in item:
                closed(item['adoption'], ('registration_root',), ('registration_root',), 'plugin adoption')
                require(isinstance(item['adoption']['registration_root'], str) and Path(item['adoption']['registration_root']).is_absolute()
                        and item['previous'] is None, 'Adoption requires an absolute existing registration and no prior ownership')
            if 'replacement' in item:
                replacement = item['replacement']
                closed(replacement, ('registration_root', 'version', 'image'), ('registration_root', 'version', 'image'), 'plugin replacement')
                require(item['previous'] is None and 'adoption' not in item and isinstance(replacement['registration_root'], str)
                        and Path(replacement['registration_root']).is_absolute(), 'Replacement requires an explicit prior registration and no known ownership')
                require(isinstance(replacement['version'], str) and VERSION.fullmatch(replacement['version'])
                        and isinstance(replacement['image'], str) and re.fullmatch(r'[0-9a-f]{64}', replacement['image']), 'Invalid replacement version/image pin')
                require(replacement['version'] != item['desired']['version'] and replacement['version'] in item['desired']['history']
                        and item['desired']['history'][replacement['version']]['image'] == replacement['image'], 'Replacement requires a different version and exact fixed recovery source')
            desired = item['desired']
            require(desired['market_root'] == str(self.root / '.claude-config-resources/marketplaces' / desired['marketplace']), 'Plugin source marketplace escapes the bound root')
            require(Path(desired['source']).resolve().is_relative_to((Path(desired['market_root']) / 'packages').resolve()), 'Plugin source escapes prepared packages')
            require(Path(desired['manifest']).resolve().is_relative_to(Path(desired['market_root']).resolve()), 'Plugin manifest escapes marketplace')
            for version, historical in desired['history'].items():
                require(Path(historical['source']).resolve().is_relative_to((Path(desired['market_root']) / 'packages').resolve()), 'Historical source escapes prepared packages')
            require(len({cache_version(self.agent, version) for version in desired['history']}) == len(desired['history']),
                    'Plugin versions collide in native cache directory encoding')
            require(not set(desired.get('retained_versions', [])).intersection(cache_version(self.agent, version) for version in desired['history']),
                    'Retained native image collides with a managed plugin version')
        self.paths = [self.config]
        if self.agent == 'claude-code':
            require(self.user_config is not None and self.user_config.is_absolute(), 'Claude plugin operation needs its verified user config')
            self.paths += [self.user_config, self.root / 'plugins/known_marketplaces.json', self.root / 'plugins/installed_plugins.json']
            self.paths += [self.cache_parent(item['desired']) / cache_version(self.agent, version) / '.orphaned_at' for item in self.plugins for version in item['desired']['history']]
        self.directories = [self.cache_parent(item['desired']) for item in self.plugins]
        if self.agent == 'claude-code':
            self.directories.append(self.root / 'backups')
        self.runner = runner
        self.identifier = fingerprint(definition)

    def cache_parent(self, receipt):
        return self.root / 'plugins/cache' / receipt['marketplace'] / receipt['selector'].split('@')[0]

    def world(self):
        return {'files': {str(path): file_fingerprint(path) for path in self.paths},
                'directories': {str(path): directory_fingerprint(path) for path in self.directories}}

    def preflight(self):
        require(file_fingerprint(self.definition['executable']) == self.definition['executable_snapshot'], 'Native installer binary changed')
        for path in self.paths:
            file_fingerprint(path)
        for item in self.plugins:
            old = item['previous'] or item.get('replacement')
            if old and old['version'] != item['desired']['version']:
                root = self.cache_parent(item['desired']) / cache_version(self.agent, old['version'])
                require(not image_in_use(root), 'Existing plugin image is in use; stop the session before upgrading')

    def _json(self, path, documents=None):
        if documents is not None:
            return documents[path]
        return parse_json(snapshot_read(path)[0])

    def _config(self, documents=None):
        return documents[self.config] if documents is not None else parse_native(self.agent, snapshot_read(self.config)[0])

    def installed_selectors(self, documents=None):
        if self.agent == 'claude-code':
            inventory = self._json(self.root / 'plugins/installed_plugins.json', documents)
            require(not inventory or inventory.get('version') == 2, 'Unknown Claude inventory format')
            entries = mapping(inventory.get('plugins', {}), 'native plugin inventory')
            require(all(isinstance(rows, list) and all(isinstance(row, dict) for row in rows) for rows in entries.values()),
                    'Unknown Claude plugin scope inventory')
            return {selector: rows for selector, rows in entries.items() if rows}
        config = self._config(documents)
        return {selector: [] for selector in mapping(config.get('plugins', {}), 'native plugins')}

    def registered_root(self, market, documents=None):
        if self.agent == 'claude-code':
            known = self._json(self.root / 'plugins/known_marketplaces.json', documents).get(market, {})
            root = mapping(known, 'native marketplace registration').get('installLocation')
        else:
            config = self._config(documents)
            known = mapping(config.get('marketplaces', {}), 'native marketplaces').get(market, {})
            root = mapping(known, 'native marketplace registration').get('source')
        require(isinstance(root, str) and Path(root).is_absolute(), 'Adoption requires a registered local marketplace checkout')
        return str(Path(root).resolve())

    def native(self, receipt, registration_root=None, documents=None):
        config = self._config(documents)
        selector, market = receipt['selector'], receipt['marketplace']
        allowed_roots = {Path(receipt['market_root']).resolve()}
        if registration_root is not None:
            allowed_roots.add(Path(registration_root).resolve())
        if self.agent == 'claude-code':
            declared_market = mapping(config.get('extraKnownMarketplaces', {}), 'native marketplace declarations').get(market)
            if declared_market:
                declared_source = mapping(mapping(declared_market, 'native marketplace entry').get('source', {}), 'native marketplace source')
                if not registration_root or declared_source.get('source') == 'directory':
                    require(declared_source.get('source') == 'directory' and isinstance(declared_source.get('path'), str)
                            and Path(declared_source['path']).resolve() in allowed_roots, 'Claude settings marketplace source differs from the fixed registration')
            known = self._json(self.root / 'plugins/known_marketplaces.json', documents).get(market)
            if known is not None:
                mapping(known, 'native marketplace registration')
                mapping(known.get('source', {}), 'native marketplace registered source')
            installed = self._json(self.root / 'plugins/installed_plugins.json', documents)
            require(not installed or type(installed.get('version')) is int and installed['version'] == 2, 'Unknown Claude inventory format')
            rows = mapping(installed.get('plugins', {}), 'native plugin inventory').get(selector, [])
            require(isinstance(rows, list) and all(isinstance(row, dict) for row in rows), 'Unknown Claude plugin scope inventory')
            users = [row for row in rows if row.get('scope') == 'user']
            require(len(users) <= 1, 'Ambiguous native user plugin inventory')
            enabled = mapping(config.get('enabledPlugins', {}), 'native plugin enablement').get(selector, True)
            item = users[0] if users else None
            source = known.get('installLocation') if known else None
            source_type = known.get('source', {}).get('source') if known else None
            if item:
                version = item.get('version')
                require(isinstance(version, str) and VERSION.fullmatch(version), 'Invalid installed plugin version')
                cache = self.cache_parent(receipt) / cache_version(self.agent, version)
                require(Path(item.get('installPath', '')).resolve() == cache, 'Native plugin inventory points to another image')
        else:
            known = mapping(config.get('marketplaces', {}), 'native marketplaces').get(market)
            if known is not None:
                mapping(known, 'native marketplace registration')
            source = known.get('source') if known else None
            source_type = known.get('source_type') if known else None
            parent = self.cache_parent(receipt)
            versions = [path.name for path in parent.iterdir() if path.is_dir()] if parent.exists() else []
            require(len(versions) <= 1 and all(VERSION.fullmatch(version) for version in versions), 'Ambiguous Codex cached version')
            declared = mapping(config.get('plugins', {}), 'native plugins').get(selector)
            if declared is not None:
                mapping(declared, 'native plugin settings')
            item = declared if declared is not None and versions else None
            enabled = declared.get('enabled', True) if declared is not None else True
            version = versions[0] if item is not None else None
            cache = parent / version if version else None
        require(type(enabled) is bool, 'Invalid native plugin enabled state')
        if known:
            require(isinstance(source, str) and Path(source).resolve() in allowed_roots
                    and (source_type in ('directory', 'local') or registration_root is not None),
                    'Native marketplace registration differs from the selected fixed source')
        return {'installed': item is not None, 'version': item.get('version') if self.agent == 'claude-code' and item else version if self.agent == 'codex' else None,
                'image': image_fingerprint(cache) if item is not None else None, 'enabled': enabled, 'marketplace': known is not None}

    def install_environment(self, home):
        """Environment for every native installer call, install and restore alike."""
        env = {'PATH': os.environ.get('PATH', ''), 'HOME': str(home), 'TMPDIR': str(home),
               'CODEX_HOME': str(self.root), 'DISABLE_AUTOUPDATER': '1', 'DISABLE_TELEMETRY': '1', 'CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC': '1'}
        if self.agent == 'claude-code':
            if self.user_config == self.root / '.claude.json':
                env['CLAUDE_CONFIG_DIR'] = str(self.root)
            else:
                require(self.root.name == '.claude' and self.user_config == self.root.parent / '.claude.json', 'Claude default native layout is not consistent')
                env['HOME'] = str(self.root.parent)
            # A fixed package already contains its runtime artifacts; installing
            # development dependencies would add a second, unrelated image.
            env['npm_config_omit'] = 'dev'
        return env

    def _call(self, args, work):
        self.preflight()
        if self.runner:
            self.runner(self, args, work)
            return
        home = private_dir(work / 'native-home')
        env = self.install_environment(home)
        if getattr(self, 'checkpoint', None) and getattr(self, '_active_row', None) is not None:
            row = self._active_row
            row['child'] = {'command': args[:3], 'status': 'launching'}
            self.checkpoint(row)
            process = subprocess.Popen([self.definition['executable'], *args], cwd=home, env=env,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
            try:
                row['child'].update(owner={**(process_identity(process.pid) or {'pid': process.pid, 'start': None}), 'host': socket.gethostname()}, status='running')
                self.checkpoint(row)
                started = time.monotonic()
                while True:
                    try:
                        process.communicate(timeout=5)
                        break
                    except subprocess.TimeoutExpired:
                        progress('等待原生插件命令：' + ' '.join(args[:3]))
                        if time.monotonic() - started >= 40:
                            raise subprocess.TimeoutExpired(args[:3], 40)
            except BaseException:
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGKILL)
                process.communicate()
                raise
            finally:
                row['child'].update(status='exited', returncode=process.returncode)
                self.checkpoint(row)
            result = process
        else:
            result = subprocess.run([self.definition['executable'], *args], cwd=home, env=env, capture_output=True, timeout=40)
        if result.returncode:
            raise RuntimeError('Native plugin CLI operation failed')

    def enable(self, receipt, work):
        if self.agent == 'claude-code':
            self._call(['plugin', 'enable' if receipt['enabled'] else 'disable', receipt['selector'], '--scope', 'user', '--json'], work)
        else:
            content, _ = snapshot_read(self.config)
            value = parse_native('codex', content)
            value.setdefault('plugins', {}).setdefault(receipt['selector'], {})['enabled'] = receipt['enabled']
            atomic_write(self.config, render_native('codex', content, value), file_fingerprint(self.config).get('mode', 0o600))

    def install(self, receipt, work, update=False):
        verb = 'update' if update and self.agent == 'claude-code' else 'install' if self.agent == 'claude-code' else 'add'
        self._call(['plugin', verb, receipt['selector'], *(['--scope', 'user'] if self.agent == 'claude-code' else []), '--json'], work)

    def remove(self, receipt, work):
        self._call(['plugin', 'uninstall' if self.agent == 'claude-code' else 'remove', receipt['selector'],
                    *(['--scope', 'user', '--keep-data'] if self.agent == 'claude-code' else []), '--json'], work)

    def capture(self, run_dir, index):
        self._restore_work = run_dir
        row = {'kind': 'native-plugin', 'path': str(self.root), 'owner': 'native-plugins', 'action': 'native_plugin',
               'operation_id': self.identifier, 'definition': self.definition, 'before': self.world(), 'status': 'started',
               'backups': {}, 'safe_recovery': False}
        paths = [*self.paths, *(Path(path) for path in historical_backup_paths(row))]
        for number, path in enumerate(paths):
            if backup_snapshot(row, path)['exists']:
                backup = run_dir / f'{index}.native.{number}.before'
                content, expected = snapshot_read(path)
                require(expected == backup_snapshot(row, path), 'Native metadata changed while backing up')
                atomic_write(backup, content)
                row['backups'][str(path)] = str(backup)
        return row

    def perform(self, row, work):
        self._active_row = row
        row['plugin_actions'] = []
        for item in self.plugins:
            desired = item['desired']
            adoption = item.get('adoption', item.get('replacement', {}))
            actual = self.native(desired, adoption.get('registration_root'))
            action = {'selector': desired['selector'], 'action': 'fresh_install' if not actual['installed'] else
                      'upgrade' if actual['version'] != desired['version'] else
                      'enable_change' if actual['enabled'] != desired['enabled'] else 'unchanged', 'status': 'started'}
            row['plugin_actions'].append(action)
            if getattr(self, 'checkpoint', None):
                self.checkpoint(row)
            if not actual['marketplace'] or self.registered_root(desired['marketplace']) != desired['market_root']:
                if actual['marketplace'] and self.agent == 'codex':
                    # Codex refuses add-with-same-name from a different source.
                    # Marketplace removal keeps plugin declarations and images;
                    # both metadata steps are covered by this one guarded journal.
                    self._call(['plugin', 'marketplace', 'remove', desired['marketplace']], work)
                self._call(['plugin', 'marketplace', 'add', desired['market_root']], work)
            if not actual['installed'] or actual['version'] != desired['version']:
                self.install(desired, work, update=actual['installed'])
            if self.native(desired)['enabled'] != desired['enabled']:
                self.enable(desired, work)
            action['status'] = 'completed'
            if getattr(self, 'checkpoint', None):
                self.checkpoint(row)

    def unchanged(self):
        for item in self.plugins:
            desired = item['desired']
            actual = self.native(desired, item.get('adoption', item.get('replacement', {})).get('registration_root'))
            if not actual['installed'] or not actual['marketplace'] or any(actual[k] != desired[k] for k in ('version', 'image', 'enabled')):
                return False
            if self.registered_root(desired['marketplace']) != desired['market_root']:
                return False
        return True

    def inspect_interrupted(self, row):
        """Accept only complete known native states, never unknown partial images."""
        child = row.get('child', {})
        require(child.get('status') != 'launching', 'Native child launch outcome is unknown; inspect the process before recovery')
        if child.get('status') == 'running':
            require(owner_status(child.get('owner')) == 'exited', 'Native child is still active or cannot be verified')
        for item in self.plugins:
            desired = item['desired']
            prior = self.prior_receipt(row, desired)
            actual = self.native(desired, item.get('adoption', item.get('replacement', {})).get('registration_root'))
            if actual['installed']:
                choices = [desired] + ([prior] if prior else [])
                require(any(all(actual[key] == candidate[key] for key in ('version', 'image', 'enabled')) for candidate in choices),
                        'Interrupted installer left an unknown plugin state')
            else:
                require(prior is None, 'Interrupted upgrade has not restored its previous image')
        self.observe(row)
        require(row['safe_recovery'], 'Interrupted installer side effects cannot be safely attributed')

    def observe(self, row):
        row['after'] = self.world()
        try:
            self.accept_effects(row)
            row['safe_recovery'] = True
        except ImageContentMismatch:
            # The installer added content of its own. Nothing outside this
            # operation changed, so the run can still be unwound exactly.
            row['safe_recovery'] = False
            row['cleanup_safe'] = True
        except (ValueError, OSError):
            row['safe_recovery'] = False

    def _before(self, row, path):
        return Path(row['backups'][str(path)]).read_bytes() if str(path) in row['backups'] else b''

    def accept_effects(self, row, removed_backups=()):
        selectors = [item['desired']['selector'] for item in self.plugins]
        markets = sorted({item['desired']['marketplace'] for item in self.plugins})
        before = parse_native(self.agent, self._before(row, self.config))
        after = parse_native(self.agent, snapshot_read(self.config)[0])
        claims = [('enabledPlugins', selector) for selector in selectors] + [('extraKnownMarketplaces', name) for name in markets] if self.agent == 'claude-code' else [('plugins', selector) for selector in selectors] + [('marketplaces', name) for name in markets]
        require(same(masked(before, claims), masked(after, claims)), 'Native installer changed unrelated configuration')
        if self.agent == 'claude-code':
            for path, claims in ((self.user_config, [(key,) for key in STARTUP_KEYS]),
                                 (self.root / 'plugins/known_marketplaces.json', [(name,) for name in markets])):
                require(same(masked(parse_json(self._before(row, path)), claims), masked(self._json(path), claims)), 'Native installer changed unrelated metadata')
            path = self.root / 'plugins/installed_plugins.json'
            def remaining(value):
                value = deepcopy(value)
                require(not value or value.get('version') == 2, 'Unknown plugin inventory format')
                value.pop('version', None)
                entries = value.get('plugins', {})
                for selector in selectors:
                    if selector in entries:
                        entries[selector] = [entry for entry in entries[selector] if entry.get('scope') != 'user']
                        if not entries[selector]:
                            del entries[selector]
                if entries == {}:
                    value.pop('plugins', None)
                return value
            require(same(remaining(parse_json(self._before(row, path))), remaining(self._json(path))), 'Native installer changed another plugin scope or inventory')
            directory = str(self.root / 'backups')
            old, new = row['before']['directories'][directory]['entries'], row['after']['directories'][directory]['entries']
            require(all(new.get(name) == value or name not in new and
                        (name in removed_backups or str(Path(directory) / name) in row['backups']) for name, value in old.items())
                    and all(re.fullmatch(r'\.claude\.json\.backup\.[0-9]+', name) for name in new.keys() - old.keys()), 'Native installer changed unrelated backup files')
        unexpected_image = False
        for item in self.plugins:
            receipt = item['desired']
            entries = row['after']['directories'][str(self.cache_parent(receipt))]['entries']
            allowed_versions = {cache_version(self.agent, version) for version in receipt['history']}
            retained = set(receipt.get('retained_versions', []))
            unexpected_image |= not all(name.split('/')[0] in retained or name.split('/')[0] in allowed_versions and 'sha256' in value
                                        for name, value in entries.items())
            before = row['before']['directories'][str(self.cache_parent(receipt))]['entries']
            unchanged_action = any(action.get('selector') == receipt['selector'] and action.get('action') == 'unchanged'
                                   for action in row.get('plugin_actions', []))
            require(not unchanged_action or before == entries, 'Native installer changed an unmodified plugin image')
            keep = lambda values: {name: value for name, value in values.items() if name.split('/')[0] in retained}
            # Retained images were never ours: their content cannot be rebuilt,
            # so a change there stays a conflict rather than a cleanup.
            require(keep(before) == keep(entries), 'Native installer changed retained unowned historical images')
        require_image(not unexpected_image, 'Native installer wrote outside declared plugin images')

    def verify(self, row):
        require(row['safe_recovery'], 'Native plugin side effects exceed the planned ownership')
        for item in self.plugins:
            desired = item['desired']
            actual = self.native(desired)
            require(actual['installed'] and actual['marketplace'] and all(actual[key] == desired[key] for key in ('version', 'image', 'enabled')),
                    'Native installed plugin differs from the exact selected source or usage state')

    def restore(self, row):
        try:
            # An install can also fail because the installer added content of its
            # own. That run is still exactly recorded, and every addition sits
            # inside an image this run created, so it unwinds like a clean one.
            cleanup = bool(row.get('cleanup_safe')) and not row.get('safe_recovery')
            require((row.get('safe_recovery') or cleanup) and self.world() == row.get('after'), 'Native state changed after this operation')
            preserve_images = set()
            if cleanup:
                # Shared metadata is restored once, but image recovery is per
                # plugin. Existing unchanged images must not block fresh installs.
                for item in self.plugins:
                    desired = item['desired']
                    key = str(self.cache_parent(desired))
                    old = row['before']['directories'][key]['entries']
                    new = row['after']['directories'][key]['entries']
                    previous = self.prior_receipt(row, desired)
                    if all(new.get(name) == value for name, value in old.items()):
                        preserve_images.add(desired['selector'])
                    else:
                        require(previous is not None and any(action.get('selector') == desired['selector'] and action.get('action') == 'upgrade'
                                for action in row.get('plugin_actions', [])), 'Cleanup cannot reconstruct an unrecorded change to an existing image')
            for item in self.plugins:
                parent = self.cache_parent(item['desired'])
                before_versions = {name.split('/')[0] for name in row['before']['directories'][str(parent)]['entries']}
                if parent.exists():
                    for version_root in parent.iterdir():
                        if version_root.is_dir() and version_root.name not in before_versions:
                            require(not image_in_use(version_root), 'Run-created image is still in use; native restoration is blocked')
            for path, backup in row['backups'].items():
                require(file_fingerprint(backup)['sha256'] == backup_snapshot(row, path)['sha256'], 'Native recovery backup changed')
            work = Path(next(iter(row['backups'].values()))).parent if row['backups'] else self._restore_work
            # Temporarily point the already-owned manifest to its pinned historical
            # body. The native installer reconstructs images; cache bytes are never
            # copied back as an installation.
            manifests = {}
            try:
                for item in reversed(self.plugins):
                    desired = item['desired']
                    if cleanup and desired['selector'] in preserve_images:
                        continue
                    previous = self.prior_receipt(row, desired)
                    try:
                        actual = self.native(desired, item.get('adoption', item.get('replacement', {})).get('registration_root'))
                    except ImageContentMismatch:
                        require(cleanup, 'Unclassified image mismatch during recovery')
                        actual = {'installed': True}
                    if previous and actual['installed'] and all(actual.get(key) == previous[key] for key in ('version', 'image')):
                        # Adoption only changed registration/usage metadata. Restore
                        # those backed-up bytes without reinstalling an existing image.
                        continue
                    if actual['installed']:
                        self.remove(desired, work)
                    if previous:
                        manifest_path = Path(desired['manifest'])
                        content, _ = snapshot_read(manifest_path)
                        manifests.setdefault(manifest_path, content)
                        parent_before = row['before']['directories'][str(self.cache_parent(desired))]
                        versions = {name.split('/')[0] for name in parent_before['entries']}
                        order = sorted(version for version in previous['history'] if cache_version(self.agent, version) in versions and version != previous['version']) + [previous['version']]
                        for version in order:
                            historical = previous['history'][version]
                            require(image_fingerprint(historical['source']) == historical['image'], 'Historical plugin source changed')
                            document = parse_json(content)
                            plugin = next(entry for entry in document['plugins'] if entry['name'] == desired['selector'].split('@')[0])
                            plugin['source'] = './' + Path(historical['source']).relative_to(Path(desired['market_root'])).as_posix()
                            atomic_write(manifest_path, (json.dumps(document, indent=2) + '\n').encode())
                            self.install({**previous, 'version': version}, work)
                        if self.native(previous)['enabled'] != previous['enabled']:
                            self.enable(previous, work)
            finally:
                for path, content in manifests.items():
                    atomic_write(path, content)
            for item in self.plugins:
                receipt = item['desired']
                parent = self.cache_parent(receipt)
                before = row['before']['directories'][str(parent)]['entries']
                versions_before = {name.split('/')[0] for name in before}
                if parent.exists():
                    for version_root in parent.iterdir():
                        if not version_root.is_dir():
                            continue
                        version = version_root.name
                        if version not in versions_before:
                            matching = [pin for pin in receipt['history'] if cache_version(self.agent, pin) == version]
                            require(len(matching) == 1, 'Recovery cannot remove an unknown plugin version directory')
                            if not cleanup:
                                require(image_fingerprint(version_root) == receipt['history'][matching[0]]['image'], 'Run-created retired image changed')
                            require(not image_in_use(version_root), 'Retired native image is still in use')
                            # Uninstall first; then remove only the unchanged,
                            # unused image this run created and native GC retained.
                            shutil.rmtree(version_root)
                        else:
                            marker = version_root / '.orphaned_at'
                            relative = version + '/.orphaned_at'
                            if marker.exists() and relative not in before:
                                marker.unlink()
            for path in self.paths:
                before = row['before']['files'][str(path)]
                if before['exists']:
                    atomic_write(path, Path(row['backups'][str(path)]).read_bytes(), before['mode'])
                else:
                    path.unlink(missing_ok=True)
            if self.agent == 'claude-code':
                root = self.root / 'backups'
                before = row['before']['directories'][str(root)]['entries']
                for path, expected in historical_backup_paths(row).items():
                    current = file_fingerprint(path)
                    if current != expected:
                        require(not current['exists'] and path in row['backups'], 'Historical native backup cannot be restored')
                        atomic_write(Path(path), Path(row['backups'][path]).read_bytes(), expected['mode'])
                for name in directory_fingerprint(root)['entries'].keys() - before.keys():
                    require(re.fullmatch(r'\.claude\.json\.backup\.[0-9]+', name), 'Unexpected recovery backup')
                    (root / name).unlink()
            for path in self.directories:
                if not row['before']['directories'][str(path)]['exists'] and path.exists():
                    for directory in sorted((p for p in path.rglob('*') if p.is_dir()), key=lambda p: len(p.parts), reverse=True):
                        directory.rmdir()
                    path.rmdir()
            require(self.world() == row['before'], 'Native recovery did not restore the recorded state')
            row.pop('recovery_error', None)
            row['status'] = 'rolled_back'
        except (ValueError, OSError, RuntimeError, subprocess.SubprocessError, StopIteration) as exc:
            row['status'] = 'recovery_conflict'
            row['recovery_error'] = {'code': 'native_recovery_blocked', 'error_type': type(exc).__name__,
                                     'reason': str(exc) if isinstance(exc, SchemaError) else 'Native reconstruction or file restoration failed; preserve the current state and inspect the recovery journal'}

    def prior_receipt(self, row, desired):
        config = parse_native(self.agent, self._before(row, self.config))
        selector = desired['selector']
        if self.agent == 'claude-code':
            inventory = parse_json(self._before(row, self.root / 'plugins/installed_plugins.json'))
            users = [entry for entry in inventory.get('plugins', {}).get(selector, []) if entry.get('scope') == 'user']
            require(len(users) <= 1, 'Ambiguous recorded user plugin scope')
            version = users[0]['version'] if users else None
            enabled = config.get('enabledPlugins', {}).get(selector, True)
        else:
            versions = {name.split('/')[0] for name in row['before']['directories'][str(self.cache_parent(desired))]['entries']}
            require(len(versions) <= 1, 'Ambiguous recorded Codex version')
            version = next(iter(versions)) if selector in config.get('plugins', {}) and versions else None
            enabled = config.get('plugins', {}).get(selector, {}).get('enabled', True)
        if version is None:
            return None
        require(version in desired['history'], 'Recorded installed version has no fixed recovery source')
        return {**desired, 'version': version, 'enabled': enabled, **desired['history'][version]}


class PluginRestoreOperation(PluginOperation):
    def __init__(self, original, run_dir):
        super().__init__(original['definition'])
        self.original = deepcopy(original)
        self._restore_work = run_dir
        self.identifier = fingerprint({'restore': original['operation_id'], 'after': original.get('after')})

    def preflight(self):
        super().preflight()
        require((self.original.get('safe_recovery') or self.original.get('cleanup_safe')) and self.world() == self.original.get('after'), 'Native recovery state differs from the recorded result')
        for path, backup in self.original['backups'].items():
            require(file_fingerprint(backup)['sha256'] == backup_snapshot(self.original, path)['sha256'], 'Native recovery backup changed')

    def capture(self, run_dir, index):
        row = super().capture(run_dir, index)
        row['action'] = 'native_plugin_restore'
        return row

    def perform(self, row, work):
        # Restoration invokes the same native installers as forward execution.
        operation = PluginOperation(self.definition)
        operation._restore_work = work
        operation.checkpoint = getattr(self, 'checkpoint', None)
        operation._active_row = row
        original = deepcopy(self.original)
        operation.restore(original)
        require(original['status'] == 'rolled_back', original.get('recovery_error', {}).get('reason', 'Native plugin recovery could not restore the recorded state'))

    def verify(self, row):
        require(row['safe_recovery'] and self.world() == self.original['before'], 'Native plugin restoration did not verify')

    def observe(self, row):
        row['after'] = self.world()
        removed = set()
        if self.agent == 'claude-code':
            key = str(self.root / 'backups')
            removed = self.original['after']['directories'][key]['entries'].keys() - self.original['before']['directories'][key]['entries'].keys()
        try:
            self.accept_effects(row, removed_backups=removed)
            row['safe_recovery'] = True
        except (ValueError, OSError):
            row['safe_recovery'] = False

    def restore(self, row):
        operation = PluginOperation(self.definition)
        operation._restore_work = self._restore_work
        operation.checkpoint = getattr(self, 'checkpoint', None)
        operation._active_row = row
        operation.restore(row)


class PluginPlanner:
    def __init__(self, resources):
        self.resources, self.manager = resources, resources.manager
        self.items, self.markets = [], {}
        self.operation = None

    def add(self, ref, item):
        package, agent = item['package'], self.manager.agent
        runtime = package.item['requires'].get('runtime', {})
        require(not set(item['slots']) - {runtime.get('binding')}, 'Plugin dependency rendering requires an explicit packaging adapter')
        entry = '.claude-plugin/plugin.json' if agent == 'claude-code' else '.codex-plugin/plugin.json'
        require(package.root.is_dir() and package.entry == entry, 'Plugin requires a complete native package and root manifest')
        manifest = parse_json(item['files'][entry])
        require(not any(set(Path(path).parts).intersection(('.orphaned_at', '.in_use')) for path in item['files']), 'Plugin package contains reserved native runtime metadata')
        name, version = manifest.get('name'), manifest.get('version')
        require(isinstance(name, str) and NAME.fullmatch(name) and isinstance(version, str) and VERSION.fullmatch(version), 'Plugin manifest requires a native name and exact SemVer version')
        alias = ref.split('@')[1]
        old = self.resources.previous.get(ref, {}).get('native', {}).get('plugin')
        adoption_selector = getattr(self.manager, 'adopt_plugins', {}).get(ref)
        replacement = getattr(self.manager, 'replace_plugins', {}).get(ref)
        require(not (adoption_selector or replacement) or old is None, 'Plugin is already managed; adoption/replacement is only for existing unowned installations')
        selector = old['selector'] if old else adoption_selector or (replacement['selector'] if replacement else name + '@' + marketplace_name(alias))
        require(isinstance(selector, str) and len(selector.split('@')) == 2 and selector.split('@')[0] == name
                and all(NAME.fullmatch(part) for part in selector.split('@')), 'Adoption selector must match the native plugin name')
        market = selector.split('@')[1]
        root = self.resources._target(self.manager.native_path.parent, '.claude-config-resources/marketplaces/' + market)
        source = self.resources._target(root, 'packages/' + name + '/' + item['rendered_fingerprint'])
        item['cache'] = source
        self.resources.records[ref]['cache'] = str(source)
        path = root / ('.claude-plugin/marketplace.json' if agent == 'claude-code' else '.agents/plugins/marketplace.json')
        history = deepcopy(old['history']) if old else {}
        if replacement:
            require(replacement['version'] != version, 'Replacement must select a distinct native version; same-version content changes are blocked')
            recovery_source = self.resources._target(root, 'packages/' + name + '/' + replacement['image'])
            self.resources.directories.add(replacement['root'])
            self.resources._directory(ref, recovery_source, replacement['files'], replacement['modes'])
            history[replacement['version']] = {'source': str(recovery_source), 'image': replacement['image']}
        require(old is None or old['selector'] == name + '@' + market, 'Plugin native identity changed; explicit replacement is required')
        require(version not in history or history[version]['image'] == item['rendered_fingerprint'], 'Plugin version reused for different content; publish a distinct native version')
        history[version] = {'source': str(source), 'image': item['rendered_fingerprint']}
        options = package.selection.get('options', {})
        usage = merge_options(self.resources, ref, {'enabled': options['enabled']} if 'enabled' in options else {},
                              {'enabled': old['enabled']} if old else {})
        receipt = {'selector': name + '@' + market, 'version': version, 'image': item['rendered_fingerprint'], 'source': str(source),
                   'marketplace': market, 'market_root': str(root), 'manifest': str(path), 'manifest_snapshot': {'exists': False},
                   'enabled': usage.get('enabled', True), 'history': history}
        if old and old.get('retained_versions'):
            receipt['retained_versions'] = deepcopy(old['retained_versions'])
        require(not any(item['desired']['selector'] == receipt['selector'] for item in self.items), 'Multiple resources select the same native plugin')
        self.items.append({'ref': ref, 'desired': receipt, 'previous': old})
        if market not in self.markets:
            content, expected = snapshot_read(path)
            self.manager.sources[path] = expected
            previous = [record['native']['plugin'] for record in self.resources.previous.values() if record['native'].get('plugin', {}).get('marketplace') == market]
            require(not expected['exists'] or previous and all(item['manifest_snapshot'] == expected for item in previous), 'Generated marketplace has unknown ownership or external changes')
            document = parse_json(content) if content else {'name': market, 'owner': {'name': 'claudespace'}, 'plugins': []}
            require(document.get('name') == market and isinstance(document.get('plugins'), list), 'Invalid generated native marketplace')
            self.markets[market] = {'path': path, 'document': document, 'expected': expected}
        document = self.markets[market]['document']
        entry = {'name': name, 'source': './' + source.relative_to(root).as_posix(), 'description': package.item['description']}
        if agent == 'codex':
            entry.update(category='Productivity', policy={'installation': 'AVAILABLE', 'authentication': 'ON_USE'})
        entries = document['plugins']
        entries[:] = [row for row in entries if row['name'] != name]
        entries.append(entry)
        entries.sort(key=lambda row: row['name'])
        self.resources.records[ref]['native']['plugin'] = receipt

    def finish(self):
        if not self.items:
            return
        from .transaction import FileChange
        for market, item in self.markets.items():
            content = (json.dumps(item['document'], indent=2, ensure_ascii=False) + '\n').encode()
            desired = {'exists': True, 'sha256': hashlib.sha256(content).hexdigest(), 'mode': item['expected'].get('mode', 0o600)}
            self.resources.changes.append(FileChange(item['path'], content, 'native-plugin-source', parse_json, item['expected']))
            for record in self.resources.records.values():
                plugin = record['native'].get('plugin')
                if plugin and plugin['marketplace'] == market:
                    plugin['manifest_snapshot'] = desired
        executable = Path(self.manager.entry['binding']['executable']).resolve()
        definition = {'agent': self.manager.agent, 'root': str(self.manager.native_path.parent.resolve()),
                      'user_config': str(claude_user_config(self.manager.entry['binding']).resolve()) if self.manager.agent == 'claude-code' else None,
                      'executable': str(executable), 'executable_snapshot': file_fingerprint(executable), 'plugins': self.items}
        operation = PluginOperation(definition)
        self.manager.sources[executable] = definition['executable_snapshot']
        documents = {}
        for path in operation.paths:
            content, expected = snapshot_read(path)
            require(path not in self.manager.sources or self.manager.sources[path] == expected, 'Native metadata changed during planning')
            self.manager.sources[path] = expected
            if path == operation.config:
                documents[path] = parse_native(operation.agent, content)
            elif path.name in ('known_marketplaces.json', 'installed_plugins.json'):
                documents[path] = parse_json(content)
        installed = operation.installed_selectors(documents)
        adopted = getattr(self.manager, 'adopt_plugins', {})
        replacements = getattr(self.manager, 'replace_plugins', {})
        for item in self.items:
            ref, receipt, previous = item['ref'], item['desired'], item['previous']
            replacement = replacements.get(ref)
            registration = operation.registered_root(receipt['marketplace'], documents) if ref in adopted or replacement else None
            actual = operation.native(receipt, registration, documents)
            managed_market = any(record['native'].get('plugin', {}).get('marketplace') == receipt['marketplace'] for record in self.resources.previous.values())
            require(not actual['marketplace'] or managed_market or registration, 'Native marketplace has unknown management ownership')
            require(not managed_market or actual['marketplace'], 'Managed native marketplace registration was removed')
            parent = operation.cache_parent(receipt)
            current = directory_fingerprint(parent)
            if not previous:
                if registration:
                    expected = replacement or receipt
                    if not (actual['marketplace'] and actual['installed'] and all(actual[key] == expected[key] for key in ('version', 'image'))):
                        self.resources._block(ref, 'replacement_target_mismatch' if replacement else 'adoption_content_mismatch',
                                              'Existing plugin differs from the explicit replacement recovery pin' if replacement else 'Existing plugin version or files differ; adoption cannot perform an upgrade')
                        continue
                    versions = {name.split('/')[0] for name in current['entries']}
                    active_version = expected['version']
                    retained = sorted(versions - {cache_version(self.manager.agent, active_version)})
                    require(not replacement or not retained, 'Replacement cannot modify unowned historical images')
                    if retained:
                        receipt['retained_versions'] = retained
                        self.resources._decision(ref, ['historical_images'], retained, retained, 'residual',
                                                 'Keep unowned historical images unchanged; do not copy them into the fixed source or claim their contents')
                    claimed = {entry['desired']['selector'] for entry in self.items if entry['ref'] in adopted or entry['ref'] in replacements or entry['previous']}
                    affected = {selector for selector in installed if selector.endswith('@' + receipt['marketplace'])}
                    require(affected <= claimed, 'Marketplace adoption would change the source of an unselected installed plugin')
                    require(all(not rows or all(row.get('scope') == 'user' for row in rows) for selector, rows in installed.items() if selector in affected),
                            'Marketplace adoption cannot change a project-scope plugin source')
                    if 'enabled' not in self.resources.rendered[ref]['package'].selection.get('options', {}):
                        receipt['enabled'] = actual['enabled']
                    if replacement:
                        prefix = cache_version(self.manager.agent, active_version) + '/'
                        local_modes = {name[len(prefix):]: value['mode'] for name, value in current['entries'].items()
                                       if name.startswith(prefix) and name != prefix + '.orphaned_at'}
                        require(local_modes == replacement['modes'], 'Recovery source file modes differ from the current native image')
                        item['replacement'] = {'registration_root': registration, 'version': active_version, 'image': expected['image']}
                    else:
                        item['adoption'] = {'registration_root': registration}
                else:
                    require(not any(selector.split('@')[0] == receipt['selector'].split('@')[0] for selector in installed),
                            'Same-name plugin is already installed; explicitly adopt its exact selector to avoid duplicate installation')
                    require(not actual['installed'] and not current['entries'], 'Existing native plugin has unknown ownership')
            else:
                keys = ('version', 'image') if ref in self.resources.conflicts.restore else ('version', 'image', 'enabled')
                require(actual['installed'] and all(actual[key] == previous[key] for key in keys), 'Native plugin version, content or enabled state drifted')
                versions = {name.split('/')[0] for name in current['entries']}
                retained = set(previous.get('retained_versions', []))
                require(previous['version'] == receipt['version'] or not versions.intersection(retained),
                        'Unowned historical images remain; review native cache cleanup before upgrading this plugin')
                for version in versions:
                    if version in retained:
                        continue
                    matching = [pin for pin in previous['history'] if cache_version(self.manager.agent, pin) == version]
                    require(len(matching) == 1 and image_fingerprint(parent / version) == previous['history'][matching[0]]['image'], 'Historical native plugin image has unknown content')
                for historical in previous['history'].values():
                    source = Path(historical['source'])
                    require(image_fingerprint(source) == historical['image'], 'Historical prepared plugin source drifted')
                    self.resources.directories.add(source)
                    for path in source.rglob('*'):
                        if path.is_file():
                            _, expected = snapshot_read(path)
                            self.manager.sources[path] = expected
            before_identity = {key: previous[key] for key in ('selector', 'version', 'image')} if previous else None
            after_identity = {key: receipt[key] for key in ('selector', 'version', 'image')}
            if registration:
                before_identity = {'selector': receipt['selector'], 'version': actual['version'], 'image': actual['image'], 'market_root': registration}
                after_identity['market_root'] = receipt['market_root']
            self.resources._decision(ref, ['native_installation'], before_identity,
                                     after_identity, 'replace_existing' if replacement else 'adopt_existing' if registration else 'unchanged' if previous and previous['image'] == receipt['image'] else 'native_update' if previous else 'native_install',
                                     'Update the explicit existing plugin version; retain its exact reconstructable prior source for native recovery' if replacement else 'Keep the existing selector and installed files; rebind its marketplace to the verified fixed source' if registration else 'Install exact prepared package through the native user-scope installer; no model calls or native trust')
            self.resources._decision(ref, ['options', 'enabled'], actual['enabled'] if previous or registration else None, receipt['enabled'],
                                     'unchanged' if (previous or registration) and actual['enabled'] == receipt['enabled'] else 'set_enabled' if previous or registration else 'native_default' if 'enabled' not in self.resources.rendered[ref]['package'].selection.get('options', {}) else 'set_enabled',
                                     'Omitted enabled preserves known state; a new installation has a native default')
        # Adoption inputs become part of the operation identity and saved journal.
        operation = PluginOperation({**definition, 'plugins': self.items})
        self.operation = operation
