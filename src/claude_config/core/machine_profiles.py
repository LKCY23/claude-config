"""Resolve explicit repository Profile choices without copying local dependencies."""
from copy import deepcopy
from pathlib import Path

import yaml

from .native import parse_yaml, snapshot_read
from .schema import require, validate_machine, validate_machine_profiles, within
from .transaction import FileChange


class MachineProfiles:
    def __init__(self, path, value=None, expected=None, *, strict=True):
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
        self.mismatches = []
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
            repo_keys = set(self.selection[group])
            local_keys = set(self.local[group])
            if repo_keys != local_keys:
                only_repo = sorted(repo_keys - local_keys)
                only_local = sorted(local_keys - repo_keys)
                message = (
                    f'Repository selections and local runtime bindings differ: {group} '
                    f'(repository only: {", ".join(only_repo) or "none"}; '
                    f'local only: {", ".join(only_local) or "none"})'
                )
                self.mismatches.append({
                    'group': group,
                    'repository_only': only_repo,
                    'local_only': only_local,
                    'message': message,
                })
                if strict:
                    require(False, message)
                for key in sorted(repo_keys & local_keys):
                    self.effective[group][key]['profile'] = self.selection[group][key]
            else:
                for key, profile in self.selection[group].items():
                    self.effective[group][key]['profile'] = profile
        if strict or not self.mismatches:
            validate_machine(self.effective)
        else:
            matched = deepcopy(self.effective)
            for mismatch in self.mismatches:
                group = mismatch['group']
                matched[group] = {key: entry for key, entry in matched[group].items() if 'profile' in entry}
            validate_machine(matched)

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
