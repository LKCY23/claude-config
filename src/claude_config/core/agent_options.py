"""Portable Agent-definition usage options through the shared merge protocol."""
from copy import deepcopy

import tomlkit

from .fields import validate_settings
from .native import snapshot_read
from .resource_options import merge_options
from .schema import require


def option_keys(agent):
    return ('model', 'model_reasoning_effort') if agent == 'codex' else ('model',)


def definition_options(agent, content):
    from .resources import frontmatter
    value = tomlkit.parse(content.decode()).unwrap() if agent == 'codex' else frontmatter(content)[0]
    options = {key: value[key] for key in option_keys(agent) if key in value}
    require(all(isinstance(value, str) and value for value in options.values()), 'Agent model/reasoning options must be nonempty strings')
    if agent == 'codex':
        validate_settings(agent, options)
    return options


def agent_options(resources, ref, item):
    """Source-present settings override; omitted native options are retained.

    Only known, unchanged managed definitions supply current values. Body edits,
    missing files and mode changes remain resource drift, not an option policy.
    """
    from .resources import render_package
    agent = resources.manager.agent
    incoming = definition_options(agent, item['files'][item['package'].entry])
    current = {}
    old = resources.previous.get(ref)
    if old and old['native'].get('identity'):
        suffix = '.toml' if agent == 'codex' else '.md'
        path = resources._target(resources.manager.native_path.parent, 'agents/' + old['native']['identity'] + suffix)
        expected = old.get('files', {}).get(str(path))
        if expected is not None:
            content, actual = snapshot_read(path)
            resources.manager.sources[path] = actual
            if ref in resources.conflicts.restore:
                content = resources.conflicts.baseline_file(ref, path)
            elif actual != expected:
                resources._block(ref, 'resource_drift', 'Managed Agent definition changed or disappeared outside the baseline')
                return item
            current = definition_options(agent, content)
    merged = merge_options(resources, ref, incoming, current)
    if not merged:
        return item
    options = deepcopy(item['package'].selection.get('options', {}))
    options.update(merged)
    result = render_package(item['package'], agent, options=options)
    result['slots'] = item['slots']
    return result
