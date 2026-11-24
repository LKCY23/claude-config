"""Bounded, read-only operational evidence; never collect credentials or sessions."""
from importlib import metadata
from pathlib import Path
import json
import os
import plistlib
import re
import subprocess
import sys
from urllib.parse import unquote, urlsplit


def tool_identity():
    from .. import __version__, __file__ as module_file
    result = {'version': __version__, 'module_file': module_file, 'python': sys.executable}
    try:
        distribution = metadata.distribution('claude-config')
        result['distribution_version'] = distribution.version
        direct = json.loads(distribution.read_text('direct_url.json') or '{}')
        url = urlsplit(direct.get('url', ''))
        result['installation'] = {'kind': 'local-artifact' if url.scheme == 'file' else 'distribution'}
        if url.scheme == 'file':
            result['installation']['path'] = unquote(url.path)
        if direct.get('vcs_info', {}).get('commit_id'):
            result['installation']['commit'] = direct['vcs_info']['commit_id']
    except (metadata.PackageNotFoundError, ValueError):
        result['installation'] = {'kind': 'source-or-unknown'}
    return result


def storage_summary(root):
    policies = {'managed': 'current-management-state', 'managed-apps': 'current-management-state',
                'resource-baselines': 'required-for-current-drift-review-and-recovery',
                'runs': 'review-run-status-before-retiring-recovery-evidence',
                'reports': 'completed-reports-eligible-for-explicit-retention-policy',
                'sources': 'pinned-sources-check-current-references-before-removal'}
    rows = []
    root = Path(root)
    for name, policy in policies.items():
        path = root / name
        count = size = 0
        if path.is_dir() and not path.is_symlink():
            for parent, directories, files in os.walk(path, followlinks=False):
                directories[:] = [d for d in directories if not (Path(parent) / d).is_symlink()]
                for filename in files:
                    item = Path(parent) / filename
                    if item.is_file() and not item.is_symlink():
                        count += 1
                        size += item.stat().st_size
        rows.append({'category': name, 'files': count, 'logical_bytes': size, 'retention': policy})
    return {'root': str(root), 'categories': rows, 'automatic_deletion': False,
            'other_directories': sorted(p.name for p in root.iterdir() if p.is_dir() and p.name not in policies) if root.is_dir() else []}


def app_instances(machine, runner=None):
    runner = runner or subprocess.run
    if sys.platform != 'darwin':
        return {'status': 'not_verified', 'reason': 'App process observation is macOS-only', 'instances': []}
    try:
        processes = runner(['ps', 'ax', '-o', 'pid=,comm='], capture_output=True, text=True, timeout=5, check=True).stdout
        bindings = {}
        for app, entry in machine.get('apps', {}).items():
            bundle = Path(entry['binding']['application'])
            plist = plistlib.loads((bundle / 'Contents/Info.plist').read_bytes())
            executable = str(bundle / 'Contents/MacOS' / plist['CFBundleExecutable'])
            bindings[executable] = (app, entry['binding']['data_root'])
        ag_entry = machine.get('agents', {}).get('antigravity')
        if ag_entry and 'application' in ag_entry.get('binding', {}):
            bundle = Path(ag_entry['binding']['application'])
            info_plist = bundle / 'Contents/Info.plist'
            if info_plist.is_file():
                plist = plistlib.loads(info_plist.read_bytes())
                executable = str(bundle / 'Contents/MacOS' / plist['CFBundleExecutable'])
                bindings[executable] = ('antigravity', ag_entry['binding']['config_root'])
        rows = []
        for line in processes.splitlines():
            parts = line.strip().split(None, 1)
            if len(parts) != 2 or parts[1] not in bindings or not parts[0].isdigit():
                continue
            pid, executable = parts
            app, bound_root = bindings[executable]
            # Arguments only: do not request or serialize the process environment.
            arguments = runner(['ps', '-p', pid, '-o', 'command='], capture_output=True, text=True, timeout=5, check=True).stdout.strip()
            match = re.search(r'--user-data-dir(?:=|\s)(.*?)(?=\s--|$)', arguments)
            observed = match[1].strip('\"\'') if match else None
            rows.append({'app': app, 'pid': int(pid), 'bound_data_root': bound_root,
                         'observed_data_root': observed,
                         'binding_matches': str(Path(observed).resolve()) == str(Path(bound_root).resolve()) if observed else None})
        return {'status': 'observed', 'instances': rows,
                'scope': 'Explicit process arguments only; default directories and effective sessions are not inferred'}
    except (OSError, ValueError, KeyError, subprocess.SubprocessError):
        return {'status': 'not_verified', 'reason': 'Process or App metadata could not be inspected', 'instances': []}


def agent_processes(machine, runner=None):
    """Observe Antigravity processes without reading environment values or sensitive arguments."""
    runner = runner or subprocess.run
    if sys.platform not in ('darwin', 'linux'):
        return {'status': 'not_verified', 'reason': 'Agent process observation requires POSIX ps', 'instances': []}
    ag_entry = machine.get('agents', {}).get('antigravity')
    if not ag_entry:
        return {'status': 'not_applicable', 'instances': []}
    binding = ag_entry.get('binding', {})
    bound_exec = str(Path(binding['executable'])) if 'executable' in binding else None
    bound_app_exec = None
    if 'application' in binding:
        info_plist = Path(binding['application']) / 'Contents/Info.plist'
        if info_plist.is_file():
            try:
                plist = plistlib.loads(info_plist.read_bytes())
                bound_app_exec = str(Path(binding['application']) / 'Contents/MacOS' / plist['CFBundleExecutable'])
            except (OSError, ValueError, KeyError):
                pass
    try:
        processes = runner(['ps', 'ax', '-o', 'pid=,comm='], capture_output=True, text=True, timeout=5, check=True).stdout
        rows = []
        for line in processes.splitlines():
            parts = line.strip().split(None, 1)
            if len(parts) != 2 or not parts[0].isdigit():
                continue
            pid, comm = parts
            name = Path(comm).name
            kind = None
            if name.startswith('language_server'):
                kind = 'language_server'
            elif bound_exec and comm == bound_exec or name == 'agy':
                kind = 'cli'
            elif bound_app_exec and comm == bound_app_exec or 'Antigravity.app/' in comm or name == 'Antigravity':
                kind = 'application'
            if kind is not None:
                rows.append({'agent': 'antigravity', 'pid': int(pid), 'process': name, 'command_path': comm, 'kind': kind})
        return {'status': 'observed', 'instances': rows,
                'scope': 'Process command paths and PIDs only; environment values and CLI arguments are not collected'}
    except (OSError, ValueError, subprocess.SubprocessError):
        return {'status': 'not_verified', 'reason': 'Agent process metadata could not be inspected', 'instances': []}
