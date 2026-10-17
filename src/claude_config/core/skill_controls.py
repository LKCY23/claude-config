"""Per-skill activation in native settings, separate from installed body and invocation."""
from copy import deepcopy
from pathlib import Path

from .resource_options import merge_options
from .schema import closed, mapping, require


def invocation_controls(resources, ref, item, skill_root):
    """Render only portable invocation leaves; activation remains in native settings."""
    from .native import parse_yaml, snapshot_read
    from .resources import frontmatter
    agent = resources.manager.agent
    keys = ('allow_implicit_invocation',) if agent == 'codex' else ('disable-model-invocation', 'user-invocable')
    if agent == 'codex':
        relative = 'agents/openai.yaml'
        source = parse_yaml(item['files'][relative]).get('policy', {}) if relative in item['files'] else {}
    else:
        relative = 'SKILL.md'
        source, _, _ = frontmatter(item['files'][relative])
    incoming = {'control': {key: source[key] for key in keys if key in source}}
    current = {}
    old = resources.previous.get(ref)
    path = Path(old['native']['root']) / relative if old and old['native'].get('root') else skill_root / relative
    expected = old.get('files', {}).get(str(path)) if old else None
    if expected:
        content, actual = snapshot_read(path)
        resources.manager.sources[path] = actual
        if ref in resources.conflicts.restore:
            content = resources.conflicts.baseline_file(ref, path)
        elif actual != expected:
            resources._block(ref, 'resource_drift', 'Skill invocation definition changed or disappeared outside the managed baseline')
            return item
        if agent == 'codex':
            data = parse_yaml(content).get('policy', {})
        else:
            data, _, _ = frontmatter(content)
        require(isinstance(data, dict), 'Native skill policy must be an object')
        current = {'control': {key: data[key] for key in keys if key in data}}
    for value in (*incoming.get('control', {}).values(), *current.get('control', {}).values()):
        require(type(value) is bool, 'Native skill invocation controls must be Boolean')
    merged = merge_options(resources, ref, incoming, current).get('control', {})
    if not merged:
        return item
    from .resources import render_package
    options = deepcopy(item['package'].selection.get('options', {}))
    options.setdefault('control', {}).update(merged)
    effective = render_package(item['package'], agent, options=options)
    effective['slots'] = item['slots']
    return effective


def validate_skill_control(value):
    closed(value, ('path', 'entry', 'name', 'plugin_selector'), ('path', 'entry'), 'managed skill activation')
    if 'name' in value:
        require(isinstance(value['name'], str) and bool(value['name']), 'Claude skill selector requires a name')
    require(isinstance(value['path'], str) and Path(value['path']).is_absolute() and Path(value['path']).name == 'SKILL.md', 'Skill activation identity must be an absolute SKILL.md path')
    closed(value['entry'], ('path', 'enabled', 'plugin_enabled'), ('path', 'enabled'), 'managed native skill entry')
    require(value['entry']['path'] == value['path'] and type(value['entry']['enabled']) is bool, 'Invalid native skill control baseline')
    if 'plugin_selector' in value:
        require('name' in value and value['plugin_selector'] == value['name'] + '@skills-dir', 'Invalid automatic skill plugin selector')
        require(type(value['entry'].get('plugin_enabled')) is bool and value['entry']['plugin_enabled'] == value['entry']['enabled'],
                'Skill and automatic plugin activation must agree')
    else:
        require('plugin_enabled' not in value['entry'], 'Plugin activation requires its owned selector')


def claude_skill_plugin_selector(item):
    """Only Claude's observed same-name skills-dir convention is supported."""
    manifest = item['files'].get('.claude-plugin/plugin.json')
    if manifest is None:
        return None
    from .native import parse_json
    require(parse_json(manifest).get('name') == item['identity'],
            'Skill-directory plugin name differs from the skill identity; reconcile its packaging')
    return item['identity'] + '@skills-dir'


def register_skill_control(resources, ref, item, skill_root):
    if resources.manager.agent == 'claude-code':
        return register_claude_skill_control(resources, ref, item, skill_root)
    controls = item['package'].selection.get('options', {}).get('control', {})
    old = resources.previous.get(ref, {}).get('native', {}).get('skill_control')
    if 'enabled' not in controls and old is None:
        return
    path = str((skill_root / 'SKILL.md').resolve())
    registration = resources.registrations
    require(registration is not None, 'Native skill control context is required')
    _, document = registration.document(resources.manager.native_path)
    native = mapping(document.get('skills', {}), 'native skills configuration')
    rows = native.get('config', [])
    require(isinstance(rows, list) and all(isinstance(row, dict) for row in rows), 'Native skills.config must be an array of objects')
    identity = item['identity']
    candidates = []
    for index, row in enumerate(rows):
        if row.get('name') == identity:
            resources._block(ref, 'skill_selector_collision', 'Name-based skill control overlaps the selected path; reconcile native selectors')
            return
        if isinstance(row.get('path'), str) and Path(row['path']).is_absolute():
            actual = str(Path(row['path']).resolve())
            if actual == path or old is not None and actual == old['path']:
                candidates.append((index, row))
    if old:
        if old['path'] != path and Path(old['path']).parent.parent != Path(path).parent.parent:
            resources._block(ref, 'skill_binding_changed', 'Moving the skill installation root requires explicit control relocation')
            return
        previous = [(index, row) for index, row in candidates if str(Path(row['path']).resolve()) == old['path']]
        if not previous and ref in resources.conflicts.restore and not candidates:
            previous = [(len(rows), old['entry'])]
        if len(previous) != 1 or (len(candidates) != 1 and ref not in resources.conflicts.restore) or set(previous[0][1]) != {'path', 'enabled'}:
            resources._block(ref, 'skill_control_drift', 'Managed skill activation row disappeared, changed identity or has ambiguous duplicates')
            return
        index, before = previous[0]
        require(type(before['enabled']) is bool, 'Native skill enabled must be Boolean')
        if ref in resources.conflicts.restore:
            before = old['entry']
        elif before['enabled'] != old['entry']['enabled']:
            resources._block(ref, 'skill_control_drift', 'Skill activation was externally changed; global source_wins cannot override it')
            return
    elif candidates:
        resources._block(ref, 'unmanaged_skill_control_collision', 'Native path selector already exists outside this resource ownership')
        return
    else:
        index, before = len(rows), {}
    incoming = {'control': {'enabled': controls['enabled']} if 'enabled' in controls else {}}
    current = {'control': {'enabled': before['enabled']}} if before else {}
    merged = merge_options(resources, ref, incoming, current)
    enabled = merged['control']['enabled']
    entry = {'path': path, 'enabled': enabled}
    result = deepcopy(rows)
    if index == len(result):
        result.append(entry)
    else:
        result[index] = entry
    document.setdefault('skills', {})['config'] = result
    registration.main_fields.add(('skills', 'config'))
    resources.records[ref]['native']['skill_control'] = {'path': path, 'entry': entry}
    if old and old['path'] != path:
        resources._decision(ref, ['native_selector'], {'path': old['path']}, {'path': path}, 'retire_previous_native_identity',
                            'Replace only the unchanged owned selector after the fixed skill identity changes')
    resources.checks.append({'name': 'native_skill_activation', 'ref': ref, 'status': 'passed', 'level': 'documented-config-interface',
                            'reason': 'Path-based activation declaration; current sessions and policy overrides are not verified'})


def register_claude_skill_control(resources, ref, item, skill_root):
    """Keep bodies installed; own the skill override and any automatic plugin gate."""
    controls = item['package'].selection.get('options', {}).get('control', {})
    old = resources.previous.get(ref, {}).get('native', {}).get('skill_control')
    if 'enabled' not in controls and old is None:
        return
    name, path = item['identity'], str(skill_root / 'SKILL.md')
    registration = resources.registrations
    _, document = registration.document(resources.manager.native_path)
    overrides = mapping(document.get('skillOverrides', {}), 'skillOverrides')
    selector = claude_skill_plugin_selector(item)
    previous_selector = old.get('plugin_selector') if old else None
    plugins = mapping(document.get('enabledPlugins', {}), 'enabledPlugins')
    if previous_selector:
        if selector != previous_selector:
            resources._block(ref, 'skill_control_binding_changed', 'Automatic skill plugin identity changed; reconcile the owned selector')
            return
        if ref not in resources.conflicts.restore and plugins.get(selector) is not old['entry']['plugin_enabled']:
            resources._block(ref, 'skill_control_drift', 'Automatic skill plugin activation changed outside the managed baseline')
            return
    elif selector in plugins:
        resources._block(ref, 'unmanaged_skill_control_collision', 'Automatic skill plugin selector already exists outside this resource ownership')
        return
    current = {}
    if old:
        if old.get('name') != name or old['path'] != path:
            resources._block(ref, 'skill_control_binding_changed', 'Claude skill identity or installation binding changed; reconcile before moving the selector')
            return
        before = 'on' if old['entry']['enabled'] else 'off'
        if ref not in resources.conflicts.restore and overrides.get(name) != before:
            resources._block(ref, 'skill_control_drift', 'Claude skill override changed or disappeared outside the managed baseline')
            return
        current = {'control': {'enabled': old['entry']['enabled']}}
    elif name in overrides:
        resources._block(ref, 'unmanaged_skill_control_collision', 'Claude skill override already exists outside this resource ownership')
        return
    incoming = {'control': {'enabled': controls['enabled']}} if 'enabled' in controls else {}
    enabled = merge_options(resources, ref, incoming, current)['control']['enabled']
    document.setdefault('skillOverrides', {})[name] = 'on' if enabled else 'off'
    registration.main_fields.add(('skillOverrides', name))
    resources.records[ref]['native']['skill_control'] = {'path': path, 'name': name, 'entry': {'path': path, 'enabled': enabled}}
    if selector:
        document.setdefault('enabledPlugins', {})[selector] = enabled
        registration.main_fields.add(('enabledPlugins', selector))
        state = resources.records[ref]['native']['skill_control']
        state['plugin_selector'] = selector
        state['entry']['plugin_enabled'] = enabled
    resources.checks.append({'name': 'native_skill_activation', 'ref': ref, 'status': 'passed', 'level': 'documented-config-interface',
                            'reason': 'Claude skill override and automatic skill-directory plugin gate follow the same selection; body remains installed. Existing sessions require native verification.'})
