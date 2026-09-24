"""Saved-list continuation through the original Collector; artificial responses."""
import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from vibe_job_radar.collection import Collector, TERMINAL
from vibe_job_radar.network import SiteFetcher
from vibe_job_radar.workspace import Workspace, InputError
from tests.test_public_category import Wire, card, data, listing, job_url, detail, BODY


class CategoryNextTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup)
        self.workspace=Workspace(tmp.name);self.collector=Collector(self.workspace)

    def finish(self,state,wire):
        self.collector.clients[(state['id'],'liepin')]=SiteFetcher({'liepin.com'},transport=wire)
        for _ in range(12):
            state=self.collector.step({'id':state['id']})
            if state['status'] in TERMINAL:return state
        self.fail('batch did not end')

    def first(self,*,count=12,wire=None):
        return self.finish(self.collector.start(data()),wire or Wire(listing(''.join(card(i) for i in range(1,count+1)))))

    def next(self,state):
        plan=self.collector.category_next_preview({'id':state['id']})
        return plan,self.collector.category_next_start(dict(id=state['id'],fingerprint=plan['fingerprint'],consent=True))

    def test_preview_and_next_batch_keep_source_report_and_do_not_refetch_list(self):
        parent=self.first()
        before=self.collector._path(parent['id']).read_bytes()
        report=self.workspace.root/'reports'/parent['report_id']
        hashes={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in report.iterdir() if p.is_file()}
        with patch('vibe_job_radar.collection.SiteFetcher',side_effect=AssertionError('preview/start must not fetch')):
            plan,result=self.next(parent)
        self.assertEqual([r['position'] for r in plan['items']],[6,7,8,9,10])
        self.assertEqual(plan['external_network_requests'],0)
        self.assertTrue(plan['source_time_known'])
        self.assertEqual(self.collector._path(parent['id']).read_bytes(),before)
        wire=Wire();child=self.finish(result['task'],wire)
        self.assertEqual(child['status'],'completed')
        self.assertEqual(child['category_attempts'],0)
        self.assertEqual(wire.calls,['https://www.liepin.com/robots.txt']+[job_url(i) for i in range(6,11)])
        current=json.loads((self.workspace.root/'reports'/child['report_id']/'run_manifest.json').read_text(encoding='utf-8'))
        self.assertEqual(current['stats']['full_text_job_groups'],5)
        self.assertEqual(hashes,{p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in report.iterdir() if p.is_file()})

    def test_short_final_batch_exhaustion_and_duplicate_clicks(self):
        parent=self.first()
        plan,first=self.next(parent)
        again=self.collector.category_next_start(dict(id=parent['id'],fingerprint=plan['fingerprint'],consent=True))
        self.assertFalse(again['created']);self.assertEqual(again['task']['id'],first['task']['id'])
        middle=self.finish(first['task'],Wire())
        last_plan,last=self.next(middle)
        self.assertEqual([r['position'] for r in last_plan['items']],[11,12])
        last=self.finish(last['task'],Wire())
        final=self.collector.category_next_preview({'id':last['id']})
        self.assertTrue(final['exhausted']);self.assertEqual(final['items'],[])
        with self.assertRaises(InputError):
            self.collector.category_next_start(dict(id=last['id'],fingerprint=final['fingerprint'],consent=True))
        self.assertEqual(len(self.collector.list()['runs']),3)

    def test_duplicates_and_invalid_cards_keep_original_positions_and_outcomes(self):
        # The repeated sixth card is not a new item; the invalid seventh card
        # still occupies one selected outcome, rather than being replaced.
        markup=listing(''.join(card(i) for i in range(1,6))+card(1)+card(7,href='https://example.invalid/job/7')+card(8)+card(8)+card(10))
        parent=self.first(wire=Wire(markup))
        plan,result=self.next(parent)
        self.assertEqual([r['position'] for r in plan['items']],[7,8,10])
        wire=Wire();child=self.finish(result['task'],wire)
        self.assertEqual(child['category_outcomes'][0]['selected_positions'],[7,8,10])
        self.assertEqual([d['status'] for d in child['details']],['category_invalid_card','ok','ok'])
        self.assertEqual(wire.calls,['https://www.liepin.com/robots.txt',job_url(8),job_url(10)])
        self.assertTrue(self.collector.category_next_preview({'id':child['id']})['exhausted'])

    def test_failed_selected_item_is_preserved_and_not_retried(self):
        wire=Wire(listing(''.join(card(i) for i in range(1,8))),details={job_url(2):detail(2,body=BODY+' 展开全部')})
        parent=self.first(wire=wire)
        self.assertEqual(parent['details'][1]['status'],'jd_incomplete')
        plan,next_batch=self.next(parent)
        self.assertEqual([r['position'] for r in plan['items']],[6,7])
        wire=Wire();child=self.finish(next_batch['task'],wire)
        self.assertEqual(child['status'],'completed')
        self.assertNotIn(job_url(2),wire.calls)
        self.assertEqual(self.collector.status({'id':parent['id']})['details'][1]['status'],'jd_incomplete')

    def test_refusal_or_uncertain_request_cannot_be_evaded_with_next_batch(self):
        for status in (403,429):
            with self.subTest(status=status):
                parent=self.first(wire=Wire(statuses={job_url(1):status}))
                with self.assertRaises(InputError):self.collector.category_next_preview({'id':parent['id']})
        parent=self.first()
        raw=self.collector._load(parent['id']);raw['details'][0]['status']='interrupted_uncertain';self.collector._save(raw)
        with self.assertRaises(InputError):self.collector.category_next_preview({'id':parent['id']})

    def test_confirmation_fields_and_stale_preview_do_not_create_task(self):
        parent=self.first();plan=self.collector.category_next_preview({'id':parent['id']})
        payload=dict(id=parent['id'],fingerprint=plan['fingerprint'],consent=True)
        for changes in ({'consent':False},{'urls':job_url(999)},{'fingerprint':'0'*64}):
            with self.assertRaises(InputError):self.collector.category_next_start({**payload,**changes})
        raw=self.collector._load(parent['id']);raw['warnings'].append('Artificial saved-task change');self.collector._save(raw)
        with self.assertRaises(InputError):self.collector.category_next_start(payload)
        self.assertEqual(len(self.collector.list()['runs']),1)

    def test_legacy_snapshot_time_and_known_a_link_upgrade_remain_explicit(self):
        markup=listing(''.join(card(i) for i in range(1,6))+card(6,href='https://www.liepin.com/a/6.shtml'))
        parent=self.first(wire=Wire(markup));raw=self.collector._load(parent['id'])
        row=raw['category_outcomes'][0];row.pop('capture_started_at');row.pop('capture_finished_at')
        row['candidates'][5]['status']='category_unsupported_detail';self.collector._save(raw)
        plan,child=self.next(parent)
        self.assertFalse(plan['source_time_known']);self.assertIsNone(plan['snapshot_observed_at'])
        self.assertEqual(child['task']['details'][0]['detail_parser'],'liepin_public_detail_v1')
        wire=Wire();state=self.finish(child['task'],wire)
        self.assertEqual(state['status'],'completed');self.assertEqual(state['category_attempts'],0)
        self.assertEqual(wire.calls[-1],'https://www.liepin.com/a/6.shtml')

    def test_invalid_saved_identity_order_and_unknown_card_are_rejected(self):
        parent=self.first();original=self.collector._load(parent['id'])
        changes=[lambda s:s['category_outcomes'][0].update(raw_sha256=''),
                 lambda s:s['category_outcomes'][0]['candidates'][5].update(url=job_url(6)+'?token=private'),
                 lambda s:s['category_outcomes'][0].update(selected_positions=[1,3,2,4,5]),
                 lambda s:s.update(id='f'*32),
                 lambda s:s['details'][0].update(url=job_url(999))]
        for change in changes:
            raw=copy.deepcopy(original);change(raw)
            # Keep the original filename when deliberately corrupting its id.
            self.collector._path(parent['id']).write_text(json.dumps(raw),encoding='utf-8')
            with self.assertRaises(InputError):self.collector.category_next_preview({'id':parent['id']})

    def test_crash_after_child_save_is_recovered_without_parent_rewrite(self):
        parent=self.first();plan=self.collector.category_next_preview({'id':parent['id']})
        before=self.collector._path(parent['id']).read_bytes();save=self.collector._save
        def interrupted(state):
            save(state)
            raise KeyboardInterrupt('Artificial crash after atomic child save')
        payload=dict(id=parent['id'],fingerprint=plan['fingerprint'],consent=True)
        with patch.object(self.collector,'_save',side_effect=interrupted):
            with self.assertRaises(KeyboardInterrupt):self.collector.category_next_start(payload)
        self.collector=Collector(self.workspace)
        result=self.collector.category_next_start(payload)
        self.assertFalse(result['created']);self.assertEqual(len(self.collector.list()['runs']),2)
        self.assertEqual(self.collector._path(parent['id']).read_bytes(),before)

    def test_two_processes_cannot_create_two_children_for_one_parent(self):
        parent=self.first();plan=self.collector.category_next_preview({'id':parent['id']})
        code='''import json,sys
from vibe_job_radar.collection import Collector
from vibe_job_radar.workspace import Workspace,InputError
try:
 c=Collector(Workspace(sys.argv[1]))
 r=c.category_next_start(dict(id=sys.argv[2],fingerprint=sys.argv[3],consent=True))
 print(json.dumps(dict(created=r['created'],id=r['task']['id'])))
except InputError:
 print(json.dumps(dict(busy=True)))
'''
        env={**os.environ,'PYTHONPATH':str(Path(__file__).resolve().parents[1]/'src')}
        args=[sys.executable,'-c',code,str(self.workspace.root),parent['id'],plan['fingerprint']]
        children=[subprocess.Popen(args,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,env=env) for _ in range(2)]
        results=[]
        try:
            for child in children:
                out,err=child.communicate(timeout=30)
                self.assertEqual(child.returncode,0,err);results.append(json.loads(out))
        finally:
            for child in children:
                if child.poll() is None:child.kill();child.wait(5)
        self.assertEqual(sum(row.get('created',False) for row in results),1)
        self.assertEqual(len(self.collector.list()['runs']),2)
        retry=self.collector.category_next_start(dict(id=parent['id'],fingerprint=plan['fingerprint'],consent=True))
        self.assertFalse(retry['created'])


if __name__=='__main__':unittest.main()
