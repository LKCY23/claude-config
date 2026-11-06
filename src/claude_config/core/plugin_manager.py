"""One checked transaction for plugin declaration, version pin and native installation."""
from copy import deepcopy
from pathlib import Path
import json
import re

import yaml

from .catalog import CatalogResolver, verify_git
from .deployment_scope import DeploymentScope
from .merge import fingerprint
from .native import parse_json, parse_yaml
from .native_plugins import PluginOperation
from .plugin_locks import (apply_pin, empty_locks, locator, read_locks, validate_locks)
from .registrations import claude_user_config
from .schema import COMMIT, REF, SchemaError, require, validate_catalogs, validate_profile, within
from .transaction import FileChange, FilePlan, UnfinishedRun, file_fingerprint
from .journal import unfinished

_PRERELEASE = r'(?:0|[1-9]\d*|[0-9]*[A-Za-z-][0-9A-Za-z-]*)'
SEMVER = re.compile(r'(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-(' + _PRERELEASE +
                    r'(?:\.' + _PRERELEASE + r')*))?(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?\Z')


def version_order(value):
    m = SEMVER.fullmatch(value)
    require(m is not None, 'Version ordering requires SemVer; an upgrade cannot guess a downgrade')
    pre = m[4]
    return (*map(int, m.group(1, 2, 3)), (1,) if pre is None else (0, tuple(
        (0, int(x)) if x.isdigit() else (1, x) for x in pre.split('.'))))


def release_commit(repo, version):
    """Only published GitHub releases; never treat a mutable branch as a version."""
    from ..fetch_baseline import api
    require(SEMVER.fullmatch(version) is not None, '--version requires an exact SemVer')
    # Search published release tags, not source branches or manifest guesses.
    # GitHub's paginated API is bounded so a missing version cannot hang planning.
    matches = []
    for page in range(1, 21):
        batch = api(f'repos/{repo}/releases?per_page=100&page={page}')
        require(isinstance(batch, list), 'Invalid release response')
        matches.extend(x for x in batch if not x.get('draft') and x.get('tag_name') in (version, 'v' + version))
        if len(batch) < 100:
            break
    require(len(matches) == 1, 'Version requires one published release; use --commit for an explicit unpublished revision')
    tag = matches[0]['tag_name']
    obj = api(f'repos/{repo}/git/ref/tags/{tag}')['object']
    for _ in range(8):
        if obj.get('type') != 'tag':
            break
        require(isinstance(obj.get('sha'), str) and COMMIT.fullmatch(obj['sha']), 'Invalid release tag object')
        obj = api(f'repos/{repo}/git/tags/{obj["sha"]}')['object']
    require(obj.get('type') == 'commit' and isinstance(obj.get('sha'), str) and COMMIT.fullmatch(obj['sha']),
            'Release tag does not resolve to a full commit')
    return obj['sha']


def decision(path, before, after, reason):
    return {'path': path, 'source': {'kind': 'plugin-intent'}, 'target': {'kind': 'profile-and-native'},
            'before_exists': before is not None, 'before': before, 'incoming_exists': True, 'incoming': after,
            'after_exists': True, 'after': after, 'action': 'unchanged' if before == after else 'update',
            'category': 'unchanged' if before == after else 'policy_merge', 'reason': reason,
            'check_status': 'passed', 'policy': 'explicit-plugin-operation', 'policy_origin': 'command'}


class PluginManager:
    def __init__(self, manager, *, fetcher=None, resolve_release=None):
        require(manager.agent == 'claude-code', 'Plugin commands currently support the Claude Code adapter only')
        self.manager = manager
        self.fetcher = fetcher
        self.resolve_release = resolve_release or release_commit
        self.prepared = []

    def context(self):
        m = self.manager
        self.profile_path, self.profile, self.profile_raw, self.profile_expected, self.name = m._profile()
        self.lock_path, self.locks, self.lock_raw, self.lock_expected = read_locks(m, self.name)
        self.state, self.state_expected = m._state()
        self.resolver = CatalogResolver(m)

    def resolve_ref(self, ref, *, registered=False, include_retained=False):
        selected = dict(self.profile.get('resources', {}).get('plugins', {}))
        if include_retained:
            selected.update({key: {} for key, row in self.state.get('resources', {}).items() if row['kind'] == 'plugin'})
        if '@' in ref:
            require(REF.fullmatch(ref) is not None, 'Invalid plugin reference')
            require(registered or ref in selected, 'Plugin is not selected; use plugin install first')
            return ref
        require(re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', ref) is not None, 'Invalid plugin name')
        matches = [r for r in selected if r.split('@')[0] == ref]
        if registered and not matches:
            # Install may add a catalog resource that is not in the Profile yet.
            for alias in self.manager.machine['catalog_roots']:
                catalog = self.resolver._catalog(alias)[3]
                if catalog['resources'].get(ref, {}).get('kind') == 'plugin':
                    matches.append(ref + '@' + alias)
        require(len(matches) == 1, 'Plugin name is missing or ambiguous; specify name@catalog')
        return matches[0]

    def ensure_catalog(self, alias):
        m = self.manager
        catalogs, _, _ = m._read(m.root / 'catalogs.yaml', lambda raw: validate_catalogs(parse_yaml(raw)))
        require(alias in catalogs['catalogs'] and alias in m.machine['catalog_roots'], 'Catalog must be declared and bound')
        declared = catalogs['catalogs'][alias]
        bound = Path(m.machine['catalog_roots'][alias])
        if not bound.exists():
            expected = m.state_root / 'sources' / declared['repo'].replace('/', '--') / declared['commit']
            require(bound.absolute().resolve() == expected.resolve(),
                    'Missing custom catalog checkout must be prepared explicitly; automatic fetching only writes private source cache')
            self.prepare_source({**declared})
        return self.resolver._catalog(alias)

    def prepare_dependencies(self, profile, ref):
        available = {r for rows in profile.get('resources', {}).values() for r in rows}
        queue, seen = [ref], set()
        while queue:
            current = queue.pop()
            if current in seen:
                continue
            seen.add(current)
            name, alias = current.split('@')
            _, _, declared, catalog = self.ensure_catalog(alias)
            item = catalog['resources'].get(name)
            require(item is not None, 'Required resource is not registered: ' + current)
            if current != ref:
                pin = self.locks['plugins'].get(current)
                if pin:
                    apply_pin(item, declared, pin)
                    self.prepare_source(pin['source'])
                elif item['source']['kind'] == 'external':
                    self.prepare_source(locator(item, declared))
            for dependency in item['requires'].get('resources', []):
                require(dependency in available, 'Dependency must be explicitly selected in Profile: ' + dependency)
                queue.append(dependency)

    def definition(self, ref):
        name, alias = ref.split('@')
        repo, directory, declared, catalog = self.ensure_catalog(alias)
        item = catalog['resources'].get(name)
        require(item is not None and item['kind'] == 'plugin', 'Resource is not a registered plugin')
        require(self.manager.agent in item['compatibility'] and item['compatibility'][self.manager.agent]['mode'] == 'native',
                'Plugin has no supported native entry for this agent')
        return deepcopy(item), declared

    def operation(self, ref, previous, desired=None):
        m = self.manager
        executable = Path(m.entry['binding']['executable']).resolve()
        m.sources[executable] = file_fingerprint(executable)
        return PluginOperation({'agent': m.agent, 'root': str(m.native_path.parent.resolve()),
            'user_config': str(claude_user_config(m.entry['binding']).resolve()), 'executable': str(executable),
            'executable_snapshot': m.sources[executable],
            'plugins': [{'ref': ref, 'previous': deepcopy(previous), 'desired': deepcopy(desired or previous)}]})

    def installed(self, ref):
        previous = self.state.get('resources', {}).get(ref, {}).get('native', {}).get('plugin')
        if previous is None:
            return None, None
        operation = self.operation(ref, previous)
        return previous, operation.native(previous)

    def prepare_source(self, source):
        from ..fetch_baseline import prepare_git
        root = self.manager.state_root / 'sources' / source['repo'].replace('/', '--') / source['commit']
        created = (self.fetcher or prepare_git)(root, source['repo'], source['commit'])
        verify_git(root, source['repo'], source['commit'])
        if created:
            self.prepared.append({'repo': source['repo'], 'commit': source['commit'], 'path': str(root)})
        return root.resolve()

    def package_pin(self, ref, item, declared, source, resolution, expected_version=None):
        root = self.prepare_source(source)
        self.resolver._verify(root, source['repo'], source['commit'])
        entry, files, modes, digest = self.resolver._tree(root, within(root, source['path']),
                                                         item['compatibility'][self.manager.agent]['entry'])
        manifest = parse_json(files[entry])
        require(isinstance(manifest.get('name'), str) and isinstance(manifest.get('version'), str), 'Plugin manifest needs name and version')
        require(expected_version is None or manifest['version'] == expected_version, 'Release version and plugin manifest disagree')
        pin = {'catalog': {k: declared[k] for k in ('repo', 'commit', 'path')}, 'source': source,
               'version': manifest['version'], 'source_fingerprint': digest, 'resolution': resolution}
        validate_locks({**empty_locks(self.manager.agent, self.name), 'plugins': {ref: pin}}, self.manager.agent, self.name)
        apply_pin(item, declared, pin)
        return pin

    def plan(self, action, ref, *, version=None, commit=None, enable=False, disabled=False, bindings=None):
        require(action in ('install', 'upgrade', 'enable', 'disable'), 'Unknown plugin operation')
        require(not (version and commit), 'Choose --version or --commit, not both')
        require(not enable or action == 'upgrade', '--enable is only for upgrade')
        require(not disabled or action == 'install', '--disabled is only for install')
        require(action in ('install', 'upgrade') or not (version or commit or bindings), 'Enable/disable cannot select versions or bindings')
        require(commit is None or isinstance(commit, str) and COMMIT.fullmatch(commit), '--commit requires a full 40-character SHA')
        require(version is None or isinstance(version, str) and SEMVER.fullmatch(version), '--version requires exact SemVer')
        require(not bindings or action == 'install', 'Bindings can only be declared during install')
        pending = unfinished(self.manager.state_root, [str(self.manager.native_path.resolve())])
        if pending:
            raise UnfinishedRun(pending)
        self.context()
        require(not self.state or self.state['profile'] == self.name, 'Complete the Profile switch before managing one plugin')
        ref = self.resolve_ref(ref, registered=action == 'install')
        before, actual = self.installed(ref)
        if action != 'install':
            require(before is not None and actual['installed'], 'Plugin is not a managed installation; use install or explicit adoption first')
        if before is not None:
            require(actual['installed'] and actual['marketplace'] and all(actual[k] == before[k] for k in ('version', 'image')),
                    'Installed plugin version or content drifted; use the existing conflict/adoption workflow')
        candidate = deepcopy(self.profile)
        selected = candidate.setdefault('resources', {}).setdefault('plugins', {}).setdefault(ref, {})
        if bindings:
            require(not before, 'Existing plugin bindings require explicit Profile migration')
            selected.setdefault('bindings', {}).update(bindings)
        if action in ('enable', 'disable'):
            selected.setdefault('options', {})['enabled'] = action == 'enable'
            return self.toggle_plan(action, ref, candidate, before, actual)
        item, declared = self.definition(ref)
        # Install without a selector must honor an existing lock, as legacy apply does.
        old_pin = self.locks['plugins'].get(ref)
        if action == 'install' and old_pin and not (version or commit):
            apply_pin(item, declared, old_pin)
            pin = old_pin
            self.prepare_source(pin['source'])
        else:
            source = locator(item, declared)
            if commit:
                source['commit'] = commit
            elif version:
                source['commit'] = self.resolve_release(source['repo'], version)
            pin = self.package_pin(ref, item, declared, source,
                {'kind': 'commit' if commit else 'release' if version else 'catalog',
                 'requested': commit or version or declared['commit']}, version)
        if before:
            if action == 'install':
                require(pin['version'] == before['version'], 'Plugin already installed at another version; use plugin upgrade')
                require(not disabled or not actual['enabled'], 'Plugin is installed and enabled; use plugin disable')
            elif pin['version'] != before['version']:
                require(version_order(pin['version']) > version_order(before['version']), 'Upgrade cannot downgrade; choose a newer pinned version')
            desired_enabled = True if enable else selected.get('options', {}).get('enabled', actual['enabled'])
        else:
            desired_enabled = not disabled
        selected.setdefault('options', {})['enabled'] = desired_enabled
        locks = deepcopy(self.locks)
        # Semantically identical requests retain provenance, for true idempotency.
        if old_pin and all(pin[k] == old_pin[k] for k in ('source', 'version', 'source_fingerprint')):
            pin = old_pin
        locks['plugins'][ref] = pin
        # Old tools must not silently ignore the new lock and reinstall the catalog pin.
        if tuple(map(int, candidate.get('minimum_tool_version', '0.0.0').split('.'))) < (0, 8, 0):
            candidate['minimum_tool_version'] = '0.8.0'
        self.prepare_dependencies(candidate, ref)
        deployment = self.manager.plan(selection=DeploymentScope(resources=(ref,)), prepare=False,
                                       profile_value=candidate, plugin_lock_value=locks)
        return self.combine(action, ref, candidate, locks, deployment, before, pin['version'], desired_enabled)

    def toggle_plan(self, action, ref, candidate, before, actual):
        """Use known native ownership only; unavailable Git sources cannot prevent disabling."""
        m = self.manager
        # Enabling must not silently activate a version different from the declared lock.
        pin = self.locks['plugins'].get(ref)
        if action == 'enable' and not pin:
            catalogs, _, _ = m._read(m.root / 'catalogs.yaml', lambda raw: validate_catalogs(parse_yaml(raw)))
            declared = catalogs['catalogs'].get(ref.split('@')[1])
            require(declared is not None and self.state['resources'][ref]['catalog_commit'] == declared['commit'],
                    'Catalog selection changed since installation; run install/upgrade to verify the selected version first')
        if action == 'enable' and pin:
            require(pin['version'] == before['version'] and self.state['resources'][ref]['source_fingerprint'] == pin['source_fingerprint'],
                    'Installed plugin differs from the selected lock; upgrade/install it first')
        desired = {**before, 'enabled': action == 'enable'}
        previous = {**before, 'enabled': actual['enabled']}
        operation = self.operation(ref, previous, desired)
        state = deepcopy(self.state)
        state['resources'][ref]['native']['plugin'] = desired
        state_change = FileChange(m.state_path, (json.dumps(state, indent=2, ensure_ascii=False) + '\n').encode(),
                                  'managed-baseline', lambda raw: m._validate_state(parse_json(raw)), self.state_expected)
        def guard():
            observed = operation.native(previous)
            require(all(observed[k] == actual[k] for k in ('installed', 'marketplace', 'version', 'image')),
                    'Plugin changed since the usage plan was prepared')
        report = {'ready': True, 'checks': [{'name': 'managed_plugin_identity', 'status': 'passed'},
            {'name': 'effective_session', 'status': 'not_verified', 'reason': 'Existing sessions may need to reload the plugin'}],
            'blockers': [], 'decisions': [], 'activation': [{'resource': ref, 'mode': 'reload_plugins', 'verified': False}],
            'deployment_scope': DeploymentScope(resources=(ref,)).report({ref}, set(), {ref}, set())}
        deployment = FilePlan([state_change], sources=m.sources, scopes=[str(m.native_path.resolve())], report=report,
                              native_operations=[operation], native_index=0, guards=[guard], prepare=False)
        return self.combine(action, ref, candidate, self.locks, deployment, previous, before['version'], desired['enabled'])

    def combine(self, action, ref, candidate, locks, deployment, before, version, enabled):
        m = self.manager
        validate_profile(candidate, expected=m.agent)
        validate_locks(locks, m.agent, self.name)
        declarations = []
        if candidate != self.profile:
            declarations.append(FileChange(self.profile_path, yaml.safe_dump(candidate, allow_unicode=True, sort_keys=False).encode(),
                'plugin-profile', lambda raw: validate_profile(parse_yaml(raw), expected=m.agent), self.profile_expected))
        if locks != self.locks:
            declarations.append(FileChange(self.lock_path, yaml.safe_dump(locks, allow_unicode=True, sort_keys=False).encode(),
                'plugin-lock', lambda raw: validate_locks(parse_yaml(raw), m.agent, self.name), self.lock_expected))
        report = deepcopy(deployment.report)
        summary = {'resource': ref, 'action': action, 'profile': self.name,
            'before': {'version': before['version'], 'enabled': before['enabled']} if before else None,
            'after': {'version': version, 'enabled': enabled}, 'prepared_sources': self.prepared,
            'installation_scope': 'user', 'activation': 'not_verified',
            'source': locks['plugins'].get(ref, {}).get('source'),
            'resolution': locks['plugins'].get(ref, {}).get('resolution')}
        report['plugin_management'] = summary
        report['decisions'].append(decision(['plugins', ref], summary['before'], summary['after'],
                                           'Apply the explicitly requested plugin version and usage state'))
        report['plan_id'] = fingerprint({'deployment': report.get('plan_id'), 'plugin': {k: v for k, v in summary.items() if k != 'prepared_sources'}})
        # Native installation and managed-baseline steps precede declarations. All
        # are in one journal: failure unwinds them in reverse, including Profile/lock.
        return FilePlan([*deployment.changes, *declarations], sources=m.sources, scopes=deployment.scopes, report=report,
            native_operations=deployment.native_operations, steps=[*deployment.steps, *declarations],
            directories=deployment.directories, guards=[*deployment.guards, *self.resolver.guards])

    def inventory(self, ref=None):
        self.context()
        refs = self.profile.get('resources', {}).get('plugins', {})
        retained = {key for key, row in self.state.get('resources', {}).items() if row['kind'] == 'plugin'}
        names = [self.resolve_ref(ref, include_retained=True)] if ref else sorted(set(refs) | retained)
        rows = []
        for name in names:
            row = {'resource': name, 'profile': self.name, 'selected': name in refs,
                'desired_enabled': refs.get(name, {}).get('options', {}).get('enabled'),
                'locked_version': self.locks['plugins'].get(name, {}).get('version'), 'session_verified': False}
            row['selected_version'] = row['locked_version'] if name in refs else None
            row['declaration_verified'] = row['selected_version'] is not None
            if name in refs and not row['selected_version']:
                try:
                    package = self.resolver.resolve(self.profile, {name}, source_only=True)[name]
                    row['selected_version'] = parse_json(package.files[package.entry]).get('version')
                    row['declaration_verified'] = True
                except (ValueError, OSError):
                    pass  # A status query never downloads missing sources.
            try:
                previous, actual = self.installed(name)
                row.update(managed=previous is not None, installed=bool(actual and actual['installed']),
                           installed_version=actual['version'] if actual else None, enabled=actual['enabled'] if actual and actual['installed'] else None)
                row['status'] = 'not_installed' if not row['installed'] else 'in_sync'
                if actual and previous and (not actual['marketplace'] or any(actual[k] != previous[k] for k in ('version', 'image'))):
                    row['status'] = 'drift'
                if row['installed'] and (row['selected_version'] and row['selected_version'] != row['installed_version'] or
                    row['desired_enabled'] is not None and row['desired_enabled'] != row['enabled']):
                    row['status'] = 'out_of_sync'
            except (SchemaError, OSError):
                row.update(status='unverified', installed=None)
            if name not in refs:
                row['status'] = 'retained_unselected' if row.get('installed') else row['status']
            rows.append(row)
        return {'ready': True, 'checks': [], 'decisions': [], 'blockers': [], 'plugins': rows}
