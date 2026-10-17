"""One incremental merge algorithm for export, apply and Profile switching."""
from copy import deepcopy
import hashlib
import json
import re

from .schema import as_pointer, policy as validate_policy, pointer

MISSING = object()
SECRET = re.compile(r'(?:^|_)(?:password|secret|api_key|access_token|refresh_token|auth_token|authorization|bearer_token)$', re.I)


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode()).hexdigest()


def redact(value, path=()):
    if path and SECRET.search(path[-1].replace('-', '_')):
        return '<redacted>'
    if isinstance(value, dict):
        return {key: redact(child, (*path, key)) for key, child in value.items()}
    if isinstance(value, list):
        return [redact(child, path) for child in value]
    return value


def same(a, b):
    # Python considers False == 0 and 1 == 1.0; configuration types are explicit.
    if type(a) is not type(b):
        return False
    if isinstance(a, dict):
        return a.keys() == b.keys() and all(same(a[key], b[key]) for key in a)
    if isinstance(a, list):
        return len(a) == len(b) and all(same(x, y) for x, y in zip(a, b))
    return a == b


def get(value, path):
    for part in path:
        if not isinstance(value, dict) or part not in value:
            return MISSING
        value = value[part]
    return value


class MergePlan:
    """Pure planner; callers validate both declared input and final native output.

    baseline is the last applied managed projection, not an entire runtime file.
    Drift resolutions are scoped to the exact current target fingerprint.
    """
    def __init__(self, incoming, target, *, source, destination, scope=None,
                 profile_policy=None, run_policy=None, machine_policy=None,
                 baseline=None, resolutions=None, set_lists=(), previous=None, number_fields=()):
        if not isinstance(incoming, dict) or not isinstance(target, dict):
            raise ValueError('Merge inputs must be mappings')
        self.incoming, self.target = deepcopy(incoming), deepcopy(target)
        self.source, self.destination = source, destination
        self.policies = {'run': run_policy or {}, 'profile': profile_policy or {}, 'machine': machine_policy or {}}
        for rules in self.policies.values():
            validate_policy(rules)
        if self.policies['machine'].get('fields'):
            raise ValueError('Machine provides a default policy, not field policies')
        self.scope = None if scope is None else tuple(pointer(p) for p in scope)
        if self.scope is not None and len(set(self.scope)) != len(self.scope):
            raise ValueError('Duplicate selected paths')
        if self.scope and any(a != b and (a[:len(b)] == b or b[:len(a)] == a) for a in self.scope for b in self.scope):
            raise ValueError('Selected paths overlap')
        self.baseline = baseline
        self.resolutions = resolutions or {}
        self.set_lists = {pointer(p) for p in set_lists}
        self.number_fields = {pointer(p) for p in number_fields}
        self.decisions, self.blockers = [], []
        self.result = deepcopy(target)
        if self.scope is None:
            self._walk(incoming, target, self.result, ())
        else:
            for path in self.scope:
                value = get(incoming, path)
                before = get(target, path)
                merged = self._value(value, before, path)
                if merged is not MISSING and value is not MISSING:
                    self._put(path, merged)
        if previous is not None:
            self._residuals(previous, incoming, ())
        self.id = fingerprint({'source': incoming, 'target': target, 'scope': self.scope,
                               'policies': self.policies, 'baseline': baseline, 'resolutions': self.resolutions,
                               'set_lists': sorted(self.set_lists), 'number_fields': sorted(self.number_fields), 'previous': previous,
                               'source_origin': source, 'target_origin': destination})

    def _put(self, path, value):
        node = self.result
        for part in path[:-1]:
            if part not in node:
                node[part] = {}
            if not isinstance(node[part], dict):
                self._decision(path, get(self.target, path), value, MISSING, 'conflict',
                               'type_check', 'preflight', 'Parent is not an object', blocker=True)
                return
            node = node[part]
        node[path[-1]] = value

    def _policy(self, path):
        key = as_pointer(path)
        for origin in ('run', 'profile'):
            if key in self.policies[origin].get('fields', {}):
                return self.policies[origin]['fields'][key], origin + '_field'
        for origin in ('run', 'profile', 'machine'):
            if 'default' in self.policies[origin]:
                return self.policies[origin]['default'], origin + '_default'
        return 'source_wins', 'program_default'

    def _decision(self, path, before, incoming, after, action, policy, origin, reason, blocker=False):
        row = dict(path=list(path), source=self.source, target=self.destination,
                   action=action, policy=policy, policy_origin=origin, reason=reason,
                   category='unresolved_conflict' if blocker else
                   'direct_add' if action == 'add' else 'unchanged' if action == 'unchanged' else 'policy_merge',
                   alternatives=['source_wins', 'keep_target', 'fail_if_different'],
                   check_status='blocked' if blocker else 'passed')
        for key, value in (('before', before), ('incoming', incoming), ('after', after)):
            row[key + '_exists'] = value is not MISSING
            if value is not MISSING:
                row[key] = redact(value, path)
        self.decisions.append(row)
        if blocker:
            self.blockers.append({'path': list(path), 'code': reason, 'decision': len(self.decisions) - 1})

    def _walk(self, incoming, before, after, path):
        # Report retained children inside selected objects, not an entire runtime.
        keys = set(incoming) | set(before) if path else set(incoming)
        for key in sorted(keys):
            child = (*path, key)
            value = self._value(incoming.get(key, MISSING), before.get(key, MISSING), child)
            if value is not MISSING:
                after[key] = value

    def _value(self, incoming, before, path):
        if incoming is MISSING:
            self._decision(path, before, incoming, before, 'preserve', 'absence', 'structural', 'Source missing or unselected')
            return deepcopy(before) if before is not MISSING else MISSING
        if isinstance(incoming, dict):
            if before is not MISSING and not isinstance(before, dict):
                self._decision(path, before, incoming, before, 'conflict', 'type_check', 'preflight', 'Object/scalar mismatch', blocker=True)
                return deepcopy(before)
            after = deepcopy(before) if before is not MISSING else {}
            self._walk(incoming, {} if before is MISSING else before, after, path)
            self._decision(path, before, incoming, after, 'object_merge', 'recursive', 'structural', 'Declared leaves merged; other children preserved')
            return after
        rule, origin = self._policy(path)
        if rule in ('replace', 'append_unique') and not isinstance(incoming, list):
            self._decision(path, before, incoming, before, 'conflict', rule, origin, 'List policy on non-list', blocker=True)
            return before
        if rule == 'append_unique' and path not in self.set_lists:
            self._decision(path, before, incoming, before, 'conflict', rule, origin, 'Field is not a registered set list', blocker=True)
            return before
        compatible_number = path in self.number_fields and type(incoming) in (int, float) and type(before) in (int, float)
        if before is not MISSING and type(incoming) is not type(before) and not compatible_number:
            self._decision(path, before, incoming, before, 'conflict', 'type_check', 'preflight', 'Value types differ', blocker=True)
            return deepcopy(before)
        expected = get(self.baseline, path) if self.baseline is not None else MISSING
        if expected is not MISSING and not same(expected, before):
            resolution = self.resolutions.get(as_pointer(path), {})
            if resolution.get('target_fingerprint') != fingerprint(self.target) or resolution.get('policy') not in ('source_wins', 'keep_target'):
                self._decision(path, before, incoming, before, 'conflict', 'drift', 'preflight', 'External drift requires a fingerprint-scoped resolution', blocker=True)
                return deepcopy(before) if before is not MISSING else MISSING
            rule, origin = resolution['policy'], 'drift_resolution'
        if before is not MISSING and same(incoming, before):
            self._decision(path, before, incoming, before, 'unchanged', rule, origin, 'Values and types are identical')
            return deepcopy(before)
        if before is MISSING:
            self._decision(path, before, incoming, incoming, 'add', rule, origin, 'Selected field is absent in target')
            return deepcopy(incoming)
        if rule == 'keep_target':
            after, action, reason = before, 'preserve', 'Target retained by explicit policy'
        elif rule == 'fail_if_different':
            self._decision(path, before, incoming, before, 'conflict', rule, origin, 'Values differ under fail_if_different', blocker=True)
            return deepcopy(before)
        elif rule == 'append_unique':
            after = deepcopy(before)
            for item in incoming:
                if not any(same(item, old) for old in after):
                    after.append(deepcopy(item))
            action, reason = 'append_unique', 'Registered set list merged without reordering existing items'
        else:
            after = incoming
            action = 'replace_array' if isinstance(incoming, list) else 'overwrite'
            reason = 'Selected field differs; source wins'
        self._decision(path, before, incoming, after, action, rule, origin, reason)
        return deepcopy(after)

    def _residuals(self, previous, incoming, path):
        for key, value in previous.items():
            child = (*path, key)
            if key not in incoming:
                current = get(self.target, child)
                self._decision(child, current, MISSING, current, 'residual', 'absence', 'profile_switch', 'Previous Profile item is no longer declared; native content retained')
            elif isinstance(value, dict) and isinstance(incoming[key], dict):
                self._residuals(value, incoming[key], child)

    def report(self):
        return dict(plan_id=self.id, snapshots={'source': fingerprint(self.incoming), 'target': fingerprint(self.target)},
                    scope=None if self.scope is None else [list(path) for path in self.scope],
                    policies=self.policies, checks=[{'name': 'merge', 'status': 'blocked' if self.blockers else 'passed'}],
                    decisions=self.decisions, blockers=self.blockers, results=[], recovery=None)
