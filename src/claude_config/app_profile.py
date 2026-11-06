"""Public selected App Profile declaration API; never reads native configuration."""
from copy import deepcopy
from pathlib import Path
import json
import re
import sys

import yaml

from . import __version__
from .core.app_fields import declaration_schema, registry, validate_settings
from .core.app_manager import AppProfileManager
from .core.execution_context import progress_output
from .core.merge import fingerprint
from .core.native import parse_json, parse_yaml, snapshot_read
from .core.schema import SchemaError, closed, require, within
from .core.transaction import FileChange, FilePlan, PlanStale, UnfinishedRun, file_fingerprint
from .profile import ReportParser, UsageError


APP = 'claude-desktop'
CAPABILITY = 'app-profile-declaration-v1'


class InvalidPatch(ValueError):
    pass


def declarations(settings):
    return {key: {'declared': True, 'value': settings[key]} if key in settings else {'declared': False}
            for key in registry(APP)}


def envelope(action):
    return {'schema_version': 1, 'capability': CAPABILITY, 'operation': 'app-profile', 'action': action,
            'tool': {'name': 'claude-config', 'version': __version__}, 'app': APP,
            'status': 'ok', 'ready': True, 'preview_only': True,
            'target': None, 'revision': None, 'settings_schema': declaration_schema(APP),
            'declarations': None, 'proposed_declarations': None, 'changes': [], 'plan_id': None,
            'execution': {'started': False, 'status': 'not_started', 'native_write': False, 'profile_saved': False},
            'recovery': {'available': False, 'run_id': None},
            'warnings': [{'code': 'native_apply_separate'}], 'errors': []}


class SelectedDeclaration:
    """Use the framework's existing explicit selection and App validation path."""
    def __init__(self, machine):
        require(Path(machine).is_absolute(), 'Explicit absolute Machine path required')
        self.manager = AppProfileManager(machine, APP)
        root = self.manager.root.resolve()
        name = self.manager.entry['profile']
        parts = Path(name).parts
        require(len(parts) == 4 and parts[:3] == ('profiles', 'apps', APP) and Path(name).suffix == '.yaml',
                'Profile must belong to the bound App directory')
        candidate = within(self.manager.root, name)
        # A declaration cannot alias an actual native file or application path.
        require(candidate.is_relative_to(root / 'profiles' / 'apps' / APP)
                and candidate.suffix == '.yaml', 'Selected declaration escapes the App Profile directory')
        require(not candidate.is_relative_to(self.manager.data_root.resolve())
                and not candidate.is_relative_to(self.manager.application.resolve()), 'Profile cannot be native application data')
        def check_reference():
            if within(self.manager.root, name) != candidate or self.manager.root.resolve() != root:
                raise PlanStale('Selected Profile binding changed before inspection')
            if candidate.is_relative_to(self.manager.data_root.resolve()) or candidate.is_relative_to(self.manager.application.resolve()):
                raise PlanStale('Selected Profile became native application data')
        # Read the path already approved above; _profile() would resolve the
        # reference again and could select a different file before reading it.
        check_reference()
        self.path = candidate
        self.original, self.expected = snapshot_read(candidate)
        check_reference()
        require(self.expected['exists'], 'Selected declaration is missing')
        self.value = self.manager._validate_profile(parse_yaml(self.original))
        self.manager.sources[candidate] = self.expected
        self.manager.references[(self.manager.root / name).absolute()] = candidate
        self.target = {'machine_id': self.manager.machine['machine_id'], 'machine_file': str(self.manager.machine_path),
                       'profile': name, 'profile_file': str(self.path),
                       'profile_selection_file': str(self.manager.machine_profiles.selection_path) if self.manager.machine_profiles.selection_path else None,
                       'source_kind': 'repository' if self.manager.machine_profiles.selection_path else 'embedded'}
        self.settings = deepcopy(self.value.get('settings', {}))
        self.revision = self.source_revision()

    def source_revision(self, profile_pin=None):
        pins = dict(self.manager.sources)
        if profile_pin is not None:
            pins[self.path] = profile_pin
        return fingerprint({'target': self.target, 'settings_schema': declaration_schema(APP),
                            'sources': [{'path': str(path), 'resolved': str(path.resolve()), 'file': pins[path]}
                                        for path in sorted(pins, key=str)]})

    def plan(self, patch_file):
        try:
            require(Path(patch_file).is_absolute(), 'Explicit absolute patch path required')
            content, expected = snapshot_read(patch_file)
            require(expected['exists'] and len(content) <= 65536, 'Patch must be an existing bounded file')
            patch = parse_json(content)
            closed(patch, ('schema_version', 'set', 'unset'), ('schema_version',), 'App declaration patch')
            require(type(patch['schema_version']) is int and patch['schema_version'] == 1, 'Unknown patch schema')
            setting = patch.get('set', {})
            validate_settings(APP, setting)
            unset = patch.get('unset', [])
            require(isinstance(unset, list) and all(isinstance(key, str) and key in registry(APP) for key in unset), 'Unknown unset field')
            require(len(unset) == len(set(unset)), 'Duplicate unset field')
            require(not set(setting).intersection(unset), 'Overlapping set/unset fields')
            require(bool(setting or unset), 'Patch must select a field')
        except (ValueError, TypeError, UnicodeError, OSError):
            raise InvalidPatch('Invalid declaration patch') from None
        updated = deepcopy(self.value)
        desired = deepcopy(self.settings)
        desired.update(setting)
        for key in unset:
            desired.pop(key, None)
        changes = []
        before, after = declarations(self.settings), declarations(desired)
        for key in registry(APP):
            if before[key] != after[key]:
                changes.append({'field': key, 'action': 'set' if key in desired else 'unset',
                                'before': before[key], 'after': after[key]})
        if 'settings' in updated or desired:
            updated['settings'] = desired
        self.manager._validate_profile(updated)
        encoded = yaml.safe_dump(updated, allow_unicode=True, sort_keys=False).encode() if changes else self.original
        sources = {**self.manager.sources, Path(patch_file).absolute(): expected}
        def binding_guard():
            current = within(self.manager.root, self.target['profile'])
            if current != self.path:
                raise PlanStale('Selected Profile binding changed')
            for reference, actual in self.manager.references.items():
                if reference.resolve() != actual:
                    raise PlanStale('Selected Profile source changed')
        report = {'operation': 'app-profile-edit', 'ready': True, 'checks': [], 'decisions': [], 'blockers': [],
                  'plan_id': fingerprint({'revision': self.revision, 'patch': patch})}
        plan = FilePlan([FileChange(self.path, encoded, 'app-profile-declaration',
                        lambda data: self.manager._validate_profile(parse_yaml(data)), self.expected)] if changes else [],
                        sources=sources, scopes=self.manager.recovery_scopes, report=report, guards=[binding_guard])
        return plan, desired, changes


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    action = argv[0] if argv and argv[0] in ('schema', 'inspect', 'edit') else 'unknown'
    report = envelope(action)
    plan = None
    write = False
    mode = 'json'
    for index, word in enumerate(argv):
        if word == '--format' and index + 1 < len(argv) and argv[index + 1] in ('json', 'text'):
            mode = argv[index + 1]
    code = 0
    with progress_output(sys.stderr):
        try:
            parser = ReportParser(prog='claude-config app-profile', description=__doc__)
            parser.add_argument('action', choices=('schema', 'inspect', 'edit'))
            parser.add_argument('--app', required=True, choices=(APP,))
            parser.add_argument('--machine', type=Path)
            parser.add_argument('--patch', type=Path)
            parser.add_argument('--write', action='store_true')
            parser.add_argument('--expect-plan-id')
            parser.add_argument('--format', choices=('json', 'text'), default='json')
            args = parser.parse_args(argv)
            mode, write = args.format, args.write
            if args.action == 'schema':
                if args.machine or args.patch or args.write or args.expect_plan_id:
                    raise UsageError('Schema does not read Machine or edit inputs')
            else:
                if not args.machine or not args.machine.is_absolute():
                    raise UsageError('An explicit absolute Machine is required')
                if args.action != 'edit' and (args.patch or args.write or args.expect_plan_id):
                    raise UsageError('Edit arguments are only valid for edit')
                if args.action == 'edit':
                    if not args.patch or not args.patch.is_absolute():
                        raise UsageError('An explicit absolute patch is required')
                    if args.write != bool(args.expect_plan_id) or args.expect_plan_id and not re.fullmatch('[0-9a-f]{64}', args.expect_plan_id):
                        raise UsageError('Write requires the exact reviewed plan ID')
                selected = SelectedDeclaration(args.machine)
                report.update(target=selected.target, revision=selected.revision, declarations=declarations(selected.settings))
                if args.action == 'edit':
                    plan, desired, changes = selected.plan(args.patch)
                    report.update(plan_id=plan.report['plan_id'], proposed_declarations=declarations(desired), changes=changes)
                    report['execution']['status'] = 'preview'
                    if args.write:
                        report['preview_only'] = False
                        if args.expect_plan_id != plan.report['plan_id']:
                            raise PlanStale('Declaration plan changed')
                        if changes:
                            plan.apply(selected.manager.state_root)
                            report['execution'].update(started=True, status='saved', profile_saved=True)
                            report['declarations'] = declarations(desired)
                            report['revision'] = selected.source_revision(file_fingerprint(selected.path))
                        else:
                            report['execution']['status'] = 'unchanged'
        except (Exception, KeyboardInterrupt) as error:
            error_code = ('invalid_arguments' if isinstance(error, UsageError) else 'invalid_patch' if isinstance(error, InvalidPatch)
                          else 'plan_stale' if isinstance(error, PlanStale) else 'unfinished_run' if isinstance(error, UnfinishedRun)
                          else 'invalid_configuration' if isinstance(error, (SchemaError, yaml.YAMLError, ValueError))
                          else 'file_access_failed' if isinstance(error, OSError) else 'interrupted' if isinstance(error, KeyboardInterrupt)
                          else 'operation_failed')
            started = bool(plan and plan.report.get('recovery'))
            report.update(status='blocked' if not started and error_code in ('invalid_arguments', 'invalid_patch', 'invalid_configuration', 'plan_stale', 'unfinished_run') else 'error',
                          ready=False, preview_only=not write, errors=[{'code': error_code}])
            report['execution'].update(started=started, status='failed', profile_saved=False)
            if started:
                report.update(declarations=None, revision=None)
            code = 130 if error_code == 'interrupted' else 1 if report['status'] == 'blocked' else 2
        if plan and plan.report.get('recovery'):
            report['recovery'] = {'available': True, 'run_id': Path(plan.report['recovery']).parent.name}
    if mode == 'json':
        print(json.dumps(report, ensure_ascii=False, allow_nan=False))
    else:
        print('App Profile declaration: ' + report['status'])
        if report['target']:
            print('Profile: ' + report['target']['profile'])
        for error in report['errors']:
            print('Error: ' + error['code'])
    return code


if __name__ == '__main__':
    sys.exit(main())
