"""Resolve explicit repository Profile choices without copying local dependencies."""
from copy import deepcopy
from pathlib import Path

import yaml

from .native import parse_yaml, snapshot_read
from .schema import require, validate_machine, validate_machine_profiles, within
from .transaction import FileChange


class MachineProfiles:
    def __init__(self, path, value=None, expected=None):
        self.path = Path(path).absolute()
        if value is None:
            raw, expected = snapshot_read(self.path)
            value = parse_yaml(raw)
        require(expected is not None, 'Machine input snapshot is required')
        self.local = validate_machine(value)
        self.sources = {self.path: expected}
        self.effective = deepcopy(self.local)
        self.selection_path = None
        self.selection = None
        if 'profile_selection' not in self.local:
            return
        name = self.local['profile_selection']
        root = Path(self.local['config_dir'])
        # Keep the original path in the plan: file plans also guard its resolution.
        within(root, name)
        self.selection_path = root / name
        raw, pin = snapshot_read(self.selection_path)
        require(pin['exists'], 'Machine Profile selection is missing')
        self.selection = validate_machine_profiles(parse_yaml(raw), self.local['machine_id'])
        self.sources[self.selection_path] = pin
        self.effective.pop('profile_selection')
        for group in ('agents', 'apps'):
            require(set(self.selection[group]) == set(self.local[group]),
                    'Repository selections and local runtime bindings differ: ' + group)
            for name, profile in self.selection[group].items():
                self.effective[group][name]['profile'] = profile
        validate_machine(self.effective)

    def switch(self, group, name, profile):
        if self.selection_path is not None:
            value = deepcopy(self.selection)
            value[group][name] = profile
            path = self.selection_path
            validate = lambda data: validate_machine_profiles(parse_yaml(data), self.local['machine_id'])
        else:
            value = deepcopy(self.local)
            value[group][name]['profile'] = profile
            path = self.path
            validate = lambda data: validate_machine(parse_yaml(data))
        return FileChange(path, yaml.safe_dump(value, allow_unicode=True, sort_keys=False).encode(),
                          'machine-binding', validate, self.sources[path])
