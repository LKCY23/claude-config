#!/usr/bin/env python3
"""Check a signed official Desktop archive in scratch space; never launch it."""
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import plistlib
import re
import struct
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request
import uuid
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

ROOT = Path(__file__).resolve().parents[1]
FEED = 'https://api.anthropic.com/api/desktop/darwin/universal/squirrel/update'
BUNDLE = 'com.anthropic.claudefordesktop'
TEAM = 'Q6L2SF6YDW'
FIELDS = ('userThemeMode', 'locale', 'chatTabEnabled', 'autoModeEnabled', 'builtinBrowserEnabled')


def version(value):
    if not isinstance(value, str) or not re.fullmatch(r'\d+\.\d+\.\d+', value):
        raise ValueError('Invalid Desktop version')
    return value


def validate_release(row):
    version(row.get('version'))
    url = urllib.parse.urlsplit(row.get('url', ''))
    if (url.scheme != 'https' or url.netloc != 'downloads.claude.ai' or
            not url.path.startswith('/releases/darwin/universal/' + row['version'] + '/') or
            not url.path.endswith('.zip') or url.query or url.fragment or '..' in url.path):
        raise ValueError('Unexpected official archive URL')
    if not re.fullmatch(r'[a-f0-9]{64}', row.get('sha256', '')):
        raise ValueError('Missing official archive checksum')
    if type(row.get('size')) is not int or not 0 < row['size'] <= 2_000_000_000:
        raise ValueError('Invalid archive size')
    return {key: row[key] for key in ('version', 'url', 'sha256', 'size')}


def select_release(feed):
    current = version(feed.get('currentRelease'))
    matches = [r['updateTo'] for r in feed.get('releases', [])
               if r.get('version') == current and r.get('updateTo', {}).get('version') == current]
    if len(matches) != 1:
        raise ValueError('Feed does not identify one current release')
    return validate_release(matches[0])


def discover():
    # A fresh CI-only identifier; no device identity or account is collected.
    query = urllib.parse.urlencode({'version': '0.0.0', 'os_version': '15.0.0', 'device_id': str(uuid.uuid4())})
    with urllib.request.urlopen(FEED + '?' + query, timeout=30) as response:
        payload = response.read(1_000_001)
    if len(payload) > 1_000_000:
        raise ValueError('Unexpected feed size')
    return select_release(json.loads(payload))


def checked_members(archive):
    for member in archive.infolist():
        path = PurePosixPath(member.filename)
        if path.is_absolute() or '..' in path.parts or '\\' in member.filename:
            raise ValueError('Archive entry escapes scratch directory')
        # Framework symlinks are allowed only when their lexical destination stays in the archive.
        if (member.external_attr >> 16) & 0o170000 == 0o120000:
            target = archive.read(member).decode()
            if target.startswith('/') or '\\' in target:
                raise ValueError('Unsafe archive symlink')
            parts = list(path.parent.parts)
            for part in PurePosixPath(target).parts:
                if part == '..':
                    if not parts:
                        raise ValueError('Archive symlink escapes scratch directory')
                    parts.pop()
                elif part != '.':
                    parts.append(part)


def download_app(release, work):
    archive = work / 'Claude.zip'
    print(f"Downloading Claude {release['version']} ({release['size'] // 1_000_000} MB)...", flush=True)
    subprocess.run(['curl', '--fail', '--silent', '--show-error', '--location', '--proto', '=https',
                    '--proto-redir', '=https', '--connect-timeout', '20', '--max-time', '240',
                    '--max-filesize', str(release['size']), '--output', str(archive), release['url']], check=True, timeout=250)
    if archive.stat().st_size != release['size']:
        raise ValueError('Archive size differs from official feed')
    with archive.open('rb') as stream:
        digest = hashlib.file_digest(stream, 'sha256').hexdigest()
    if digest != release['sha256']:
        raise ValueError('Archive checksum differs from official feed')
    print('Official archive size and checksum verified; extracting in scratch space.', flush=True)
    with zipfile.ZipFile(archive) as bundle:
        checked_members(bundle)
    unpacked = work / 'unpacked'
    subprocess.run(['ditto', '-x', '-k', str(archive), str(unpacked)], check=True, timeout=120)
    app = unpacked / 'Claude.app'
    if not app.is_dir():
        raise ValueError('Official archive is missing Claude.app')
    return app


def inspect(app, expected_version):
    from claude_config.core.app_native import AppProbe
    info = plistlib.loads((app / 'Contents/Info.plist').read_bytes())
    if info.get('CFBundleIdentifier') != BUNDLE or info.get('CFBundleShortVersionString') != expected_version:
        raise ValueError('Application identity or version differs from release')
    subprocess.run(['codesign', '--verify', '--deep', '--strict', str(app)], check=True, capture_output=True, timeout=60)
    signature = subprocess.run(['codesign', '-dv', str(app)], check=True, capture_output=True, text=True, timeout=20)
    if 'TeamIdentifier=' + TEAM not in signature.stderr.splitlines():
        raise ValueError('Application signer is not the reviewed Anthropic identity')
    print('Application version, bundle and signer verified; checking preferences.', flush=True)
    settings = dict(userThemeMode='dark', locale='en-US', chatTabEnabled=True, autoModeEnabled=False, builtinBrowserEnabled=True)
    checks = AppProbe().check('claude-desktop', app, settings, {})
    # Diagnostic fingerprints are evidence only. They never authorize writes.
    modules = []
    try:
        archive = (app / 'Contents/Resources/app.asar').read_bytes()
        header_size, json_size = struct.unpack_from('<I', archive, 4)[0], struct.unpack_from('<I', archive, 12)[0]
        if not 16 + json_size <= 8 + header_size <= len(archive):
            raise ValueError('Unrecognized source archive bounds')
        tree = json.loads(archive[16:16 + json_size]); modules = []
        def walk(node, prefix=''):
            for name, item in node.get('files', {}).items():
                path = prefix + name
                if 'files' in item:
                    walk(item, path + '/')
                elif path.endswith('.js') and 'offset' in item:
                    start = 8 + header_size + int(item['offset']); data = archive[start:start + item['size']]
                    present = [key for key in FIELDS if key.encode() in data]
                    if len(present) == len(FIELDS):
                        modules.append({'path': path, 'sha256': hashlib.sha256(data).hexdigest(), 'fields': present})
        walk(tree)
    except (OSError, ValueError, TypeError, KeyError, struct.error, RecursionError):
        # Diagnostic extraction must not suppress an unknown-interface report.
        print("Preference-module diagnostics unavailable; retaining interface check result.", flush=True)
        modules = []
    return checks, modules


def run(release, work, app=None):
    app = app or download_app(release, work)
    checks, modules = inspect(app, release['version'])
    known = all(c['status'] == 'passed' for c in checks) and bool(checks)
    result = {'schema_version': 1, 'release': release, 'status': 'needs-review',
              'checks': checks, 'candidate_modules': modules, 'native_smoke': 'not-run',
              'scope': 'Source interface and temporary preference files; no GUI, account, provider or business validation'}
    if known:
        env = os.environ.copy()
        env.update(CLAUDE_CONFIG_RUN_NATIVE_SMOKE='1', CLAUDE_CONFIG_CLAUDE_APPLICATION=str(app))
        proc = subprocess.run([sys.executable, '-m', 'unittest', 'discover', '-s', 'tests',
                               '-p', 'test_native_app_smoke.py', '-v'], cwd=ROOT, env=env,
                              capture_output=True, text=True, timeout=120)
        (work / 'native-smoke.log').write_text(proc.stdout + proc.stderr)
        result['native_smoke'] = 'passed' if proc.returncode == 0 else 'failed'
        result['status'] = 'compatible' if proc.returncode == 0 else 'failed'
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report', type=Path, help='Recheck the exact release pinned in a pending report')
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--require-compatible', action='store_true')
    args = parser.parse_args(); args.out.mkdir(parents=True, exist_ok=True)
    print('Resolving pinned release...' if args.report else 'Checking official stable update feed...', flush=True)
    release = validate_release(json.loads(args.report.read_text())['release']) if args.report else discover()
    with tempfile.TemporaryDirectory(prefix='desktop-compatibility-') as temporary:
        result = run(release, Path(temporary))
        log = Path(temporary) / 'native-smoke.log'
        if log.exists():
            (args.out / log.name).write_bytes(log.read_bytes())
    (args.out / 'report.json').write_text(json.dumps(result, indent=2) + '\n')
    print(f"Claude Desktop {release['version']}: {result['status']}")
    print('Full report:', args.out / 'report.json')
    if output := os.environ.get('GITHUB_OUTPUT'):
        with open(output, 'a') as stream:
            stream.write(f"status={result['status']}\nversion={release['version']}\n")
    if result['status'] == 'failed' or args.require_compatible and result['status'] != 'compatible':
        raise SystemExit(1)


if __name__ == '__main__':
    main()
