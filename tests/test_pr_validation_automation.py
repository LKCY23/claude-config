import base64
from copy import deepcopy
import json
from pathlib import Path
import sys
import unittest
import yaml
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import pr_validation as m
from test_desktop_compatibility_watch import release


def pr():
    return {'number': 1, 'state': 'open', 'draft': False, 'changed_files': 1, 'auto_merge': None,
            'user': {'login': 'github-actions[bot]'},
            'base': {'ref': 'master', 'repo': {'full_name': 'owner/repo'}},
            'head': {'sha': 'a'*40, 'ref': 'compatibility/claude-desktop-3.6556.2', 'repo': {'full_name': 'owner/repo'}}}


def report():
    return {'release': release(), 'status': 'compatible', 'native_smoke': 'passed', 'checks': [{'status': 'passed'}]}


class Hub:
    repo = 'owner/repo'
    def __init__(self):
        self.pr = pr(); self.files = [{'filename': 'compatibility/claude-desktop/3.6556.2.json', 'status': 'added'}]
        self.report = report(); self.posts=[]; self.commands=[]
    def api(self, method, path, payload=None):
        if method == 'POST': self.posts.append((path,payload)); return None
        if path == '': return {'default_branch': 'master', 'allow_auto_merge': True}
        if '/files?' in path: return self.files
        if path.startswith('pulls/'): return self.pr
        if path.startswith('contents/'): return {'content':base64.b64encode(json.dumps(self.report).encode()).decode()}
        raise AssertionError(path)
    def command(self,*args): self.commands.append(args)


class AutomationTests(unittest.TestCase):
    def test_only_report_pass_can_request_auto_merge(self):
        h=Hub();self.assertIn('auto-merge-requested',m.finish(h,1,'a'*40,'success','success','url'))
        self.assertEqual(h.commands,[('merge','1','--auto','--squash','--match-head-commit','a'*40)])
        self.assertTrue(all(p['state']=='success' for _,p in h.posts))

    def test_unknown_empty_checks_wrong_archive_and_drafts_require_review(self):
        for edit in [lambda h:h.report.update(status='needs-review'),lambda h:h.report.update(native_smoke='not-run'),
                     lambda h:h.report.update(checks=[]),lambda h:h.report['release'].update(url='https://evil.invalid/app.zip'),
                     lambda h:h.pr.update(draft=True),lambda h:h.pr['user'].update(login='human')]:
            h=Hub();edit(h);m.finish(h,1,'a'*40,'success','success','url');self.assertFalse(h.commands)

    def test_code_changes_and_renames_cannot_be_report_only(self):
        for edits in [ [{'filename':'src/adapter.py','status':'modified'}],
                       [{'filename':'compatibility/claude-desktop/3.6556.2.json','status':'renamed','previous_filename':'another.json'}] ]:
            h=Hub();h.files=edits; m.finish(h,1,'a'*40,'success','skipped' if edits[0]['filename'].endswith('.py') else 'success','url');self.assertFalse(h.commands)
        h=Hub();h.files.append({'filename':'src/adapter.py','status':'modified'});h.pr['changed_files']=2
        m.finish(h,1,'a'*40,'success','success','url');self.assertFalse(h.commands)

    def test_superseded_forks_and_cancelled_tests_get_no_merge_credit(self):
        for edit in [lambda h:h.pr['head'].update(sha='b'*40),lambda h:h.pr['head'].update(repo={'full_name':'fork/repo'}),lambda h:h.pr.update(state='closed')]:
            h=Hub();edit(h);self.assertEqual(m.finish(h,1,'a'*40,'success','success','url'),'superseded');self.assertFalse(h.posts)
        for result in ['cancelled','skipped','failure']:
            h=Hub();m.finish(h,1,'a'*40,result,'success','url');self.assertFalse(h.commands);self.assertEqual(h.posts[0][1]['state'],'failure')
        h=Hub();m.finish(h,1,'a'*40,'success','skipped','url');self.assertFalse(h.commands);self.assertEqual(h.posts[1][1]['state'],'failure')

    def test_automation_consent_is_revoked_before_credit_for_changed_scope(self):
        h=Hub();h.pr['auto_merge']={'enabled_by':{'login':'github-actions[bot]'}};h.report['status']='needs-review'
        m.finish(h,1,'a'*40,'success','success','url');self.assertEqual(h.commands,[('merge','1','--disable-auto')])

    def test_files_are_complete_and_report_selection_is_unambiguous(self):
        h=Hub();h.pr['changed_files']=2
        with self.assertRaises(ValueError):m.changed_files(h,h.pr)
        with self.assertRaises(ValueError):m.report_version(h.files+[{'filename':'compatibility/claude-desktop/1.2.3.json'}])

    def test_workflow_privilege_separation_and_exact_checkout(self):
        root=Path(__file__).resolve().parents[1]/'.github/workflows'
        value=yaml.load((root/'pr-validation.yml').read_text(),Loader=yaml.BaseLoader)
        self.assertNotIn('pull_request_target',value['on'])
        for name in ['package','desktop']:
            self.assertEqual(value['jobs'][name]['permissions'],{'contents':'read'})
            self.assertNotIn('secrets',str(value['jobs'][name]))
            self.assertEqual(value['jobs'][name]['with']['head'],'${{ needs.select.outputs.head }}')
        for name in ['select','report']:
            self.assertEqual(value['jobs'][name]['steps'][0]['with']['ref'],'${{ github.sha }}')
        for name in ['package-tests.yml','desktop-compatibility.yml']:
            data=yaml.load((root/name).read_text(),Loader=yaml.BaseLoader)
            self.assertEqual(data['permissions'],{'contents':'read'})
            self.assertIn('enable-cache: false',(root/name).read_text())


if __name__=='__main__': unittest.main()
