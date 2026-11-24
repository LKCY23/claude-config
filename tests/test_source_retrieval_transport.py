"""Tests for source-retrieval-transport (--git-transport ssh and public Release fallback)."""
import contextlib
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import unittest
import urllib.error
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from claude_config.core.plugin_manager import PluginManager
from claude_config.core.reporting import build_report, render_text
from claude_config.core.schema import SchemaError
from claude_config.core.source_transport import (
    DEFAULT_GIT_TRANSPORT,
    github_api,
    git_remote_url,
    prepare_git,
    validate_git_transport,
)
from claude_config.fetch_baseline import command, fetch, main as fetch_main
from claude_config.plugin import main as plugin_main
from test_profile_plugins import FakeInstaller
from test_profile_resources import ResourceFixture, run_git


class _HttpResponse:
    def __init__(self, payload: bytes):
        self._payload = payload

    def read(self) -> bytes:
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class SourceRetrievalTransportTests(ResourceFixture):
    def prepare_release_fixture(self):
        self.add('guide', 'instruction', {'guide.md': 'v1'}, 'guide.md')
        self.commit = self.save()
        self.release = 'baseline-' + self.commit
        self.record = {
            'schema_version': 1,
            'kind': 'claudespace-baseline',
            'repo': 'owner/catalog',
            'commit': self.commit,
            'catalog_path': '.',
            'catalog_sha256': hashlib.sha256((self.repo / 'catalog.json').read_bytes()).hexdigest(),
            'validation': {'status': 'passed', 'level': 'structural', 'scope': 'catalog-schema'},
        }

    def test_validate_git_transport_and_remote_urls(self):
        self.assertEqual(validate_git_transport('gh-https'), 'gh-https')
        self.assertEqual(validate_git_transport('ssh'), 'ssh')
        self.assertEqual(git_remote_url('LKCY23/folio', 'gh-https'), 'https://github.com/LKCY23/folio.git')
        self.assertEqual(git_remote_url('LKCY23/folio', 'ssh'), 'git@github.com:LKCY23/folio.git')
        with self.assertRaises(SchemaError):
            validate_git_transport('https-anonymous')

    def test_prepare_git_gh_https_vs_ssh_arguments_and_verify_git(self):
        self.prepare_release_fixture()
        calls_https = []
        root_https = self.state / 'sources/https-case'

        def runner_https(args, **kwargs):
            calls_https.append((list(args), kwargs))
            args_list = list(args)
            if 'fetch' in args_list:
                args_list[args_list.index('protocol.file.allow=never')] = 'protocol.file.allow=always'
                args_list[args_list.index('origin')] = str(self.repo)
            return command(args_list, **kwargs)

        self.assertTrue(prepare_git(root_https, 'owner/catalog', self.commit, git_transport='gh-https', runner=runner_https))
        self.assertEqual(run_git(root_https, 'remote', 'get-url', 'origin'), 'https://github.com/owner/catalog.git')
        fetch_call_https = next(args for args, _ in calls_https if 'fetch' in args)
        self.assertIn('credential.helper=!gh auth git-credential', fetch_call_https)

        calls_ssh = []
        root_ssh = self.state / 'sources/ssh-case'

        def runner_ssh(args, **kwargs):
            calls_ssh.append((list(args), kwargs))
            args_list = list(args)
            if 'fetch' in args_list:
                args_list[args_list.index('protocol.file.allow=never')] = 'protocol.file.allow=always'
                args_list[args_list.index('origin')] = str(self.repo)
            return command(args_list, **kwargs)

        self.assertTrue(prepare_git(root_ssh, 'owner/catalog', self.commit, git_transport='ssh', runner=runner_ssh))
        self.assertEqual(run_git(root_ssh, 'remote', 'get-url', 'origin'), 'git@github.com:owner/catalog.git')
        fetch_args, fetch_kwargs = next((a, kw) for a, kw in calls_ssh if 'fetch' in a)
        self.assertNotIn('credential.helper=!gh auth git-credential', fetch_args)
        self.assertIn('credential.helper=', fetch_args)
        self.assertEqual(fetch_kwargs['extra_env']['GIT_SSH_COMMAND'], 'ssh -o BatchMode=yes')

    def test_ssh_command_failure_maps_to_clear_ssh_diagnostic(self):
        with patch('subprocess.run') as run_mock:
            run_mock.return_value = subprocess.CompletedProcess(
                args=['git', 'fetch'],
                returncode=128,
                stdout=b'',
                stderr=b'git@github.com: Permission denied (publickey).',
            )
            with self.assertRaisesRegex(SchemaError, 'SSH 拉取失败'):
                command(['git', 'fetch'])

    def test_github_api_prefers_gh_when_available(self):
        state = {}
        called_http = []

        def fake_gh(args):
            return b'{"ok": true}'

        def fake_http(req, timeout=30):
            called_http.append(req.full_url)
            raise AssertionError('HTTP fallback must not run when gh succeeds')

        out = github_api('repos/owner/catalog/releases/tags/v1', gh_runner=fake_gh, http_opener=fake_http, access_state=state)
        self.assertEqual(out, {'ok': True})
        self.assertEqual(state, {'release_access': 'gh'})
        self.assertEqual(called_http, [])

    def test_github_api_falls_back_to_public_https_only_on_gh_auth_unavailable(self):
        for exc in (
            FileNotFoundError('gh'),
            SchemaError('GitHub 凭据当前不可用或被拒绝；HTTP 401'),
        ):
            state = {}
            seen_headers = {}

            def failing_gh(args, err=exc):
                raise err

            def public_http(req, timeout=30):
                seen_headers['url'] = req.full_url
                seen_headers['accept'] = req.get_header('Accept')
                return _HttpResponse(b'{"tag_name": "baseline-abc"}')

            data = github_api(
                'repos/owner/catalog/releases/tags/baseline-abc',
                gh_runner=failing_gh,
                http_opener=public_http,
                access_state=state,
            )
            self.assertEqual(data, {'tag_name': 'baseline-abc'})
            self.assertEqual(state['release_access'], 'public-https-fallback')
            self.assertEqual(state['release_fallback_reason'], 'gh_auth_unavailable')
            self.assertEqual(seen_headers['url'], 'https://api.github.com/repos/owner/catalog/releases/tags/baseline-abc')
            self.assertEqual(seen_headers['accept'], 'application/vnd.github+json')

        # Binary asset download fallback
        bin_state = {}
        raw = github_api(
            'repos/owner/catalog/releases/assets/9',
            binary=True,
            gh_runner=lambda args: (_ for _ in ()).throw(SchemaError('GitHub 凭据当前不可用或被拒绝')),
            http_opener=lambda req, timeout=30: _HttpResponse(b'{"asset": 1}'),
            access_state=bin_state,
        )
        self.assertEqual(raw, b'{"asset": 1}')
        self.assertEqual(bin_state['release_access'], 'public-https-fallback')

    def test_github_api_private_repo_404_on_fallback_and_non_auth_gh_errors_fail_closed(self):
        # gh auth unavailable + private repo HTTP 404 -> fail closed with clear message
        def auth_fail(args):
            raise SchemaError('GitHub 凭据当前不可用或被拒绝')

        def http_404(req, timeout=30):
            raise urllib.error.HTTPError(req.full_url, 404, 'Not Found', {}, None)

        with self.assertRaisesRegex(SchemaError, '私有仓库 Release'):
            github_api('repos/private/repo/releases/tags/x', gh_runner=auth_fail, http_opener=http_404)

        # Authenticated gh 404 must NOT trigger public fallback
        def gh_404(args):
            raise SchemaError('GitHub 拒绝访问或找不到明确来源；核对仓库、Release 和当前账号权限。')

        with self.assertRaisesRegex(SchemaError, '找不到明确来源'):
            github_api(
                'repos/private/repo/releases/tags/x',
                gh_runner=gh_404,
                http_opener=lambda req, timeout=30: (_ for _ in ()).throw(AssertionError('must not fall back')),
            )

    def test_fetch_baseline_cli_with_ssh_and_public_fallback_reports_transport(self):
        self.prepare_release_fixture()
        raw_record = (json.dumps(self.record) + '\n').encode()

        def gh_401(args, **kwargs):
            raise SchemaError('GitHub 凭据当前不可用或被拒绝')

        def public_http(req, timeout=30):
            url = req.full_url
            if '/releases/tags/' in url:
                payload = {'tag_name': self.release, 'draft': False, 'assets': [{'id': 7, 'name': 'test.baseline.json', 'size': len(raw_record)}]}
                return _HttpResponse(json.dumps(payload).encode())
            if '/git/ref/tags/' in url:
                return _HttpResponse(json.dumps({'object': {'type': 'commit', 'sha': self.commit}}).encode())
            if url.endswith('/releases/assets/7'):
                return _HttpResponse(raw_record)
            raise AssertionError(f'Unexpected URL: {url}')

        def local_ssh_runner(args, **kwargs):
            args_list = list(args)
            if 'fetch' in args_list:
                self.assertEqual(kwargs.get('extra_env', {}).get('GIT_SSH_COMMAND'), 'ssh -o BatchMode=yes')
                args_list[args_list.index('protocol.file.allow=never')] = 'protocol.file.allow=always'
                args_list[args_list.index('origin')] = str(self.repo)
            return command(args_list, **kwargs)

        with patch('claude_config.core.source_transport.urllib.request.urlopen', side_effect=public_http):
            def dispatch_command(args, **kwargs):
                if args and args[0] == 'gh':
                    return gh_401(args, **kwargs)
                return local_ssh_runner(args, **kwargs)

            with patch('claude_config.fetch_baseline.command', side_effect=dispatch_command), \
                    contextlib.redirect_stdout(io.StringIO()) as out:
                rc = fetch_main([
                    '--machine', str(self.machine_path),
                    '--agent', 'claude-code',
                    '--catalog', 'test',
                    '--release', self.release,
                    '--git-transport', 'ssh',
                    '--format', 'text',
                ])
        self.assertEqual(rc, 0, msg=out.getvalue())
        self.assertIn('传输方式：git=ssh，release=public-https-fallback（gh_auth_unavailable）', out.getvalue())

        # Also verify JSON shape via fetch()
        with patch('claude_config.core.source_transport.urllib.request.urlopen', side_effect=public_http), \
                patch('claude_config.fetch_baseline.command', side_effect=lambda args, **kwargs: gh_401(args) if args[0] == 'gh' else local_ssh_runner(args, **kwargs)):
            result = fetch(self.manager(), 'test', self.release, git_transport='ssh')
        self.assertEqual(
            result['source_retrieval'],
            {
                'git_transport': 'ssh',
                'release_access': 'public-https-fallback',
                'release_fallback_reason': 'gh_auth_unavailable',
            },
        )
        report = build_report(result, operation='fetch-baseline', preview_only=False, execution_started=True)
        self.assertIn('public-https-fallback', render_text(report))

    def test_plugin_cli_passes_git_transport_and_rejects_on_read_only_or_toggle(self):
        self.add('tiny', 'plugin', {
            '.claude-plugin/plugin.json': json.dumps({'name': 'tiny', 'version': '1.0.0'}),
            'body.txt': 'one',
        }, '.claude-plugin/plugin.json')
        commit = self.save()
        installer = FakeInstaller()
        seen_transports = []

        def fake_prepare(root, repo, sha, **kwargs):
            seen_transports.append(kwargs.get('git_transport', DEFAULT_GIT_TRANSPORT))
            if root.exists():
                return False
            root.parent.mkdir(parents=True, exist_ok=True)
            run_git(self.root, 'clone', '--no-hardlinks', str(self.repo), str(root))
            run_git(root, 'remote', 'set-url', 'origin', git_remote_url(repo, kwargs.get('git_transport', DEFAULT_GIT_TRANSPORT)))
            run_git(root, 'checkout', '--detach', sha)
            return True

        with patch('claude_config.plugin.ProfileManager', side_effect=lambda *a: self.manager()), \
                patch.object(PluginOperation_cls := __import__('claude_config.core.native_plugins', fromlist=['PluginOperation']).PluginOperation, '_call', lambda op, args, work: installer(op, args, work)), \
                patch('claude_config.fetch_baseline.prepare_git', side_effect=fake_prepare), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            rc = plugin_main([
                'install', 'tiny',
                '--agent', 'claude-code',
                '--machine', str(self.machine_path),
                '--commit', commit,
                '--git-transport', 'ssh',
                '--dry-run',
                '--format', 'json',
            ])
        self.assertEqual(rc, 0, msg=out.getvalue())
        self.assertEqual(seen_transports, ['ssh'])

        # Reject --git-transport ssh on list/status/enable/disable
        for bad_args in (
            ['list', '--agent', 'claude-code', '--machine', str(self.machine_path), '--git-transport', 'ssh', '--format', 'json'],
            ['enable', 'tiny', '--agent', 'claude-code', '--machine', str(self.machine_path), '--git-transport', 'ssh', '--format', 'json'],
        ):
            with contextlib.redirect_stdout(io.StringIO()) as err_out:
                rc_bad = plugin_main(bad_args)
            self.assertNotEqual(rc_bad, 0)
            payload = json.loads(err_out.getvalue())
            self.assertEqual(payload['errors'][0]['code'], 'invalid_arguments')


if __name__ == '__main__':
    unittest.main()
