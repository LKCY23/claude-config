import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import yaml

spec = importlib.util.spec_from_file_location('agent_config', Path(__file__).resolve().parents[1] / 'scripts/agent_config.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class Host:
    def __init__(self):
        self.commands = []
        self.catalogs = {'claude': {}, 'codex': {}}
        self.plugins = {'claude': {}, 'codex': {}}

    def run(self, command):
        self.commands.append(command)
        host = command[0]
        if command[1:4] == ['plugin', 'marketplace', 'list']:
            data = [{'name': name, 'installLocation' if host == 'claude' else 'root': root}
                    for name, root in self.catalogs[host].items()]
            return json.dumps(data if host == 'claude' else {'marketplaces': data})
        if command[1:4] == ['plugin', 'marketplace', 'add']:
            root = command[4]
            catalog = json.loads((Path(root) / '.claude-plugin/marketplace.json').read_text())
            self.catalogs[host][catalog['name']] = root
        elif command[1:4] == ['plugin', 'marketplace', 'remove']:
            self.catalogs[host].pop(command[4], None)
        elif command[1:3] == ['plugin', 'list']:
            values = list(self.plugins[host].values())
            return json.dumps(values if host == 'claude' else {'installed': values})
        elif command[1:3] in (['plugin', 'install'], ['plugin', 'add']):
            selector = command[3]
            plugin, catalog = selector.split('@')
            root = Path(self.catalogs[host][catalog])
            entries = json.loads((root / '.claude-plugin/marketplace.json').read_text())['plugins']
            source = root / next(p['source'] for p in entries if p['name'] == plugin)
            self.plugins[host][selector] = {'id' if host == 'claude' else 'pluginId': selector,
                                          'enabled': True, 'version': '1.0.0', 'installPath': str(source)}
        elif command[1:3] in (['plugin', 'remove'], ['plugin', 'uninstall']):
            self.plugins[host].pop(command[3], None)
        elif command[1:3] in (['plugin', 'enable'], ['plugin', 'disable']):
            self.plugins[host][command[3]]['enabled'] = command[2] == 'enable'
        return '{}'


class DeploymentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.data, self.catalog, self.home = [self.root / p for p in ('data', 'catalog', 'home')]
        self.data.mkdir()
        (self.catalog / '.claude-plugin').mkdir(parents=True)
        (self.catalog / 'packages/picked').mkdir(parents=True)
        (self.catalog / 'packages/unselected').mkdir(parents=True)
        for name in ['picked', 'unselected']:
            for host in ['claude', 'codex']:
                p = self.catalog / 'packages' / name / f'.{host}-plugin/plugin.json'
                p.parent.mkdir(parents=True)
                p.write_text(json.dumps({'name': name, 'version': '1.0.0'}))
        (self.catalog / '.claude-plugin/marketplace.json').write_text(json.dumps({
            'name': 'candidates', 'plugins': [
                {'name': 'picked', 'source': './packages/picked'},
                {'name': 'unselected', 'source': './packages/unselected'}]}))
        self.declaration = {'version': 1, 'catalogs': {'candidates': {'repo': 'owner/repo', 'path': '.'}},
                            'agents': {h: {'plugins': ['picked@candidates']} for h in module.HOSTS}}
        self.machine = {'catalog_roots': {'owner/repo': str(self.catalog)}, 'backup_dir': str(self.root / 'backups')}
        self.host = Host()
        self.environment = patch.dict(os.environ, {'CODEX_HOME': '', 'CLAUDE_CONFIG_DIR': ''})
        self.environment.start()
        self.save()

    def tearDown(self):
        self.environment.stop()
        self.temp.cleanup()

    def save(self):
        (self.data / 'agents.yaml').write_text(yaml.safe_dump(self.declaration))

    def manager(self, host='both'):
        return module.Manager(self.data, host, self.machine, self.host.run, self.home)

    def codex_overlay(self, text):
        (self.data / 'config.toml').write_text(text)
        self.declaration['agents']['codex']['config'] = {'source': 'config.toml'}
        self.save()
        target = self.home / '.codex/config.toml'
        target.parent.mkdir(parents=True, exist_ok=True)
        return target

    def test_only_explicit_selection_installs_and_second_apply_succeeds(self):
        self.manager().apply()
        self.manager().apply()
        for host in module.HOSTS:
            self.assertEqual(set(self.host.plugins[host]), {'picked@candidates'})
        self.assertNotIn('unselected', json.dumps(self.host.commands))

    def test_host_selection_does_not_mutate_other_host(self):
        self.manager('codex').apply()
        self.assertEqual(self.host.plugins['claude'], {})
        self.assertEqual({c[0] for c in self.host.commands}, {'codex'})

    def test_plan_is_read_only(self):
        target = self.codex_overlay('model = "selected"\n')
        target.write_text('model = "existing"\n')
        before = target.read_bytes()
        self.assertTrue(self.manager('codex').plan()[0]['config'][0]['changed'])
        self.assertEqual(target.read_bytes(), before)
        self.assertEqual(self.host.commands, [])
        self.assertFalse((self.root / 'backups').exists())

    def test_toml_preserves_comments_credentials_and_unmanaged_tables(self):
        target = self.codex_overlay('model_reasoning_effort = "high"\n')
        original = '# private host config\nmodel = "existing" # keep\n[model_providers.local]\nbase_url = "http://localhost:1234"\napi_key = "LOCAL-ONLY"\n'
        target.write_text(original)
        self.manager('codex').apply()
        result = target.read_text()
        self.assertIn('# private host config', result)
        self.assertIn('model = "existing" # keep', result)
        self.assertIn('api_key = "LOCAL-ONLY"', result)
        self.assertIn('model_reasoning_effort = "high"', result)
        backups = list((self.root / 'backups').iterdir())
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(), original)
        self.assertEqual(backups[0].stat().st_mode & 0o777, 0o600)

    def test_empty_overlay_keeps_runtime_bytes_identical(self):
        target = self.codex_overlay('# nothing selected\n')
        target.write_text('# comment\nmodel="existing"\n')
        original = target.read_bytes()
        self.manager('codex').apply()
        self.assertEqual(target.read_bytes(), original)
        self.assertFalse((self.root / 'backups').exists())

    def test_secret_overlay_fails_before_any_host_is_mutated(self):
        self.codex_overlay('[mcp_servers.tool.env]\nMY_API_KEY = "do-not-copy"\n')
        with self.assertRaisesRegex(ValueError, 'Secret value'):
            self.manager().apply()
        self.assertEqual(self.host.commands, [])

    def test_bad_second_host_selection_fails_before_first_host_install(self):
        self.declaration['agents']['codex']['plugins'] = ['missing@candidates']
        self.save()
        with self.assertRaisesRegex(ValueError, 'not registered'):
            self.manager().apply()
        self.assertEqual(self.host.commands, [])

    def test_source_paths_cannot_escape_data(self):
        self.declaration['agents']['codex']['config'] = {'source': '../private.toml'}
        self.save()
        with self.assertRaisesRegex(ValueError, 'escapes'):
            self.manager('codex').plan()

    def test_existing_instructions_and_repeated_apply_are_preserved(self):
        self.declaration['agents']['codex']['instructions'] = {'source': 'instructions.md'}
        (self.data / 'instructions.md').write_text('Shared instructions\n')
        self.save()
        target = self.home / '.codex/AGENTS.md'
        target.parent.mkdir(parents=True)
        target.write_text('Existing user instructions\n')
        self.manager('codex').apply()
        original = target.read_text()
        self.manager('codex').apply()
        self.assertEqual(target.read_text(), original)
        self.assertTrue(original.startswith('Existing user instructions'))
        self.assertEqual(original.count('claude-config:begin'), 1)

    def test_symlinked_runtime_file_is_not_replaced(self):
        target = self.codex_overlay('model="new"\n')
        other = self.root / 'other.toml'
        other.write_text('model="old"\n')
        target.symlink_to(other)
        with self.assertRaisesRegex(ValueError, 'symlinked'):
            self.manager('codex').apply()
        self.assertEqual(other.read_text(), 'model="old"\n')

    def test_different_marketplace_source_is_a_reported_conflict(self):
        self.host.catalogs['codex']['candidates'] = str(self.root / 'old-source')
        with self.assertRaisesRegex(ValueError, 'points elsewhere'):
            self.manager('codex').apply()
        self.assertEqual(self.host.plugins['codex'], {})

    def test_remote_sources_require_commit_pins(self):
        path = self.catalog / '.claude-plugin/marketplace.json'
        catalog = json.loads(path.read_text())
        catalog['plugins'][0]['source'] = {'source': 'git-subdir', 'url': 'https://github.com/owner/repo', 'path': '.'}
        path.write_text(json.dumps(catalog))
        with self.assertRaisesRegex(ValueError, 'exact commit pin'):
            self.manager().plan()

    def test_marketplace_conflict_preflights_before_config_write(self):
        target = self.codex_overlay('model="new"\n'); target.write_text('model="old"\n')
        self.host.catalogs['codex']['candidates'] = str(self.root / 'old')
        with self.assertRaisesRegex(ValueError, 'points elsewhere'):
            self.manager().apply()
        self.assertEqual(target.read_text(), 'model="old"\n')
        self.assertEqual(self.host.plugins['claude'], {})

    def test_rollback_restores_managed_key_and_preserves_unrelated_later_change(self):
        target = self.codex_overlay('model="new"\n'); target.write_text('model="old"\n')
        result = self.manager('codex').apply()
        target.write_text(target.read_text() + 'unrelated="later"\n')
        self.manager('codex').rollback(result['run_id'])
        self.assertIn('model="old"', target.read_text().replace(' ', ''))
        self.assertIn('unrelated="later"', target.read_text().replace(' ', ''))
        self.assertEqual(self.host.plugins['codex'], {})
        self.assertEqual(self.host.catalogs['codex'], {})

    def test_rollback_refuses_to_overwrite_new_user_value(self):
        target = self.codex_overlay('model="new"\n'); target.write_text('model="old"\n')
        result = self.manager('codex').apply(); target.write_text('model="user-change"\n')
        with self.assertRaisesRegex(ValueError, 'Managed key changed'):
            self.manager('codex').rollback(result['run_id'])
        self.assertEqual(target.read_text(), 'model="user-change"\n')

    def test_failure_is_recorded_and_retry_skips_completed_installs(self):
        real = self.host.run
        once = [True]
        def fail(command):
            if command[:3] == ['codex', 'plugin', 'add'] and once[0]:
                once[0] = False
                raise RuntimeError('simulated failure')
            return real(command)
        self.host.run = fail
        with self.assertRaisesRegex(RuntimeError, 'Partial apply recorded'):
            self.manager().apply()
        journals = list((self.root / 'state/runs').glob('*.json'))
        self.assertEqual(json.loads(journals[0].read_text())['status'], 'failed')
        self.manager().apply()
        installs = [c for c in self.host.commands if c[:3] == ['claude', 'plugin', 'install']]
        self.assertEqual(len(installs), 1)

    def test_permission_merge_keeps_existing_rules(self):
        target = self.home / '.claude/settings.json';target.parent.mkdir(parents=True)
        target.write_text(json.dumps({'permissions': {'allow': ['Read'], 'deny': ['Bash(rm *)']}, 'unmanaged': 42}))
        (self.data / 'settings.json').write_text(json.dumps({'permissions': {'allow': ['Read', 'Glob']}}))
        self.declaration['agents']['claude']['config'] = {'source': 'settings.json'};self.save()
        result = self.manager('claude').apply()
        value = json.loads(target.read_text())
        self.assertEqual(value['permissions']['allow'], ['Read', 'Glob'])
        self.assertEqual(value['permissions']['deny'], ['Bash(rm *)'])
        self.manager('claude').rollback(result['run_id'])
        self.assertEqual(json.loads(target.read_text())['permissions']['allow'], ['Read'])

    def test_codex_skill_overrides_merge_by_path(self):
        result = module.merge_toml('[[skills.config]]\npath="a"\nenabled=false\n[[skills.config]]\npath="b"\nenabled=true\n', {'skills': {'config': [{'path': 'a', 'enabled': True}]}})
        value = module.tomlkit.parse(result).unwrap()['skills']['config']
        self.assertEqual(value, [{'path':'a','enabled':True},{'path':'b','enabled':True}])

    def test_native_agent_file_validation_and_rollback(self):
        (self.data/'reviewer.toml').write_text('name="reviewer"\ndescription="Review"\ndeveloper_instructions="Report only"\n')
        self.declaration['agents']['codex']['files']=[{'source':'reviewer.toml','target':'agents/reviewer.toml'}];self.save()
        result=self.manager('codex').apply()
        target=self.home/'.codex/agents/reviewer.toml'
        self.assertTrue(target.exists())
        self.manager('codex').rollback(result['run_id']);self.assertFalse(target.exists())

    def test_declared_file_cannot_target_auth(self):
        (self.data/'value.json').write_text('{}')
        self.declaration['agents']['codex']['files']=[{'source':'value.json','target':'auth.json'}];self.save()
        with self.assertRaisesRegex(ValueError, 'Only explicitly'):
            self.manager().preflight()

    def test_instruction_rollback_preserves_unrelated_new_text(self):
        (self.data/'instructions.md').write_text('Shared rules\n')
        self.declaration['agents']['codex']['instructions']={'source':'instructions.md'};self.save()
        target=self.home/'.codex/AGENTS.md';target.parent.mkdir(parents=True);target.write_text('User rules\n')
        result=self.manager('codex').apply()
        target.write_text(target.read_text()+'\nLater user rules\n')
        self.manager('codex').rollback(result['run_id'])
        self.assertIn('User rules',target.read_text());self.assertIn('Later user rules',target.read_text())
        self.assertNotIn('Shared rules',target.read_text())

    def test_existing_version_drift_is_rejected_before_config_write(self):
        self.manager('codex').apply()
        self.host.plugins['codex']['picked@candidates']['version']='another-version'
        target=self.codex_overlay('model="new"\n');target.write_text('model="old"\n')
        with self.assertRaisesRegex(ValueError,'version drift'):self.manager('codex').apply()
        self.assertEqual(target.read_text(),'model="old"\n')

    def test_mcp_registration_retry_and_scoped_rollback(self):
        self.declaration['agents']['codex']['mcp_servers']={'fixture':{'command':'fixture-command','args':['serve']}}
        self.save(); real=self.host.run; servers={};calls=[]
        def runner(command):
            if command[1]=='mcp':
                calls.append(command)
                if command[2]=='get':
                    if command[3] not in servers: raise RuntimeError('No MCP server named fixture')
                    return json.dumps({'transport':servers[command[3]]})
                if command[2]=='add': servers[command[3]]={'command':command[5],'args':command[6:]}
                if command[2]=='remove': servers.pop(command[3])
                return '{}'
            return real(command)
        self.host.run=runner
        result=self.manager('codex').apply();self.manager('codex').apply()
        self.assertEqual(sum(c[2]=='add' for c in calls),1)
        self.manager('codex').rollback(result['run_id']);self.assertEqual(servers,{})

    def test_mcp_rejects_credentials_before_plugin_install(self):
        self.declaration['agents']['codex']['mcp_servers']={'fixture':{'url':'https://example.test','bearer_token':'SECRET'}}
        self.save()
        with self.assertRaisesRegex(ValueError,'credential variable'):self.manager().apply()
        self.assertEqual(self.host.commands,[])

    def test_unmanaged_mcp_is_not_adopted_or_overwritten(self):
        self.declaration['agents']['codex']['mcp_servers']={'fixture':{'command':'fixture-command'}}
        self.save(); real=self.host.run
        self.host.run=lambda c: json.dumps({'transport':{'command':'other'}}) if c[1]=='mcp' else real(c)
        with self.assertRaisesRegex(ValueError,'unmanaged'):self.manager().apply()
        self.assertEqual(self.host.plugins['claude'],{})

    def test_codex_hooks_merge_and_rollback_preserve_unmanaged_events(self):
        old={'hooks':{'Stop':[{'hooks':[{'type':'command','command':'existing'}]}]}}
        new={'hooks':{'SessionStart':[{'hooks':[{'type':'command','command':'selected'}]}]}}
        target=self.home/'.codex/hooks.json';target.parent.mkdir(parents=True);target.write_text(json.dumps(old))
        (self.data/'hooks.json').write_text(json.dumps(new))
        self.declaration['agents']['codex']['files']=[{'source':'hooks.json','target':'hooks.json'}];self.save()
        result=self.manager('codex').apply()
        self.assertIn('Stop',json.loads(target.read_text())['hooks'])
        self.manager('codex').rollback(result['run_id'])
        self.assertNotIn('SessionStart',json.loads(target.read_text())['hooks'])
        self.assertEqual(json.loads(target.read_text()),old)

    def test_invalid_hook_event_fails_before_writes(self):
        self.codex_overlay('[[hooks.NotAnEvent]]\n[[hooks.NotAnEvent.hooks]]\ntype="command"\ncommand="example"\n')
        with self.assertRaisesRegex(ValueError,'Unsupported native hook event'):self.manager().apply()
        self.assertEqual(self.host.commands,[])

    def test_symlinked_parent_is_refused(self):
        root=self.home/'.codex';root.mkdir(parents=True)
        other=self.root/'external';other.mkdir();(root/'agents').symlink_to(other)
        (self.data/'role.toml').write_text('name="role"\ndescription="Role"\ndeveloper_instructions="Review"\n')
        self.declaration['agents']['codex']['files']=[{'source':'role.toml','target':'agents/role.toml'}];self.save()
        with self.assertRaises(ValueError):self.manager().plan()
        self.assertFalse((other/'role.toml').exists())


if __name__ == '__main__':
    unittest.main()
