"""Same-page retry with real durable quotas and authored upstream responses."""
import copy
from dataclasses import replace
import hashlib
import json
import unittest
from unittest.mock import patch

from vibe_job_radar.collection import Collector
from vibe_job_radar.collection_runtime import CollectionRunner
from vibe_job_radar.network import FetchError
from vibe_job_radar.public_category import URL, get_category
from vibe_job_radar.public_category_next import HARD_STOP
from vibe_job_radar.workspace import InputError
import test_collection_shared_rate as shared_tests
from test_public_category import data, card, job_url
from test_public_category_pages import PageWire, page_html


class FailedPageWire(PageWire):
    def __init__(self,url=URL,code='network_error'):
        super().__init__();self.failed_url=url;self.code=code

    def public_get(self,url):
        if url==self.failed_url:
            self.calls.append(url);raise FetchError(self.code,'Authored transport failure')
        return super().public_get(url)


class PageRetryTests(unittest.TestCase):
    setUp=shared_tests.CollectionSharedRateTests.setUp
    finish=shared_tests.CollectionSharedRateTests.finish

    def failed(self,**changes):
        return self.finish(self.collector.start(data(**changes)),FailedPageWire())

    def retry(self,parent):
        plan=self.collector.category_page_retry_preview({'id':parent['id']})
        saved=self.collector.category_page_retry_start(dict(id=parent['id'],fingerprint=plan['fingerprint'],consent=True))
        return plan,saved

    def hashes(self,report):
        return {p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in (self.workspace.root/'reports'/report).iterdir() if p.is_file()}

    def test_first_page_offline_save_restart_and_report_preserve_original_failure(self):
        parent=self.failed(detail_budget=2);raw=self.collector._path(parent['id']).read_bytes();before=self.ledger.summary('liepin')
        with patch('vibe_job_radar.collection.SiteFetcher',side_effect=AssertionError('offline')):
            plan,saved=self.retry(parent)
        self.assertEqual((plan['page'],plan['url'],plan['selection_limit'],plan['generation']),(1,URL,2,1))
        self.assertEqual(plan['external_network_requests'],0);self.assertFalse(plan['task_created'])
        self.assertEqual(self.ledger.summary('liepin'),before)
        child=saved['task'];self.assertTrue(saved['created']);self.assertEqual(child['status'],'paused')
        self.assertEqual((child['category_attempts'],child['detail_attempts'],child['details']),(0,0,[]))
        self.collector=Collector(self.workspace);wire=PageWire();done=self.finish(child,wire)
        self.assertEqual(done['status'],'completed');self.assertEqual(done['saved_detail_count'],2)
        self.assertEqual(wire.calls,['https://www.liepin.com/robots.txt',URL,job_url(1),job_url(2)])
        self.assertEqual(self.collector._path(parent['id']).read_bytes(),raw)
        self.assertIn('第1次显式同页重试',done['user_summary'])
        report=json.loads((self.workspace.root/'reports'/done['report_id']/'run_manifest.json').read_text(encoding='utf-8'))
        self.assertEqual(report['stats']['full_text_job_groups'],2)
        self.assertEqual(self.ledger.summary('liepin')['page']['day'],before['page']['day']+3)

    def test_adjacent_page_retries_only_observed_page_and_preserves_prior_report(self):
        first=self.finish(self.collector.start(data(detail_budget=2)),PageWire());old=self.hashes(first['report_id'])
        plan=self.collector.category_page_preview({'id':first['id']})
        second=self.collector.category_page_start(dict(id=first['id'],fingerprint=plan['fingerprint'],consent=True))['task']
        failed=self.finish(second,FailedPageWire(URL+'pn1/'));raw=self.collector._path(failed['id']).read_bytes()
        plan,saved=self.retry(failed);self.assertEqual(plan['page'],2);self.assertEqual(plan['url'],URL+'pn1/')
        self.assertEqual(saved['task']['category_page_context'],failed['category_page_context'])
        wire=PageWire();done=self.finish(saved['task'],wire)
        self.assertEqual(done['status'],'completed');self.assertNotIn(URL,wire.calls)
        self.assertEqual(done['category_outcomes'][0]['page_snapshot']['page'],1)
        self.assertEqual(self.hashes(first['report_id']),old);self.assertEqual(self.collector._path(failed['id']).read_bytes(),raw)
        next_plan=self.collector.category_page_preview({'id':done['id']})
        self.assertEqual(next_plan['next_page'],3)
        next_task=self.collector.category_page_start(dict(id=done['id'],fingerprint=next_plan['fingerprint'],consent=True))['task']
        self.assertNotIn('category_page_retry',next_task)

    def test_duplicate_confirmation_after_restart_completion_and_atomic_save(self):
        parent=self.failed();plan=self.collector.category_page_retry_preview({'id':parent['id']})
        args=dict(id=parent['id'],fingerprint=plan['fingerprint'],consent=True);save=self.collector._save
        def interrupted(state):save(state);raise KeyboardInterrupt('after atomic save')
        with patch.object(self.collector,'_save',side_effect=interrupted),self.assertRaises(KeyboardInterrupt):
            self.collector.category_page_retry_start(args)
        self.collector=Collector(self.workspace);saved=self.collector.category_page_retry_start(args)
        self.assertFalse(saved['created']);done=self.finish(saved['task'],PageWire());before=self.ledger.summary('liepin')
        again=self.collector.category_page_retry_start(args)
        self.assertFalse(again['created']);self.assertEqual(again['task']['report_id'],done['report_id'])
        self.assertEqual(self.ledger.summary('liepin'),before);self.assertEqual(len(self.collector.list()['runs']),2)

    def test_three_explicit_generations_stop_without_automatic_retries(self):
        parent=self.failed();initial=self.collector._path(parent['id']).read_bytes()
        for generation in range(1,4):
            plan,saved=self.retry(parent);self.assertEqual(plan['generation'],generation)
            wire=FailedPageWire();parent=self.finish(saved['task'],wire)
            self.assertEqual(parent['status'],'needs_attention');self.assertEqual(wire.calls.count(URL),1)
        with self.assertRaises(InputError):self.retry(parent)
        self.assertEqual(len(self.collector.list()['runs']),4)
        self.assertIn(b'network_error',initial)

    def test_other_failures_cannot_use_transport_retry(self):
        parent=self.failed();raw=self.collector._load(parent['id']);before=self.ledger.summary('liepin')
        for code in HARD_STOP|{'http_500','category_structure_changed','pending','ok','unknown'}:
            with self.subTest(code=code):
                changed=copy.deepcopy(raw);changed['category_outcomes'][0]['status']=code;self.collector._save(changed)
                with self.assertRaises(InputError):self.retry(changed)
        self.assertEqual(self.ledger.summary('liepin'),before)

    def test_incomplete_or_expanded_failed_scope_is_rejected(self):
        parent=self.failed();raw=self.collector._load(parent['id'])
        changes=[lambda s:s.update(status='running'),lambda s:s.update(phase='category'),
            lambda s:s.update(report_id='a'*32),lambda s:s.update(details=[{}]),lambda s:s.update(detail_attempts=1),
            lambda s:s.update(category_attempts=0),lambda s:s.update(category_attempts=True),
            lambda s:s.update(detail_budget=6),lambda s:s.update(detail_budget=True),lambda s:s.update(permit_platforms=[]),
            lambda s:s.update(rights_note=''),lambda s:s.update(in_flight={'queue':'category_outcomes','index':0}),
            lambda s:s.update(blocked_hosts=['www.liepin.com']),lambda s:s.update(category_continuation={}),
            lambda s:s.update(category_rate_recovery={}),lambda s:s.update(roles=['domain_algorithm']),
            lambda s:s['category_outcomes'][0].update(candidates=[{}]),lambda s:s['category_outcomes'][0].update(page_snapshot={}),
            lambda s:s['category_outcomes'][0].update(raw_sha256='a'*64),lambda s:s.update(schema_version=True)]
        for change in changes:
            with self.subTest(change=change):
                changed=copy.deepcopy(raw);change(changed);self.collector._save(changed)
                with self.assertRaises(InputError):self.retry(changed)

    def test_changed_parent_fingerprint_or_retry_scope_blocks_before_network(self):
        parent=self.failed();_,saved=self.retry(parent);original=self.collector._load(saved['task']['id'])
        changes=[lambda s:s.update(detail_budget=1),lambda s:s.update(rights_note='different'),
            lambda s:s.update(fresh_hours=720),lambda s:s.update(permit_platforms=[]),
            lambda s:s['category_outcomes'][0].update(url=URL+'pn1/'),
            lambda s:s['category_page_retry'].update(generation=True),
            lambda s:s['category_page_retry'].update(parent_id=s['id']),
            lambda s:s['category_page_retry'].update(parent_fingerprint='0'*64)]
        changes.append(lambda s:s.update(category_page_retry=None))
        before=self.ledger.summary('liepin')
        for change in changes:
            changed=copy.deepcopy(original);change(changed);self.collector._save(changed)
            with self.subTest(change=change),self.assertRaises(InputError):self.collector.step({'id':changed['id']})
        self.collector._save(original);changed=self.collector._load(parent['id']);changed['rights_note']='updated';self.collector._save(changed)
        with self.assertRaises(InputError):self.collector.step({'id':original['id']})
        runner=CollectionRunner(self.collector);self.addCleanup(runner.close)
        with self.assertRaises(InputError):runner.start(dict(id=original['id'],consent=True))
        self.assertEqual(self.ledger.summary('liepin'),before)

    def test_shared_cooldown_blocks_new_retry_without_refunding_old_attempt(self):
        parent=self.failed();_,saved=self.retry(parent);self.ledger.cool('liepin',3600)
        before=self.ledger.summary('liepin');wire=PageWire();done=self.finish(saved['task'],wire)
        self.assertEqual(done['category_outcomes'][0]['status'],'cooldown');self.assertEqual(wire.calls,[])
        self.assertEqual(self.ledger.summary('liepin'),before)
        with self.assertRaises(InputError):self.retry(done)

    def test_new_list_continuation_has_its_own_origin_after_retry(self):
        parent=self.failed(detail_budget=1);_,saved=self.retry(parent);done=self.finish(saved['task'],PageWire())
        plan=self.collector.category_next_preview({'id':done['id']})
        task=self.collector.category_next_start(dict(id=done['id'],fingerprint=plan['fingerprint'],consent=True))['task']
        self.assertNotIn('category_page_retry',task)
        finished=self.finish(task,PageWire());self.assertEqual(finished['saved_detail_count'],1)
        self.assertEqual(finished['details'][0]['url'],job_url(2))

    def test_exact_confirmation_schema_and_stale_preview(self):
        parent=self.failed();plan=self.collector.category_page_retry_preview({'id':parent['id']})
        args=dict(id=parent['id'],fingerprint=plan['fingerprint'],consent=True)
        for payload in ({**args,'consent':False},{**args,'url':URL},{**args,'fingerprint':'0'*64},{'id':parent['id']}):
            with self.subTest(payload=payload),self.assertRaises(InputError):self.collector.category_page_retry_start(payload)
        self.assertEqual(len(self.collector.list()['runs']),1)

    def test_algorithm_first_page_uses_its_original_scope(self):
        category=get_category('algorithm');request={**data(category_id='algorithm'),'roles':list(category.roles)}
        parent=self.finish(self.collector.start(request),FailedPageWire(category.url));plan,saved=self.retry(parent)
        self.assertEqual(plan['url'],category.url);self.assertEqual(saved['task']['roles'],list(category.roles))
        self.assertEqual(saved['task']['category_id'],'algorithm')

    def test_existing_child_with_removed_origin_cannot_be_silently_reused(self):
        parent=self.failed();_,saved=self.retry(parent)
        child=self.collector._load(saved['task']['id']);child.pop('category_page_retry');self.collector._save(child)
        with self.assertRaises(InputError):self.retry(parent)

    def test_waiting_detail_recovery_after_page_retry_preserves_both_histories(self):
        parent=self.failed(detail_budget=3);_,saved=self.retry(parent)
        self.ledger.limits=replace(self.ledger.limits,pages_day=3)
        done=self.finish(saved['task'],PageWire())
        self.assertEqual([d['status'] for d in done['details']],['ok','daily_limit','host_stopped'])
        plan=self.collector.category_recovery_preview({'id':done['id']})
        recovery=self.collector.category_recovery_start(dict(id=done['id'],fingerprint=plan['fingerprint'],consent=True))['task']
        self.assertNotIn('category_page_retry',recovery);self.now+=86401
        finished=self.finish(recovery,PageWire());self.assertEqual(finished['saved_detail_count'],3)
        self.assertEqual(self.collector.status({'id':parent['id']})['category_outcomes'][0]['status'],'network_error')

    def test_direct_step_rejects_details_inserted_before_a_new_list_is_read(self):
        parent=self.failed();_,saved=self.retry(parent)
        changed=self.collector._load(saved['task']['id'])
        changed['details']=[dict(url=job_url(999),platform='liepin',status='pending',record_id='')]
        self.collector._save(changed);before=self.ledger.summary('liepin')
        wire=PageWire()
        with self.assertRaises(InputError):self.finish(changed,wire)
        self.assertEqual(wire.calls,[]);self.assertEqual(self.ledger.summary('liepin'),before)
