"""Resolve logical local dependencies and construct quoted native commands."""
import os
from pathlib import Path
import shlex

from .fields import lookup
from .native import snapshot_read
from .schema import binding_ref, pointer, require
from .transaction import PlanStale

GROUPS = {'executable': 'executables', 'path': 'paths', 'endpoint': 'endpoints', 'secret': 'secrets'}


class Bindings:
    def __init__(self, manager):
        self.manager = manager
        self.checks = []
        self.guards = []

    def resolve(self, reference, expected_kind=None, allow_missing=False):
        group, name = binding_ref(reference)
        require(expected_kind is None or group == GROUPS[expected_kind], 'Binding kind disagrees with the required slot')
        available = self.manager.machine['bindings'].get(group, {})
        require(name in available, 'Required Machine binding is not defined: ' + reference)
        value = available[name]
        if group in ('executables', 'paths'):
            original = Path(value)
            actual = original.resolve()
            require(allow_missing or actual.exists(), 'Bound local dependency does not exist: ' + reference)
            self.manager.references[original] = actual
            if group == 'executables':
                require(actual.is_file() and os.access(actual, os.X_OK), 'Bound program is not executable: ' + reference)
                _, expected = snapshot_read(actual)
                self.manager.sources[actual] = expected
            elif actual.is_file():
                _, expected = snapshot_read(actual)
                self.manager.sources[actual] = expected
            if not allow_missing:
                def guard(actual=actual):
                    if not actual.exists():
                        raise PlanStale('Bound local dependency disappeared')
                self.guards.append(guard)
            value = str(actual)
        elif group == 'secrets':
            # Never materialize the value; native authentication remains native.
            if value['provider'] == 'env':
                require(bool(os.environ.get(value['variable'])), 'Required environment credential reference is unavailable: ' + reference)
                def guard(variable=value['variable']):
                    if not os.environ.get(variable):
                        raise PlanStale('Required credential reference is no longer available')
                self.guards.append(guard)
            self.checks.append({'name': 'credential_reference', 'binding': reference, 'status': 'passed' if value['provider'] == 'env' else 'not_verified',
                                'reason': 'Logical reference only; no credential value is copied'})
        self.checks.append({'name': 'binding', 'binding': reference, 'status': 'passed', 'kind': group})
        return value

    def slots(self, package):
        declared = package.item['requires'].get('bindings', {})
        chosen = package.selection.get('bindings', {})
        require(not set(chosen) - set(declared), 'Resource contains an undeclared binding slot: ' + package.ref)
        resolved = {}
        for slot, requirement in declared.items():
            require(not requirement['required'] or slot in chosen, 'Resource requires an explicit binding slot: ' + slot)
            if slot in chosen:
                resolved[slot] = self.resolve(chosen[slot], requirement['kind'], allow_missing=package.item['kind'] == 'skill' and slot == 'install_root' and requirement['kind'] == 'path')
        return resolved

    def command(self, destination, declaration, rendered):
        path = pointer(destination)
        field = lookup(self.manager.agent, path[1:])
        require(path[:1] == ('settings',) and field is not None and field.binding,
                'This agent has no command binding adapter for the selected field')
        executable = self.resolve(declaration['executable'], 'executable')
        args = []
        for arg in declaration['args']:
            if isinstance(arg, str):
                require('\0' not in arg, 'Command argument contains NUL')
                args.append(arg)
            else:
                require(arg['resource'] in rendered, 'Command argument refers to an unselected resource')
                package = rendered[arg['resource']]
                name = str(Path(arg['path']))
                require(name in package['files'], 'Command argument is not a tracked resource file')
                args.append(str(package['cache'] / name))
        return shlex.join([executable, *args])
