"""Select a prepared released catalog without deployment or Git checkout mutation."""
from copy import deepcopy
import hashlib
from pathlib import Path

import yaml

from .catalog import CatalogResolver, git, verify_git
from .catalog_schema import validate_catalog
from .merge import MergePlan
from .native import parse_json, parse_yaml, snapshot_read
from .schema import COMMIT, RESOURCE_KINDS, closed, require, validate_catalogs, validate_machine, validate_profile, within
from .transaction import FileChange, FilePlan


def adopt_baseline(manager, alias, commit, checkout, release_record):
    require(isinstance(commit, str) and COMMIT.fullmatch(commit), 'Baseline adoption requires a full lowercase commit SHA')
    catalogs_path = manager.root / 'catalogs.yaml'
    catalogs, original, expected = manager._read(catalogs_path, lambda data: validate_catalogs(parse_yaml(data)))
    require(alias in catalogs['catalogs'] and alias in manager.machine['catalog_roots'], 'Catalog adoption requires an existing declared and bound alias')
    old = catalogs['catalogs'][alias]
    record, _, _ = manager._read(Path(release_record).absolute(), parse_json)
    keys = ('schema_version', 'kind', 'repo', 'commit', 'catalog_path', 'catalog_sha256', 'validation')
    closed(record, keys, keys, 'baseline release record')
    require(all(isinstance(record[key], str) for key in ('repo', 'commit', 'catalog_path', 'catalog_sha256')), 'Release identity fields must be strings')
    require(record['schema_version'] == 1 and type(record['schema_version']) is int and record['kind'] == 'claudespace-baseline', 'Unknown release record format')
    require(record['repo'].lower() == old['repo'].lower() and record['commit'] == commit and record['catalog_path'] == old['path'], 'Release identity differs from the selected catalog')
    closed(record['validation'], ('status', 'level', 'scope'), ('status', 'level', 'scope'), 'release validation')
    require(record['validation'] == {'status': 'passed', 'level': 'structural', 'scope': 'catalog-schema'}, 'Unsupported or failed release validation record')
    reference = Path(checkout).absolute()
    root = reference.resolve()
    manager.references[reference] = root
    verify_git(root, old['repo'], commit)
    file = within(root, old['path']) / 'catalog.json'
    value, raw, _ = manager._read(file, parse_json)
    validate_catalog(value)
    require(value['name'] == alias and raw == git(root, 'show', commit + ':' + file.relative_to(root).as_posix()), 'Candidate catalog identity or committed bytes differ')
    require(record['catalog_sha256'] == hashlib.sha256(raw).hexdigest(), 'Release catalog fingerprint differs from prepared contents')
    checks = [{'name': 'release_record', 'status': 'passed', 'level': 'structural', 'reason': 'Prepared release identity and committed registry bytes verified; no resource business logic is executed'}]
    impacts, guards = [], []
    directories = set()
    for agent in ('claude-code', 'codex'):
        directory = manager.root / 'profiles/agents' / agent
        directories.add(directory)
        for path in sorted(directory.glob('*.yaml')):
            profile, _, _ = manager._read(path, parse_yaml)
            validate_profile(profile, expected=agent)
            selections = {ref for entries in profile.get('resources', {}).values() for ref in entries}
            selected = [(group, ref) for group, entries in profile.get('resources', {}).items() for ref in entries if ref.split('@')[1] == alias]
            if not selected:
                continue
            affected = []
            for group, ref in selected:
                identifier = ref.split('@')[0]
                item = value['resources'].get(identifier)
                require(item is not None and item['kind'] == RESOURCE_KINDS[group], 'New baseline removes or reclassifies a selected resource')
                require(agent in item['compatibility'] and item['compatibility'][agent]['validation']['status'] != 'failed', 'New baseline lacks passing/unevaluated compatibility for an affected Profile')
                require(all(dep in selections for dep in item['requires'].get('resources', [])), 'New baseline adds an unselected resource dependency')
                affected.append(ref)
            impacts.append({'profile': path.relative_to(manager.root).as_posix(), 'agent_type': agent, 'resources': affected,
                            'bound': manager.machine['agents'].get(agent, {}).get('profile') == path.relative_to(manager.root).as_posix()})
    candidate = deepcopy(catalogs)
    candidate['catalogs'][alias]['commit'] = commit
    # Parse every affected prepared package, including exact external/submodule
    # sources and dependency cycles, through the existing trusted resolver.
    for impact in impacts:
        profile_path = manager.root / impact['profile']
        profile = parse_yaml(snapshot_read(profile_path)[0])
        preview = type(manager)(manager.machine_path, impact['agent_type'], probe=manager.probe) if impact['agent_type'] in manager.machine['agents'] else None
        if preview is None:
            # Unbound Profiles still use this Machine's prepared source roots;
            # no executable or deployment target is inferred for them.
            from types import SimpleNamespace
            preview = SimpleNamespace(agent=impact['agent_type'], root=manager.root, state_root=manager.state_root,
                                      machine=deepcopy(manager.machine), sources=manager.sources, references=manager.references, _read=manager._read)
        preview.machine = deepcopy(manager.machine)
        preview.machine['catalog_roots'][alias] = str(root)
        resolver = CatalogResolver(preview)
        resolver.catalogs = candidate
        resolver.resolve(profile, selected=set(impact['resources']))
        guards += resolver.guards
        manager.sources.update(preview.sources)
        manager.references.update(preview.references)
        checks.append({'name': 'affected_sources', 'status': 'passed', 'profile': impact['profile'], 'reason': 'Exact selected bodies and explicit dependencies are prepared'})
    machine = deepcopy(manager.machine)
    machine['catalog_roots'][alias] = str(root)
    merge = MergePlan({'catalogs': {alias: {'commit': commit}}, 'catalog_roots': {alias: str(root)}},
                      {'catalogs': catalogs['catalogs'], 'catalog_roots': manager.machine['catalog_roots']},
                      source={'kind': 'released-baseline', 'file': str(release_record)}, destination={'kind': 'catalog-selection'},
                      scope=['/catalogs/' + alias.replace('~', '~0').replace('/', '~1') + '/commit', '/catalog_roots/' + alias.replace('~', '~0').replace('/', '~1')])
    report = merge.report()
    report.update(operation='adopt-baseline', agent_type=manager.agent, ready=True, impacts=impacts)
    report['checks'] += checks
    def guard(data):
        verify_git(root, old['repo'], commit)
        for check in guards:
            check()
        for path, target in manager.references.items():
            require(path.resolve() == target, 'Prepared baseline source binding changed')
    changes = [FileChange(catalogs_path, original if catalogs == candidate else yaml.safe_dump(candidate, sort_keys=False, allow_unicode=True).encode(),
                          'catalog-baseline-selection', lambda data: (guard(data), validate_catalogs(parse_yaml(data))), expected),
               FileChange(manager.machine_path, snapshot_read(manager.machine_path)[0] if machine == manager.machine else yaml.safe_dump(machine, sort_keys=False, allow_unicode=True).encode(),
                          'catalog-prepared-root', lambda data: (guard(data), validate_machine(parse_yaml(data))), manager.sources[manager.machine_path])]
    return FilePlan(changes, sources=manager.sources, scopes=[str(manager.native_path.resolve())], report=report, directories=directories)
