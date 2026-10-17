#!/usr/bin/env python3
"""Publish explicit release assets without replacing published contents."""
import argparse
import json
from pathlib import Path
import re
import subprocess


def gh(args, *, optional=False):
    result = subprocess.run(['gh', *args], capture_output=True, timeout=120)
    if result.returncode:
        if optional and (b'(HTTP 404)' in result.stderr or result.stderr.strip() == b'release not found'):
            return None
        raise ValueError('GitHub release operation failed; check authentication, network and repository permissions')
    return result.stdout


def api(path, *, optional=False, binary=False):
    raw = gh(['api', path, *(['-H', 'Accept: application/octet-stream'] if binary else [])], optional=optional)
    return raw if binary or raw is None else json.loads(raw)


def tag_commit(repo, tag):
    ref = api(f'repos/{repo}/git/ref/tags/{tag}', optional=True)
    if ref is None:
        return None
    obj = ref['object']
    for _ in range(4):
        if obj.get('type') != 'tag':
            break
        obj = api(f'repos/{repo}/git/tags/{obj["sha"]}')['object']
    if obj.get('type') != 'commit':
        raise ValueError('Release tag must resolve to a commit')
    return obj['sha']


def find_release(repo, tag):
    # gh resolves pending-tag drafts as well as published releases.
    raw = gh(['release', 'view', tag, '--repo', repo, '--json', 'databaseId'], optional=True)
    if raw is None:
        return None
    identifier = json.loads(raw)['databaseId']
    if type(identifier) is not int:
        raise ValueError('Invalid release identity')
    return api(f'repos/{repo}/releases/{identifier}')


def publish(repo, tag, commit, assets, *, title, notes, latest=False):
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repo) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', tag):
        raise ValueError('Invalid repository or release tag')
    if not re.fullmatch(r'[0-9a-f]{40}', commit):
        raise ValueError('An exact lowercase commit SHA is required')
    files = {Path(p).name: Path(p) for p in assets}
    if not files or len(files) != len(assets) or any(not p.is_file() or p.is_symlink() for p in files.values()):
        raise ValueError('Release assets must be explicit regular files with unique names')
    pointed = tag_commit(repo, tag)
    if pointed is not None and pointed != commit:
        raise ValueError('Existing release tag points to a different commit')
    release = find_release(repo, tag)
    if release is None:
        gh(['release', 'create', tag, '--repo', repo, '--target', commit, '--title', title, '--notes', notes, '--draft'])
        release = find_release(repo, tag)
    if release['tag_name'] != tag or pointed is None and release['target_commitish'] != commit:
        raise ValueError('Release identity differs from the requested source commit')
    if not release['draft'] and pointed != commit:
        raise ValueError('Published release is missing its exact source tag')
    existing = {row['name']: row for row in release['assets']}
    if len(existing) != len(release['assets']) or not set(existing) <= files.keys():
        raise ValueError('Release has unexpected or duplicate assets; nothing will be overwritten')
    for name, asset in existing.items():
        if api(f'repos/{repo}/releases/assets/{asset["id"]}', binary=True) != files[name].read_bytes():
            raise ValueError('Existing release asset differs: ' + name)
    missing = files.keys() - existing.keys()
    if missing and not release['draft']:
        raise ValueError('Published release is incomplete; published assets will not be changed')
    if missing:
        gh(['release', 'upload', tag, '--repo', repo, *(str(files[name]) for name in sorted(missing))])
    if release['draft']:
        ready = find_release(repo, tag)
        if {row['name'] for row in ready['assets']} != files.keys():
            raise ValueError('Draft release upload is incomplete')
        for asset in ready['assets']:
            if api(f'repos/{repo}/releases/assets/{asset["id"]}', binary=True) != files[asset['name']].read_bytes():
                raise ValueError('Uploaded release asset differs')
        pointed = tag_commit(repo, tag)
        if pointed is not None and pointed != commit:
            raise ValueError('Release tag changed during upload')
        gh(['release', 'edit', tag, '--repo', repo, '--draft=false', '--latest=' + str(latest).lower()])
        if tag_commit(repo, tag) != commit:
            raise ValueError('Published release tag does not match the source commit')
    return {'repo': repo, 'tag': tag, 'commit': commit, 'assets': sorted(files),
            'url': f'https://github.com/{repo}/releases/tag/{tag}',
            'status': 'published' if release['draft'] else 'unchanged'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', required=True)
    parser.add_argument('--tag', required=True)
    parser.add_argument('--commit', required=True)
    parser.add_argument('--title', required=True)
    parser.add_argument('--notes', required=True)
    parser.add_argument('--latest', action='store_true')
    parser.add_argument('assets', nargs='+', type=Path)
    args = parser.parse_args()
    print(json.dumps(publish(args.repo, args.tag, args.commit, args.assets,
                             title=args.title, notes=args.notes, latest=args.latest), indent=2))


if __name__ == '__main__':
    main()
