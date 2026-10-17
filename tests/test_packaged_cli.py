"""Installed command/bootstrap behavior; all writes use private temporary roots."""
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from claude_config.cli import main
from claude_config.setup import Setup, doctor
from claude_config.core.recovery import rollback_plan
from claude_config.core.transaction import file_fingerprint, PlanStale, atomic_write
from claude_config.core.schema import SchemaError, validate_machine


class PackagedCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.target = self.root / 'tool/machine.yaml'
        self.args = SimpleNamespace(machine=self.target, from_machine=None, config_dir=self.root/'data',
                                    skill=[], retire_script_install=False)

    def test_help_version_do_not_import_business_or_write(self):
        out=io.StringIO()
        with redirect_stdout(out), patch('claude_config.profile.main', side_effect=AssertionError('business loaded')):
            self.assertEqual(main(['--version']),0)
            self.assertEqual(main(['--help']),0)
        from claude_config import __version__
        self.assertIn('claude-config ' + __version__,out.getvalue())
        self.assertFalse(self.target.exists())

    def test_init_preview_apply_repeat_and_rollback(self):
        manager=Setup(self.args); plan=manager.plan()
        self.assertFalse(self.target.exists())
        plan.apply(manager.state_root)
        self.assertTrue(self.target.is_file())
        self.assertFalse(Setup(self.args).plan().changes)
        rollback=rollback_plan(Setup(self.args),Path(plan.report['recovery']).parent.name)
        rollback.apply(manager.state_root)
        self.assertFalse(self.target.exists())

    def test_bootstrap_does_not_create_native_profiles_or_choose_clients(self):
        manager=Setup(self.args); manager.plan().apply(manager.state_root)
        value=yaml.safe_load(self.target.read_text())
        self.assertEqual(value['agents'],{})
        self.assertEqual(value['apps'],{})
        self.assertFalse((self.root/'data/profiles').exists())
        raw,_=doctor(self.target)
        self.assertTrue(raw['ready'])
        self.assertTrue(any(row['name']=='client_bindings' for row in raw['checks']))

    def legacy(self):
        self.target.parent.mkdir()
        value={'schema_version':1,'machine_id':'test','config_dir':str(self.root/'data'),
               'catalog_roots':{}, 'agents':{},'apps':{},'bindings':{},
               'runtime':{'python':'/old/python','framework_source':'/old/release','framework_commit':'a'*40}}
        self.target.write_text(yaml.safe_dump(value))
        code=self.target.parent/'scripts/profile_config.py'; code.parent.mkdir();code.write_text('old')
        receipt={'schema_version':1,'kind':'profile-framework-install','files':{
            str(self.target):file_fingerprint(self.target),str(code):file_fingerprint(code)}}
        path=self.target.parent/'.profile-framework-install.json';path.write_text(json.dumps(receipt))
        self.args.retire_script_install=True
        return code,path,value

    def test_legacy_fields_rejected_until_explicit_init_and_exact_files_retired(self):
        code,receipt,value=self.legacy()
        with self.assertRaises(SchemaError):validate_machine(value)
        untouched=self.target.parent/'private-note';untouched.write_text('retain')
        manager=Setup(self.args);plan=manager.plan();plan.apply(manager.state_root)
        self.assertNotIn('runtime',yaml.safe_load(self.target.read_text()))
        self.assertFalse(code.exists());self.assertFalse(receipt.exists())
        self.assertEqual(untouched.read_text(),'retain')
        rollback_plan(Setup(self.args),Path(plan.report['recovery']).parent.name).apply(manager.state_root)
        self.assertEqual(code.read_text(),'old')
        self.assertEqual(yaml.safe_load(self.target.read_text()),value)

    def test_changed_owned_file_blocks_retirement(self):
        code,_,_=self.legacy();code.write_text('user modification')
        with self.assertRaises(SchemaError):Setup(self.args).plan()
        self.assertEqual(code.read_text(),'user modification')

    def test_unknown_receipt_target_cannot_be_deleted(self):
        _,path,_=self.legacy()
        outside=self.root/'credentials';outside.write_text('retain')
        value=json.loads(path.read_text());value['files'][str(outside)]=file_fingerprint(outside);path.write_text(json.dumps(value))
        with self.assertRaises(SchemaError):Setup(self.args).plan()
        self.assertTrue(outside.exists())

    def test_drift_after_preview_preserved(self):
        manager=Setup(self.args);plan=manager.plan()
        self.target.parent.mkdir();self.target.write_text('later')
        with self.assertRaises(PlanStale):plan.apply(manager.state_root)
        self.assertEqual(self.target.read_text(),'later')

    def test_failure_restores_legacy_machine_and_code(self):
        code,receipt,value=self.legacy()
        manager=Setup(self.args);plan=manager.plan()
        def writer(path,content,mode):
            if path==self.target:raise OSError('injected')
            atomic_write(path,content,mode)
        with self.assertRaises(OSError):plan.apply(manager.state_root,writer=writer)
        self.assertEqual(yaml.safe_load(self.target.read_text()),value)
        self.assertTrue(receipt.exists());self.assertTrue(code.exists())

    def test_missing_machine_message_and_default_independent_of_cwd(self):
        out=io.StringIO()
        with patch.dict(os.environ,HOME=str(self.root)),redirect_stdout(out):
            code=main(['plan','--agent','codex','--format','json'])
        self.assertEqual(code,1)
        report=json.loads(out.getvalue())
        self.assertIn('init',json.dumps(report,ensure_ascii=False))
        self.assertFalse(self.target.exists())

    def test_skill_requires_existing_client_and_distinct_target(self):
        self.args.skill=['codex='+str(self.root/'skills/SKILL.md')]
        with self.assertRaises(SchemaError):Setup(self.args)

    def test_doctor_checks_local_executable_without_running_it(self):
        manager=Setup(self.args);plan=manager.plan();plan.apply(manager.state_root)
        value=yaml.safe_load(self.target.read_text());value['bindings']={'executables':{'node':'/nonexistent/node'}}
        self.target.write_text(yaml.safe_dump(value))
        result,_=doctor(self.target)
        self.assertFalse(result['ready'])


    def test_late_delete_failure_restores_all_prior_changes(self):
        code,receipt,value=self.legacy()
        manager=Setup(self.args);plan=manager.plan()
        original=Path.unlink
        def unlink(path,*args,**kwargs):
            if path==receipt:raise OSError('injected late delete failure')
            return original(path,*args,**kwargs)
        with patch.object(Path,'unlink',unlink),self.assertRaises(OSError):
            plan.apply(manager.state_root)
        self.assertEqual(yaml.safe_load(self.target.read_text()),value)
        self.assertEqual(code.read_text(),'old')
        self.assertTrue(receipt.exists())

    def test_init_lock_failure_is_not_reported_as_started(self):
        out=io.StringIO()
        with patch('claude_config.core.transaction.FilePlan.apply',side_effect=OSError('PRIVATE')),redirect_stdout(out):
            status=main(['init','--machine',str(self.target),'--config-dir',str(self.root/'data'),'--write','--format','json'])
        report=json.loads(out.getvalue())
        self.assertEqual(status,2)
        self.assertFalse(report['execution']['started'])
        self.assertEqual(report['execution']['status'],'failed')
        self.assertNotIn('PRIVATE',out.getvalue())
        self.assertEqual(report['targets'][0]['machine_file'],str(self.target))


    def test_edit_between_receipt_check_and_plan_is_preserved(self):
        code,receipt,value=self.legacy()
        original=file_fingerprint
        def changed_after_read(path):
            result=original(path)
            if path==code:
                code.write_text('concurrent user edit')
            return result
        with patch('claude_config.setup.file_fingerprint',side_effect=changed_after_read):
            with self.assertRaises((PlanStale,ValueError)):
                manager=Setup(self.args);manager.plan().apply(manager.state_root)
        self.assertEqual(code.read_text(),'concurrent user edit')
        self.assertTrue(receipt.exists())


    def test_equals_form_rollback_reports_recovery_operation(self):
        manager=Setup(self.args);plan=manager.plan();plan.apply(manager.state_root)
        run=Path(plan.report['recovery']).parent.name
        out=io.StringIO()
        with redirect_stdout(out):
            status=main(['init','--machine',str(self.target),'--run='+run,'--write','--format','json'])
        self.assertEqual(status,0)
        report=json.loads(out.getvalue())
        self.assertEqual(report['operation'],'init-rollback')
        self.assertFalse(self.target.exists())

if __name__=='__main__':unittest.main()
