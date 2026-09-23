"""Artificial dated jobs test bookkeeping; none are real acceptance evidence."""
from contextlib import closing, redirect_stdout
from dataclasses import replace
import hashlib
import io
import json
from pathlib import Path
import runpy
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from vibe_job_radar.guided.adapters import builtins
from vibe_job_radar.guided.service import GuidedService
from vibe_job_radar.live_acceptance import AcceptanceLedger, ROOT_NAME
from vibe_job_radar.models import JobRecord
from vibe_job_radar.qualification import source_files
from vibe_job_radar.store import Store
from vibe_job_radar.utils import atomic_json
from vibe_job_radar.workspace import InputError, Workspace


class LiveAcceptanceTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup)
        self.workspace=Workspace(tmp.name)
        self.now=['2026-09-01T00:00:00+00:00']
        self.ledger=AcceptanceLedger(self.workspace.root,clock=lambda:self.now[0])
        self.data={'keyword':'时间序列算法','roles':['time_series'],'search_url':'','backend':'native',
            'selection_rule':'人工测试：保留本批所有选择与失败，不是市场采样计划', 'timezone':'UTC',
            'source_revision':'a'*40,'source_sha256':'b'*64,'browser':'msedge','browser_version':'fixture',
            'planner_os':'ARTIFICIAL TEST','network_mode':'system','consent':True}
        self.plan=self.ledger.create(self.data)
        self.service=GuidedService(self.workspace);self.addCleanup(self.service.close)
        self.adapter=builtins().get('liepin')

    def batch(self, day=1, count=1, *, start=1000, statuses=None, keyword=None, record_changes=None):
        self.now[0]=f'2026-09-{day:02}T12:00:00+00:00'
        with patch.object(self.service,'_submit'):
            result=self.service.create({'platform':'liepin','keyword':keyword or self.plan['keyword'],
                'roles':['time_series'],'max_pages':1,'max_jobs':20,'consent':True,
                'backend':'native','native_consent':True,'rights_note':'ARTIFICIAL OFFLINE TEST DATA ONLY'})
        state=self.service._load(result['id'])
        state.update(created_at=f'2026-09-{day:02}T10:00:00+00:00',status='completed',phase='report')
        state['cards']=[];state['selection']=[]
        for index,status in enumerate(statuses or ['ok']*count):
            ident=f'{start+index:024x}'
            url=f'https://www.liepin.com/job/{start+index}.shtml'
            title=f'时间序列算法工程师 {start+index}'
            row={'id':ident,'url':url,'source_url':state['search_url'],'resolved_url':'','title':title,
                 'status':status,'record_id':''}
            if status=='ok':
                record=JobRecord(title=title,text='人工兼容测试，并非真实招聘。要求使用 Cursor 辅助编程、代码审查和单元测试。',
                    company='ARTIFICIAL FIXTURE ONLY',url=url,platform='liepin',source_mode='browser_fetch',
                    source_ref=f"guided:{state['id']}:{ident}",rights_note=state['rights_note'],
                    collected_at=f'2026-09-{day:02}T11:00:00+00:00',parser='artificial_fixture_only',raw_sha256='c'*64)
                if record_changes:
                    record=replace(record,record_id='',**record_changes)
                with Store(self.workspace.db) as store:store.add(record)
                row.update(resolved_url=url,record_id=record.record_id,body_sha256=hashlib.sha256(record.text.encode()).hexdigest(),
                    parser=record.parser,adapter_version=self.adapter.version,platform_job_id=self.adapter.job_identity(url))
            state['cards'].append(row);state['selection'].append(ident)
        self.service._finalize_report(state,self.adapter)
        self.service._save(state,'completed',status='completed')
        return state

    def capture(self):
        return self.ledger.capture(self.plan['id'])

    def test_plan_and_empty_verify_are_offline_and_do_not_create_business_data(self):
        with patch('socket.create_connection',side_effect=AssertionError('offline')):
            result=self.ledger.verify(self.plan['id'])
            self.assertEqual(result['selected_items'],0)
            self.assertIn('no_observations',result['gaps'])
        self.assertFalse(self.workspace.db.exists())
        self.assertEqual(list((self.workspace.root/'reports').iterdir()),[])

    def test_records_all_selected_failures_denials_and_pending_items(self):
        self.batch(statuses=['ok','http_403','robots_denied','discovered'])
        result=self.capture()
        self.assertEqual(result['selected_items'],4)
        self.assertEqual(result['known_not_permitted_items'],1)
        self.assertEqual(result['selected_items_except_known_denials'],3)
        self.assertEqual(result['verified_full_target_items'],1)
        self.assertAlmostEqual(result['observed_completion_ratio'],1/3)
        self.assertEqual(result['statuses'],{'ok':1,'http_403':1,'robots_denied':1,'discovered':1})

    def test_30_unique_jobs_3_dates_do_not_auto_certify_or_approve_threshold(self):
        for day in (1,2,3):
            self.batch(day,count=10,start=day*1000);result=self.capture()
        self.assertEqual(result['unique_full_target_jobs'],30)
        self.assertEqual(result['local_dates'],['2026-09-01','2026-09-02','2026-09-03'])
        self.assertEqual(result['observed_completion_ratio'],1)
        self.assertEqual(result['certification'],'not_live_verified')
        self.assertFalse(result['default_backend_changed'])
        self.assertIn('runtime_environment',result['gaps'])
        self.assertIn('proposed_threshold_requires_user_review',result['gaps'])

    def test_repeat_job_across_dates_is_one_distinct_job(self):
        for day in (1,2,3):
            self.batch(day,start=1000);result=self.capture()
        self.assertEqual(result['unique_full_target_jobs'],1)
        self.assertEqual(result['verified_full_target_items'],3)
        self.assertEqual(len(result['local_dates']),3)

    def test_local_date_uses_registered_timezone_instead_of_utc_date(self):
        self.plan=self.ledger.create({**self.data,'timezone':'Pacific/Kiritimati'})
        self.batch(day=1)
        self.assertEqual(self.capture()['local_dates'],['2026-09-02'])

    def test_exact_preregistered_scope_excludes_earlier_or_other_queries(self):
        state=self.batch(keyword='架构师')
        result=self.capture()
        self.assertEqual(result['selected_items'],0)
        folder=self.workspace.root/ROOT_NAME/self.plan['id']
        capture=json.loads((folder/'capture-0001.json').read_text(encoding='utf-8'))
        self.assertEqual(capture['excluded_tasks'],[{'id':state['id'],'reason':'outside_registered_query'}])

    def test_repeated_capture_does_not_duplicate_denominator(self):
        self.batch(statuses=['ok','http_403'])
        self.capture(); result=self.capture()
        self.assertEqual(result['selected_items'],2)
        self.assertEqual(result['previous_observed_outcomes'],{})

    def test_removing_selection_later_does_not_remove_prior_failure(self):
        state=self.batch(statuses=['ok','http_403'])
        self.capture()
        state['selection']=[];state['report_id']=''
        self.service._save(state)
        result=self.capture()
        self.assertEqual(result['selected_items'],2)
        self.assertEqual(result['statuses']['http_403'],1)

    def test_changed_report_or_missing_database_observation_invalidates_evidence(self):
        state=self.batch();self.capture()
        path=self.workspace.root/'reports'/state['report_id']/'jobs.jsonl'
        previous=path.read_bytes();path.write_bytes(previous+b'\n')
        result=self.ledger.verify(self.plan['id'])
        self.assertEqual(result['verified_full_target_items'],0)
        self.assertIn('previously_observed_evidence_no_longer_valid',result['gaps'])
        path.write_bytes(previous)
        with closing(sqlite3.connect(self.workspace.db)) as db:
            db.execute('DELETE FROM observations');db.commit()
        self.assertEqual(self.ledger.verify(self.plan['id'])['verified_full_target_items'],0)

    def test_wrong_identity_or_body_hash_is_not_counted(self):
        state=self.batch();state['cards'][0]['body_sha256']='0'*64;self.service._save(state)
        result=self.capture()
        self.assertEqual(result['verified_full_target_items'],0)
        self.assertIn('invalid_item_evidence',result['gaps'])

    def test_manual_snippet_and_synthetic_materials_do_not_count_as_browser_full_jd(self):
        for index,changes in enumerate(({'source_mode':'manual'}, {'evidence_level':'snippet'},
                                       {'source_mode':'synthetic','is_synthetic':True})):
            self.batch(start=1000+index,record_changes=changes)
        result=self.capture()
        self.assertEqual(result['selected_items'],3)
        self.assertEqual(result['verified_full_target_items'],0)
        self.assertEqual(result['unique_full_target_jobs'],0)
        self.assertIn('invalid_item_evidence',result['gaps'])

    def test_capture_and_verify_leave_business_files_unchanged_and_do_not_connect(self):
        self.batch(statuses=['ok','http_403'])
        def business_files():
            return {p.relative_to(self.workspace.root).as_posix():hashlib.sha256(p.read_bytes()).hexdigest()
                    for p in self.workspace.root.rglob('*') if p.is_file()
                    and ROOT_NAME not in p.relative_to(self.workspace.root).parts
                    # SQLite may create/update WAL coordination sidecars even
                    # for mode=ro. Do not use immutable=1: it ignores live WAL.
                    and p.name not in {'jobs.sqlite-wal','jobs.sqlite-shm'}}
        before=business_files()
        with patch('socket.create_connection',side_effect=AssertionError('offline')):
            self.capture();self.ledger.verify(self.plan['id'])
        self.assertEqual(before,business_files())

    def test_cli_verification_returns_incomplete_without_business_writes(self):
        script=Path(__file__).resolve().parents[1]/'scripts'/'check_live_acceptance.py'
        output=io.StringIO()
        with patch('sys.argv',[str(script),'--workspace',str(self.workspace.root),'verify','--plan',self.plan['id']]), \
                patch('socket.create_connection',side_effect=AssertionError('offline')),redirect_stdout(output):
            with self.assertRaises(SystemExit) as result:
                runpy.run_path(str(script),run_name='__main__')
        self.assertEqual(result.exception.code,2)
        self.assertEqual(json.loads(output.getvalue())['certification'],'not_live_verified')
        self.assertFalse(self.workspace.db.exists())

    def test_future_collection_timestamp_is_rejected(self):
        state=self.batch(day=2)
        # Task is already created, but its immutable report records a future observation.
        state['created_at']='2026-09-01T01:00:00+00:00';self.service._save(state)
        self.now[0]='2026-09-01T12:00:00+00:00'
        result=self.capture()
        self.assertEqual(result['verified_full_target_items'],0)

    def test_active_task_is_not_captured_as_a_stable_denominator(self):
        state=self.batch();self.service._save(state,status='running')
        with self.assertRaises(InputError):self.capture()
        self.assertEqual(list((self.workspace.root/ROOT_NAME/self.plan['id']).glob('capture-*.json')),[])

    def test_missing_selected_card_fails_without_partial_snapshot(self):
        state=self.batch();state['selection'].append('missing');self.service._save(state)
        with self.assertRaises(InputError):self.capture()

    def test_modified_plan_or_removed_capture_tail_is_detected(self):
        self.batch();self.capture();self.capture()
        folder=self.workspace.root/ROOT_NAME/self.plan['id']
        (folder/'capture-0002.json').unlink()
        with self.assertRaises(InputError):self.ledger.verify(self.plan['id'])
        plan_path=folder/'plan.json'
        value=json.loads(plan_path.read_text(encoding='utf-8'));value['keyword']='changed'
        atomic_json(plan_path,value)
        with self.assertRaises(InputError):self.ledger.verify(self.plan['id'])

    def test_unknown_fields_invalid_query_and_role_types_are_rejected(self):
        for changes in ({'password':'DO-NOT-PERSIST'},{'consent':False},{'roles':[{}]},
                        {'search_url':'https://www.liepin.com/zhaopin/?key=other'}, {'timezone':'bad/zone'}):
            with self.subTest(changes=changes),self.assertRaises(InputError):
                self.ledger.create({**self.data,**changes})

    def test_acceptance_metadata_cannot_enter_source_candidate_payload(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder=Path(tmp)/'tests'/ROOT_NAME;folder.mkdir(parents=True)
            (folder/'plan.json').write_text('{}')
            with self.assertRaises(ValueError):source_files(Path(tmp))


if __name__=='__main__':
    unittest.main()
