"""One-time conversion input inventory. Never imported by runtime managers.

Reads old declarations and referenced local assets, not native auth or caches.
An inventory is evidence for the converter, not a new configuration or adoption.
"""
from pathlib import Path

from .app_fields import native_paths, registry, validate_settings as validate_app_settings
from .fields import lookup
from .native import parse_native, parse_yaml, projection, snapshot_read
from .schema import REF, SchemaError, closed, require


MANIFEST_ROUTES = {
    'metadata': 'data_repository_metadata', 'platforms': 'platform_selection',
    'skills': 'resource_or_framework_entry', 'rules': 'resource_source', 'agents': 'resource_source',
    'archives': 'app_profile_source', 'settings': 'retained_legacy_settings',
    'permissions': 'retained_legacy_permissions', 'hooks': 'resource_and_command_binding',
    'memory': 'instruction_resources', 'env': 'portable_settings_and_local_references',
    'exclude': 'local_exclusions', 'external': 'runtime_setup_outside_agent_profiles',
}


def inventory(data_root):
    root = Path(data_root).absolute()
    require(root.is_dir() and not root.is_symlink(), 'Inventory requires a real configuration data directory')
    report = {'checks': [], 'decisions': [], 'blockers': [], 'results': [], 'ready': True,
              'deployment_ready': False,
              'deployment_blockers': [{'code': 'migration_not_complete', 'reason': 'Inventory does not generate or deploy new Profiles, catalogs or management baselines'}]}
    entries = {'data_root': str(root), 'plugins': [], 'agent_settings': [], 'app_settings': [],
               'assets': [], 'manifest_sections': [], 'exclusions': [
                   'No archived plugins.yaml or observed v2 draft is an active declaration',
                   'No native credentials, account state, project directories or plugin caches are imported',
                   'Worktree directory and custom file handlers are permanently excluded']}
    report['migration_inventory'] = entries
    snapshots, seen = {}, set()

    def problem(path, code, reason):
        row = {'name': 'migration_input', 'path': list(path), 'status': 'blocked', 'code': code, 'reason': reason}
        report['checks'].append(row)
        report['blockers'].append({key: row[key] for key in ('path', 'code', 'reason')})

    def read(path):
        data, expected = snapshot_read(path)
        snapshots[str(path)] = expected
        require(expected['exists'], 'Declared input is missing')
        return data

    def document(name, *, required):
        path = root / name
        if not path.exists() and not required:
            return {}
        try:
            value = parse_yaml(read(path))
            require(type(value.get('version')) is int and value['version'] == 1, 'Conversion inventory accepts only the active version 1 declaration')
            if name == 'agents.yaml':
                closed(value, ('version', 'catalogs', 'agents'), ('version', 'catalogs', 'agents'), 'Old Agent declaration')
            report['checks'].append({'name': 'migration_input', 'path': [name], 'status': 'passed'})
            return value
        except Exception as exc:
            problem([name], 'invalid_legacy_input', 'Declaration could not be read or validated (' + type(exc).__name__ + ')')
            return {}

    active = document('agents.yaml', required=True)
    manifest = document('manifest.yaml', required=False)

    def fields(value, prefix=()):
        for key, item in value.items():
            if isinstance(item, dict) and item:
                yield from fields(item, (*prefix, key))
            else:
                yield (*prefix, key), item

    def settings_rows(value, target, source, role):
        result = []
        for path, item in fields(value):
            row = {'path': list(path), 'value_type': type(item).__name__, 'source': source, 'role': role}
            if target in ('claude-code', 'codex'):
                field = lookup(target, path)
                if field and field.binding:
                    row.update(status='needs_binding', reason='Native command must become a resource reference and a Machine executable binding')
                elif field:
                    try:
                        field.check(item, path)
                        row.update(status='supported', reason='Registered portable field type; target runtime capability still needs validation')
                    except SchemaError:
                        row.update(status='invalid_type_or_value', reason='Native field value does not satisfy the new registered type or enum')
                elif path == ('includeCoAuthoredBy',) or target == 'codex' and path == ('agents', 'max_threads'):
                    row.update(status='needs_semantic_conversion', reason='Old alias needs an explicit new-field mapping and difference report')
                elif path[:1] in (('hooks',), ('mcp_servers',), ('skills',), ('plugins',)):
                    row.update(status='needs_resource_conversion', reason='Resource declaration must become catalog references and usage options')
                else:
                    row.update(status='excluded', reason='Not in the selected portable Agent field scope; preserve existing native value')
                row['agent_type'] = target
            else:
                row['app'] = target
                key = path[0]
                if key not in registry(target) or path not in native_paths(target):
                    row.update(status='excluded', reason='Not in the selected App preference scope')
                else:
                    try:
                        # Excluded native siblings must not make portable color
                        # leaves look invalid or accidentally become a selection.
                        validate_app_settings(target, projection(value, [path]))
                        row.update(status='supported', reason='Registered App preference; deployment scope and App source audit still need validation')
                    except SchemaError:
                        row.update(status='invalid_type_or_value', reason='App preference does not satisfy the new registered schema')
            result.append(row)
        return result

    def asset(reference, *, owner, role, target=None):
        display = reference if isinstance(reference, str) and '://' not in reference else '<non-local or invalid reference>'
        row = {'reference': display, 'owner': owner, 'role': role}
        entries['assets'].append(row)
        if not isinstance(reference, str) or Path(reference).is_absolute() or '..' in Path(reference).parts or '\\' in reference:
            row['status'] = 'needs_explicit_source_binding'
            return
        path = root / reference
        if not path.resolve().is_relative_to(root.resolve()):
            row['status'] = 'external_symlink_needs_binding'
            return
        try:
            if path.is_dir():
                links = [str(item.relative_to(path)) for item in path.rglob('*') if item.is_symlink()]
                row.update(status='needs_resource_registration', directory=True, symlinks=links)
                return
            data = read(path)
            row.update(status='present', sha256=snapshots[str(path)]['sha256'])
            if target:
                codec = 'codex' if target == 'codex' else 'claude-code'
                value = parse_native(codec, data)
                destination = 'agent_settings' if target in ('claude-code', 'codex') else 'app_settings'
                entries[destination] += settings_rows(value, target, reference, role)
        except Exception as exc:
            row.update(status='missing_or_invalid', error_type=type(exc).__name__)
            problem([*owner, 'source'], 'invalid_asset', 'Referenced local input is missing, symlinked or not parseable')

    catalogs = active.get('catalogs', {})
    if not isinstance(catalogs, dict):
        problem(['agents.yaml', 'catalogs'], 'invalid_catalogs', 'Catalog declarations must be an object')
        catalogs = {}
    agents = active.get('agents', {})
    if not isinstance(agents, dict):
        problem(['agents.yaml', 'agents'], 'invalid_agents', 'Agent declarations must be an object')
        agents = {}
    for host, declaration in agents.items():
        if host not in ('claude', 'codex') or not isinstance(declaration, dict):
            problem(['agents.yaml', 'agents', host], 'unknown_agent', 'Old Agent entry has no unambiguous new Agent type')
            continue
        target = 'claude-code' if host == 'claude' else 'codex'
        plugins = declaration.get('plugins', [])
        if not isinstance(plugins, list):
            problem(['agents.yaml', 'agents', host, 'plugins'], 'invalid_plugin_list', 'Plugin selections must be an array')
            plugins = []
        for ref in plugins:
            valid = isinstance(ref, str) and REF.fullmatch(ref) and ref.rsplit('@', 1)[1] in catalogs
            if not valid or (host, ref) in seen:
                problem(['agents.yaml', 'agents', host, 'plugins'], 'invalid_plugin_selection', 'Plugin must have an explicit known catalog and unique selection')
                continue
            seen.add((host, ref))
            entries['plugins'].append({'agent_type': target, 'ref': ref, 'status': 'needs_catalog_and_native_identity_validation'})
        for name, item in declaration.items():
            owner = ['agents.yaml', 'agents', host, name]
            if name == 'plugins':
                continue
            if name in ('config', 'instructions', 'mcp') and isinstance(item, dict) and 'source' in item:
                asset(item['source'], owner=owner, role='active_agent_' + name, target=target if name == 'config' else None)
            elif name == 'files' and isinstance(item, list):
                for record in item:
                    if isinstance(record, dict) and 'source' in record:
                        asset(record['source'], owner=owner, role='active_resource_file')
                    else:
                        problem(owner, 'invalid_file_selection', 'Selected role/rule/hook file requires a source declaration')
            else:
                problem(owner, 'unclassified_agent_input', 'Agent input needs an explicit conversion adapter')

    def walk(node, path):
        if not isinstance(node, dict):
            return
        if 'source' in node:
            top = path[1]
            if top == 'external':
                return  # Remote installers are metadata, never local reads or executions.
            role = MANIFEST_ROUTES[top]
            target = 'claude-desktop' if top == 'archives' and 'claude_desktop' in path else 'claude-code' if top in ('settings', 'permissions') else None
            asset(node['source'], owner=path, role=role, target=target)
        for key, item in node.items():
            if isinstance(item, dict):
                walk(item, [*path, key])
    for key, value in manifest.items():
        if key == 'version':
            continue
        if key not in MANIFEST_ROUTES:
            problem(['manifest.yaml', key], 'unclassified_manifest_section', 'Manifest section requires an explicit migration classification')
            continue
        entries['manifest_sections'].append({'section': key, 'route': MANIFEST_ROUTES[key]})
        walk(value, ['manifest.yaml', key])
    fixed = manifest.get('env', {}).get('fixed', {})
    if isinstance(fixed, dict) and fixed:
        entries['agent_settings'] += settings_rows({'env': fixed}, 'claude-code', 'manifest.yaml#env.fixed', 'retained_legacy_environment')
    primary = {tuple(row['path']) for row in entries['agent_settings'] if row['role'] == 'active_agent_config' and row['agent_type'] == 'claude-code'}
    for row in entries['agent_settings']:
        if row['role'] == 'retained_legacy_settings' and tuple(row['path']) in primary:
            row['status'] = 'shadowed_legacy_setting'
            row['reason'] = 'Active agents.yaml config source is authoritative; legacy base must not overwrite it during conversion'
    entries['counts'] = {key: len(entries[key]) for key in ('plugins', 'agent_settings', 'app_settings', 'assets', 'manifest_sections')}
    report['file_snapshots'] = snapshots
    report['scope'] = [row['owner'] for row in entries['assets']]
    report['ready'] = not report['blockers']
    return report
