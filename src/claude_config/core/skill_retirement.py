"""Explicit retirement of unselected, unchanged standalone skill deployments."""
from copy import deepcopy
from pathlib import Path
import json

from .catalog import CatalogResolver
from .merge import fingerprint
from .native import parse_json, parse_native, render_native
from .resource_inventory import cc_switch_root, external_claims
from .resources import render_package
from .schema import require
from .transaction import FileChange, FilePlan, PlanStale, directory_fingerprint, file_fingerprint


def retirement_plan(manager, refs):
    require(bool(refs) and len(refs) == len(set(refs)), 'Retirement requires distinct --resource references')
    _, profile, _, _, name = manager._profile()
    state, state_pin = manager._state()
    selected = {ref for entries in profile.get('resources', {}).values() for ref in entries}
    require(not selected.intersection(refs), 'Remove the resource selection from the Profile before explicit retirement')
    records = state.get('resources', {})
    require(all(ref in records and records[ref]['kind'] == 'skill' for ref in refs),
            'Only recorded standalone skills can be retired; plugins and unknown resources need their own lifecycle')
    resolver = CatalogResolver(manager)
    packages = resolver.resolve(profile)
    selected_names = {render_package(p, manager.agent)['identity'] for p in packages.values() if p.item['kind'] == 'skill'}
    native, original, native_pin = manager._read(manager.native_path, lambda b: parse_native(manager.agent, b))
    result, new_state = deepcopy(native), deepcopy(state)
    changes, decisions, directories = [], [], set()
    identities = {records[ref]['native']['identity'] for ref in refs}
    require(not identities.intersection(selected_names), 'A selected resource still uses the retired native identity')
    cc_root = cc_switch_root(manager)
    claims = external_claims(cc_root, manager.agent, identities)
    require(claims['status'] != 'unreadable' and not claims['claims'], 'Hand off external skill management before retirement')
    other_files = {path for ref, row in records.items() if ref not in refs for path in row['files']}
    for ref in refs:
        old = records[ref]
        identity = old['native']['identity']
        root, cache = Path(old['native']['root']), Path(old['cache'])
        expected_cache = manager.native_path.parent.resolve() / '.claude-config-resources' / ref.split('@')[1] / ref.split('@')[0] / old['rendered_fingerprint']
        require(cache == expected_cache and cache.resolve() == cache and root.resolve() == root,
                'Recorded retirement root or cache is no longer an unambiguous bound path')
        if manager.agent == 'claude-code':
            require(root == manager.native_path.parent.resolve() / 'skills' / identity, 'Retirement root differs from the bound Claude skills directory')
        else:
            roots = {Path(value).resolve() for value in manager.machine.get('bindings', {}).get('paths', {}).values()
                     if Path(value).parts[-2:] == ('.agents', 'skills')}
            require(root.name == identity and root.parent in roots, 'Retirement root is not a currently bound Codex skill location')
        require(not other_files.intersection(old['files']), 'Another managed resource shares retirement files')
        for directory in (root, cache):
            directories.add(directory)
            world = directory_fingerprint(directory)
            require(all(str(directory / path) in old['files'] for path in world['entries']),
                    'Unknown files remain in the skill directory; retirement cannot remove them')
        for location, before in old['files'].items():
            path = Path(location)
            require(any(path.is_relative_to(directory) for directory in (root, cache)), 'Recorded file escapes the retired skill roots')
            require(file_fingerprint(path) == before, 'Skill changed since deployment; resolve its drift before retirement')
            changes.append(FileChange(path, b'', 'skill-retirement', lambda b: None, before, delete=True))
            decisions.append({'path': ['resources', 'skills', ref, 'files', location],
                              'action': 'delete', 'before_exists': True, 'before': before, 'after_exists': False,
                              'policy': 'explicit-retirement', 'reason': 'Delete only the unchanged owned file'})
        control = old['native'].get('skill_control')
        require(not control or 'plugin_selector' not in control, 'A skills-dir plugin requires the native plugin lifecycle')
        if manager.agent == 'claude-code':
            overrides = result.get('skillOverrides', {})
            if control:
                require(overrides.get(control['name']) == ('on' if control['entry']['enabled'] else 'off'),
                        'Owned skill activation changed; retirement is blocked')
                del overrides[control['name']]
            else:
                require(identity not in overrides, 'Unmanaged skill activation overlaps retirement')
        else:
            rows = result.get('skills', {}).get('config', [])
            matches = [row for row in rows if isinstance(row, dict) and row.get('path') == str(root / 'SKILL.md')]
            require(matches == [control['entry']] if control else not matches, 'Skill activation differs from its recorded ownership')
            if control:
                rows.remove(matches[0])
        del new_state['resources'][ref]
    if result != native:
        content = render_native(manager.agent, original, result)
        changes.append(FileChange(manager.native_path, content, 'skill-retirement-control',
                                  lambda data: require(parse_native(manager.agent, data) == result, 'Retirement changed unrelated native configuration'), native_pin))
    changes.append(FileChange(manager.state_path, (json.dumps(new_state, ensure_ascii=False, indent=2) + '\n').encode(),
                              'managed-baseline', lambda data: manager._validate_state(parse_json(data)), state_pin))
    def ownership_guard():
        for reference, actual in manager.references.items():
            if reference.resolve() != actual:
                raise PlanStale('Profile source binding changed before retirement')
        if external_claims(cc_root, manager.agent, identities) != claims:
            raise PlanStale('External skill ownership changed before retirement')
    report = {'operation': 'retire-skills', 'agent_type': manager.agent, 'ready': True,
              'checks': [{'name': 'skill_retirement_scope', 'status': 'passed', 'reason': 'Unselected, unchanged owned standalone skill files only'}],
              'decisions': decisions, 'blockers': [], 'retired_resources': sorted(refs),
              'binding': {'profile': name},
              'plan_id': fingerprint({'operation': 'retire-skills', 'refs': sorted(refs), 'state': state_pin, 'claims': claims})}
    return FilePlan(changes, sources=manager.sources, scopes=[str(manager.native_path.resolve())],
                    directories=directories, guards=[*resolver.guards, ownership_guard], report=report)
