"""New Agent Profile entry point. All mutations use the common checked file plan."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import subprocess

import yaml

from .fields import leaves, lookup, selected_paths, validate_native_result, validate_settings, version_tuple
from .merge import MergePlan, MISSING, fingerprint, get, same
from .native import NativeProbe, parse_json, parse_native, parse_yaml, projection, put, render_native, snapshot_read
from .schema import (SchemaError, as_pointer, closed, mapping, pointer, require, state_directory,
                     validate_profile, version, within)
from .transaction import FileChange, FilePlan, PlanStale, file_fingerprint
from .machine_profiles import MachineProfiles
from .resources import ResourcePlan, validate_resource_state
from .resource_options import validate_resource_policy
from .permissions import permission_dependencies


class ProfileManager:
    def __init__(self, machine_file, agent, probe=None, *, state_root=None):
        require(agent in ('claude-code', 'codex'), 'Explicit agent_type is required')
        self.machine_path = Path(machine_file).absolute()
        self.machine_profiles = MachineProfiles(self.machine_path)
        self.local_machine = self.machine_profiles.local
        self.machine = self.machine_profiles.effective
        require(agent in self.machine['agents'], 'Machine has no binding for ' + agent)
        self.agent, self.probe = agent, probe or NativeProbe()
        self.sources = dict(self.machine_profiles.sources)
        self.references = {}
        self.root = Path(self.machine['config_dir'])
        self.entry = self.machine['agents'][agent]
        self.native_path = Path(self.entry['binding']['config_root']) / ('settings.json' if agent == 'claude-code' else 'config.toml')
        self.state_root = state_directory(self.machine) if state_root is None else Path(state_root)
        key = hashlib.sha256(str(self.native_path.resolve()).encode()).hexdigest()
        self.state_path = self.state_root / 'managed' / (key + '.json')

    def _read(self, path, parser):
        content, expected = snapshot_read(path)
        self.sources[Path(path).absolute()] = expected
        return parser(content), content, expected

    def _profile(self, name=None, new=False):
        name = name or self.entry['profile']
        parts = Path(name).parts
        require(len(parts) == 4 and parts[:3] == ('profiles', 'agents', self.agent) and Path(name).suffix == '.yaml',
                'Profile must belong to the bound agent directory')
        path = within(self.root, name, must_exist=not new)
        self.references[(self.root / name).absolute()] = path
        if new and not path.exists():
            self.sources[path] = {'exists': False}
            return path, {'schema_version': 1, 'agent_type': self.agent}, b'', {'exists': False}, name
        value, content, expected = self._read(path, parse_yaml)
        validate_profile(value, expected=self.agent)
        validate_settings(self.agent, value.get('settings', {}))
        return path, value, content, expected, name

    def _state(self):
        if not self.state_path.exists():
            self.sources[self.state_path] = {'exists': False}
            return {}, {'exists': False}
        value, _, expected = self._read(self.state_path, parse_json)
        self._validate_state(value)
        return value, expected

    def _validate_state(self, value):
        closed(value, ('schema_version', 'kind', 'agent_type', 'native_path', 'profile', 'managed', 'declared', 'resources', 'bindings'),
               ('schema_version', 'kind', 'agent_type', 'native_path', 'profile', 'managed', 'declared'), 'managed state')
        version(value)
        require(value['kind'] == 'profile-managed-state' and value['agent_type'] == self.agent,
                'Unknown or old managed state; converter/recovery is separate')
        require(value['native_path'] == str(self.native_path.resolve()), 'Managed state belongs to a different binding')
        require(isinstance(value['profile'], str) and Path(value['profile']).parts[:3] == ('profiles', 'agents', self.agent),
                'Managed state has an invalid Profile identity')
        validate_resource_state(value.get('resources', {}))
        bound = value.get('bindings', {})
        mapping(bound, 'managed command bindings')
        refs = {arg['resource']: {} for entry in bound.values() for arg in entry.get('args', []) if isinstance(arg, dict)}
        validate_profile({'schema_version': 1, 'agent_type': self.agent, 'bindings': bound, 'resources': {'hooks': refs}})
        generated = {pointer(path)[1:] for path in bound}
        validate_settings(self.agent, value['managed'], generated=generated)
        validate_settings(self.agent, value['declared'], generated=generated)

    def _validate_policies(self, profile, run_policy=None, resources_selected=True):
        for origin, policy in (('profile', profile.get('merge_policy', {})), ('run', run_policy or {})):
            for path, action in policy.get('fields', {}).items():
                parsed = pointer(path)
                if parsed[:1] == ('resources',):
                    require(resources_selected or origin == 'profile', 'Run resource policies are not applied during ordinary settings export')
                    validate_resource_policy(self.agent, profile, path, action)
                    continue
                field = lookup(self.agent, parsed[1:]) if parsed[:1] == ('settings',) else None
                require(field is not None, 'Policy target is not a registered field: ' + path)
                require(action != 'append_unique', 'No selected ordinary Agent list has set semantics')
                require(action != 'replace' or field.kind in ('strings', 'picker'), 'replace requires an array field')

    def _preflight(self, profile, settings, context, *, run_policy=None, resources_selected=True):
        checks = []
        self._validate_policies(profile, run_policy, resources_selected)
        if settings or resources_selected and any(profile.get('resources', {}).values()):
            executables = [self.entry['binding']['executable']]
            app = self.machine.get('apps', {}).get('codex-app') if self.agent == 'codex' else None
            if app:
                backend = app['binding'].get('backend')
                if backend:
                    executables.append(backend)
                else:
                    checks.append({'name': 'app_backend', 'status': 'blocked', 'code': 'capability_unknown',
                                   'reason': 'Bound Codex App requires its backend path for shared Agent configuration checks'})
            for executable in dict.fromkeys(executables):
                actual = Path(executable).resolve()
                try:
                    expected = file_fingerprint(actual)
                    require(expected['exists'], 'Native executable does not exist')
                    self.sources[actual] = expected
                    backend_context = {'features': {key: val for key, val in context.get('features', {}).items()
                                                    if key in ('multi_agent_v2', 'network_proxy')}} if isinstance(context.get('features', {}), dict) else {}
                    for row in self.probe(self.agent, actual, settings, backend_context):
                        checks.append({**row, 'executable': executable, 'resolved_executable': str(actual)})
                        registrations = any(profile.get('resources', {}).get(group) for group in ('plugins', 'mcp_servers', 'hooks', 'skills'))
                        if resources_selected and registrations and row['name'] == 'runtime_version' and row['status'] == 'passed':
                            minimum = (2, 1, 278) if self.agent == 'claude-code' else (0, 160, 0)
                            supported = version_tuple(row['version']) >= minimum
                            checks.append({'name': 'registration_capability', 'status': 'passed' if supported else 'blocked',
                                           'code': 'capability_unknown', 'executable': executable,
                                           'reason': 'Registration adapter baseline is ' + '.'.join(map(str, minimum)),
                                           'level': 'documented-config-interface'})
                except (OSError, ValueError, subprocess.SubprocessError) as exc:
                    checks.append({'name': 'native_capability', 'status': 'blocked', 'code': 'capability_unknown',
                                   'executable': executable, 'reason': 'Native capability check failed', 'error_type': type(exc).__name__})
        return checks

    def _file_plan(self, merge, changes, checks, operation, resources=None, *, prepare=True):
        report = merge.report()
        report['operation'] = operation
        report['agent_type'] = self.agent
        report['checks'] += checks
        if resources is not None:
            report['decisions'] += resources.decisions
            if hasattr(resources, 'inventory'):
                report['resource_inventory'] = resources.inventory
            if hasattr(resources, 'conflicts'):
                report['resource_conflicts'] = resources.conflicts.reviews
                report['kept_resources'] = sorted(resources.conflicts.kept)
                report['synchronization'] = 'local_changes_retained' if resources.conflicts.kept else 'selected_plan'
                report['plan_id'] = fingerprint({'merge': report['plan_id'], 'resource_conflicts': report['resource_conflicts'],
                                                 'kept_resources': report['kept_resources']})
            if hasattr(resources, 'inventory'):
                report['plan_id'] = fingerprint({'plan': report['plan_id'],
                                                'skill_ownership': resources.inventory['ownership_fingerprint'],
                                                'legacy_entries': [row['legacy_entry'] for row in resources.inventory['skills']]})
        report['checks'].append({'name': 'effective_session', 'status': 'not_verified',
                                 'reason': 'Plan validates explicit user configuration; account, policy, CLI and session overrides are not session-tested'})
        for check in checks:
            if check['status'] == 'blocked':
                report['blockers'].append({key: check[key] for key in ('name', 'code', 'path', 'reason') if key in check})
        report['ready'] = not report['blockers']
        # Guard an executable symlink changing after its actual binary was checked.
        resolutions = {row['executable']: row['resolved_executable'] for row in checks if 'resolved_executable' in row}
        def binding_guard():
            for reference, actual in self.references.items():
                if reference.resolve() != actual:
                    raise PlanStale('Profile source binding changed')
            for path, actual in resolutions.items():
                if str(Path(path).resolve()) != actual:
                    raise PlanStale('Native executable binding changed')
        guards = [binding_guard]
        if resources is not None:
            guards += [*resources.resolver.guards, *resources.binder.guards]
        native = [resources.plugins.operation] if resources is not None and resources.plugins.operation is not None else []
        index = next((i for i, change in enumerate(changes) if change.owner in ('managed-baseline', 'machine-binding')), len(changes))
        return FilePlan(changes, sources=self.sources, scopes=[str(self.native_path.resolve())], report=report,
                        directories=resources.directories if resources is not None else (), native_operations=native, native_index=index,
                        guards=guards, prepare=prepare)

    def plan(self, *, profile=None, run_policy=None, resolutions=None, adopt_plugins=None, replace_plugins=None, prepare=True):
        _, chosen, _, _, name = self._profile(profile)
        self.adopt_plugins = mapping(adopt_plugins or {}, 'plugin adoption selections')
        require(set(self.adopt_plugins) <= set(chosen.get('resources', {}).get('plugins', {})), 'Adoption must name an explicitly selected plugin resource')
        require(all(isinstance(value, str) for value in self.adopt_plugins.values()), 'Adoption selectors must be text')
        require(len(set(self.adopt_plugins.values())) == len(self.adopt_plugins), 'Duplicate native adoption selector')
        replacements = mapping(replace_plugins or {}, 'plugin replacement selections')
        require(set(replacements) <= set(chosen.get('resources', {}).get('plugins', {})), 'Replacement must name an explicitly selected plugin resource')
        require(not set(replacements).intersection(self.adopt_plugins), 'Plugin adoption and replacement are distinct operations')
        from .plugin_replacement import replacement_source
        self.replace_plugins = {ref: replacement_source(self, path) for ref, path in replacements.items()}
        selectors = [*self.adopt_plugins.values(), *(item['selector'] for item in self.replace_plugins.values())]
        require(len(set(selectors)) == len(selectors), 'Duplicate native adoption/replacement selector')
        native, original, expected = self._read(self.native_path, lambda data: parse_native(self.agent, data))
        state, state_expected = self._state()
        self._validate_policies(chosen, run_policy)
        resource_resolutions, settings_resolutions = {}, {}
        for path, resolution in mapping(resolutions or {}, 'drift resolutions').items():
            parts = pointer(path)
            if parts[:1] == ('resources',):
                require(len(parts) == 3 and parts[2] in chosen.get('resources', {}).get(parts[1], {}), 'Resource resolution must name a selected resource')
                closed(resolution, ('conflict_id', 'action'), ('conflict_id', 'action'), 'resource resolution')
                require(isinstance(resolution['conflict_id'], str) and len(resolution['conflict_id']) == 64 and
                        all(char in '0123456789abcdef' for char in resolution['conflict_id']) and resolution['action'] in ('adopt_source', 'keep_local', 'update_source'),
                        'Invalid resource conflict resolution')
                resource_resolutions[path] = resolution
            else:
                settings_resolutions[path] = resolution
        resources = ResourcePlan(self, state, chosen, native, original, run_policy, resource_resolutions)
        self.last_resources = resources
        resource_failed = False
        try:
            resources.prepare()
            from .resource_inventory import inspect_skills
            resources.inventory, ownership_guard = inspect_skills(self, resources.previous, resources.rendered, native)
            resources.checks.extend(resources.inventory['checks'])
            resources.resolver.guards.append(ownership_guard)
        except (ValueError, OSError, subprocess.SubprocessError) as exc:
            resource_failed = True
            resources.checks.append({'name': 'resource_resolution', 'status': 'blocked', 'code': 'resource_preflight_failed',
                                     'reason': str(exc) if isinstance(exc, SchemaError) else 'Resource schema, source or definition failed validation',
                                     'error_type': type(exc).__name__})
        settings = deepcopy(chosen.get('settings', {}))
        for path, value in leaves(resources.generated):
            put(settings, path, value)
            # This is a required native command-handler structure, not an optional
            # product default. An explicitly supplied type owns its own leaf.
            if path[-1] == 'command':
                settings[path[0]].setdefault('type', 'command')
        generated = {pointer(path)[1:] for path in chosen.get('bindings', {})}
        validate_settings(self.agent, settings, generated=generated)
        for path, resolution in settings_resolutions.items():
            selected = pointer(path)
            require(selected[:1] == ('settings',) and selected[1:] in {leaf for leaf, _ in leaves(settings)},
                    'Drift resolution must name a selected settings leaf')
            closed(resolution, ('target_fingerprint', 'policy'), ('target_fingerprint', 'policy'), 'drift resolution')
            require(resolution['policy'] in ('source_wins', 'keep_target') and isinstance(resolution['target_fingerprint'], str)
                    and len(resolution['target_fingerprint']) == 64 and all(char in '0123456789abcdef' for char in resolution['target_fingerprint']),
                    'Invalid fingerprint-scoped drift resolution')
        scope = [as_pointer(('settings', *path)) for path, _ in leaves(settings)]
        baseline = {'settings': state['managed']} if state else None
        previous = {'settings': state['declared']} if state else None
        merge = MergePlan({'settings': settings}, {'settings': native},
                          source={'kind': 'profile', 'file': name}, destination={'kind': 'native-config', 'file': str(self.native_path)},
                          scope=scope, profile_policy=chosen.get('merge_policy'), run_policy=run_policy,
                          machine_policy=self.machine.get('merge_policy'), baseline=baseline, resolutions=settings_resolutions, previous=previous,
                          number_fields=[as_pointer(('settings', *path)) for path, _ in leaves(settings) if lookup(self.agent, path).kind == 'number'])
        result = resources.registrations.native_result(merge.result['settings'])
        managed_paths = [path for path, _ in leaves(settings)]
        managed = projection(result, managed_paths)
        probe_settings = deepcopy(settings)
        if self.agent == 'codex' and not merge.blockers and ('default_permissions' in settings or 'permissions' in settings):
            try:
                dependencies = permission_dependencies(settings, result)
                if dependencies:
                    probe_settings['permissions'] = dependencies
                if 'default_permissions' in settings:
                    probe_settings['default_permissions'] = result['default_permissions']
            except SchemaError:
                # Final-result validation records the exact dependency failure.
                # Do not send an unresolved policy to an isolated native probe.
                probe_settings.pop('permissions', None)
                probe_settings.pop('default_permissions', None)
        if isinstance(settings.get('approval_policy'), dict) and not merge.blockers:
            probe_settings['approval_policy'] = projection(result, [('approval_policy', 'granular', key) for key in
                                                                    ('mcp_elicitations', 'rules', 'sandbox_approval', 'request_permissions', 'skill_approval')]).get('approval_policy', {})
        checks = self._preflight(chosen, probe_settings, result, run_policy=run_policy)
        checks += resources.checks
        changes = []
        result_valid = not merge.blockers and not resource_failed
        if not merge.blockers:
            try:
                validate_settings(self.agent, managed, generated=generated)
                validate_native_result(self.agent, result, settings)
                checks.append({'name': 'result_validation', 'status': 'passed'})
            except SchemaError as exc:
                result_valid = False
                checks.append({'name': 'result_validation', 'status': 'blocked', 'code': 'invalid_native_result', 'reason': str(exc)})
        if (settings or resources.registrations.main_roots or resources.registrations.main_fields) and result_valid:
            def valid_native(data):
                value = parse_native(self.agent, data)
                validate_settings(self.agent, projection(value, managed_paths), generated=generated)
                validate_native_result(self.agent, value, settings)
            changes.append(FileChange(self.native_path, render_native(self.agent, original, result), 'agent-settings', valid_native, expected))
        if (settings or state or resources.records or chosen.get('bindings')) and result_valid:
            new_state = {'schema_version': 1, 'kind': 'profile-managed-state', 'agent_type': self.agent,
                         'native_path': str(self.native_path.resolve()), 'profile': name, 'managed': managed, 'declared': settings}
            if resources.records:
                new_state['resources'] = resources.records
            if chosen.get('bindings'):
                new_state['bindings'] = chosen['bindings']
            content = (json.dumps(new_state, indent=2, ensure_ascii=False) + '\n').encode()
            changes.append(FileChange(self.state_path, content, 'managed-baseline', lambda data: self._validate_state(parse_json(data)), state_expected))
        if profile and profile != self.entry['profile'] and result_valid:
            changes.append(self.machine_profiles.switch('agents', self.agent, name))
        if not resource_failed:
            changes = resources.changes + changes
        for change in changes:
            require(not any(change.path.resolve().is_relative_to(root) for root in resources.resolver.repositories), 'Write target overlaps a fixed source repository')
        switching = profile is not None or state and state['profile'] != name
        plan = self._file_plan(merge, changes, checks, 'switch' if switching else 'apply', resources, prepare=prepare)
        plan.report['binding'] = {'previous_profile': state.get('profile', self.entry['profile']), 'profile': name}
        return plan

    def review_conflicts(self, *, run_policy=None, resolutions=None):
        deployment = self.plan(run_policy=run_policy, resolutions=resolutions)
        resources = self.last_resources
        require(bool(resources.conflicts.reviews), 'No owned resource conflict has review details')
        changes = resources.conflicts.private_review()
        report = deepcopy(deployment.report)
        report.update(operation='review-conflicts', deployment_ready=deployment.report['ready'],
                      deployment_blockers=deployment.report['blockers'], blockers=[], ready=True)
        report['review_directories'] = [str(self.state_root / 'resource-reviews' / row['conflict_id']) for row in resources.conflicts.reviews]
        def binding_guard():
            for reference, actual in self.references.items():
                if reference.resolve() != actual:
                    raise PlanStale('Resource review binding changed')
        return FilePlan(changes, sources=self.sources, scopes=[str(self.native_path.resolve())],
                        report=report, directories=resources.directories,
                        guards=[binding_guard, *resources.resolver.guards, *resources.binder.guards])

    def export(self, selectors, *, profile=None, run_policy=None, resource_refs=()):
        require(bool(selectors or resource_refs), 'Export requires explicitly selected fields or resource references')
        path, chosen, original, expected, name = self._profile(profile, new=True)
        scope = selected_paths(self.agent, selectors, bound=chosen.get('bindings', {})) if selectors else []
        native, _, _ = self._read(self.native_path, lambda data: parse_native(self.agent, data))
        bound_scope = [as_pointer(item) for item in scope if lookup(self.agent, item[1:]).binding]
        ordinary_scope = [as_pointer(item) for item in scope if not lookup(self.agent, item[1:]).binding]
        resources = None
        if bound_scope:
            resources = ResourcePlan(self, {}, chosen)
            try:
                resources.prepare(deploy=False, binding_scope=bound_scope)
            except (ValueError, OSError, subprocess.SubprocessError) as exc:
                resources.checks.append({'name': 'binding_export', 'status': 'blocked', 'code': 'binding_resolution_failed',
                                        'reason': str(exc) if isinstance(exc, SchemaError) else 'Logical binding cannot be resolved'})
            for destination in bound_scope:
                native_path = pointer(destination)[1:]
                actual = get(native, native_path)
                rendered = get(resources.generated, native_path)
                matched = rendered is not MISSING and actual is not MISSING and same(actual, rendered)
                resources.decisions.append({'path': list(pointer(destination)), 'source': {'kind': 'native-config'}, 'target': {'kind': 'logical-binding'},
                                            'before_exists': actual is not MISSING, 'before': '<local command>' if actual is not MISSING else None,
                                            'incoming_exists': rendered is not MISSING, 'incoming': '<resolved binding>' if rendered is not MISSING else None,
                                            'after_exists': True, 'after': chosen['bindings'][destination],
                                            'action': 'preserve_binding' if matched else 'binding_drift', 'category': 'unchanged' if matched else 'unresolved_conflict',
                                            'policy': 'logical-reference', 'policy_origin': 'binding_adapter',
                                            'reason': 'Native value matches logical binding' if matched else 'Native value differs; edit the logical binding and replan',
                                            'alternatives': ['retain_profile_binding', 'edit_binding_and_replan'], 'check_status': 'passed' if matched else 'blocked'})
                if not matched:
                    resources.checks.append({'name': 'binding_export', 'path': list(pointer(destination)), 'status': 'blocked', 'code': 'binding_drift',
                                            'reason': 'Local command cannot be safely reverse-mapped into the Profile'})
        incoming = {'settings': projection(native, [pointer(item)[1:] for item in ordinary_scope])}
        resource_scope = []
        if resource_refs:
            from .resource_export import ResourceExport
            state, _ = self._state()
            observer = ResourceExport(self, chosen, native, state)
            try:
                observer.observe(resource_refs)
            except (ValueError, OSError, subprocess.SubprocessError) as exc:
                observer.checks.append({'name': 'resource_export', 'status': 'blocked', 'code': 'resource_mapping_failed',
                                        'reason': str(exc) if isinstance(exc, SchemaError) else 'Fixed source or resource identity could not be verified'})
            incoming.update(observer.incoming)
            resource_scope = observer.scope
            if resources is None:
                resources = observer
            else:
                resources.decisions += observer.decisions
                resources.checks += observer.checks
                resources.resolver.guards += observer.resolver.guards
                resources.binder.guards += observer.binder.guards
                resources.directories.update(observer.directories)
        validate_settings(self.agent, incoming['settings'])
        checks = self._preflight(chosen, incoming['settings'], native, run_policy=run_policy, resources_selected=bool(resource_refs))
        if resources is not None:
            checks += resources.checks
        merge = MergePlan(incoming, chosen, source={'kind': 'native-config', 'file': str(self.native_path)},
                          destination={'kind': 'profile', 'file': name}, scope=ordinary_scope + resource_scope,
                          profile_policy=chosen.get('merge_policy'), run_policy=run_policy, machine_policy=self.machine.get('merge_policy'),
                          number_fields=[as_pointer(path) for path in scope if lookup(self.agent, path[1:]).kind == 'number'] +
                                        [as_pointer(('resources', 'mcp_servers', ref, 'options', key)) for ref in resource_refs
                                         if ref in chosen.get('resources', {}).get('mcp_servers', {}) for key in ('startup_timeout_sec', 'tool_timeout_sec')])
        validate_profile(merge.result, expected=self.agent)
        validate_settings(self.agent, merge.result.get('settings', {}))
        content = yaml.safe_dump(merge.result, allow_unicode=True, sort_keys=False).encode()
        # Preserve bytes/comments on a semantic no-op.
        if original and merge.result == chosen:
            content = original
        changes = [FileChange(path, content, 'profile-export',
                              lambda data: (validate_profile(parse_yaml(data), expected=self.agent),
                                            validate_settings(self.agent, parse_yaml(data).get('settings', {}))), expected)]
        return self._file_plan(merge, changes, checks, 'export', resources)
