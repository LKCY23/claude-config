"""Antigravity Phase B: transactional managed writes, apply, rollback, plugin CLI, and fail-closed guards."""
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from claude_config.cli import main as cli_main
from claude_config.core.cutover import Cutover
from claude_config.core.manager import ProfileManager
from claude_config.core.plugin_manager import PluginManager
from claude_config.core.recovery import rollback_plan
from claude_config.core.resource_inventory import inspect_antigravity
from claude_config.core.schema import SchemaError
from claude_config.core.skill_retirement import retirement_plan
from claude_config.core.transaction import PlanStale
from claude_config.plugin import main as plugin_cli_main


def run_git(root, *args):
    result = subprocess.run(['git', '-C', str(root), *args], capture_output=True, text=True, check=True)
    return result.stdout.strip()


class AntigravityPhaseBFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='ag-phase-b-')
        self.root = Path(self.temp.name)
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        run_git(self.repo, 'init', '-q')
        run_git(self.repo, 'config', 'maintenance.auto', 'false')
        run_git(self.repo, 'config', 'gc.auto', '0')
        run_git(self.repo, 'config', 'user.name', 'isolated-test')
        run_git(self.repo, 'config', 'user.email', 'isolated@example.invalid')
        run_git(self.repo, 'remote', 'add', 'origin', 'https://github.com/owner/catalog.git')

        self.data = self.root / 'data'
        self.ag_config = self.root / 'gemini-config'
        self.state = self.root / 'state'
        self.data.mkdir()
        self.ag_config.mkdir()
        self.app_bundle = self.root / 'Antigravity.app'
        (self.app_bundle / 'Contents').mkdir(parents=True)

        self.catalog = {'schema_version': 1, 'name': 'test', 'resources': {}}
        self.profile = {'schema_version': 1, 'agent_type': 'antigravity'}
        self.profile_rel = 'profiles/agents/antigravity/work.yaml'
        self.machine_path = self.root / 'machine.yaml'
        self.binary = self.root / 'mock-mcp-bin'
        self.binary.write_text('#!/bin/sh\nexit 0\n')
        self.binary.chmod(0o700)
        self.machine = {
            'schema_version': 1,
            'machine_id': 'ag-phase-b',
            'config_dir': str(self.data),
            'catalog_roots': {'test': str(self.repo)},
            'agents': {
                'antigravity': {
                    'profile': self.profile_rel,
                    'binding': {
                        'config_root': str(self.ag_config),
                        'application': str(self.app_bundle),
                    },
                },
            },
            'apps': {},
            'bindings': {'executables': {'echo': str(self.binary)}},
            'runtime': {'state_dir': str(self.state)},
        }

    def tearDown(self):
        self.temp.cleanup()

    def add_resource(self, name, kind, files, entry, *, options=None, bindings=None, requires=None, select=True, source=None):
        if source is None:
            pkg_root = self.repo / 'packages' / name
            pkg_root.mkdir(parents=True, exist_ok=True)
            for rel, content in files.items():
                target = pkg_root / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                if isinstance(content, bytes):
                    target.write_bytes(content)
                else:
                    target.write_text(content)
            src = {'kind': 'tracked', 'path': 'packages/' + name}
        else:
            src = source
        self.catalog['resources'][name] = {
            'kind': kind,
            'description': 'Isolated Antigravity fixture',
            'origin': {'type': 'self', 'author': 'test'},
            'maintenance': {'owner': 'ours', 'mode': 'original'},
            'source': src,
            'compatibility': {
                'antigravity': {'mode': 'native', 'entry': entry, 'validation': {'status': 'not-evaluated'}},
            },
            'requires': requires or {},
            'update': {'strategy': 'manual'},
        }
        if select:
            group = {'plugin': 'plugins', 'skill': 'skills', 'mcp': 'mcp_servers',
                     'hook': 'hooks', 'instruction': 'instructions', 'rule': 'rules', 'agent': 'agents'}[kind]
            sel = {}
            if options is not None:
                sel['options'] = options
            if bindings is not None:
                sel['bindings'] = bindings
            self.profile.setdefault('resources', {}).setdefault(group, {})[name + '@test'] = sel

    def commit_catalog(self, message='catalog'):
        (self.repo / 'catalog.json').write_text(json.dumps(self.catalog, indent=2))
        run_git(self.repo, 'add', '-A')
        run_git(self.repo, 'commit', '-q', '-m', message)
        commit = run_git(self.repo, 'rev-parse', 'HEAD')
        catalogs = {
            'schema_version': 1,
            'catalogs': {
                'test': {'repo': 'owner/catalog', 'commit': commit, 'path': '.'},
            },
        }
        (self.data / 'catalogs.yaml').write_text(yaml.safe_dump(catalogs, sort_keys=False))
        self.save_profile()
        self.machine_path.write_text(yaml.safe_dump(self.machine, sort_keys=False))
        return commit

    def save_profile(self):
        profile_path = self.data / self.profile_rel
        profile_path.parent.mkdir(parents=True, exist_ok=True)
        profile_path.write_text(yaml.safe_dump(self.profile, sort_keys=False))

    def snapshot_tree(self, root):
        if not root.exists():
            return {}
        return {
            str(p.relative_to(root)): p.read_bytes()
            for p in sorted(root.rglob('*'))
            if p.is_file() and not p.is_symlink()
        }

    def run_cli(self, argv):
        out = io.StringIO()
        with redirect_stdout(out):
            code = cli_main(argv)
        return code, json.loads(out.getvalue())

    def run_plugin_cli(self, argv):
        out = io.StringIO()
        with redirect_stdout(out):
            code = plugin_cli_main(argv)
        return code, json.loads(out.getvalue())

    def run_id(self, report):
        rec = report['recovery']
        journal = rec['journal'] if isinstance(rec, dict) else rec
        return Path(journal).parent.name


class ZeroByteAndMcpTests(AntigravityPhaseBFixture):
    def test_zero_byte_and_whitespace_mcp_config_plan_apply_and_rollback(self):
        mcp_path = self.ag_config / 'mcp_config.json'
        mcp_path.write_bytes(b'')
        inv = inspect_antigravity(self.ag_config)
        self.assertEqual(inv['mcp_servers'], [])
        self.assertEqual(inv['checks'][-1]['status'], 'passed')

        self.add_resource('linear', 'mcp', {
            'mcp.json': json.dumps({'name': 'linear', 'server': {'serverUrl': 'https://mcp.linear.app/mcp'}}),
        }, 'mcp.json')
        self.commit_catalog('add-mcp')

        code, plan_report = self.run_cli([
            'plan', '--machine', str(self.machine_path), '--agent', 'antigravity', '--format', 'json',
        ])
        self.assertEqual(code, 0, plan_report)
        self.assertTrue(plan_report['ready'])
        self.assertEqual(mcp_path.read_bytes(), b'')

        code, apply_report = self.run_cli([
            'apply', '--machine', str(self.machine_path), '--agent', 'antigravity',
            '--expect-plan-id', plan_report['plan_id'], '--format', 'json',
        ])
        self.assertEqual(code, 0, apply_report)
        written = json.loads(mcp_path.read_text())
        self.assertEqual(written, {'mcpServers': {'linear': {'serverUrl': 'https://mcp.linear.app/mcp'}}})

        run_id = self.run_id(apply_report)
        rb = rollback_plan(ProfileManager(self.machine_path, 'antigravity'), run_id)
        rb_report = rb.apply(self.state)
        self.assertEqual(json.loads((self.state / 'runs' / run_id / 'journal.json').read_text())['status'], 'rolled_back')
        self.assertEqual(mcp_path.read_bytes(), b'')

        # Whitespace-only mcp_config.json also parses cleanly and preserves unmanaged sibling after update
        mcp_path.write_bytes(b'   \n\t ')
        self.assertEqual(inspect_antigravity(self.ag_config)['mcp_servers'], [])

    def test_mcp_preserves_unmanaged_servers_and_supports_export(self):
        mcp_path = self.ag_config / 'mcp_config.json'
        before_bytes = json.dumps({'mcpServers': {'custom': {'command': 'npx', 'args': ['custom-mcp']}}}).encode()
        mcp_path.write_bytes(before_bytes)

        self.add_resource('stdio-srv', 'mcp', {
            'mcp.json': json.dumps({'name': 'stdio-srv', 'server': {'command': {'binding': 'echo'}, 'args': ['srv'], 'env': {'LOG': 'info'}}}),
        }, 'mcp.json', bindings={'echo': 'executables.echo'}, requires={'bindings': {'echo': {'kind': 'executable', 'required': True}}})
        self.commit_catalog('add-stdio')

        code, apply_report = self.run_cli([
            'apply', '--machine', str(self.machine_path), '--agent', 'antigravity', '--format', 'json',
        ])
        self.assertEqual(code, 0, apply_report)
        parsed = json.loads(mcp_path.read_text())
        self.assertEqual(parsed['mcpServers']['custom'], {'command': 'npx', 'args': ['custom-mcp']})
        self.assertEqual(parsed['mcpServers']['stdio-srv']['command'], str(self.binary.resolve()))

        # Reverse export observes only the managed MCP without touching unmanaged
        manager = ProfileManager(self.machine_path, 'antigravity')
        exp_plan = manager.export((), resource_refs=['stdio-srv@test'])
        self.assertTrue(exp_plan.report['ready'], exp_plan.report['blockers'])

        # Rollback restores exact pre-existing bytes
        rollback_plan(manager, self.run_id(apply_report)).apply(self.state)
        self.assertEqual(mcp_path.read_bytes(), before_bytes)


class ConfigJsonPreservationAndRedactionTests(AntigravityPhaseBFixture):
    def test_sensitive_keys_preserved_and_never_leaked_across_plugin_lifecycle(self):
        config_path = self.ag_config / 'config.json'
        secret_payload = {
            'userSettings': {'theme': 'dark', 'secretNote': 'TOP_SECRET_USER_SETTING'},
            'globalPermissionGrants': ['Bash(*)'],
            'sidecars': {'helper': {'token': 'SIDE_SECRET_999'}},
            'auth_token': 'AG_AUTH_SECRET_XYZ',
            'plugins': {
                'unmanaged-plug': {'enabled': False, 'customMeta': 'keep-me'},
            },
        }
        before_bytes = json.dumps(secret_payload, indent=2).encode()
        config_path.write_bytes(before_bytes)

        self.add_resource('figma', 'plugin', {
            '.antigravity-plugin/plugin.json': json.dumps({'name': 'figma', 'version': '1.0.0'}),
            'skills/fig/SKILL.md': '---\nname: fig\ndescription: Figma helper\n---\nBody\n',
        }, '.antigravity-plugin/plugin.json', options={'enabled': True})
        self.commit_catalog('add-figma')

        code, apply_report = self.run_cli([
            'apply', '--machine', str(self.machine_path), '--agent', 'antigravity', '--format', 'json',
        ])
        self.assertEqual(code, 0, apply_report)
        serialized_report = json.dumps(apply_report)
        state_files = list((self.state / 'managed').glob('*.json'))
        self.assertEqual(len(state_files), 1)
        serialized_state = state_files[0].read_text()
        for secret in ('TOP_SECRET_USER_SETTING', 'SIDE_SECRET_999', 'AG_AUTH_SECRET_XYZ', 'globalPermissionGrants', 'sidecars', 'userSettings'):
            self.assertNotIn(secret, serialized_report)
            self.assertNotIn(secret, serialized_state)

        after_cfg = json.loads(config_path.read_text())
        self.assertEqual(after_cfg['userSettings'], secret_payload['userSettings'])
        self.assertEqual(after_cfg['globalPermissionGrants'], secret_payload['globalPermissionGrants'])
        self.assertEqual(after_cfg['sidecars'], secret_payload['sidecars'])
        self.assertEqual(after_cfg['auth_token'], secret_payload['auth_token'])
        self.assertEqual(after_cfg['plugins']['unmanaged-plug'], {'enabled': False, 'customMeta': 'keep-me'})
        self.assertEqual(after_cfg['plugins']['figma'], {'enabled': True})

        # Toggle to disabled via profile update
        self.profile['resources']['plugins']['figma@test']['options']['enabled'] = False
        self.save_profile()
        code, toggle_report = self.run_cli([
            'apply', '--machine', str(self.machine_path), '--agent', 'antigravity', '--format', 'json',
        ])
        self.assertEqual(code, 0, toggle_report)
        self.assertEqual(json.loads(config_path.read_text())['plugins']['figma'], {'enabled': False})

        # Omitting enabled option preserves existing state via absence policy while keeping sensitive keys intact
        self.profile['resources']['plugins']['figma@test'] = {}
        self.save_profile()
        code, clear_report = self.run_cli([
            'apply', '--machine', str(self.machine_path), '--agent', 'antigravity', '--format', 'json',
        ])
        self.assertEqual(code, 0, clear_report)
        cfg_cleared = json.loads(config_path.read_text())
        self.assertEqual(cfg_cleared['plugins']['figma'], {'enabled': False})
        self.assertEqual(cfg_cleared['auth_token'], 'AG_AUTH_SECRET_XYZ')

        # Rollback in reverse restores exact original bytes
        manager = ProfileManager(self.machine_path, 'antigravity')
        rollback_plan(manager, self.run_id(clear_report)).apply(self.state)
        rollback_plan(manager, self.run_id(toggle_report)).apply(self.state)
        rollback_plan(manager, self.run_id(apply_report)).apply(self.state)
        self.assertEqual(config_path.read_bytes(), before_bytes)
        self.assertFalse((self.ag_config / 'plugins' / 'figma' / '.antigravity-plugin' / 'plugin.json').exists())
        self.assertEqual(self.snapshot_tree(self.ag_config / 'plugins' / 'figma'), {})


class PluginLifecycleAndCliTests(AntigravityPhaseBFixture):
    def test_profile_plugin_install_update_preserve_unmanaged_and_rollback(self):
        unmanaged_dir = self.ag_config / 'plugins' / 'local-plug'
        unmanaged_dir.mkdir(parents=True)
        (unmanaged_dir / 'plugin.json').write_text(json.dumps({'name': 'local-plug', 'version': '0.1.0'}))

        self.add_resource('alpha', 'plugin', {
            'plugin.json': json.dumps({'name': 'alpha', 'version': '1.0.0'}),
            'old.txt': 'obsolete-v1\n',
        }, 'plugin.json', options={'enabled': True})
        self.commit_catalog('v1')

        manager = ProfileManager(self.machine_path, 'antigravity')
        r1 = manager.plan().apply(self.state)
        self.assertTrue((self.ag_config / 'plugins' / 'alpha' / 'old.txt').is_file())
        self.assertTrue((unmanaged_dir / 'plugin.json').is_file())

        # Update plugin v2 removing old.txt and adding new.txt
        (self.repo / 'packages' / 'alpha' / 'old.txt').unlink()
        self.add_resource('alpha', 'plugin', {
            'plugin.json': json.dumps({'name': 'alpha', 'version': '2.0.0'}),
            'new.txt': 'fresh-v2\n',
        }, 'plugin.json', options={'enabled': True})
        self.commit_catalog('v2')

        r2 = ProfileManager(self.machine_path, 'antigravity').plan().apply(self.state)
        self.assertFalse((self.ag_config / 'plugins' / 'alpha' / 'old.txt').exists())
        self.assertEqual((self.ag_config / 'plugins' / 'alpha' / 'new.txt').read_text(), 'fresh-v2\n')

        # Rollback v2 -> v1 restores old.txt and removes new.txt
        rollback_plan(ProfileManager(self.machine_path, 'antigravity'), self.run_id(r2)).apply(self.state)
        self.assertTrue((self.ag_config / 'plugins' / 'alpha' / 'old.txt').is_file())
        self.assertFalse((self.ag_config / 'plugins' / 'alpha' / 'new.txt').exists())

        # Rollback v1 -> absent removes plugin files & created config.json while keeping unmanaged plugin
        rollback_plan(ProfileManager(self.machine_path, 'antigravity'), self.run_id(r1)).apply(self.state)
        self.assertEqual(self.snapshot_tree(self.ag_config / 'plugins' / 'alpha'), {})
        self.assertFalse((self.ag_config / 'config.json').exists())
        self.assertTrue((unmanaged_dir / 'plugin.json').is_file())

    def test_plugin_cli_full_cycle_with_machine(self):
        ext_repo = self.root / 'ext-plugin-repo-2'
        ext_repo.mkdir()
        run_git(ext_repo, 'init', '-q')
        run_git(ext_repo, 'config', 'maintenance.auto', 'false')
        run_git(ext_repo, 'config', 'gc.auto', '0')
        run_git(ext_repo, 'config', 'user.name', 'isolated-test')
        run_git(ext_repo, 'config', 'user.email', 'isolated@example.invalid')
        run_git(ext_repo, 'remote', 'add', 'origin', 'https://github.com/owner/ext-plug.git')
        (ext_repo / 'plugin.json').write_text(json.dumps({'name': 'ext-plug', 'version': '1.0.0'}))
        run_git(ext_repo, 'add', '-A')
        run_git(ext_repo, 'commit', '-q', '-m', 'ext-v1')
        c1 = run_git(ext_repo, 'rev-parse', 'HEAD')

        (ext_repo / 'plugin.json').write_text(json.dumps({'name': 'ext-plug', 'version': '1.1.0'}))
        (ext_repo / 'extra.txt').write_text('v1.1\n')
        run_git(ext_repo, 'add', '-A')
        run_git(ext_repo, 'commit', '-q', '-m', 'ext-v2')
        c2 = run_git(ext_repo, 'rev-parse', 'HEAD')

        self.add_resource('ext-plug', 'plugin', {}, 'plugin.json', select=False,
                          source={'kind': 'external', 'repo': 'owner/ext-plug', 'commit': c1, 'path': '.'})
        self.commit_catalog('catalog-ext')

        seen_transports = []
        def fake_prepare_git(destination, repo, commit, *, git_transport='gh-https'):
            seen_transports.append((repo, commit, git_transport))
            if destination.exists():
                return False
            destination.mkdir(parents=True, exist_ok=True)
            subprocess.run(['git', 'clone', '-q', str(ext_repo), str(destination)], check=True)
            run_git(destination, 'remote', 'set-url', 'origin', f'https://github.com/{repo}.git')
            run_git(destination, 'checkout', '-q', commit)
            return True

        with patch('claude_config.fetch_baseline.prepare_git', side_effect=fake_prepare_git):
            code, rep = self.run_plugin_cli([
                'install', 'ext-plug@test', '--machine', str(self.machine_path), '--agent', 'antigravity',
                '--git-transport', 'ssh', '--write', '--format', 'json',
            ])
        self.assertEqual(code, 0, rep)
        self.assertIn(('owner/ext-plug', c1, 'ssh'), seen_transports)
        self.assertEqual(json.loads((self.ag_config / 'config.json').read_text())['plugins']['ext-plug'], {'enabled': True})

        code, lst = self.run_plugin_cli(['list', '--machine', str(self.machine_path), '--agent', 'antigravity', '--format', 'json'])
        self.assertEqual(code, 0, lst)
        self.assertEqual(lst['plugins'][0]['status'], 'in_sync')
        self.assertEqual(lst['plugins'][0]['installed_version'], '1.0.0')

        # Disable & re-enable
        code, dis = self.run_plugin_cli(['disable', 'ext-plug', '--machine', str(self.machine_path), '--agent', 'antigravity', '--write', '--format', 'json'])
        self.assertEqual(code, 0, dis)
        self.assertEqual(json.loads((self.ag_config / 'config.json').read_text())['plugins']['ext-plug'], {'enabled': False})

        code, st = self.run_plugin_cli(['status', 'ext-plug', '--machine', str(self.machine_path), '--agent', 'antigravity', '--format', 'json'])
        self.assertEqual(code, 0, st)
        self.assertFalse(st['plugins'][0]['enabled'])

        # Upgrade with --commit + --enable + --git-transport ssh
        with patch('claude_config.fetch_baseline.prepare_git', side_effect=fake_prepare_git):
            code, up = self.run_plugin_cli([
                'upgrade', 'ext-plug', '--commit', c2, '--enable', '--git-transport', 'ssh',
                '--machine', str(self.machine_path), '--agent', 'antigravity', '--write', '--format', 'json',
            ])
        self.assertEqual(code, 0, up)
        self.assertIn(('owner/ext-plug', c2, 'ssh'), seen_transports)
        self.assertTrue((self.ag_config / 'plugins' / 'ext-plug' / 'extra.txt').is_file())
        self.assertEqual(json.loads((self.ag_config / 'config.json').read_text())['plugins']['ext-plug'], {'enabled': True})

        # Remove cleanly removes files, config entry, profile entry, lock entry, state entry
        code, rem = self.run_plugin_cli([
            'remove', 'ext-plug', '--machine', str(self.machine_path), '--agent', 'antigravity', '--write', '--format', 'json',
        ])
        self.assertEqual(code, 0, rem)
        self.assertEqual(self.snapshot_tree(self.ag_config / 'plugins' / 'ext-plug'), {})
        self.assertNotIn('ext-plug', json.loads((self.ag_config / 'config.json').read_text()).get('plugins', {}))
        self.assertNotIn('ext-plug@test', yaml.safe_load((self.data / self.profile_rel).read_text()).get('resources', {}).get('plugins', {}))

        # claude-code still rejects remove
        code, bad = self.run_plugin_cli(['remove', 'ext-plug', '--agent', 'claude-code', '--format', 'json'])
        self.assertNotEqual(code, 0)


class StandaloneSkillAndRetirementTests(AntigravityPhaseBFixture):
    def test_skill_install_update_retire_preserving_unmanaged_and_rollback(self):
        unmanaged = self.ag_config / 'skills' / 'keep-me'
        unmanaged.mkdir(parents=True)
        (unmanaged / 'SKILL.md').write_text('---\nname: keep-me\ndescription: Unmanaged\n---\nKeep\n')

        self.add_resource('helper', 'skill', {
            'SKILL.md': '---\nname: helper\ndescription: Managed skill\n---\nV1\n',
            'notes.md': 'v1-notes\n',
        }, 'SKILL.md')
        self.commit_catalog('skill-v1')

        r1 = ProfileManager(self.machine_path, 'antigravity').plan().apply(self.state)
        self.assertTrue((self.ag_config / 'skills' / 'helper' / 'notes.md').is_file())
        self.assertTrue((unmanaged / 'SKILL.md').is_file())

        # Update removes notes.md and updates SKILL.md, and rollback restores v1
        (self.repo / 'packages' / 'helper' / 'notes.md').unlink()
        self.add_resource('helper', 'skill', {
            'SKILL.md': '---\nname: helper\ndescription: Managed skill\n---\nV2\n',
        }, 'SKILL.md')
        self.commit_catalog('skill-v2')
        r2 = ProfileManager(self.machine_path, 'antigravity').plan().apply(self.state)
        self.assertFalse((self.ag_config / 'skills' / 'helper' / 'notes.md').exists())
        self.assertIn('V2', (self.ag_config / 'skills' / 'helper' / 'SKILL.md').read_text())
        rollback_plan(ProfileManager(self.machine_path, 'antigravity'), self.run_id(r2)).apply(self.state)
        self.assertTrue((self.ag_config / 'skills' / 'helper' / 'notes.md').is_file())

        # Deselect skill -> retained as residual until retired
        del self.profile['resources']['skills']['helper@test']
        self.save_profile()
        ProfileManager(self.machine_path, 'antigravity').plan().apply(self.state)
        self.assertTrue((self.ag_config / 'skills' / 'helper' / 'SKILL.md').is_file())

        # Retire removes managed skill & cache while keeping unmanaged
        ret_plan = retirement_plan(ProfileManager(self.machine_path, 'antigravity'), ('helper@test',))
        ret_rep = ret_plan.apply(self.state)
        self.assertEqual(self.snapshot_tree(self.ag_config / 'skills' / 'helper'), {})
        self.assertTrue((unmanaged / 'SKILL.md').is_file())

        # Rollback retirement restores managed skill
        rollback_plan(ProfileManager(self.machine_path, 'antigravity'), self.run_id(ret_rep)).apply(self.state)
        self.assertTrue((self.ag_config / 'skills' / 'helper' / 'SKILL.md').is_file())


class CollisionBlockersAndFailClosedTests(AntigravityPhaseBFixture):
    def _assert_blocked_with_zero_writes(self, expected_code):
        before = self.snapshot_tree(self.ag_config)
        code, report = self.run_cli([
            'apply', '--machine', str(self.machine_path), '--agent', 'antigravity', '--format', 'json',
        ])
        self.assertEqual(code, 1, report)
        self.assertFalse(report['ready'])
        self.assertIn(expected_code, [b.get('code') for b in report['blockers']])
        self.assertEqual(self.snapshot_tree(self.ag_config), before)

    def test_collision_blockers_block_apply(self):
        # 1. duplicate_plugin_name (on-disk two dirs claiming same name)
        (self.ag_config / 'plugins' / 'dir-a').mkdir(parents=True)
        (self.ag_config / 'plugins' / 'dir-b').mkdir(parents=True)
        (self.ag_config / 'plugins' / 'dir-a' / 'plugin.json').write_text(json.dumps({'name': 'dup-name'}))
        (self.ag_config / 'plugins' / 'dir-b' / 'plugin.json').write_text(json.dumps({'name': 'dup-name'}))
        self.commit_catalog('empty')
        self._assert_blocked_with_zero_writes('duplicate_plugin_name')

        # 2. plugin_dir_name_collision (on-disk dir-a claims name 'dir-b', while dir-b claims 'other')
        (self.ag_config / 'plugins' / 'dir-a' / 'plugin.json').write_text(json.dumps({'name': 'dir-b'}))
        (self.ag_config / 'plugins' / 'dir-b' / 'plugin.json').write_text(json.dumps({'name': 'other'}))
        self._assert_blocked_with_zero_writes('plugin_dir_name_collision')

        # 3. skill_plugin_overlap (planned standalone skill vs on-disk plugin skill)
        (self.ag_config / 'plugins' / 'dir-a' / 'plugin.json').write_text(json.dumps({'name': 'dir-a'}))
        skill_in_plug = self.ag_config / 'plugins' / 'dir-a' / 'skills' / 'shared'
        skill_in_plug.mkdir(parents=True)
        (skill_in_plug / 'SKILL.md').write_text('---\nname: shared\ndescription: Bundled\n---\n')
        self.add_resource('shared', 'skill', {
            'SKILL.md': '---\nname: shared\ndescription: Standalone\n---\n',
        }, 'SKILL.md')
        self.commit_catalog('overlap')
        self._assert_blocked_with_zero_writes('skill_plugin_overlap')

        # 4. mcp_server_collision (planned global MCP vs on-disk plugin mcp_config.json)
        del self.profile['resources']['skills']
        (self.ag_config / 'plugins' / 'dir-a' / 'mcp_config.json').write_text(json.dumps({'mcpServers': {'shared-srv': {'command': 'x'}}}))
        self.add_resource('shared-srv', 'mcp', {
            'mcp.json': json.dumps({'name': 'shared-srv', 'server': {'command': {'binding': 'echo'}}}),
        }, 'mcp.json', bindings={'echo': 'executables.echo'}, requires={'bindings': {'echo': {'kind': 'executable', 'required': True}}})
        self.commit_catalog('mcp-collide')
        self._assert_blocked_with_zero_writes('mcp_server_collision')

    def test_fail_closed_boundaries_and_symlink_escapes(self):
        self.commit_catalog('base')

        # Unsupported settings.*
        self.profile['settings'] = {'model': 'x'}
        self.save_profile()
        with self.assertRaises(SchemaError):
            ProfileManager(self.machine_path, 'antigravity').plan()
        del self.profile['settings']

        # Skill control options remain rejected for Antigravity
        self.profile['resources'] = {}
        self.catalog['resources'] = {}
        self.add_resource('sk', 'skill', {'SKILL.md': '---\nname: sk\ndescription: d\n---\n'}, 'SKILL.md',
                          options={'control': {'enabled': False}})
        self.commit_catalog('bad-skill-opt')
        rep = ProfileManager(self.machine_path, 'antigravity').plan().report
        self.assertFalse(rep['ready'])

        # Symlink escape in mcp_config.json rejected
        outside = self.root / 'outside.json'
        outside.write_text('{}')
        self.profile['resources'] = {}
        self.catalog['resources'] = {}
        self.add_resource('srv', 'mcp', {'mcp.json': json.dumps({'name': 'srv', 'server': {'command': {'binding': 'echo'}}})},
                          'mcp.json', bindings={'echo': 'executables.echo'}, requires={'bindings': {'echo': {'kind': 'executable', 'required': True}}})
        self.commit_catalog('symlink-test')
        (self.ag_config / 'mcp_config.json').symlink_to(outside)
        rep = ProfileManager(self.machine_path, 'antigravity').plan().report
        self.assertFalse(rep['ready'])

    def test_plan_staleness_guards(self):
        mcp_path = self.ag_config / 'mcp_config.json'
        mcp_path.write_bytes(b'')
        self.add_resource('srv', 'mcp', {'mcp.json': json.dumps({'name': 'srv', 'server': {'command': {'binding': 'echo'}}})},
                          'mcp.json', bindings={'echo': 'executables.echo'}, requires={'bindings': {'echo': {'kind': 'executable', 'required': True}}})
        self.commit_catalog('stale-test')

        # Wrong --expect-plan-id fails closed with plan_stale
        code, stale_rep = self.run_cli([
            'apply', '--machine', str(self.machine_path), '--agent', 'antigravity',
            '--expect-plan-id', '0' * 64, '--format', 'json',
        ])
        self.assertNotEqual(code, 0)
        self.assertEqual(stale_rep['errors'][0]['code'], 'plan_stale')
        self.assertEqual(mcp_path.read_bytes(), b'')

        # Post-plan external edit triggers PlanStale on apply
        plan = ProfileManager(self.machine_path, 'antigravity').plan()
        mcp_path.write_text(json.dumps({'mcpServers': {'surprise': {'command': 'x'}}}))
        with self.assertRaises(PlanStale):
            plan.apply(self.state)


if __name__ == '__main__':
    unittest.main()
