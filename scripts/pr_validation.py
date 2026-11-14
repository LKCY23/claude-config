#!/usr/bin/env python3
"""Validate same-repository PR heads and auto-merge only verified report-only updates."""
import argparse
import base64
import json
import os
from pathlib import Path
import re
import subprocess
from urllib.parse import quote
from desktop_compatibility import validate_release

SHA = re.compile(r'[a-f0-9]{40}')
REPORT = re.compile(r'compatibility/claude-desktop/(\d+\.\d+\.\d+)\.json')
CONTEXTS = ('claude-config/package', 'claude-config/desktop')


class GitHub:
    def __init__(self, repo):
        if not re.fullmatch(r'[\w.-]+/[\w.-]+', repo): raise ValueError('Invalid repository')
        self.repo = repo

    def api(self, method, route, payload=None):
        args = ['gh', 'api', '--method', method, 'repos/'+self.repo+('/'+route if route else '')]
        if payload is not None: args += ['--input', '-']
        proc = subprocess.run(args, input=json.dumps(payload) if payload is not None else None,
                              capture_output=True, text=True, timeout=45)
        if proc.returncode: raise RuntimeError('GitHub request failed: '+method+' '+route.split('?')[0])
        return json.loads(proc.stdout) if proc.stdout.strip() else None

    def command(self, *args):
        subprocess.run(['gh', 'pr', *args, '--repo', self.repo], check=True, capture_output=True, text=True, timeout=45)


def eligible(pr, repo, base, sha):
    return (pr['state'] == 'open' and pr['base']['ref'] == base and pr['base']['repo']['full_name'] == repo
            and pr['head']['repo'] is not None and pr['head']['repo']['full_name'] == repo
            and pr['head']['sha'] == sha)


def select(hub, event, number='', sha=''):
    base = hub.api('GET', '')['default_branch']
    if 'workflow_run' in event:
        run = hub.api('GET', 'actions/runs/'+str(int(event['workflow_run']['id'])))
        if (run['event'] != 'pull_request' or run['path'] != '.github/workflows/package.yml'
                or run['head_repository']['full_name'] != hub.repo): return None
        sha = run['head_sha']
        prs = hub.api('GET', 'pulls?state=open&per_page=100&head='+quote(hub.repo.split('/')[0]+':'+run['head_branch'], safe=''))
        if len(prs) != 1: return None
        pr = hub.api('GET', 'pulls/'+str(prs[0]['number']))
    else:
        if not str(number).isdigit() or int(number) <= 0: raise ValueError('Invalid PR number')
        pr = hub.api('GET', 'pulls/'+str(int(number)))
    if not isinstance(sha, str) or not SHA.fullmatch(sha): raise ValueError('Expected a full head SHA')
    return pr if eligible(pr, hub.repo, base, sha) else None


def changed_files(hub, pr):
    count = pr['changed_files']
    if not isinstance(count, int) or not 0 <= count <= 3000:
        raise ValueError('PR exceeds GitHub file-list limit; explicit review required')
    files = []
    for page in range(1, (count + 99) // 100 + 1):
        rows = hub.api('GET', 'pulls/'+str(pr['number'])+'/files?per_page=100&page='+str(page))
        files.extend(rows)
        if len(rows) < 100: break
    if len(files) != count or len({row['filename'] for row in files}) != count:
        raise ValueError('Complete, unambiguous PR file list required')
    return files


def report_version(files):
    reports = [REPORT.fullmatch(f['filename']) for f in files if REPORT.fullmatch(f['filename'])]
    if len(reports) > 1: raise ValueError('Expected at most one pinned Desktop report')
    return reports[0][1] if reports else ''


def report_only_compatible(pr, files, report):
    """File type alone is insufficient: archive identity, checks, and smoke must agree."""
    try:
        version = report_version(files)
        return (not pr['draft'] and len(files) == 1 and files[0]['status'] in ('added', 'modified')
                and 'previous_filename' not in files[0]
                and pr['user']['login'] == 'github-actions[bot]'
                and pr['head']['ref'] == 'compatibility/claude-desktop-'+version
                and validate_release(report['release'])['version'] == version
                and report['status'] == 'compatible' and report['native_smoke'] == 'passed'
                and bool(report['checks']) and all(c['status'] == 'passed' for c in report['checks']))
    except (ValueError, KeyError, TypeError): return False


def status(hub, sha, context, state, url, description):
    hub.api('POST', 'statuses/'+sha, {'context': context, 'state': state, 'target_url': url, 'description': description})


def disable_owned_auto_merge(hub, pr):
    if (pr.get('auto_merge') or {}).get('enabled_by', {}).get('login') == 'github-actions[bot]':
        hub.command('merge', str(pr['number']), '--disable-auto')


def finish(hub, number, sha, package, desktop, url):
    meta = hub.api('GET', ''); pr = hub.api('GET', 'pulls/'+str(int(number)))
    if not SHA.fullmatch(sha) or not eligible(pr, hub.repo, meta['default_branch'], sha): return 'superseded'
    files = changed_files(hub, pr); version = report_version(files)
    desktop_ok = desktop == 'success' if version else desktop == 'skipped'
    passed = package == 'success' and desktop_ok
    allowed = False
    if passed and version and len(files) == 1:
        blob = hub.api('GET', 'contents/'+quote(files[0]['filename'], safe='/')+'?ref='+sha)
        report = json.loads(base64.b64decode(blob['content']))
        allowed = report_only_compatible(pr, files, report)
    # Remove automation-owned consent before crediting a changed, ineligible head.
    if not allowed: disable_owned_auto_merge(hub, pr)
    status(hub, sha, CONTEXTS[0], 'success' if package == 'success' else 'failure', url, 'Exact-head package matrix: '+package)
    status(hub, sha, CONTEXTS[1], 'success' if desktop_ok else 'failure', url,
           'Exact-release Desktop verification: '+desktop if version else 'No Desktop report changed; package checks required')
    if allowed and meta.get('allow_auto_merge'):
        fresh = hub.api('GET', 'pulls/'+str(pr['number']))
        if not eligible(fresh, hub.repo, meta['default_branch'], sha): return 'superseded'
        if not report_only_compatible(fresh, files, report): return 'ready-for-review'
        if fresh.get('mergeable_state') == 'behind':
            disable_owned_auto_merge(hub, fresh)
            hub.api('PUT', 'pulls/'+str(pr['number'])+'/update-branch', {'expected_head_sha': sha})
            return 'base-update-requested: validate the new head before merging'
        hub.command('merge', str(pr['number']), '--auto', '--squash', '--match-head-commit', sha)
        return 'auto-merge-requested: verified report only'
    return 'ready-for-review' if passed else 'needs-attention'


def main():
    parser = argparse.ArgumentParser(description=__doc__); parser.add_argument('operation', choices=['select','report']); parser.add_argument('--repo', required=True)
    args = parser.parse_args(); hub = GitHub(args.repo)
    url = os.environ['GITHUB_SERVER_URL']+'/'+args.repo+'/actions/runs/'+os.environ['GITHUB_RUN_ID']
    if args.operation == 'select':
        event = json.loads(Path(os.environ['GITHUB_EVENT_PATH']).read_text())
        pr = select(hub, event, os.environ.get('PR_NUMBER',''), os.environ.get('PR_HEAD',''))
        with open(os.environ['GITHUB_OUTPUT'], 'a') as out:
            out.write('eligible='+str(bool(pr)).lower()+'\n')
            if pr:
                version = report_version(changed_files(hub, pr)); sha = pr['head']['sha']
                out.write('number='+str(pr['number'])+'\nhead='+sha+'\nversion='+version+'\n')
                for context in CONTEXTS: status(hub, sha, context, 'pending', url, 'Trusted exact-head validation is running')
        result = 'Selected PR #'+str(pr['number']) if pr else 'Skipped: closed, fork, superseded or unrelated run'
    else:
        result = finish(hub, os.environ['PR_NUMBER'], os.environ['PR_HEAD'], os.environ['PACKAGE_RESULT'], os.environ['DESKTOP_RESULT'], url)
    print(result)
    with open(os.environ['GITHUB_STEP_SUMMARY'], 'a') as out: out.write('PR validation: '+result+'\n')


if __name__ == '__main__': main()
