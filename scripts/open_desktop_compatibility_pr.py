#!/usr/bin/env python3
"""Open one report-only draft PR per version; never overwrite a repair branch."""
import argparse
import base64
import json
import os
from pathlib import Path
import subprocess

from desktop_compatibility import validate_release


def api(path, payload=None):
    command = ['gh', 'api', path]
    if payload is not None:
        command += ['--method', 'POST', '--input', '-']
    proc = subprocess.run(command, input=json.dumps(payload) if payload is not None else None,
                          capture_output=True, text=True, timeout=40)
    if proc.returncode:
        raise RuntimeError(proc.stderr.strip())
    return json.loads(proc.stdout)


def publish(repo, report):
    release = validate_release(report['release'])
    if report['status'] != 'needs-review':
        return {'status': 'no-pr-needed'}
    branch = 'compatibility/claude-desktop-' + release['version']
    owner = repo.split('/')[0]
    prior = api(f'repos/{repo}/pulls?state=all&head={owner}:{branch}')
    if prior:
        return {'status': 'existing-' + prior[0]['state'], 'url': prior[0]['html_url']}
    base = api(f'repos/{repo}')['default_branch']
    head = api(f'repos/{repo}/git/ref/heads/{base}')['object']['sha']
    commit = api(f'repos/{repo}/git/commits/{head}')
    # A report contains evidence, not downloaded source or executable patches.
    content = json.dumps(report, indent=2) + '\n'
    blob = api(f'repos/{repo}/git/blobs', {'content': base64.b64encode(content.encode()).decode(), 'encoding': 'base64'})
    path = 'compatibility/claude-desktop/' + release['version'] + '.json'
    tree = api(f'repos/{repo}/git/trees', {'base_tree': commit['tree']['sha'], 'tree': [
        {'path': path, 'mode': '100644', 'type': 'blob', 'sha': blob['sha']}]})
    new = api(f'repos/{repo}/git/commits', {'message': 'Record Claude Desktop ' + release['version'] + ' compatibility evidence',
                                          'tree': tree['sha'], 'parents': [head]})
    # Ref creation fails rather than replacing any existing repair work.
    api(f'repos/{repo}/git/refs', {'ref': 'refs/heads/' + branch, 'sha': new['sha']})
    body = f'''Claude Desktop **{release['version']}** has a valid official archive and signer, but its configuration interface needs review. This is not proof that its settings are incompatible.

Evidence: `{path}`. The archive URL and checksum pin the exact release for reproduction. No application code, account data or API keys are committed.

To request a fix with your connected subscription, **post this as a new PR comment**:

```text
@codex Follow .github/codex/desktop-compatibility.md and audit compatibility/claude-desktop/{release['version']}.json. Determine whether the five managed preferences still have the same storage and behavior, implement only the necessary adaptation, and add independent regression evidence. Do not merely replace a hash or weaken checks. Push the fix to this PR and report any macOS checks you cannot run.
```

The report alone is not a fix. Keep this PR in draft until the dedicated Desktop compatibility workflow passes. Use its manual run with `version={release['version']}` and branch `{branch}` if GitHub does not start or requires approval for workflows on this bot-created PR. After the human-requested Codex task pushes changes, rerun both package CI and Desktop compatibility checks.

No automatic Codex repair request, merge, release or machine upgrade is performed.
'''
    pr = api(f'repos/{repo}/pulls', {'title': 'Audit Claude Desktop ' + release['version'] + ' compatibility',
                                   'head': branch, 'base': base, 'body': body, 'draft': True})
    return {'status': 'created', 'url': pr['html_url']}


if __name__ == '__main__':
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
            stream.write('Compatibility PR: ' + result.get('url', result['status']) + '\n')
