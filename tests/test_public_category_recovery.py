"""Default factory plus real durable quotas; only upstream responses are artificial."""
import copy
from contextlib import closing
from dataclasses import replace
import hashlib
import json
import sqlite3
import unittest
from unittest.mock import patch

from vibe_job_radar.collection import Collector
from vibe_job_radar.public_category_next import HARD_STOP
from vibe_job_radar.public_category_recovery import WAITING
from vibe_job_radar.workspace import InputError
import test_collection_shared_rate as shared_tests
from test_public_category import Wire, data, job_url, listing, card
from test_public_category_pages import page_html
from vibe_job_radar.public_category import URL, get_category
from vibe_job_radar.network import Response


class CategoryRecoveryTests(unittest.TestCase):
    setUp = shared_tests.CollectionSharedRateTests.setUp
    finish = shared_tests.CollectionSharedRateTests.finish

    def parent(self):
        self.ledger.limits=replace(self.ledger.limits,pages_day=2)
        wire=Wire(listing(''.join(card(i) for i in range(1,8))))
        state=self.finish(self.collector.start(data(detail_budget=3)),wire)
        self.assertEqual([r['status'] for r in state['details']],['ok','daily_limit','host_stopped'])
        return state

    def child(self,parent):
        plan=self.collector.category_recovery_preview({'id':parent['id']})
        result=self.collector.category_recovery_start(dict(id=parent['id'],fingerprint=plan['fingerprint'],consent=True))
        return plan,result

    def hashes(self,report):
        return {p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in
                (self.workspace.root/'reports'/report).iterdir() if p.is_file()}

    def test_saved_remaining_selection_restart_and_report_preserve_success_and_parent(self):
        parent=self.parent();raw=self.collector._path(parent['id']).read_bytes();hashes=self.hashes(parent['report_id'])
        counts=self.ledger.summary('liepin')
        with patch('vibe_job_radar.collection.SiteFetcher',side_effect=AssertionError('offline')):
            plan,result=self.child(parent)
        self.assertEqual([row['position'] for row in plan['items']],[2,3])
        self.assertEqual(plan['selection_limit'],2);self.assertEqual(plan['inherited_success_count'],1)
        self.assertEqual(result['task']['status'],'paused');self.assertEqual(result['task']['detail_attempts'],0)
        self.assertEqual(self.ledger.summary('liepin'),counts)
        self.now+=86401;self.collector=Collector(self.workspace);wire=Wire()
        child=self.finish(result['task'],wire)
        self.assertEqual(child['status'],'completed');self.assertEqual(child['detail_attempts'],2)
        self.assertEqual(child['category_attempts'],0);self.assertEqual(child['saved_detail_count'],3)
        self.assertEqual(child['details'][0],parent['details'][0])
        self.assertEqual(wire.calls,['https://www.liepin.com/robots.txt',job_url(2),job_url(3)])
        self.assertEqual(self.collector._path(parent['id']).read_bytes(),raw)
        self.assertEqual(self.hashes(parent['report_id']),hashes)
        report=json.loads((self.workspace.root/'reports'/child['report_id']/'run_manifest.json').read_text(encoding='utf-8'))
        self.assertEqual(report['stats']['full_text_job_groups'],3)
        self.assertIn('其中1条正文继承',child['user_summary'])

    def test_unexpired_wait_and_cooldown_do_not_reserve_or_request(self):
        parent=self.parent();_,saved=self.child(parent);before=self.ledger.summary('liepin');wire=Wire()
        self.ledger.cool('liepin',3600)
        child=self.finish(saved['task'],wire)
        # The daily window is later than this shorter cooldown; keep the stricter reason.
        self.assertEqual([r['status'] for r in child['details']],['ok','daily_limit','host_stopped'])
        self.assertEqual(wire.calls,[]);self.assertEqual(self.ledger.summary('liepin'),before)
        self.assertEqual(child['details'][1]['fetch_diagnostic']['http_attempts'],0)
        self.ledger.cool('liepin',90000)
        _,again=self.child(child);cooled=self.finish(again['task'],wire)
        self.assertEqual(cooled['details'][1]['status'],'cooldown')
        self.assertEqual(wire.calls,[]);self.assertEqual(self.ledger.summary('liepin'),before)

    def test_repeated_confirmation_after_completion_returns_same_child(self):
        parent=self.parent();plan,saved=self.child(parent);self.now+=86401
        finished=self.finish(saved['task'],Wire())
        self.collector=Collector(self.workspace)
        with patch('vibe_job_radar.collection.SiteFetcher',side_effect=AssertionError('no replay')):
            again=self.collector.category_recovery_start(dict(id=parent['id'],fingerprint=plan['fingerprint'],consent=True))
        self.assertFalse(again['created']);self.assertEqual(again['task']['id'],finished['id'])
        self.assertEqual(again['task']['report_id'],finished['report_id'])
        self.assertEqual(len(self.collector.list()['runs']),2)

    def test_crash_after_atomic_child_save_keeps_idempotency(self):
        parent=self.parent();plan=self.collector.category_recovery_preview({'id':parent['id']});save=self.collector._save
        def crash(state):
            save(state);raise KeyboardInterrupt('controlled after-save interruption')
        payload=dict(id=parent['id'],fingerprint=plan['fingerprint'],consent=True)
        with patch.object(self.collector,'_save',side_effect=crash):
            with self.assertRaises(KeyboardInterrupt):self.collector.category_recovery_start(payload)
        self.collector=Collector(self.workspace)
        self.assertFalse(self.collector.category_recovery_start(payload)['created'])
        self.assertEqual(len(self.collector.list()['runs']),2)

    def test_original_next_batch_and_page_still_reject_waiting_parent(self):
        parent=self.parent()
        for method in (self.collector.category_next_preview,self.collector.category_page_preview):
            with self.assertRaises(InputError):method({'id':parent['id']})

    def test_all_other_hard_stops_remain_ineligible(self):
        parent=self.parent();original=self.collector._load(parent['id'])
        for code in HARD_STOP-WAITING-{'host_stopped'}:
            with self.subTest(code=code):
                state=copy.deepcopy(original);state['details'][0]['status']=code;self.collector._save(state)
                with self.assertRaises(InputError):self.collector.category_recovery_preview({'id':parent['id']})

    def test_orphan_host_stop_missing_list_or_wrong_parser_cannot_recover(self):
        parent=self.parent();original=self.collector._load(parent['id'])
        changes=[lambda s:s['details'][1].update(status='host_stopped'),
                 lambda s:s['category_outcomes'][0].update(status='cooldown'),
                 lambda s:s['details'][1].update(detail_parser='unreviewed-v99'),
                 lambda s:s['details'][1].update(url=job_url(888)),
                 lambda s:s.update(schema_version=99),lambda s:s.update(permit_platforms=[]),
                 lambda s:s.update(blocked_hosts=['other.example'])]
        for change in changes:
            state=copy.deepcopy(original);change(state);self.collector._save(state)
            with self.assertRaises(InputError):self.collector.category_recovery_preview({'id':parent['id']})

    def test_confirmation_and_stale_parent_stop_before_creation(self):
        parent=self.parent();plan=self.collector.category_recovery_preview({'id':parent['id']})
        payload=dict(id=parent['id'],fingerprint=plan['fingerprint'],consent=True)
        for change in ({'consent':False},{'fingerprint':'0'*64},{'urls':job_url(4)}):
            with self.assertRaises(InputError):self.collector.category_recovery_start({**payload,**change})
        state=self.collector._load(parent['id']);state['rights_note']='Changed purpose';self.collector._save(state)
        with self.assertRaises(InputError):self.collector.category_recovery_start(payload)
        self.assertEqual(len(self.collector.list()['runs']),1)

    def test_parent_revocation_after_save_blocks_before_any_request(self):
        parent=self.parent();_,saved=self.child(parent)
        state=self.collector._load(parent['id']);state['permit_platforms']=[];self.collector._save(state)
        self.now+=86401
        with patch('vibe_job_radar.collection.SiteFetcher',side_effect=AssertionError('no request')):
            with self.assertRaises(InputError):self.collector.step({'id':saved['task']['id']})

    def test_child_scope_and_inherited_success_cannot_be_changed(self):
        parent=self.parent();_,saved=self.child(parent);original=self.collector._load(saved['task']['id'])
        changes=[lambda s:s.update(detail_budget=5),lambda s:s.update(phase='category'),
                 lambda s:s['details'][1].update(url=job_url(800)),lambda s:s['details'][0].update(status='pending'),
                 lambda s:s.update(permit_platforms=[]),lambda s:s['category_outcomes'][0].update(raw_sha256='0'*64)]
        for change in changes:
            state=copy.deepcopy(original);change(state);self.collector._save(state)
            with self.assertRaises(InputError):self.collector.step({'id':state['id']})

    def test_three_explicit_recoveries_are_bounded_and_ancestors_stay_immutable(self):
        parent=self.parent();original=self.collector._path(parent['id']).read_bytes()
        for generation in range(1,4):
            _,saved=self.child(parent);parent=self.finish(saved['task'],Wire())
            self.assertEqual(parent['category_rate_recovery']['generation'],generation)
        with self.assertRaises(InputError):self.collector.category_recovery_preview({'id':parent['id']})
        first=[s for s in self.collector.list()['runs'] if s['id'] not in {parent['id']}]
        self.assertEqual(len(first),3)
        self.assertIn(original,[p.read_bytes() for p in self.collector.root.glob('*.json')])

    def test_completed_recovery_can_continue_original_list_without_inheriting_execution_scope(self):
        parent=self.parent();_,saved=self.child(parent);self.now+=86401
        finished=self.finish(saved['task'],Wire());plan=self.collector.category_next_preview({'id':finished['id']})
        self.assertEqual([x['position'] for x in plan['items']],[4,5])
        next_batch=self.collector.category_next_start(dict(id=finished['id'],fingerprint=plan['fingerprint'],consent=True))
        self.assertNotIn('category_rate_recovery',next_batch['task'])

    def test_missing_or_modified_success_stops_before_network_and_never_recreates_store(self):
        parent=self.parent();_,saved=self.child(parent)
        with closing(sqlite3.connect(self.workspace.db)) as db:
            original=db.execute('SELECT body FROM records WHERE record_id=?',(parent['details'][0]['record_id'],)).fetchone()[0]
            body=json.loads(original);body['text']='Changed original body'
            db.execute('UPDATE records SET body=?',(json.dumps(body),))
            db.commit()
        with self.assertRaises(InputError):self.collector.step({'id':saved['task']['id']})
        with closing(sqlite3.connect(self.workspace.db)) as db:
            db.execute('DELETE FROM records');db.commit()
        with self.assertRaises(InputError):self.collector.category_recovery_preview({'id':parent['id']})
        moved=self.workspace.db.with_suffix('.missing-backup');self.workspace.db.rename(moved)
        try:
            with self.assertRaises(InputError):self.collector.category_recovery_preview({'id':parent['id']})
            self.assertFalse(self.workspace.db.exists())
        finally:moved.rename(self.workspace.db)

    def test_malformed_or_cyclic_recovery_context_and_missing_detail_are_rejected(self):
        parent=self.parent();_,saved=self.child(parent);original=self.collector._load(saved['task']['id'])
        changes=[lambda s:s['category_rate_recovery'].update(version=True),
                 lambda s:s['category_rate_recovery'].update(generation=4),
                 lambda s:s['category_rate_recovery'].update(parent_id=s['id']),
                 lambda s:s.update(details=[]),lambda s:s['details'].__setitem__(1,None)]
        for change in changes:
            state=copy.deepcopy(original);change(state);self.collector._save(state)
            with self.assertRaises(InputError):self.collector.step({'id':state['id']})

    def test_second_page_ancestry_is_checked_after_saving_recovery(self):
        first=self.finish(self.collector.start(data(detail_budget=3)),Wire(page_html(cards=''.join(card(i) for i in range(1,4)))))
        plan=self.collector.category_page_preview({'id':first['id']})
        second=self.collector.category_page_start(dict(id=first['id'],fingerprint=plan['fingerprint'],consent=True))['task']
        self.ledger.limits=replace(self.ledger.limits,pages_day=self.ledger.summary('liepin')['page']['day']+2)
        wire=Wire();upstream=wire.public_get
        def page_response(url):
            if url==URL+'pn1/':
                wire.calls.append(url);return Response(200,{'content-type':'text/html'},page_html(1).encode(),url)
            return upstream(url)
        wire.public_get=page_response
        second=self.finish(second,wire);self.assertEqual(second['details'][1]['status'],'daily_limit')
        _,saved=self.child(second)
        original=self.collector._load(first['id']);original['rights_note']='Changed ancestor purpose';self.collector._save(original)
        with self.assertRaises(InputError):self.collector.step({'id':saved['task']['id']})

    def test_algorithm_category_recovery_preserves_its_original_roles_and_list(self):
        category=get_category('algorithm');self.ledger.limits=replace(self.ledger.limits,pages_day=2)
        params=data(detail_budget=3);params.update(category_id=category.key,roles=list(category.roles))
        wire=Wire();original=wire.public_get
        def get(url):
            if url==category.url:
                wire.calls.append(url)
                return Response(200,{'content-type':'text/html'},page_html(category_id='algorithm',cards=''.join(card(i) for i in range(1,4))).encode(),url)
            return original(url)
        wire.public_get=get
        parent=self.finish(self.collector.start(params),wire);plan,saved=self.child(parent)
        self.assertEqual([x['position'] for x in plan['items']],[2,3])
        self.assertEqual(saved['task']['roles'],list(category.roles));self.assertEqual(saved['task']['category_id'],'algorithm')
        self.now+=86401;wire=Wire();finished=self.finish(saved['task'],wire)
        self.assertEqual(finished['status'],'completed');self.assertEqual(finished['category_attempts'],0)
        self.assertNotIn(category.url,wire.calls)


if __name__=='__main__':unittest.main()
