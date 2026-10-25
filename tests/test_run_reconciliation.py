from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from unittest.mock import patch

from claude_config.core.journal import read_journal, unfinished
from claude_config.core.run_reconciliation import reconcile_plan
from claude_config.core.native_plugins import PluginOperation
from test_profile_plugins import FakeInstaller
from test_profile_resources import ResourceFixture


class RunReconciliationTests(ResourceFixture):
    def setUp(self):
        super().setUp()
        installer = FakeInstaller()
        patched = patch.object(PluginOperation, '_call', lambda op, args, work: installer(op, args, work))
        patched.start()
        self.addCleanup(patched.stop)

    def historical_failure(self):
        self.add('tiny', 'plugin', {'.claude-plugin/plugin.json': json.dumps({'name': 'tiny', 'version': '1.0.0'}),
                                   'body.txt': 'original'}, '.claude-plugin/plugin.json')
        self.save()
        deployed = self.manager().plan()
        deployed.apply(self.state)
        journal = read_journal(deployed.report['recovery'])
        native = deepcopy(next(row for row in journal['operations'] if row.get('kind') == 'native-plugin'))
        native['status'] = 'recovery_conflict'
        run = 'a' * 32
        directory = self.state / 'runs' / run
        directory.mkdir()
        old = {'schema_version': 1, 'kind': 'profile-file-run', 'plan_id': 'b' * 64,
               'created_at': '2000-01-01T00:00:00+00:00', 'status': 'failed',
               'scopes': [str(self.manager().native_path.resolve())], 'operations': [native]}
        path = directory / 'journal.json'
        path.write_text(json.dumps(old))
        return run, path, deployed.native_operations[0]

    def test_reconciliation_preserves_native_state_and_original_journal(self):
        run, path, operation = self.historical_failure()
        original, before = path.read_bytes(), operation.world()
        self.assertTrue(unfinished(self.state))
        plan = reconcile_plan(self.manager(), run)
        self.assertTrue(plan.report['ready'])
        self.assertFalse((self.state / 'run-reconciliations').exists())
        plan.apply(self.state)
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(operation.world(), before)
        self.assertFalse(unfinished(self.state))
        changed = json.loads(original)
        changed['error_type'] = 'changed evidence'
        path.write_text(json.dumps(changed))
        self.assertTrue(unfinished(self.state))

    def test_changed_current_image_cannot_be_reconciled(self):
        run, path, operation = self.historical_failure()
        receipt = operation.plugins[0]['desired']
        (operation.cache_parent(receipt) / receipt['version'] / 'body.txt').write_text('manual change')
        with self.assertRaises(ValueError):
            reconcile_plan(self.manager(), run)
        self.assertFalse((self.state / 'run-reconciliations').exists())

    def test_requires_successful_deployment_newer_than_failure(self):
        run, path, operation = self.historical_failure()
        old = json.loads(path.read_bytes())
        old['created_at'] = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
        path.write_text(json.dumps(old))
        with self.assertRaises(ValueError):
            reconcile_plan(self.manager(), run)

    def test_cli_requires_previewed_identity_for_reconciliation(self):
        from claude_config import profile
        run, path, operation = self.historical_failure()
        args = ['reconcile-run', '--machine', str(self.machine_path), '--agent', 'claude-code', '--run', run, '--format', 'json']
        with patch('builtins.print') as output:
            self.assertEqual(profile.main(args), 0)
            preview = json.loads(output.call_args.args[0])
            self.assertTrue(preview['preview_only'])
            self.assertEqual(profile.main([*args, '--write', '--expect-plan-id', preview['plan_id']]), 0)
        self.assertFalse(unfinished(self.state))
