#!/usr/bin/env python3
"""Maintain one rolling tracking issue for Claude Desktop compatibility updates."""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess

from desktop_compatibility import FIELDS, validate_release

LABEL = 'desktop-compatibility'
LABEL_COLOR = 'd93f0b'
LABEL_DESCRIPTION = 'Claude Desktop compatibility watch'
MARKER = '<!-- claude-desktop-compatibility-watch -->'
HISTORY_START = '<!-- claude-desktop-watch-history-start -->'
HISTORY_END = '<!-- claude-desktop-watch-history-end -->'
HISTORY_LIMIT = 20
MODULE_PATH = re.compile(r'[A-Za-z0-9._/@+-]+')
SHA256 = re.compile(r'[a-f0-9]{64}')
REPO = re.compile(r'[\w.-]+/[\w.-]+')
VERSION = re.compile(r'\d+\.\d+\.\d+')


def api(method, path, payload=None):
    command = ['gh', 'api', '--method', method, path]
    if payload is not None:
        command += ['--input', '-']
    proc = subprocess.run(command, input=json.dumps(payload) if payload is not None else None,
                          capture_output=True, text=True, timeout=40)
    if proc.returncode:
        raise RuntimeError(proc.stderr.strip() or f'GitHub API {method} {path} failed')
    return json.loads(proc.stdout) if proc.stdout.strip() else None


def ensure_label(repo):
    try:
        api('POST', f'repos/{repo}/labels', {
            'name': LABEL,
            'color': LABEL_COLOR,
            'description': LABEL_DESCRIPTION,
        })
    except RuntimeError:
        pass


def find_tracking_issue(repo):
    rows = api('GET', f'repos/{repo}/issues?state=open&per_page=100') or []
    for item in rows:
        if (item.get('pull_request') is None
                and (item.get('user') or {}).get('login') == 'github-actions[bot]'
                and MARKER in (item.get('body') or '')):
            return item
    return None


def sanitize_modules(modules):
    if not isinstance(modules, list):
        return []
    cleaned = []
    for row in modules:
        if not isinstance(row, dict):
            continue
        path = row.get('path')
        digest = row.get('sha256')
        fields = row.get('fields')
        if (isinstance(path, str) and MODULE_PATH.fullmatch(path)
                and isinstance(digest, str) and SHA256.fullmatch(digest)
                and isinstance(fields, list)):
            cleaned.append({
                'path': path,
                'sha256': digest,
                'fields': [f for f in fields if isinstance(f, str) and f in FIELDS],
            })
    return cleaned


def parse_history(body):
    if not isinstance(body, str):
        return []
    start = body.find(HISTORY_START)
    end = body.find(HISTORY_END)
    if start == -1 or end == -1 or end <= start:
        return []
    rows = []
    for line in body[start + len(HISTORY_START):end].splitlines():
        stripped = line.strip()
        if not stripped.startswith('|') or stripped.startswith('| Version') or stripped.startswith('| ---'):
            continue
        cells = [cell.strip().strip('`') for cell in stripped.strip('|').split('|')]
        if len(cells) != 4:
            continue
        ver, status, sha_prefix, module_path = cells
        if not VERSION.fullmatch(ver) or status not in ('needs-review', 'compatible', 'failed'):
            continue
        rows.append((ver, status, sha_prefix, module_path))
    return rows


def render_body(report, existing_body=None):
    release = validate_release(report['release'])
    modules = sanitize_modules(report.get('candidate_modules', []))
    sanitized_report = {
        'schema_version': report.get('schema_version', 1),
        'release': release,
        'status': report['status'],
        'checks': report.get('checks', []) if isinstance(report.get('checks'), list) else [],
        'candidate_modules': modules,
        'native_smoke': report.get('native_smoke', 'not-run'),
        'scope': report.get('scope', '') if isinstance(report.get('scope'), str) else '',
    }
    primary_module = modules[0]['path'] if modules else '-'
    current_row = (release['version'], report['status'], release['sha256'][:12], primary_module)
    history = [current_row]
    seen_versions = {release['version']}
    for row in parse_history(existing_body):
        if row[0] not in seen_versions:
            seen_versions.add(row[0])
            history.append(row)
        if len(history) >= HISTORY_LIMIT:
            break

    lines = [
        MARKER,
        f"Claude Desktop **{release['version']}** has a valid official archive and signer and requires configuration-interface review; this is not proof that its settings are incompatible.",
        '',
        'This single rolling issue tracks unreviewed Claude Desktop updates without opening per-version branches or draft PRs. Fix on demand only when an interface break or upgrade requires adaptation.',
        '',
        '### Latest Release',
        f"- **Version**: `{release['version']}`",
        f"- **Archive URL**: `{release['url']}`",
        f"- **Archive SHA-256**: `{release['sha256']}` ({release['size']} bytes)",
        f"- **Primary Candidate Module**: `{primary_module}`",
        '',
        '### How to Adapt When Needed',
        f"1. Follow `.github/codex/desktop-compatibility.md` to audit `Claude Desktop {release['version']}` and verify the five managed preferences.",
        f"2. Open a dedicated PR that commits `compatibility/claude-desktop/{release['version']}.json`, any minimal adapter/signature updates, and references this issue (so merging closes it).",
        '3. Pushing `compatibility/claude-desktop/<version>.json` triggers exact-release verification in `desktop-compatibility.yml` and `pr-validation.yml`.',
        '',
        '### Latest Evidence Report',
        '```json',
        json.dumps(sanitized_report, indent=2),
        '```',
        '',
        '### Recent Observed Releases',
        HISTORY_START,
        '| Version | Status | Archive SHA-256 | Candidate Module |',
        '| --- | --- | --- | --- |',
    ]
    for ver, status, sha_prefix, module_path in history:
        lines.append(f'| `{ver}` | `{status}` | `{sha_prefix}` | `{module_path}` |')
    lines.append(HISTORY_END)
    return '\n'.join(lines) + '\n'


def publish(repo, report):
    if not isinstance(repo, str) or not REPO.fullmatch(repo):
        raise ValueError('Invalid repository')
    release = validate_release(report['release'])
    status = report.get('status')
    if status not in ('needs-review', 'compatible'):
        return {'status': 'no-issue-needed'}

    existing = find_tracking_issue(repo)
    if status == 'compatible':
        if existing is None:
            return {'status': 'compatible-no-issue'}
        number = existing['number']
        api('POST', f'repos/{repo}/issues/{number}/comments', {
            'body': f"Claude Desktop **{release['version']}** passed the current interface and native smoke checks (`compatible`). Closing this tracking issue.",
        })
        api('PATCH', f'repos/{repo}/issues/{number}', {
            'state': 'closed',
            'state_reason': 'completed',
        })
        return {'status': 'closed', 'number': number, 'url': existing['html_url']}

    title = f"Claude Desktop compatibility watch: {release['version']} needs review"
    body = render_body(report, existing.get('body') if existing else None)
    if existing is None:
        ensure_label(repo)
        created = api('POST', f'repos/{repo}/issues', {
            'title': title,
            'body': body,
            'labels': [LABEL],
        })
        return {'status': 'created', 'number': created['number'], 'url': created['html_url']}

    number = existing['number']
    if existing.get('title') == title and (existing.get('body') or '').strip() == body.strip():
        return {'status': 'unchanged', 'number': number, 'url': existing['html_url']}

    updated = api('PATCH', f'repos/{repo}/issues/{number}', {
        'title': title,
        'body': body,
    })
    return {'status': 'updated', 'number': number, 'url': updated['html_url']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', required=True)
    parser.add_argument('--report', type=Path, required=True)
    args = parser.parse_args()
    if args.repo != os.environ.get('GITHUB_REPOSITORY'):
        raise SystemExit('Publisher must target the workflow repository')
    result = publish(args.repo, json.loads(args.report.read_text()))
    print(json.dumps(result))
    if summary := os.environ.get('GITHUB_STEP_SUMMARY'):
        with open(summary, 'a') as stream:
            stream.write('Compatibility watch issue: ' + result.get('url', result['status']) + '\n')


if __name__ == '__main__':
    main()
