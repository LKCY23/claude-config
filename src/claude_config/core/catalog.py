"""Resolve explicitly selected resources from exact, already prepared Git sources."""
from dataclasses import dataclass
from copy import deepcopy
import hashlib
import io
import os
from pathlib import Path
import re
import subprocess

from .catalog_schema import validate_catalog
from .merge import fingerprint
from .native import parse_json, parse_yaml, snapshot_read
from .schema import RESOURCE_KINDS, SchemaError, require, resolve_catalog_root, validate_catalogs, within
from .transaction import PlanStale


def git(root, *args, input=None):
    env = {key: value for key, value in os.environ.items() if not key.startswith('GIT_')}
    result = subprocess.run(['git', '--literal-pathspecs', '-c', 'core.fsmonitor=false', '-c', 'core.hooksPath=/dev/null', '-C', str(root), *args],
                            env=env, input=input, capture_output=True, timeout=20)
    if result.returncode:
        raise SchemaError('Cannot inspect the selected Git source')
    return result.stdout


def verify_git(root, repo, commit):
    root = Path(root).resolve()
    require(root.is_dir(), 'Selected source must be prepared before planning')
    lines = git(root, 'rev-parse', '--show-toplevel', 'HEAD').decode().splitlines()
    require(len(lines) == 2 and Path(lines[0]).resolve() == root, 'Source root is not a repository root')
    require(lines[1].strip() == commit, 'Selected checkout does not match the fixed commit')
    remote = git(root, 'remote', 'get-url', 'origin').decode().strip()
    match = re.fullmatch(r'(?:https://github\.com/|git@github\.com:|ssh://git@github\.com/)([^/]+/[^/]+?)(?:\.git)?', remote)
    require(match is not None and match[1].lower() == repo.lower(), 'Selected repository identity does not match the declared source')
    require(not git(root, 'status', '--porcelain', '--untracked-files=all').strip(), 'Selected source has local changes')


@dataclass
class Package:
    ref: str
    group: str
    item: dict
    selection: dict
    root: Path
    entry: str
    files: dict
    modes: dict
    commit: str
    source_fingerprint: str
    sibling_files: frozenset = frozenset()


class CatalogResolver:
    def __init__(self, manager):
        self.manager = manager
        self.catalogs = None
        self.loaded = {}
        self.guards = []
        self.repositories = []
        self._verified = set()
        self._tracked_cache = {}

    def _verify(self, root, repo, commit):
        root = Path(root).resolve()
        key = (root, repo.lower(), commit)
        if key in self._verified:
            return
        verify_git(root, repo, commit)
        self._verified.add(key)
        self.repositories.append(root)
        def guard():
            try:
                verify_git(root, repo, commit)
            except (ValueError, OSError, subprocess.SubprocessError) as exc:
                raise PlanStale('Selected resource source changed after planning') from exc
        self.guards.append(guard)

    def _tracked_index(self, repository):
        repository = Path(repository).resolve()
        cached = self._tracked_cache.get(repository)
        if cached is not None:
            return cached
        rows = git(repository, 'ls-files', '--stage', '-z').split(b'\0')
        index = {}
        for row in rows:
            if not row:
                continue
            metadata, raw_name = row.split(b'\t', 1)
            mode, oid, stage = metadata.decode().split()
            index.setdefault(raw_name.decode(), []).append((mode, oid, stage))
        self._tracked_cache[repository] = index
        return index

    def _verify_repo_blobs(self, repository, requests):
        if not requests:
            return
        oids = []
        seen_oids = set()
        for _, _, _, oid in requests:
            if oid not in seen_oids:
                seen_oids.add(oid)
                oids.append(oid)
        blobs = io.BytesIO(git(repository, 'cat-file', '--batch', input=''.join(oid + '\n' for oid in oids).encode()))
        contents = {}
        for oid in oids:
            header = blobs.readline().decode().strip().split()
            require(len(header) == 3 and header[:2] == [oid, 'blob'], 'Resource Git object is missing or not a blob')
            content = blobs.read(int(header[2]))
            require(blobs.read(1) == b'\n', 'Resource Git object is missing or not a blob')
            contents[oid] = content
        for files, name, _, oid in requests:
            require(contents[oid] == files[name], 'Resource content differs from its pinned Git object')

    def _catalog(self, alias):
        if alias in self.loaded:
            return self.loaded[alias]
        if self.catalogs is None:
            self.catalogs, _, _ = self.manager._read(self.manager.root / 'catalogs.yaml',
                                                   lambda data: validate_catalogs(parse_yaml(data)))
        require(alias in self.catalogs['catalogs'] and alias in self.manager.machine['catalog_roots'], 'Selected catalog is not declared and bound: ' + alias)
        declared = self.catalogs['catalogs'][alias]
        reference = resolve_catalog_root(self.manager.machine, alias, declared, state_root=self.manager.state_root)
        root = reference.resolve()
        self.manager.references[reference] = root
        self._verify(root, declared['repo'], declared['commit'])
        directory = within(root, declared['path'])
        self.manager.references[root / declared['path']] = directory
        value, content, _ = self.manager._read(directory / 'catalog.json', parse_json)
        require(content == git(root, 'show', 'HEAD:' + (directory / 'catalog.json').relative_to(root).as_posix()),
                'Catalog content differs from its pinned Git object')
        validate_catalog(value)
        require(value['name'] == alias, 'Catalog name disagrees with its alias')
        self.loaded[alias] = (root, directory, declared, value)
        return self.loaded[alias]

    def _tree(self, repository, root, entry, *, pending_blobs=None):
        files, modes = {}, {}
        is_file = root.is_file()
        base = root.parent if is_file else root
        require(is_file or root.is_dir(), 'Resource source is not a file or directory')
        repo_key = Path(repository).resolve()
        relative = root.relative_to(repository).as_posix()
        index = self._tracked_index(repo_key)
        tracked = {}
        for rel_name, entries in index.items():
            if relative != '.' and rel_name != relative and not rel_name.startswith(relative + '/'):
                continue
            require(len(entries) == 1, 'Resource has unresolved Git entries')
            mode, oid, stage = entries[0]
            require(stage == '0', 'Resource has unresolved Git entries')
            require(mode != '160000', 'Nested submodule must be an explicitly pinned resource source')
            tracked[repository / rel_name] = (mode, oid)
        selected_files = []
        resolved_base = base.resolve()
        for source, metadata in tracked.items():
            require(source.is_relative_to(base), 'Resource file is outside its package')
            require(not any(part in ('__pycache__', '.DS_Store', '.in_use', '.git') for part in source.relative_to(base).parts),
                    'Runtime caches and Git metadata cannot be resource bodies')
            if is_file and source != root:
                continue
            actual = source.resolve()
            require(actual.is_relative_to(resolved_base), 'Resource symlink escapes its package')
            require(actual.is_file(), 'Directory symlinks and non-file resources are not supported')
            # A tracked symlink cannot smuggle an ignored/untracked target into a pin.
            if actual != source:
                target = tracked.get(actual)
                if target is None:
                    rel_actual = actual.relative_to(repository).as_posix()
                    entries = index.get(rel_actual, ())
                    require(len(entries) == 1, 'Resource symlink target must be tracked')
                    mode, oid, stage = entries[0]
                    require(stage == '0' and mode in ('100644', '100755'), 'Resource symlink target must be a regular tracked file')
                    target = (mode, oid)
                metadata = target
            self.manager.references[source] = actual
            content, expected = snapshot_read(actual)
            require(bool(expected['mode'] & 0o111) == (metadata[0] == '100755'), 'Resource executable mode differs from its Git pin')
            self.manager.sources[actual] = expected
            name = source.relative_to(base).as_posix()
            files[name] = content
            modes[name] = 0o700 if expected['mode'] & 0o111 else 0o600
            selected_files.append((name, metadata[1]))
        require(bool(files), 'Selected resource has no tracked files')
        requests = [(files, name, None, oid) for name, oid in selected_files]
        if pending_blobs is not None:
            pending_blobs.setdefault(repo_key, []).extend(requests)
        else:
            self._verify_repo_blobs(repo_key, requests)
        selected = root.name if is_file and entry == '.' else str(Path(entry))
        require(selected in files, 'Native entry must be a tracked package file')
        manifest = {name: {'sha256': hashlib.sha256(data).hexdigest(), 'executable': bool(modes[name] & 0o111)} for name, data in files.items()}
        return selected, files, modes, fingerprint(manifest)

    def resolve(self, profile, selected=None, *, source_only=False):
        packages = {}
        available = {ref: (group, selection) for group, entries in profile.get('resources', {}).items() for ref, selection in entries.items()}
        needed = set(available) if selected is None else set(selected)
        queue = list(needed)
        while queue:
            ref = queue.pop()
            require(ref in available, 'Resource dependency must be explicitly selected: ' + ref)
            identifier, alias = ref.split('@')
            catalog = self._catalog(alias)[3]
            require(identifier in catalog['resources'], 'Resource is not registered: ' + ref)
            for dependency in catalog['resources'][identifier]['requires'].get('resources', []):
                require(dependency in available, 'Resource dependency must be explicitly selected: ' + dependency)
                if dependency not in needed:
                    needed.add(dependency)
                    queue.append(dependency)
        pending_blobs = {}
        pending_pin_checks = []
        for group, selections in profile.get('resources', {}).items():
            for ref, selection in selections.items():
                if ref not in needed:
                    continue
                identifier, alias = ref.split('@')
                repository, catalog_root, declared, catalog = self._catalog(alias)
                require(identifier in catalog['resources'], 'Resource is not registered: ' + ref)
                item = deepcopy(catalog['resources'][identifier])
                require(item['kind'] == RESOURCE_KINDS[group], 'Resource kind disagrees with Profile group: ' + ref)
                require(self.manager.agent in item['compatibility'], 'Resource does not declare support for this agent: ' + ref)
                pin = getattr(self.manager, 'plugin_locks', {}).get('plugins', {}).get(ref)
                if pin is not None:
                    from .plugin_locks import apply_pin
                    require(item['kind'] == 'plugin', 'Plugin lock applied to a non-plugin resource')
                    item = apply_pin(item, declared, pin)
                compatible = item['compatibility'][self.manager.agent]
                default_entry = compatible['entry']
                if item['kind'] == 'mcp' and 'entry' in selection.get('options', {}):
                    label = selection['options']['entry']
                    require(isinstance(label, str) and label in compatible.get('entries', {}), 'MCP runtime entry is not declared for this agent: ' + ref)
                    compatible['entry'] = compatible['entries'][label]
                source = item['source']
                if source['kind'] == 'external':
                    external = self.manager.state_root / 'sources' / source['repo'].replace('/', '--') / source['commit']
                    repository = external.resolve()
                    self.manager.references[external] = repository
                    self._verify(repository, source['repo'], source['commit'])
                    root = within(repository, source['path'])
                else:
                    root = within(catalog_root, source['path'])
                    if source['kind'] == 'submodule':
                        relative = root.relative_to(repository).as_posix()
                        entries = self._tracked_index(repository).get(relative, ())
                        require(len(entries) == 1 and entries[0][:2] == ('160000', source['commit']), 'Submodule pin disagrees with catalog source')
                        self._verify(root, source['repo'], source['commit'])
                        repository = root
                entry, files, modes, digest = self._tree(repository, root, item['compatibility'][self.manager.agent]['entry'], pending_blobs=pending_blobs)
                require(all(location in files for location in compatible.get('entries', {}).values()), 'Declared MCP entry is not a tracked package file: ' + ref)
                require(not compatible.get('entries') or default_entry in files, 'Default MCP entry is not a tracked package file: ' + ref)
                if source['kind'] == 'snapshot':
                    require(source['sha256'] == digest, 'Snapshot content fingerprint disagrees with the declared baseline')
                validation = item['compatibility'][self.manager.agent]['validation']
                if validation['status'] in ('passed', 'failed'):
                    evidence = within(root if root.is_dir() else root.parent, validation['evidence'])
                    _, expected = snapshot_read(evidence)
                    self.manager.sources[evidence] = expected
                packages[ref] = Package(ref, group, item, selection, root, entry, files, modes, declared['commit'], digest)
                if pin is not None:
                    pending_pin_checks.append((packages[ref], pin))
        for repo_key, requests in pending_blobs.items():
            self._verify_repo_blobs(repo_key, requests)
        if pending_pin_checks:
            from .plugin_locks import validate_package_pin
            for pkg, pin in pending_pin_checks:
                validate_package_pin(pkg, pin)
        for ref, package in packages.items():
            for dependency in package.item['requires'].get('resources', []):
                require(dependency in packages, 'Resource dependency must be explicitly selected: ' + dependency)
            for other in packages.values():
                if other.item['kind'] == 'plugin' and other.ref != ref and package.root.is_relative_to(other.root):
                    raise SchemaError('Independent resource overlaps a selected plugin component')
        visiting, visited = set(), set()
        def visit(ref):
            require(ref not in visiting, 'Resource dependency cycle')
            if ref in visited:
                return
            visiting.add(ref)
            for dependency in packages[ref].item['requires'].get('resources', []):
                visit(dependency)
            visiting.remove(ref)
            visited.add(ref)
        for ref in packages:
            visit(ref)
        # Independent skills retain links to other selected skills installed beside
        # them. These are navigation links, not acyclic installation dependencies.
        from .resources import frontmatter
        from .bindings import Bindings
        skill_groups = {}
        for package in packages.values():
            if package.item['kind'] != 'skill':
                continue
            if self.manager.agent == 'codex':
                binding = package.selection.get('bindings', {}).get('install_root')
                require(binding is not None, 'Codex skill requires an install_root binding')
                parent = binding if source_only else Bindings(self.manager).resolve(binding, 'path', allow_missing=True)
            else:
                parent = 'claude-user-skills' if source_only else str(self.manager.native_path.parent / 'skills')
            identity = frontmatter(package.files[package.entry])[0].get('name')
            require(isinstance(identity, str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', identity), 'Unsafe skill identity')
            group = skill_groups.setdefault(parent, {})
            require(identity not in group, 'Duplicate selected skill identity: ' + identity)
            group[identity] = package
        for group in skill_groups.values():
            for identity, package in group.items():
                package.sibling_files = frozenset('../' + other + '/' + path
                    for other, sibling in group.items() if other != identity for path in sibling.files)
        return packages
