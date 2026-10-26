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
