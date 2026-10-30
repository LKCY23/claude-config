"""Explicit deployment selection; Profile selection and source pins are unchanged."""
from dataclasses import dataclass
from copy import deepcopy

from .fields import leaves
from .native import projection
from .schema import RESOURCE_KINDS, as_pointer, pointer, require


@dataclass(frozen=True)
class DeploymentScope:
    mode: str = 'all'
    resources: tuple = ()
    groups: tuple = ()
    fields: tuple = ()

    def __post_init__(self):
        require(self.mode in ('all', 'settings', 'resources'), 'Unknown deployment scope')
        require(sum(bool(v) for v in (self.resources, self.groups, self.fields)) <= 1,
                'Choose resources, resource groups, or settings fields, not a combination')
        require(self.mode == 'all' or not (self.resources or self.groups or self.fields),
                'Do not combine a scope with individual selectors')
        for name in ('resources', 'groups', 'fields'):
            values = getattr(self, name)
            require(len(values) == len(set(values)), 'Duplicate deployment selector')
            object.__setattr__(self, name, tuple(sorted(values)))
        require(not set(self.groups) - set(RESOURCE_KINDS), 'Unknown resource group')

    @property
    def full(self):
        return self.mode == 'all' and not (self.resources or self.groups or self.fields)

    def select(self, profile, *, app=False):
        require(not app or not (self.resources or self.groups or self.mode == 'resources'),
                'App Profiles only support settings selection')
        available = {ref for entries in profile.get('resources', {}).values() for ref in entries}
        if self.full or self.mode == 'resources':
            refs = available
        elif self.groups:
            refs = {ref for group in self.groups for ref in profile.get('resources', {}).get(group, {})}
        else:
            refs = set(self.resources)
        require(refs <= available, 'Resource must already be selected in the bound Profile')
        declared = {as_pointer(('settings', *p)) for p, _ in leaves(profile.get('settings', {}))}
        declared.update(profile.get('bindings', {}))
        if self.fields:
            fields = set(self.fields)
            require(fields <= declared, 'Field must be an explicitly declared settings leaf or command binding')
        elif self.full or self.mode == 'settings':
            fields = declared
        else:
            fields = set()
        return refs, fields

    def settings(self, profile, fields):
        paths = [pointer(p)[1:] for p in fields if p not in profile.get('bindings', {})]
        return projection(profile.get('settings', {}), paths)

    def report(self, resources, fields, effective_resources, effective_fields):
        return {
            'mode': 'all' if self.full else 'partial',
            'requested': {'scope': self.mode if not (self.resources or self.groups or self.fields) else None, 'resources': list(self.resources),
                          'resource_groups': list(self.groups), 'fields': list(self.fields)},
            'effective': {'resources': sorted(effective_resources), 'fields': sorted(effective_fields)},
            'expanded': {'resources': sorted(set(effective_resources) - set(resources)),
                         'fields': sorted(set(effective_fields) - set(fields))},
        }


def update_leaves(previous, values):
    result = deepcopy(previous)
    for path, value in leaves(values):
        node = result
        for key in path[:-1]:
            require(key not in node or isinstance(node[key], dict), 'Selected settings parent is not an object')
            node = node.setdefault(key, {})
        node[path[-1]] = deepcopy(value)
    return result
