"""Shared source retrieval transports (Git HTTPS/SSH and public Release fallback)."""
from pathlib import Path
import os
import tempfile
import urllib.error
import urllib.request

from .catalog import verify_git
from .native import parse_json
from .reporting import _private_path
from .schema import SchemaError, require

GIT_TRANSPORTS = ('gh-https', 'ssh')
DEFAULT_GIT_TRANSPORT = 'gh-https'

_AUTH_ERROR_MARKERS = (
    '401',
    'requires authentication',
    'not logged',
    'bad credentials',
    '凭据当前不可用',
)
_SSH_ERROR_MARKERS = (
    'permission denied',
    'publickey',
    'host key verification',
    'could not read from remote repository',
)


def validate_git_transport(transport):
    """Ensure the requested Git transport policy is one of the supported values."""
    require(transport in GIT_TRANSPORTS, f'Unsupported git transport: {transport}')
    return transport


def git_remote_url(repo, transport=DEFAULT_GIT_TRANSPORT):
    """Return the GitHub remote URL corresponding to the selected Git transport."""
    validate_git_transport(transport)
    if transport == 'ssh':
        return f'git@github.com:{repo}.git'
    return f'https://github.com/{repo}.git'


def classify_command_error(stderr_text):
    """Map native git/gh stderr to safe, credential-free SchemaError diagnostics."""
    diagnostic = (stderr_text or '').lower()
    if any(marker in diagnostic for marker in _AUTH_ERROR_MARKERS):
        return SchemaError('GitHub 凭据当前不可用或被拒绝；检查当前执行环境的登录和钥匙串可访问性，不能仅据此认定 token 过期。')
    if any(marker in diagnostic for marker in _SSH_ERROR_MARKERS):
        return SchemaError('SSH 拉取失败：请检查本机 GitHub SSH Key、ssh-agent 或 ~/.ssh/config；未修改配置选择。')
    if '403' in diagnostic or '404' in diagnostic or 'repository not found' in diagnostic:
        return SchemaError('GitHub 拒绝访问或找不到明确来源；核对仓库、Release 和当前账号权限。')
    if any(text in diagnostic for text in ('could not resolve', 'timed out', 'failed to connect', 'network is unreachable')):
        return SchemaError('获取固定来源时网络不可达或超时；已保留准备好的来源，未采用配置。')
    return SchemaError('获取失败：检查 GitHub 访问权限、网络及 Git/gh 配置；未修改配置选择。')


def _is_auth_unavailable(exc):
    if isinstance(exc, FileNotFoundError):
        return True
    text = str(exc).lower()
    return any(marker in text for marker in _AUTH_ERROR_MARKERS)


def _invoke_runner(runner, args, *, extra_env=None):
    if extra_env is None:
        return runner(args)
    return runner(args, extra_env=extra_env)


def prepare_git(root, repo, commit, *, git_transport=DEFAULT_GIT_TRANSPORT, runner=None):
    """Keep existing checkouts intact; publish a new checkout only when complete."""
    validate_git_transport(git_transport)
    root = Path(root).absolute()
    require(not root.is_symlink(), 'Prepared source cannot be a symlink')
    if root.exists() and any(root.iterdir()):
        verify_git(root, repo, commit)
        return False
    if runner is None:
        from ..fetch_baseline import command as runner

    _private_path(root.parent)
    remote_url = git_remote_url(repo, git_transport)
    with tempfile.TemporaryDirectory(prefix='.fetch-', dir=root.parent) as stage:
        _invoke_runner(runner, ['git', '-c', 'core.hooksPath=/dev/null', 'init', '--quiet', stage])
        _invoke_runner(runner, ['git', '-C', stage, 'remote', 'add', 'origin', remote_url])
        common = [
            'git', '-C', stage,
            '-c', 'core.hooksPath=/dev/null',
            '-c', 'core.fsmonitor=false',
            '-c', 'protocol.file.allow=never',
            '-c', 'credential.helper=',
        ]
        extra_env = None
        if git_transport == 'gh-https':
            common.append('-c')
            common.append('credential.helper=!gh auth git-credential')
        else:
            extra_env = {'GIT_SSH_COMMAND': os.environ.get('GIT_SSH_COMMAND') or 'ssh -o BatchMode=yes'}
        _invoke_runner(runner, [*common, 'fetch', '--quiet', '--depth=1', 'origin', commit], extra_env=extra_env)
        _invoke_runner(runner, [*common, 'checkout', '--quiet', '--detach', 'FETCH_HEAD'])
        verify_git(Path(stage), repo, commit)
        if root.exists():
            require(root.is_dir() and not any(root.iterdir()), 'Source destination changed during retrieval')
            root.rmdir()
        Path(stage).rename(root)
    return True


def _public_github_get(path, *, binary=False, http_opener=None):
    opener = http_opener or urllib.request.urlopen
    url = f"https://api.github.com/{path.lstrip('/')}"
    accept = 'application/octet-stream' if binary else 'application/vnd.github+json'
    req = urllib.request.Request(
        url,
        headers={
            'Accept': accept,
            'User-Agent': 'claude-config',
        },
    )
    try:
        with opener(req, timeout=30) as resp:
            return resp.read()
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403, 404):
            raise SchemaError(
                'GitHub 凭据不可用且公开只读访问失败（私有仓库 Release 需本机有效的 gh 登录态，不能匿名读取）。'
            ) from exc
        raise SchemaError(f'公开只读访问 GitHub Release 失败（HTTP {exc.code}）；请检查网络或稍后重试。') from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise SchemaError('公开只读访问 GitHub Release 时网络不可达或超时；已保留准备好的来源，未采用配置。') from exc


def github_api(path, *, binary=False, gh_runner=None, http_opener=None, access_state=None):
    """Call gh api first; fall back to read-only public HTTPS only when gh credentials are unavailable."""
    if gh_runner is None:
        from ..fetch_baseline import command as gh_runner

    args = ['gh', 'api', path]
    if binary:
        args += ['-H', 'Accept: application/octet-stream']

    try:
        data = gh_runner(args)
        if access_state is not None:
            access_state['release_access'] = 'gh'
        return data if binary else parse_json(data)
    except (SchemaError, FileNotFoundError) as exc:
        if not _is_auth_unavailable(exc):
            raise
        data = _public_github_get(path, binary=binary, http_opener=http_opener)
        if access_state is not None:
            access_state['release_access'] = 'public-https-fallback'
            access_state['release_fallback_reason'] = 'gh_auth_unavailable'
        return data if binary else parse_json(data)
