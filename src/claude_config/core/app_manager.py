"""Optional App preferences through the shared merge/file/recovery executor."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re

import yaml

from .app_fields import (FEATURES, export_paths, registry,
                         native_paths, validate_policies, validate_settings)
from .app_native import AppProbe
from .fields import leaves
from .merge import MISSING, MergePlan, get, same
from .native import parse_json, parse_native, parse_yaml, projection, put, render_native, snapshot_read
from .schema import (as_pointer, closed, mapping, pointer, require, state_directory,
                     validate_profile, version, within)
from .transaction import FileChange, FilePlan, PlanStale
from .machine_profiles import MachineProfiles
from .deployment_scope import DeploymentScope, update_leaves


class AppProfileManager:
    def __init__(self, machine_file, app, probe=None, *, state_root=None):
        require(app in ('codex-app', 'claude-desktop'), 'Explicit app is required')
        self.machine_path = Path(machine_file).absolute()
        self.machine_profiles = MachineProfiles(self.machine_path)
        self.local_machine = self.machine_profiles.local
        self.machine = self.machine_profiles.effective
        require(app in self.machine['apps'], 'Machine has no binding for ' + app)
        self.app, self.probe = app, probe or AppProbe()
        self.sources = dict(self.machine_profiles.sources)
        self.references = {}
        self.root = Path(self.machine['config_dir'])
        self.entry = self.machine['apps'][app]
        self.data_root = Path(self.entry['binding']['data_root'])
        self.application = Path(self.entry['binding']['application'])
        self.state_root = state_directory(self.machine) if state_root is None else Path(state_root)
        if app == 'codex-app':
            require('codex' in self.machine['agents'], 'Codex App needs the verified shared Codex config_root binding')
            self.native_path = Path(self.machine['agents']['codex']['binding']['config_root']) / 'config.toml'
            self.recovery_scopes = [str(self.native_path.resolve()), str(self.data_root.resolve())]
        else:
            self.native_path = self.data_root / 'config.json'
            self.recovery_scopes = [str(self.native_path.resolve()), str((self.data_root / 'configLibrary').resolve())]
        require(len(set(self.recovery_scopes)) == len(self.recovery_scopes), 'App binding scopes must be distinct')
        key = hashlib.sha256(json.dumps([app, *self.recovery_scopes]).encode()).hexdigest()
        self.state_path = self.state_root / 'managed-apps' / (key + '.json')

    def _read(self, path, parser):
        data, expected = snapshot_read(path)
        self.sources[Path(path).absolute()] = expected
        return parser(data), data, expected

    def _validate_profile(self, value):
        validate_profile(value, kind='app', expected=self.app)
        validate_settings(self.app, value.get('settings', {}))
        require(not value.get('bindings'), 'App command binding interfaces are not yet verified')
        validate_policies(self.app, value.get('merge_policy'))
        return value

    def _profile(self, name=None, new=False):
        name = name or self.entry['profile']
        parts = Path(name).parts
        require(len(parts) == 4 and parts[:3] == ('profiles', 'apps', self.app) and Path(name).suffix == '.yaml',
                'Profile must belong to the bound App directory')
        path = within(self.root, name, must_exist=not new)
        self.references[(self.root / name).absolute()] = path
        if new and not path.exists():
            self.sources[path] = {'exists': False}
            return path, {'schema_version': 1, 'app': self.app}, b'', {'exists': False}, name
        value, content, expected = self._read(path, parse_yaml)
        return path, self._validate_profile(value), content, expected, name

    def _validate_state(self, value):
        closed(value, ('schema_version', 'kind', 'app', 'scopes', 'profile', 'managed', 'declared'),
               ('schema_version', 'kind', 'app', 'scopes', 'profile', 'managed', 'declared'), 'App managed state')
        version(value)
        require(value['kind'] == 'app-profile-managed-state' and value['app'] == self.app
                and value['scopes'] == self.recovery_scopes, 'App managed state belongs to another binding')
        require(isinstance(value['profile'], str) and Path(value['profile']).parts[:3] == ('profiles', 'apps', self.app),
                'App managed state has an invalid Profile identity')
        for key in ('managed', 'declared'):
            validate_settings(self.app, value[key])

    def _state(self):
        value, _, expected = self._read(self.state_path, parse_json)
        if expected['exists']:
            self._validate_state(value)
        return value, expected

    def _native(self, keys):
        """Only portable whitelisted values enter the merge/report projection."""
        codec = 'codex' if self.app == 'codex-app' else 'claude-code'
        value, original, expected = self._read(self.native_path, lambda data: parse_native(codec, data))
        if self.app == 'claude-desktop':
            # Desktop reads its deployment choice from this separate file.
            # It is an input only; providers, credentials and mode are never written.
            deployment, _, _ = self._read(self.data_root / 'claude_desktop_config.json', parse_json)
            self.deployment_mode = deployment.get('deploymentMode')
        stores = {'main': (self.native_path, value, original, expected, codec)}
        checks = []
        if self.app == 'codex-app':
            prefs = mapping(value.get('desktop', {}), 'native desktop preferences')
            result = projection(prefs, native_paths(self.app))
            return result, stores, checks
        result = projection(value, [('userThemeMode',), ('locale',)])
        if set(keys) & FEATURES:
            if self.deployment_mode != '3p':
                checks.append({'name': 'claude_deployment_scope', 'status': 'blocked', 'code': 'capability_unknown',
                               'reason': 'The selected feature preferences require an existing third-party Desktop deployment'})
                return result, stores, checks
            library = self.data_root / 'configLibrary'
            metadata, _, _ = self._read(library / '_meta.json', parse_json)
            # Desktop's hybrid reader substitutes bootstrap configuration for
            # appliedId. Do not write/export an inactive local feature store.
            if 'hybridPointer' in metadata:
                checks.append({'name': 'claude_applied_profile', 'status': 'blocked', 'code': 'capability_unknown',
                               'reason': 'Hybrid configuration does not verify a locally applied feature store'})
                return result, stores, checks
            applied = metadata.get('appliedId')
            if not isinstance(applied, str) or not re.fullmatch(r'[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}', applied):
                checks.append({'name': 'claude_applied_profile', 'status': 'blocked', 'code': 'capability_unknown',
                               'reason': 'Desktop has no verifiable existing applied configuration'})
                return result, stores, checks
            path = library / (applied + '.json')
            prefs, original, expected = self._read(path, parse_json)
            if not expected['exists']:
                checks.append({'name': 'claude_applied_profile', 'status': 'blocked', 'code': 'capability_unknown',
                               'reason': 'The applied Desktop configuration file is missing'})
                return result, stores, checks
            result.update(projection(prefs, [(key,) for key in FEATURES]))
            stores['features'] = (path, prefs, original, expected, 'claude-code')
        return result, stores, checks

    def _file_plan(self, merge, changes, checks, operation, native_write=False, *, prepare=True):
        report = merge.report()
        report.update(operation=operation, app=self.app)
        if hasattr(self, 'deployment_scope'):
            report['deployment_scope'] = self.deployment_scope
        report['checks'] += checks
        report['checks'].append({'name': 'gui_effect', 'status': 'not_verified',
                                 'reason': 'Native files are validated; GUI reload, account and policy overrides are not exercised'})
        if native_write:
            try:
                self.probe.assert_stopped(self.application)
                report['checks'].append({'name': 'app_lifecycle', 'status': 'passed',
                                         'reason': 'App must remain stopped until the write completes; restart manually afterwards'})
            except (OSError, ValueError, RuntimeError) as exc:
                checks.append({'name': 'app_lifecycle', 'status': 'blocked', 'code': 'app_running',
                               'reason': str(exc) if isinstance(exc, PlanStale) else 'App lifecycle could not be verified'})
                report['checks'].append(checks[-1])
        for row in checks:
            if row['status'] == 'blocked':
                report['blockers'].append({key: row[key] for key in ('name', 'code', 'path', 'reason') if key in row})
        report['ready'] = not report['blockers']
        def binding_guard():
            for reference, actual in self.references.items():
                if reference.resolve() != actual:
                    raise PlanStale('App Profile source binding changed')
        def step_guard(change):
            if change.owner == 'app-preferences':
                self.probe.assert_stopped(self.application)
        return FilePlan(changes, sources=self.sources, scopes=self.recovery_scopes, report=report,
                        guards=[binding_guard], step_guard=step_guard, prepare=prepare,
                        recovery_guard=(lambda: self.probe.assert_stopped(self.application)) if native_write else None)

    def plan(self, *, profile=None, run_policy=None, resolutions=None, prepare=True, selection=None):
        _, chosen, _, _, name = self._profile(profile)
        selection = selection or DeploymentScope()
        require(profile is None or selection.full, 'Profile switching requires a full deployment')
        refs, fields = selection.select(chosen, app=True)
        require(selection.full or set((run_policy or {}).get('fields', {})) <= fields, 'Run policy is outside the deployment scope')
        declared = chosen.get('settings', {}) if selection.full else selection.settings(chosen, fields)
        settings = deepcopy(declared)
        state, state_expected = self._state()
        require(selection.full or not state or state['profile'] == name, 'Complete the Profile switch before partial deployment')
        self.deployment_scope = selection.report(refs, fields, (), fields)
        native, stores, checks = self._native(set(settings) | (set(state.get('declared', {})) if selection.full else set()))
        validate_policies(self.app, run_policy)
        checks += self.probe.check(self.app, self.application, declared, self.sources)
        selected = {path for path, _ in leaves(settings)}
        for selector, resolution in mapping(resolutions or {}, 'App drift resolutions').items():
            path = pointer(selector)
            require(path[:1] == ('settings',) and path[1:] in selected, 'App drift resolution must name a selected preference')
            closed(resolution, ('target_fingerprint', 'policy'), ('target_fingerprint', 'policy'), 'App drift resolution')
            require(resolution['policy'] in ('source_wins', 'keep_target') and isinstance(resolution['target_fingerprint'], str)
                    and re.fullmatch(r'[0-9a-f]{64}', resolution['target_fingerprint']), 'Invalid App drift resolution')
        scope = [as_pointer(('settings', *path)) for path, _ in leaves(settings)]
        merge = MergePlan({'settings': settings}, {'settings': native},
                          source={'kind': 'app-profile', 'file': name}, destination={'kind': 'app-preferences'},
                          scope=scope, profile_policy=chosen.get('merge_policy'), run_policy=run_policy,
                          machine_policy=self.machine.get('merge_policy'), resolutions=resolutions,
                          baseline={'settings': state['managed'] if selection.full else projection(state['managed'], selected)} if state else None,
                          previous={'settings': state['declared'] if selection.full else projection(state['declared'], selected)} if state else None,
                          number_fields=[as_pointer(('settings', key)) for key in settings
                                         if registry(self.app).get(key, ('',))[0] == 'number'])
        changes, native_write = [], False
        ready = not merge.blockers and not any(row['status'] == 'blocked' for row in checks)
        managed = projection(merge.result['settings'], [path for path, _ in leaves(settings)])
        if ready:
            validate_settings(self.app, managed)
            for store, (path, value, original, expected, codec) in stores.items():
                result = deepcopy(value)
                for field, _ in leaves(settings):
                    item = get(managed, field)
                    if item is MISSING:
                        continue
                    if self.app == 'codex-app':
                        put(result, ('desktop', *field), item)
                    elif (field[0] in FEATURES) == (store == 'features'):
                        put(result, field, item)
                content = render_native(codec, original, result)
                if content != original:
                    native_write = True
                    changes.append(FileChange(path, content, 'app-preferences',
                                              lambda data, codec=codec: parse_native(codec, data), expected))
            if settings or state:
                new_state = {'schema_version': 1, 'kind': 'app-profile-managed-state', 'app': self.app,
                             'scopes': self.recovery_scopes, 'profile': name,
                             'managed': managed if selection.full else update_leaves(state.get('managed', {}), managed),
                             'declared': settings if selection.full else update_leaves(state.get('declared', {}), settings)}
                changes.append(FileChange(self.state_path, (json.dumps(new_state, indent=2, ensure_ascii=False) + '\n').encode(),
                                          'app-managed-baseline', lambda data: self._validate_state(parse_json(data)), state_expected))
            if profile and profile != self.entry['profile']:
                changes.append(self.machine_profiles.switch('apps', self.app, name))
        operation = 'switch' if profile is not None or state and state['profile'] != name else 'apply'
        plan = self._file_plan(merge, changes, checks, operation, native_write, prepare=prepare)
        plan.report['binding'] = {'previous_profile': state.get('profile', self.entry['profile']), 'profile': name}
        return plan

    def export(self, selectors, *, profile=None, run_policy=None):
        require(bool(selectors), 'App export requires explicit field selectors')
        path, chosen, original, expected, name = self._profile(profile, new=True)
        scope = export_paths(self.app, selectors)
        keys = {item[1] for item in scope}
        native, _, checks = self._native(keys)
        incoming = projection(native, [item[1:] for item in scope])
        validate_settings(self.app, incoming)
        validate_policies(self.app, run_policy)
        checks += self.probe.check(self.app, self.application, {key: incoming.get(key) for key in keys}, self.sources)
        merge = MergePlan({'settings': incoming}, chosen, source={'kind': 'app-preferences'},
                          destination={'kind': 'app-profile', 'file': name}, scope=[as_pointer(item) for item in scope],
                          profile_policy=chosen.get('merge_policy'), run_policy=run_policy, machine_policy=self.machine.get('merge_policy'),
                          number_fields=[as_pointer(item) for item in scope if registry(self.app).get(item[1], ('',))[0] == 'number'])
        self._validate_profile(merge.result)
        content = original if original and same(merge.result, chosen) else yaml.safe_dump(merge.result, allow_unicode=True, sort_keys=False).encode()
        changes = [FileChange(path, content, 'app-profile-export', lambda data: self._validate_profile(parse_yaml(data)), expected)]
        return self._file_plan(merge, changes, checks, 'export')

    def recovery_check(self, path):
        if self._native_recovery_path(path):
            self.probe.assert_stopped(self.application)

    def _native_recovery_path(self, path):
        path = Path(path).resolve()
        native = path == self.native_path.resolve()
        if self.app == 'claude-desktop':
            native = native or path.parent == (self.data_root / 'configLibrary').resolve()
        return native

    def recovery_phase_check(self, paths):
        if any(self._native_recovery_path(path) for path in paths):
            self.probe.assert_stopped(self.application)
