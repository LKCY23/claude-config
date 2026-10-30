"""Owned-resource drift reviews and one-review-only resolution decisions."""
from copy import deepcopy
import difflib
import hashlib
import json
from pathlib import Path
import re

from .merge import fingerprint
from .native import parse_json, snapshot_read
from .schema import as_pointer, closed, mapping, pointer, require
from .transaction import FileChange, PlanStale, directory_fingerprint, file_fingerprint
from .resource_inventory import change_kind


def encoded(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()


def validate_review(value):
    closed(value, ('files', 'registration', 'instruction'), ('files',), 'resource review baseline')
    for target, snapshot in mapping(value['files'], 'resource baseline files').items():
        require(Path(target).is_absolute() and isinstance(snapshot, str) and Path(snapshot).is_absolute(), 'Invalid resource baseline reference')
    if 'registration' in value:
        require(isinstance(value['registration'], str) and Path(value['registration']).is_absolute(), 'Invalid registration baseline reference')
    if 'instruction' in value:
        require(isinstance(value['instruction'], str) and Path(value['instruction']).is_absolute(), 'Invalid instruction baseline reference')


class ResourceConflicts:
    def __init__(self, resources, resolutions=None):
        self.r = resources
        self.manager = resources.manager
        self.resolutions = resolutions or {}
        self.reviews, self.restore, self.kept, self.used = [], set(), set(), set()
        self.snapshots = {}
        self.locations = {}
        self.native_details = {}

    def _snapshot_path(self, data):
        return self.manager.state_root / 'resource-baselines' / (hashlib.sha256(data).hexdigest() + '.bin')

    def save(self, data):
        path = self._snapshot_path(data)
        if path not in self.snapshots:
            before, expected = snapshot_read(path)
            self.manager.sources[path] = expected
            require(not expected['exists'] or before == data, 'Private resource baseline was externally changed')
            digest = hashlib.sha256(data).hexdigest()
            self.snapshots[path] = FileChange(path, data, 'resource-baseline',
                lambda content, digest=digest: require(hashlib.sha256(content).hexdigest() == digest, 'Resource baseline digest differs'), expected, 0o600)
        return str(path)

    def load(self, location, digest=None):
        path = Path(location)
        require(path.parent.resolve() == (self.manager.state_root / 'resource-baselines').resolve()
                and re.fullmatch(r'[0-9a-f]{64}\.bin', path.name), 'Resource baseline escapes private snapshot storage')
        data, expected = snapshot_read(path)
        self.manager.sources[path] = expected
        require(expected['exists'] and self._snapshot_path(data).name == path.name, 'Private resource baseline is missing or changed')
        require(digest is None or hashlib.sha256(data).hexdigest() == digest, 'Resource baseline does not match the managed digest')
        return data

    def baseline_file(self, ref, path):
        old = self.r.previous[ref]
        review = old['native'].get('review', {})
        require(str(path) in review.get('files', {}), 'Prior resource body snapshot is unavailable; prepare a new review baseline')
        return self.load(review['files'][str(path)], old['files'][str(path)]['sha256'])

    def baseline_registration(self, ref):
        old = self.r.previous[ref]
        review = old['native'].get('review', {})
        require('registration' in review, 'Prior native registration body is unavailable; cannot resolve this drift')
        value = parse_json(self.load(review['registration']))
        registration = old['native']['registration']
        if registration['kind'] == 'mcp':
            require(fingerprint(value) == registration['sha256'], 'MCP baseline differs from its recorded digest')
        else:
            require([{'event': row['event'], 'sha256': fingerprint(row['body'])} for row in value['groups']] == registration['groups'],
                    'Hook baseline differs from its recorded groups')
        return value

    def _native(self, ref, item):
        old = self.r.previous[ref]
        state = old['native']
        result, incoming, issues = {}, {}, []
        if 'registration' in state:
            from .registrations import mcp_definition, hook_definition
            registration = state['registration']
            path, doc = self.r.registrations.document(Path(registration['path']))
            if registration['kind'] == 'mcp':
                key = 'mcpServers' if self.manager.agent == 'claude-code' else 'mcp_servers'
                entries = mapping(doc.get(key, {}), 'native MCP entries')
                actual = entries.get(registration['name'])
                require(actual is None or isinstance(actual, dict), 'Native MCP entry has an invalid type')
                result['mcp'] = {'exists': actual is not None, 'sha256': fingerprint(actual) if actual is not None else None}
                name, desired = mcp_definition(item, self.manager.agent)
                incoming['mcp'] = {'name': name, 'sha256': fingerprint(desired)}
                self.native_details[ref] = {'local': actual, 'incoming': desired}
                if name != registration['name'] and name in entries:
                    issues.append('unmanaged_mcp_collision')
                from .registrations import claude_user_config
                wanted_path = claude_user_config(self.manager.entry['binding']) if self.manager.agent == 'claude-code' else self.manager.native_path
                if Path(registration['path']).resolve() != wanted_path.resolve():
                    issues.append('registration_binding_changed')
            else:
                events = mapping(doc.get('hooks', {}), 'native Hook events')
                base = self.baseline_registration(ref)
                positions, groups, local_bodies = [], [], []
                for row in base['groups']:
                    rows = events.get(row['event'], [])
                    require(isinstance(rows, list) and all(isinstance(group, dict) for group in rows), 'Invalid native Hook groups')
                    candidates = [i for i, group in enumerate(rows) if fingerprint(group) == fingerprint(row['body'])]
                    if not candidates:
                        candidates = [i for i, group in enumerate(rows) if group.get('matcher') == row['body'].get('matcher')]
                        if len(candidates) == 1 and row.get('index') != candidates[0]:
                            issues.append('hook_identity_ambiguous')
                        if not candidates and any(fingerprint(group) not in base.get('context', {}).get(row['event'], []) for group in rows):
                            issues.append('hook_identity_unknown')
                    if len(candidates) > 1:
                        issues.append('hook_identity_ambiguous')
                    index = candidates[0] if len(candidates) == 1 else None
                    if index is not None:
                        require(isinstance(rows[index].get('hooks'), list) and all(isinstance(h, dict) and h.get('type') == 'command'
                                and isinstance(h.get('command'), str) for h in rows[index]['hooks']), 'Invalid native Hook handler type')
                        positions.append((row['event'], index))
                        local_bodies.append({'event': row['event'], 'body': rows[index]})
                    groups.append({'event': row['event'], 'exists': index is not None,
                                   'sha256': fingerprint(rows[index]) if index is not None else None})
                if len(positions) != len(set(positions)):
                    issues.append('hook_identity_ambiguous')
                self.locations[ref] = positions
                result['hooks'] = groups
                incoming['hooks'] = {event: [fingerprint(group) for group in groups]
                                     for event, groups in hook_definition(item, self.manager.agent).items()}
                self.native_details[ref] = {'local': {'groups': local_bodies}, 'incoming': hook_definition(item, self.manager.agent)}
        if 'skill_control' in state:
            old_row = state['skill_control']
            _, doc = self.r.registrations.document(self.manager.native_path)
            if 'name' in old_row:
                overrides = mapping(doc.get('skillOverrides', {}), 'skillOverrides')
                name = old_row['name']
                value = overrides.get(name)
                observed = {'path': old_row['path'], 'enabled': value == 'on'} if value in ('on', 'off') else {'state': value}
                if 'plugin_selector' in old_row:
                    plugins = mapping(doc.get('enabledPlugins', {}), 'enabledPlugins')
                    observed['plugin_enabled'] = plugins.get(old_row['plugin_selector'])
                    if name not in overrides:
                        observed['override_missing'] = True
                    if old_row['plugin_selector'] not in plugins:
                        observed['plugin_missing'] = True
                result['skill_control'] = {'exists': name in overrides,
                                           'sha256': fingerprint(observed) if name in overrides or 'plugin_selector' in old_row else None}
            else:
                rows = mapping(doc.get('skills', {}), 'native skills').get('config', [])
                require(isinstance(rows, list) and all(isinstance(row, dict) for row in rows), 'Invalid skills.config')
                matches = [row for row in rows if row.get('path') == old_row['path']]
                if len(matches) > 1 or any(row.get('name') == state['identity'] for row in rows):
                    issues.append('skill_identity_ambiguous')
                result['skill_control'] = {'exists': len(matches) == 1, 'sha256': fingerprint(matches[0]) if len(matches) == 1 else None}
                if matches:
                    require(set(matches[0]) == {'path', 'enabled'} and type(matches[0]['enabled']) is bool, 'Invalid native skill activation')
            incoming['skill_control'] = item['package'].selection.get('options', {}).get('control', {})
        if 'global_block' in state:
            from .resources import START, END
            block = state['global_block']
            data, expected = snapshot_read(block['path'])
            self.manager.sources[Path(block['path'])] = expected
            text = data.decode()
            require(text.count(START) == text.count(END) and text.count(START) <= 1, 'Malformed instruction ownership markers')
            body = text[text.index(START):text.index(END) + len(END)] if START in text else None
            result['instruction'] = {'exists': body is not None, 'sha256': hashlib.sha256(body.encode()).hexdigest() if body is not None else None}
            from .resource_references import instruction_body
            desired = START + '\n' + instruction_body(item, self.manager.agent) + '\n' + END
            incoming['instruction'] = {'sha256': hashlib.sha256(desired.encode()).hexdigest()}
            self.native_details[ref] = {'local': {'block': body}, 'incoming': {'block': desired}}
        if 'plugin' in state:
            from .native_plugins import PluginOperation
            from .registrations import claude_user_config
            receipt = state['plugin']
            executable = Path(self.manager.entry['binding']['executable']).resolve()
            operation = PluginOperation({'agent': self.manager.agent, 'root': str(self.manager.native_path.parent.resolve()),
                'user_config': str(claude_user_config(self.manager.entry['binding']).resolve()) if self.manager.agent == 'claude-code' else None,
                'executable': str(executable), 'executable_snapshot': file_fingerprint(executable),
                'plugins': [{'ref': ref, 'desired': receipt, 'previous': receipt}]})
            for path in operation.paths:
                if path == operation.user_config:
                    continue
                _, expected = snapshot_read(path)
                self.manager.sources[path] = expected
            self.r.directories.add(operation.cache_parent(receipt))
            result['plugin'] = operation.native(receipt)
            incoming['plugin'] = {'image': item['rendered_fingerprint'], 'options': item['package'].selection.get('options', {})}
            if not result['plugin']['installed'] or not result['plugin']['marketplace'] or any(result['plugin'][key] != receipt[key] for key in ('version', 'image')):
                issues.append('plugin_version_or_image_unknown')
        return result, incoming, issues

    def inspect(self, ref, item, packages):
        old = self.r.previous.get(ref)
        if old is None:
            return False
        base_files, local_files, differences, issues = old['files'], {}, [], []
        for location, expected in base_files.items():
            path = Path(location)
            data, actual = snapshot_read(path)
            self.manager.sources[path] = actual
            local_files[location] = actual
            if actual != expected:
                base_data = None
                snapshot = old['native'].get('review', {}).get('files', {}).get(location)
                if snapshot:
                    base_data = self.load(snapshot, expected['sha256'])
                spans = None
                if base_data is not None:
                    try:
                        spans = [{'tag': tag, 'base_lines': [a + 1, b], 'local_lines': [c + 1, d]}
                                 for tag, a, b, c, d in difflib.SequenceMatcher(None, base_data.decode().splitlines(), data.decode().splitlines()).get_opcodes() if tag != 'equal']
                    except UnicodeError:
                        pass
                differences.append({'path': location, 'baseline': expected, 'local': actual,
                                    'kind': change_kind(expected, actual),
                                    'baseline_body_available': base_data is not None, 'line_changes': spans})
        roots = [Path(old['cache'])]
        if old['native'].get('root'):
            roots.append(Path(old['native']['root']))
        for root in roots:
            self.r.directories.add(root)
            world = directory_fingerprint(root)
            for relative, value in world['entries'].items():
                if str(root / relative) not in base_files or value.get('kind') == 'symlink':
                    issues.append('unmanaged_resource_collision')
        native, desired_native, native_issues = self._native(ref, item)
        issues.extend(native_issues)
        baseline_native = {}
        state = old['native']
        registration = state.get('registration')
        if registration:
            baseline_native['mcp' if registration['kind'] == 'mcp' else 'hooks'] = (
                {'exists': True, 'sha256': registration['sha256']} if registration['kind'] == 'mcp' else
                [{'event': group['event'], 'exists': True, 'sha256': group['sha256']} for group in registration['groups']])
        if 'skill_control' in state:
            baseline_native['skill_control'] = {'exists': True, 'sha256': fingerprint(state['skill_control']['entry'])}
        if 'global_block' in state:
            baseline_native['instruction'] = {'exists': True, 'sha256': state['global_block']['sha256']}
        if 'plugin' in state:
            baseline_native['plugin'] = {'installed': True, 'version': state['plugin']['version'], 'image': state['plugin']['image'],
                                         'enabled': state['plugin']['enabled'], 'marketplace': True}
        drift = bool(differences) or native != baseline_native
        if not drift and not issues:
            return False
        executable = Path(self.manager.entry['binding']['executable']).resolve()
        runtimes = [executable]
        app = self.manager.machine.get('apps', {}).get('codex-app') if self.manager.agent == 'codex' else None
        if app and app['binding'].get('backend'):
            runtimes.append(Path(app['binding']['backend']).resolve())
        runtime_snapshots = {str(path): file_fingerprint(path) for path in runtimes}
        self.manager.sources.update({path: runtime_snapshots[str(path)] for path in runtimes})
        binding_snapshots = {slot: self.manager.sources.get(Path(value)) for slot, value in item['slots'].items()
                             if isinstance(value, str) and Path(value).is_absolute() and Path(value).is_file()}
        identity = {'protocol': 'resource-drift-v1', 'agent': self.manager.agent, 'binding': str(self.manager.native_path.resolve()), 'ref': ref,
                    'baseline': {'files': base_files, 'native': baseline_native, 'management_record_fingerprint': fingerprint(old)},
                    'local': {'files': local_files, 'native': native},
                    'incoming': {'commit': item['package'].commit, 'rendered': item['rendered_fingerprint'],
                                 'selection': item['package'].selection, 'slots': item['slots'], 'native': desired_native,
                                 'runtime_snapshots': runtime_snapshots, 'binding_snapshots': binding_snapshots,
                                 'policies': [self.r.profile.get('merge_policy'), self.r.run_policy, self.manager.machine.get('merge_policy')],
                                 'dependencies': {dep: packages[dep].source_fingerprint for dep in packages}}}
        conflict_id = fingerprint(identity)
        selector = as_pointer(('resources', item['package'].group, ref))
        decision = self.resolutions.get(selector)
        available = ['adopt_source', 'keep_local', 'update_source'] if not issues else []
        if any(not difference['baseline_body_available'] for difference in differences):
            available = [action for action in available if action != 'adopt_source']
        declared_changes = []
        cache = Path(old['cache'])
        names = set(item['files']) | {str(Path(location).relative_to(cache)) for location in base_files if Path(location).is_relative_to(cache)}
        for name in sorted(names):
            location = str(cache / name)
            before = base_files.get(location)
            wanted = item['files'].get(name)
            after = {'exists': True, 'sha256': hashlib.sha256(wanted).hexdigest(), 'mode': item['modes'][name]} if wanted is not None else {'exists': False}
            if before == after:
                continue
            declared_changes.append({'package_path': name, 'baseline': before or {'exists': False}, 'declared_incoming': after})
        row = {'conflict_id': conflict_id, 'path': ['resources', item['package'].group, ref], 'ref': ref,
               'baseline': identity['baseline'], 'local': identity['local'],
               'incoming': {'commit': item['package'].commit, 'rendered_fingerprint': item['rendered_fingerprint'],
                            'native': desired_native, 'stage': 'declaration_before_optional_usage_preservation'},
               'file_changes': differences, 'declared_source_changes': declared_changes, 'issues': sorted(set(issues)),
               'eligible_actions': available, 'status': 'unresolved',
               'workflow': {'options': {'operation': 'export', 'resource': ref, 'writes': 'Profile only; apply still requires a fresh drift review'},
                            'body': {'operation': 'manual_source_authoring', 'review_operation': 'review-conflicts', 'automated': False,
                                     'destination': 'claudespace resource source',
                                     'steps': ['inspect_private_diff', 'edit_resource_source', 'validate_and_publish_baseline', 'select_baseline_and_replan']}}}
        self.reviews.append(row)
        if decision:
            self.used.add(selector)
            require(decision['conflict_id'] == conflict_id, 'Resource conflict review is stale; inspect the new baseline/local/source fingerprints')
            require(decision['action'] in available, 'Resource identity, ownership or structure cannot be overridden by a resolution')
            row['action'] = decision['action']
            if decision['action'] == 'adopt_source':
                self.restore.add(ref)
                row['status'] = 'resolved_adopt_source'
                return False
            if decision['action'] == 'keep_local':
                self.kept.add(ref)
                self.r.records[ref] = deepcopy(old)
                item['cache'] = Path(old['cache'])
                row['status'] = 'resolved_keep_local'
                self.r.checks.append({'name': 'resource_resolution', 'ref': ref, 'status': 'passed',
                                     'reason': 'One reviewed run retains local content; management baseline and Profile remain unchanged'})
                return True
            row['status'] = 'source_update_required'
        code = 'resource_drift'
        if registration and not differences:
            code = 'mcp_drift' if registration['kind'] == 'mcp' else 'hook_drift'
        if 'registration_binding_changed' in issues:
            code = 'registration_binding_changed'
        self.r._block(ref, 'resource_source_update_required' if row['status'] == 'source_update_required' else code,
                      'Update the selected Profile/resource source and replan' if row['status'] == 'source_update_required' else
                      'Owned resource differs from its baseline; choose an eligible action for this exact review')
        return True

    def check_kept_dependencies(self, packages, bindings):
        changed = {ref for ref in self.kept if self.r.previous[ref]['source_fingerprint'] != packages[ref].source_fingerprint
                   or self.r.previous[ref]['rendered_fingerprint'] != self.r.rendered[ref]['rendered_fingerprint']}
        while True:
            expanded = changed | {ref for ref, package in packages.items() if
                {dep if '@' in dep else dep + '@' + ref.split('@')[1] for dep in package.item['requires'].get('resources', [])} & changed}
            if expanded == changed:
                break
            changed = expanded
        for ref, package in packages.items():
            deps = {dep if '@' in dep else dep + '@' + ref.split('@')[1] for dep in package.item['requires'].get('resources', [])}
            if ref not in self.kept and deps & changed:
                self.r._block(ref, 'kept_resource_dependency', 'A required resource retained its prior state instead of the selected new baseline')
        for field, declaration in bindings.items():
            if any(isinstance(arg, dict) and arg['resource'] in changed for arg in declaration['args']):
                self.r.checks.append({'name': 'resource_dependency', 'path': list(pointer(field)), 'status': 'blocked',
                                     'code': 'kept_resource_dependency', 'reason': 'Bound command requires the skipped new resource image'})

    def freeze(self):
        # Restore any changed prior owned images that are still referenced for
        # rollback/commands, even when the new image lives at another path.
        written = {str(change.path) for change in self.r.changes}
        for ref in self.restore:
            for location, expected in self.r.previous[ref]['files'].items():
                if location in written:
                    continue
                _, actual = snapshot_read(location)
                if actual != expected:
                    content = self.baseline_file(ref, location)
                    self.r.changes.append(FileChange(Path(location), content, 'resource:' + ref,
                        lambda data, digest=expected['sha256']: require(hashlib.sha256(data).hexdigest() == digest, 'Prior image restore digest differs'), actual, expected['mode']))
        planned = {str(change.path): change.content for change in self.r.changes if not change.delete}
        for ref, record in self.r.records.items():
            if ref not in self.r.processed or any(row['status'] == 'blocked' and (row.get('ref') == ref or row.get('path', [])[:3] == ['resources', record['group'], ref]) for row in self.r.checks):
                continue
            review = {'files': {}}
            for location, expected in record['files'].items():
                data = planned.get(location)
                if data is None:
                    data, actual = snapshot_read(location)
                    self.manager.sources[Path(location)] = actual
                require(hashlib.sha256(data).hexdigest() == expected['sha256'], 'Cannot record a baseline from a differing resource body')
                review['files'][location] = self.save(data)
            registration = record['native'].get('registration')
            if registration:
                doc = self.r.registrations.documents[Path(registration['path'])]['value']
                if registration['kind'] == 'mcp':
                    key = 'mcpServers' if self.manager.agent == 'claude-code' else 'mcp_servers'
                    value = doc[key][registration['name']]
                else:
                    value = {'groups': [], 'context': {group['event']: [fingerprint(row) for row in doc['hooks'][group['event']]]
                                                     for group in registration['groups']}}
                    for group in registration['groups']:
                        matches = [row for row in doc['hooks'][group['event']] if fingerprint(row) == group['sha256']]
                        require(len(matches) == 1, 'Cannot capture an ambiguous Hook baseline')
                        value['groups'].append({'event': group['event'], 'body': matches[0],
                                                'index': doc['hooks'][group['event']].index(matches[0])})
                review['registration'] = self.save(encoded(value))
            if 'global_block' in record['native']:
                from .resources import START, END
                item = self.r.rendered[ref]
                from .resource_references import instruction_body
                block = START + '\n' + instruction_body(item, self.manager.agent) + '\n' + END
                require(hashlib.sha256(block.encode()).hexdigest() == record['native']['global_block']['sha256'], 'Instruction snapshot differs from managed block')
                review['instruction'] = self.save(block.encode())
            record['native']['review'] = review
        self.r.changes += list(self.snapshots.values())
        require(set(self.resolutions) == self.used, 'A resource resolution did not match a current selected conflict')

    def private_review(self):
        """Immutable local details, separate from portable Profile or publication."""
        changes = []
        for row in self.reviews:
            ref = row['ref']
            old = self.r.previous[ref]
            root = self.manager.state_root / 'resource-reviews' / row['conflict_id']
            manifest = {'conflict_id': row['conflict_id'], 'ref': ref, 'files': [], 'workflow': row['workflow'],
                        'incoming_stage': row['incoming']['stage'], 'declared_source_changes': row['declared_source_changes']}
            for index, difference in enumerate(row['file_changes']):
                location = difference['path']
                current, expected = snapshot_read(location)
                require(expected == difference['local'], 'Resource changed while preparing review details')
                self.manager.sources[Path(location)] = expected
                base = self.baseline_file(ref, location) if difference['baseline_body_available'] else None
                sources = [('local', current)] + ([('baseline', base)] if base is not None else [])
                for label, data in sources:
                    path = root / (str(index) + '-' + label + '.bin')
                    _, before = snapshot_read(path)
                    changes.append(FileChange(path, data, 'private-resource-review', lambda content: None, before, 0o600))
                manifest['files'].append({'target': location, 'index': index, 'baseline_available': base is not None})
            for name, data in self.r.rendered[ref]['files'].items():
                path = root / 'incoming' / name
                _, before = snapshot_read(path)
                changes.append(FileChange(path, data, 'private-resource-review', lambda content: None, before, 0o600))
            if ref in self.native_details:
                for label, value in self.native_details[ref].items():
                    path = root / ('native-' + label + '.json')
                    _, before = snapshot_read(path)
                    changes.append(FileChange(path, encoded(value), 'private-resource-review', lambda content: json.loads(content), before, 0o600))
                if 'registration' in old['native']:
                    base = self.baseline_registration(ref)
                else:
                    review = old['native'].get('review', {})
                    require('instruction' in review, 'Instruction baseline body is unavailable')
                    base = {'block': self.load(review['instruction'], old['native']['global_block']['sha256']).decode()}
                path = root / 'native-baseline.json'
                _, before = snapshot_read(path)
                changes.append(FileChange(path, encoded(base), 'private-resource-review', parse_json, before, 0o600))
            path = root / 'review.json'
            _, before = snapshot_read(path)
            changes.append(FileChange(path, encoded(manifest), 'private-resource-review', parse_json, before, 0o600))
        return changes
