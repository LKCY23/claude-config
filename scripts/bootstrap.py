#!/usr/bin/env python3
"""Deploy the reviewed local framework to this Mac, preserving runtime authentication."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys

import yaml

SOURCE = Path(__file__).resolve().parents[1]


def git(directory, *args):
    result = subprocess.run(['git', '-C', str(directory), *args], text=True, capture_output=True, check=True)
    return result.stdout.strip()


def atomic_write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.bootstrap-tmp')
    temporary.write_text(text);os.chmod(temporary, 0o600);os.replace(temporary, path)


def deploy(source, tool_dir, config_dir, catalog_root, hosts, home=None, apply=False):
    home = Path(home or Path.home())
    source, tool_dir, config_dir, catalog_root = [Path(p).expanduser().resolve() for p in (source, tool_dir, config_dir, catalog_root)]
    if source == tool_dir:
        raise ValueError('Source checkout and installed framework must be separate')
    head = git(source, 'rev-parse', 'HEAD')
    if git(source, 'status', '--porcelain', '--untracked-files=no'):
        raise ValueError('Commit reviewed framework changes before bootstrap')
    if tool_dir.exists() and git(tool_dir, 'status', '--porcelain', '--untracked-files=no'):
        raise ValueError('Installed framework has tracked changes; preserve/reconcile before bootstrap')
    subprocess.run([sys.executable, '-c', 'import yaml, tomlkit'], check=True)
    if not (config_dir / 'agents.yaml').is_file():
        raise ValueError('Prepare explicit agents.yaml before bootstrap')
    entries = {}
    for host in hosts:
        if host == 'claude':
            directory = Path(os.environ.get('CLAUDE_CONFIG_DIR') or home / '.claude') / 'skills/claude-config'
        else:
            native, legacy = home / '.agents/skills/claude-config', Path(os.environ.get('CODEX_HOME') or home / '.codex') / 'skills/claude-config'
            if native.exists() and legacy.exists():
                raise ValueError('Duplicate Codex bootstrap entries: reconcile the native and legacy directories')
            directory = legacy if legacy.exists() else native
        target = directory / 'SKILL.md'
        if directory.is_symlink() or target.is_symlink():
            raise ValueError('Bootstrap entry is linked to another source; preserve/reconcile it explicitly')
        entries[host] = target
    result = {'source': str(source), 'source_commit': head, 'tool_dir': str(tool_dir),
              'config_dir': str(config_dir), 'catalog_root': str(catalog_root),
              'entries': {host: str(path) for host, path in entries.items()}, 'applied': apply}
    if not apply:
        return result
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    state = tool_dir.parent / '.claude-config-bootstrap-state' / stamp
    state.mkdir(parents=True, mode=0o700);os.chmod(state, 0o700)
    receipt = {**result, 'previous_head': git(tool_dir, 'rev-parse', 'HEAD') if tool_dir.exists() else None,
               'previous_branch': git(tool_dir, 'branch', '--show-current') if tool_dir.exists() else None,
               'files': {}, 'status': 'started'}
    for target in [tool_dir / 'config.yaml', *entries.values()]:
        receipt['files'][str(target)] = target.read_text() if target.exists() else None
    receipt_path = state / 'receipt.json'
    atomic_write(receipt_path, json.dumps(receipt, indent=2) + '\n')
    if not tool_dir.exists():
        subprocess.run(['git', 'clone', '--no-checkout', str(source), str(tool_dir)], check=True, capture_output=True)
    git(tool_dir, 'fetch', str(source), head)
    git(tool_dir, 'checkout', '-B', 'installed-adaptation', 'FETCH_HEAD')
    config = yaml.safe_load((tool_dir / 'config.yaml').read_text()) if (tool_dir / 'config.yaml').exists() else {}
    config = config or {}
    config.update({'config_dir': str(config_dir), 'python': sys.executable,
                   'framework_source': str(source), 'framework_commit': head})
    config.setdefault('catalog_roots', {})['LKCY23/claudespace'] = str(catalog_root)
    config.setdefault('state_dir', str(tool_dir / 'state'))
    config.setdefault('backup_dir', str(tool_dir / 'backups'))
    atomic_write(tool_dir / 'config.yaml', yaml.safe_dump(config, sort_keys=False))
    for host, target in entries.items():
        body = f'''---
name: claude-config
description: Manage declared Claude Code and Codex plugin selections and configuration with the installed claude-config framework.
---

Read the canonical framework instructions at `{tool_dir / 'SKILL.md'}`.
The default host for this entry is **{host}**. Pass `--agent {host}` unless the
user explicitly selects the other host or both. Do not merge legacy plugin
selections into the explicit profiles or install all available candidates.

Run the helper with `{sys.executable}` and `{tool_dir / 'scripts/agent_config.py'}`.
Its machine configuration is `{tool_dir / 'config.yaml'}`. Do not copy this entry
back into the framework source or overwrite the canonical rules with it.
'''
        atomic_write(target, body)
    receipt['status'] = 'complete'
    receipt['after_files'] = {str(p): p.read_text() for p in [tool_dir / 'config.yaml', *entries.values()]}
    atomic_write(receipt_path, json.dumps(receipt, indent=2) + '\n')
    result['receipt'] = str(receipt_path)
    return result


def rollback(receipt_path):
    path = Path(receipt_path).expanduser().resolve()
    if path.parent.parent.name != '.claude-config-bootstrap-state':
        raise ValueError('Use the private bootstrap receipt')
    receipt = json.loads(path.read_text())
    tool = Path(receipt['tool_dir'])
    if git(tool, 'rev-parse', 'HEAD') != receipt['source_commit']:
        raise ValueError('Installed framework changed since bootstrap')
    for filename, value in receipt.get('after_files', {}).items():
        target = Path(filename)
        if not target.exists() or target.read_text() != value:
            raise ValueError('Bootstrap-managed entry changed: ' + filename)
    for filename, value in receipt['files'].items():
        target = Path(filename)
        if value is None:
            target.unlink(missing_ok=True)
        else:
            atomic_write(target, value)
    if receipt['previous_head']:
        if receipt['previous_branch']:
            git(tool, 'checkout', '-B', receipt['previous_branch'], receipt['previous_head'])
        else:
            git(tool, 'checkout', receipt['previous_head'])
    receipt['status'] = 'rolled-back'
    atomic_write(path, json.dumps(receipt, indent=2) + '\n')
    return {'receipt': str(path), 'status': receipt['status']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('plan', 'apply', 'rollback'))
    parser.add_argument('--source', type=Path, default=SOURCE)
    parser.add_argument('--tool-dir', type=Path, default=Path.home() / '.claude-config-tool')
    parser.add_argument('--config-dir', type=Path, default=Path.home() / 'claude-config-data')
    parser.add_argument('--catalog-root', type=Path, default=Path.home() / 'claudespace')
    parser.add_argument('--agent', choices=('claude', 'codex', 'both'), default='both')
    parser.add_argument('--receipt', type=Path)
    args = parser.parse_args()
    if args.command == 'rollback':
        result = rollback(args.receipt)
    else:
        hosts = ('claude', 'codex') if args.agent == 'both' else (args.agent,)
        result = deploy(args.source, args.tool_dir, args.config_dir, args.catalog_root, hosts, apply=args.command == 'apply')
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
