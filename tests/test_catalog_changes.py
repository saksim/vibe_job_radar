"""Artificial complete catalogs test local changes, not real vacancies."""
import copy
import hashlib
import json
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from vibe_job_radar.catalog_changes import FIELDS, compare_catalogs, compact_change, validate_change
from vibe_job_radar.local_public import API_URL, SOURCE, LocalPublicDataClient, parse_board, revision
from vibe_job_radar.public_contract import ContractError
from vibe_job_radar.network import FetchError, SafeHTTP, JSONRepresentation
from vibe_job_radar.store import Store
from vibe_job_radar.utils import atomic_json
from vibe_job_radar.workspace import Workspace
import test_local_public as fixtures
import test_workbench as http_fixtures


def snapshot(data=None, now=1700000000):
    jobs = parse_board(fixtures.payload() if data is None else data, now)
    return {'api': API_URL, 'observed_at': now, 'revision': revision(jobs), 'jobs': jobs}


def changed_payload():
    data = fixtures.payload(4)
    data['jobs'].pop(1)  # Missing does NOT assert closure.
    data['jobs'][0]['content'] += '<p>A new artificial duty.</p>'
    data['meta']['total'] = len(data['jobs'])
    return data


class ComparisonTests(unittest.TestCase):
    def setUp(self):
        self.before = snapshot()
        self.after = snapshot(changed_payload(),1700000601)

    def compare(self, before=None, after=None):
        return compare_catalogs(self.before if before is None else before,
                                self.after if after is None else after, SOURCE.key)

    def test_first_observation_is_baseline_not_three_new_jobs(self):
        delta = compare_catalogs(None,self.before,SOURCE.key)
        self.assertEqual(delta['status'],'baseline')
        self.assertEqual(delta['counts']['current_total'],3)
        self.assertEqual(delta['counts']['added'],0)
        self.assertIsNone(delta['before_observed_at'])

    def test_added_changed_missing_and_unchanged_are_separate(self):
        delta = self.compare()
        self.assertEqual(delta['added'],['80003'])
        self.assertEqual(delta['updated'],[{'id':'80000','fields':['text']}])
        self.assertEqual(delta['missing'],['80001'])
        self.assertEqual(delta['counts'],dict(previous_total=3,current_total=3,added=1,updated=1,missing=1,unchanged=1))

    def test_observation_time_alone_does_not_change_content(self):
        after = snapshot(now=1700000601)
        self.assertNotEqual(after['revision'],self.before['revision'])
        delta = self.compare(after=after)
        self.assertEqual(delta['updated'],[])
        self.assertEqual(delta['counts']['unchanged'],3)

    def test_each_semantic_field_changes_once(self):
        for field in FIELDS:
            with self.subTest(field=field):
                after = copy.deepcopy(self.before)
                after['jobs'][0][field] = True if field=='remote' else 'changed'
                after['revision'] = revision(after['jobs'])
                self.assertEqual(self.compare(after=after)['updated'],[{'id':'80000','fields':[field]}])

    def test_order_changes_do_not_make_content_changes(self):
        after = copy.deepcopy(self.before);after['jobs'].reverse()
        after['revision'] = revision(after['jobs'])
        self.assertEqual(self.compare(after=after)['counts']['unchanged'],3)

    def test_adapter_change_rebaselines_not_mass_updates(self):
        after = copy.deepcopy(self.after)
        for row in after['jobs']:row['adapter_version']='new-parser'
        after['revision']=revision(after['jobs'])
        delta=self.compare(after=after)
        self.assertEqual((delta['status'],delta['reason']),('baseline','adapter_changed'))
        self.assertEqual(delta['counts']['added'],0)
        self.assertIn('重新建立基线',compact_change(delta)['message'])

    def test_valid_empty_catalog_is_missing_observation_not_closed(self):
        delta=self.compare(after=snapshot(fixtures.payload(0),1700000601))
        self.assertEqual(delta['missing'],['80000','80001','80002'])
        self.assertEqual(delta['counts']['current_total'],0)
        self.assertIn('不等于岗位已关闭',compact_change(delta)['message'])
        self.assertNotIn('closed',json.dumps(delta))

    def test_empty_baseline_can_observe_additions(self):
        delta=self.compare(before=snapshot(fixtures.payload(0)))
        self.assertEqual(delta['counts']['added'],3)
        self.assertEqual(delta['counts']['previous_total'],0)

    def test_input_snapshots_are_not_mutated(self):
        old,new=copy.deepcopy(self.before),copy.deepcopy(self.after)
        delta=self.compare();delta['updated'][0]['fields'].append('fake')
        self.assertEqual((self.before,self.after),(old,new))
        self.assertEqual(self.compare()['updated'][0]['fields'],['text'])

    def test_duplicate_identity_and_wrong_source_are_rejected(self):
        for edit in ('duplicate','source'):
            after=copy.deepcopy(self.after)
            if edit=='duplicate':after['jobs'].append(after['jobs'][0])
            else:after['jobs'][0]['source']='boss'
            with self.subTest(edit=edit),self.assertRaises(ContractError):self.compare(after=after)

    def test_changed_endpoint_and_clock_rollback_are_rejected(self):
        for patch_value in ({'api':'other'}, {'observed_at':1699999999}, {'observed_at':float('nan')}):
            with self.subTest(value=patch_value),self.assertRaises(ContractError):
                self.compare(after={**self.after,**patch_value})

    def test_invalid_metadata_unknown_fields_and_revision_are_rejected(self):
        for edit in ({'extra':'SECRET'}, {'after_revision':'a'*64}, {'schema_version':True},
                     {'observed_at':True}, {'before_observed_at':float('inf')}, {'scope':'market'}):
            with self.subTest(edit=edit),self.assertRaises(ContractError) as error:
                validate_change({**self.compare(),**edit},self.after,SOURCE.key)
            self.assertNotIn('SECRET',str(error.exception))

    def test_invalid_counts_duplicates_and_cross_group_ids_are_rejected(self):
        mutations=[lambda d:d['counts'].update(added=True),lambda d:d['counts'].update(unchanged=9),
                   lambda d:d.update(missing=['80000']),lambda d:d.update(added=['80003','80003']),
                   lambda d:d.update(added=['absent']),lambda d:d['counts'].update(previous_total=-1),
                   lambda d:d['updated'][0].update(fields=['text','text']),
                   lambda d:d['updated'][0].update(fields=['password']),
                   lambda d:d['updated'][0].update(id='80003')]
        for mutation in mutations:
            d=self.compare();mutation(d)
            with self.subTest(change=d),self.assertRaises(ContractError):validate_change(d,self.after,SOURCE.key)

    def test_compact_view_has_no_text_queries_or_id_lists(self):
        compact=compact_change(self.compare())
        text=json.dumps(compact,ensure_ascii=False)
        self.assertLess(len(text),2000)
        for secret in ('ARTIFICIAL','80000','80003','fields','jobs','Architect'):
            self.assertNotIn(secret,text)
        self.assertIn('不是本页或关键词结果',compact['message'])


class LocalChangeTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup)
        self.workspace=Workspace(tmp.name);self.now=[time.time()]
        self.wire=Mock();self.wire.json.return_value=fixtures.payload()
        self.client=LocalPublicDataClient(self.workspace,transport=self.wire,clock=lambda:self.now[0])

    def search(self, **kwargs):
        return self.client.search(fixtures.query(query='Cursor',**kwargs),consent=True)

    def refresh(self):
        self.now[0]+=601;self.wire.json.return_value=changed_payload()
        return self.search()

    def test_full_snapshot_delta_is_independent_of_query_and_page(self):
        self.search();result=self.refresh()
        same=self.client.search(fixtures.query(query='not-in-catalog'),consent=True)
        self.assertEqual(same['response']['jobs'],[])
        self.assertEqual(same['catalog_change'],result['catalog_change'])
        page=self.search(limit=1)
        following=self.search(limit=1,cursor=page['response']['next_cursor'])
        self.assertEqual(following['catalog_change'],result['catalog_change'])
        self.assertEqual(self.wire.json.call_count,2)

    def test_cached_queries_and_restart_keep_original_comparison(self):
        self.search();expected=self.refresh()['catalog_change'];self.now[0]+=10
        self.client=LocalPublicDataClient(self.workspace,transport=self.wire,clock=lambda:self.now[0])
        actual=self.search()
        self.assertEqual(actual['catalog_change'],expected)
        self.assertTrue(actual['cache_reused'])
        self.assertEqual(self.wire.json.call_count,2)

    def test_same_content_refresh_has_zero_added_and_updated(self):
        self.search();self.now[0]+=601
        d=self.search()['catalog_change']
        self.assertEqual(d['counts']['unchanged'],3)
        self.assertEqual((d['added'],d['updated'],d['missing']),([],[],[]))

    def test_legacy_cache_remains_readable_then_compares_on_fresh_fetch(self):
        self.search();old=json.loads(self.client.path.read_text(encoding='utf-8'))
        del old['catalog_change'];atomic_json(self.client.path,old)
        self.assertIsNone(self.search()['catalog_change'])
        self.assertEqual(self.refresh()['catalog_change']['counts']['updated'],1)

    def test_v1_migration_is_lazy_and_preserves_original_for_rollback(self):
        self.client.root.mkdir(exist_ok=True)
        old=snapshot(now=self.now[0]);atomic_json(self.client.legacy_path,old)
        before=self.client.legacy_path.read_bytes()
        self.assertIsNone(self.search()['catalog_change'])
        self.assertFalse(self.client.path.exists())
        self.wire.json.assert_not_called()
        self.assertEqual(self.refresh()['catalog_change']['counts']['updated'],1)
        self.assertTrue(self.client.path.exists())
        self.assertEqual(self.client.legacy_path.read_bytes(),before)

    def test_invalid_v2_does_not_silently_fall_back_to_valid_v1(self):
        self.client.root.mkdir(exist_ok=True)
        atomic_json(self.client.legacy_path,snapshot(now=self.now[0]))
        self.client.path.write_text('{}',encoding='utf-8')
        with self.assertRaises(ContractError):self.search()
        self.wire.json.assert_not_called()

    def test_expired_baseline_is_not_all_new(self):
        self.search();self.now[0]+=7*86400+1
        result=self.search()
        self.assertEqual(result['catalog_change']['status'],'baseline')
        self.assertEqual(result['catalog_change']['counts']['added'],0)

    def test_bad_or_partial_response_cannot_advance_cache(self):
        self.search();old=self.client.path.read_bytes();self.now[0]+=601
        self.wire.json.return_value={'jobs':[],'meta':{'total':10}}
        with self.assertRaises(ContractError):self.search()
        self.assertEqual(self.client.path.read_bytes(),old)

    def test_network_outage_preserves_old_delta_and_timestamp(self):
        self.search();expected=self.refresh();old=self.client.path.read_bytes();self.now[0]+=601
        self.wire.json.side_effect=FetchError('network_error')
        result=self.search()
        self.assertTrue(result['stale']);self.assertEqual(result['refresh_error'],'network_error')
        self.assertEqual(result['catalog_change'],expected['catalog_change'])
        self.assertEqual(self.client.path.read_bytes(),old)

    def test_hard_failure_never_becomes_new_comparison(self):
        self.search();old=self.client.path.read_bytes();self.now[0]+=601
        self.wire.json.side_effect=FetchError('tls_verification_failed')
        with self.assertRaises(FetchError):self.search()
        with self.assertRaises(FetchError):self.search()
        self.assertEqual(self.client.path.read_bytes(),old)
        historical=self.client.cached(fixtures.query())
        self.assertEqual(historical['refresh_error'],'tls_verification_failed')

    def test_corrupt_change_metadata_is_rejected_before_fetch(self):
        self.search();value=json.loads(self.client.path.read_text(encoding='utf-8'))
        value['catalog_change']['counts']['added']=True;atomic_json(self.client.path,value)
        self.now[0]+=601
        with self.assertRaises(ContractError):self.search()
        self.assertEqual(self.wire.json.call_count,1)

    def test_atomic_write_failure_does_not_split_snapshot_and_audit(self):
        self.search();old=self.client.path.read_bytes();self.now[0]+=601
        self.wire.json.return_value=changed_payload()
        with patch('vibe_job_radar.utils.os.replace',side_effect=OSError('test disk unavailable')):
            with self.assertRaises(OSError):self.search()
        self.assertEqual(self.client.path.read_bytes(),old)

    def test_returned_metadata_does_not_modify_cached_copy(self):
        self.search();result=self.refresh()
        result['catalog_change']['counts']['added']=999
        self.assertEqual(self.search()['catalog_change']['counts']['added'],1)

    def test_complete_empty_catalog_has_observation_not_deletions(self):
        self.search();self.now[0]+=601;self.wire.json.return_value=fixtures.payload(0)
        result=self.search()
        self.assertEqual(result['catalog_change']['counts']['missing'],3)
        self.assertEqual(result['response']['jobs'],[])


class ChangeHTTPTests(unittest.TestCase):
    setUp=http_fixtures.HTTPTests.setUp
    tearDown=http_fixtures.HTTPTests.tearDown
    call=http_fixtures.HTTPTests.call
    wait=fixtures.LocalPublicHTTPTests.wait

    def submit(self, query=None):
        code,_,_=self.call('/api/public/search',{'consent':True,'query':(query or fixtures.query()).payload()})
        self.assertEqual(code,200)
        state=self.wait();self.assertEqual(state['status'],'completed',state)
        return state

    def test_change_report_is_downloadable_hashed_and_private_data_isolated(self):
        self.server.workspace.add_job(http_fixtures.capture())
        client=self.server.public_tasks.hybrid
        now=[time.time()];client.clock=lambda:now[0]
        with patch.object(SafeHTTP,'conditional_json',return_value=JSONRepresentation(200,fixtures.payload(),None)) as wire:
            first=self.submit();now[0]+=601;wire.return_value=JSONRepresentation(200,changed_payload(),None)
            second=self.submit()
            self.assertEqual(second['catalog_change']['counts']['updated'],1)
            self.assertNotIn('added',second['catalog_change'])
            report=json.loads(self.call('/api/report/'+second['report_id'])[2])
            self.assertIn('catalog_changes.json',report['files'])
            folder=self.server.workspace.root/'reports'/second['report_id']
            content=(folder/'catalog_changes.json').read_bytes()
            audit=json.loads(content)
            self.assertEqual(audit['missing'],['80001'])
            self.assertEqual(report['manifest']['output_files_sha256']['catalog_changes.json'],hashlib.sha256(content).hexdigest())
            self.assertNotIn(b'TEST FIXTURE NOT MARKET DATA',content)
            original=self.server.workspace.root/'reports'/first['report_id']/'catalog_changes.json'
            self.assertEqual(json.loads(original.read_text(encoding='utf-8'))['status'],'baseline')
            self.assertEqual(wire.call_count,2)
        with Store(self.server.workspace.db) as store:self.assertGreaterEqual(len(store.records()),3)

    def test_empty_filter_exposes_catalog_counts_without_inventing_report(self):
        with patch.object(SafeHTTP,'conditional_json',return_value=JSONRepresentation(200,fixtures.payload(),None)):
            state=self.submit(fixtures.query(query='DO-NOT-MATCH'))
        self.assertEqual(state['report_id'],'')
        self.assertEqual(state['code'],'public_empty')
        self.assertEqual(state['catalog_change']['counts']['current_total'],3)

    def test_state_and_page_are_offline_and_cannot_accept_change_injection(self):
        with patch.object(SafeHTTP,'conditional_json') as wire:
            self.assertEqual(self.call('/api/public/state',authorized=False)[0],403)
            self.assertIn('public-changes',self.call('/')[2].decode('utf-8'))
            self.assertEqual(self.call('/api/public/search',{'consent':True,'query':fixtures.query().payload(),
                'catalog_change':{'added':['FAKE']}})[0],400)
            wire.assert_not_called()
