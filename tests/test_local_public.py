"""Local acquisition fixtures are artificial, not a source of market claims."""
import copy
import json
import tempfile
import time
import unittest
from dataclasses import replace
from unittest.mock import Mock, patch

from vibe_job_radar.local_public import API_URL, SOURCE, SCOPE, LocalPublicDataClient, parse_board, revision
from vibe_job_radar.public_contract import ContractError, PublicQuery, PublicSource, validate_batch
from vibe_job_radar.public_data import PublicDataClient
from vibe_job_radar.public_example import PublicExample
from vibe_job_radar.public_gateway import PublicGateway
from vibe_job_radar.network import FetchError, SafeHTTP, JSONRepresentation
from vibe_job_radar.guided.rate import RateLimit
from vibe_job_radar.workspace import InputError, Workspace
from vibe_job_radar.utils import atomic_json
from vibe_job_radar.store import Store
import test_workbench as http_fixtures


def payload(count=3):
    jobs = [{'id': 80000+i, 'absolute_url': f'https://job-boards.greenhouse.io/anthropic/jobs/{80000+i}',
             'title': 'Architect 人工测试' if i % 2 == 0 else 'Engineer 人工测试',
             'location': {'name': 'London' if i % 2 == 0 else 'Paris'},
             'content': '<p>ARTIFICIAL TEST FIXTURE. NOT MARKET DATA.</p><p>'
                        'Use Cursor for AI-assisted coding and code review. Build reliable systems, write unit tests, '
                        'and document design. 人工测试正文，不是实际招聘岗位。</p>'}
            for i in range(count)]
    return {'jobs': jobs, 'meta': {'total': count}}


def query(**kw):
    return PublicQuery(**{'query': 'Architect', 'source_scope': (SOURCE.key,), **kw})


class LocalSourceTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup)
        self.workspace=Workspace(tmp.name);self.now=[time.time()]
        self.transport=Mock();self.transport.json.return_value=payload()
        self.client=LocalPublicDataClient(self.workspace, transport=self.transport, clock=lambda:self.now[0])

    def test_no_network_at_construction_or_status(self):
        self.transport.json.assert_not_called()
        self.assertIsNone(self.client.cached(query()))
        self.transport.json.assert_not_called()

    def test_unrelated_workspace_start_is_not_blocked_by_bad_optional_cache(self):
        self.client.root.mkdir(exist_ok=True)
        self.client.key_path.write_bytes(b'bad')
        other=LocalPublicDataClient(self.workspace,transport=self.transport)
        self.assertEqual(other.registry[SOURCE.key],SOURCE)
        with self.assertRaises(ContractError):other.search(query(),consent=True)
        self.transport.json.assert_not_called()

    def test_direct_constant_get_and_private_query_stays_on_device(self):
        value=self.client.search(query(query='Cursor'),consent=True)
        self.transport.json.assert_called_once_with(API_URL)
        self.assertEqual(value['matching_jobs'],3)
        self.assertEqual(value['execution_mode'],'local_direct')
        self.assertFalse(value['stale']);self.assertEqual(value['network_requests'],1)
        self.assertTrue(all(j['remote'] is None for j in value['response']['jobs']))

    def test_filter_by_keyword_and_region_without_inferred_remote(self):
        value=self.client.search(query(region='London'),consent=True)
        self.assertEqual(len(value['response']['jobs']),2)
        other=self.client.search(query(query='Engineer',region='Paris'),consent=True)
        self.assertEqual(other['returned_jobs'],1)
        unknown=self.client.search(query(remote=True),consent=True)
        self.assertEqual(unknown['response']['jobs'],[])
        self.assertEqual(self.transport.json.call_count,1)

    def test_consent_and_source_scope_before_network(self):
        for consent in (False,None,1,'true'):
            with self.assertRaises(InputError):self.client.search(query(),consent=consent)
        for sources in (('boss',),(SOURCE.key,'boss')):
            with self.assertRaises(ContractError):self.client.search(query(source_scope=sources),consent=True)
        self.transport.json.assert_not_called()

    def test_api_payload_cannot_change_endpoint_or_add_secrets(self):
        for key in ('url','headers','cookie','password','api_key','access_mode'):
            with self.assertRaises(ContractError):
                PublicQuery.from_dict({**query().payload(),key:'SECRET'})
        self.transport.json.assert_not_called()

    def test_distribution_remains_disabled_for_local_source(self):
        value=self.client.search(query(),consent=True)
        self.assertTrue(SOURCE.local_access_approved);self.assertFalse(SOURCE.distribution_approved)
        with self.assertRaises(ContractError):validate_batch(value['response'],query(),{SOURCE.key:SOURCE})
        with self.assertRaises(ContractError):PublicGateway({SOURCE.key:SOURCE},value['response']['jobs'])
        remote=PublicDataClient(self.workspace.root/'remote','https://service.fixture.test',{SOURCE.key:SOURCE},transport=Mock())
        with self.assertRaises(ContractError):remote.search(query(),consent=True)
        with self.assertRaises(ContractError):
            validate_batch(value['response'],query(),{SOURCE.key:SOURCE},access_mode='anything')
        with self.assertRaises(ContractError):replace(SOURCE,local_access_approved=1)

    def test_timestamp_cache_and_keyword_change_survive_restart(self):
        first=self.client.search(query(),consent=True)
        self.now[0]+=100
        again=LocalPublicDataClient(self.workspace,transport=self.transport,clock=lambda:self.now[0])
        result=again.search(query(query='Engineer'),consent=True)
        self.assertTrue(result['cache_reused']);self.assertEqual(result['network_requests'],0)
        self.assertEqual(result['observed_at'],first['observed_at'])
        self.assertEqual(result['response']['jobs'][0]['collected_at'],first['response']['jobs'][0]['collected_at'])
        self.assertEqual(self.transport.json.call_count,1)

    def test_cursor_query_snapshot_bound_and_local_after_restart(self):
        first=self.client.search(query(limit=1),consent=True)
        cursor=first['response']['next_cursor'];self.assertTrue(cursor)
        again=LocalPublicDataClient(self.workspace,transport=self.transport,clock=lambda:self.now[0])
        second=again.search(query(limit=1,cursor=cursor),consent=True)
        self.assertNotEqual(first['response']['jobs'][0]['id'],second['response']['jobs'][0]['id'])
        self.assertEqual(second['response']['next_cursor'],'');self.assertEqual(self.transport.json.call_count,1)
        for q in (query(query='Engineer',limit=1,cursor=cursor),query(limit=2,cursor=cursor),
                  query(limit=1,cursor=cursor+'x')):
            with self.assertRaises(ContractError):again.search(q,consent=True)
        self.assertEqual(self.transport.json.call_count,1)

    def test_unknown_or_expired_cursor_does_not_fetch(self):
        with self.assertRaises(ContractError):self.client.search(query(cursor='1.fake'),consent=True)
        self.transport.json.assert_not_called()
        cursor=self.client.search(query(limit=1),consent=True)['response']['next_cursor']
        self.now[0]+=7*86400+1
        with self.assertRaises(ContractError):self.client.search(query(limit=1,cursor=cursor),consent=True)
        self.assertEqual(self.transport.json.call_count,1)

    def test_outage_retains_stale_timestamp_not_synthetic_jobs(self):
        first=self.client.search(query(),consent=True);self.now[0]+=601
        self.transport.json.side_effect=FetchError('network_error')
        value=self.client.search(query(),consent=True)
        self.assertTrue(value['stale']);self.assertTrue(value['cache_reused'])
        self.assertEqual(value['refresh_error'],'network_error')
        self.assertEqual(value['response']['jobs'],first['response']['jobs'])

    def test_permission_tls_or_bad_payload_never_hidden_by_cache(self):
        self.client.search(query(),consent=True)
        for code in ('http_401','http_403','tls_verification_failed','non_public_address'):
            self.now[0]+=601;self.transport.json.side_effect=FetchError(code)
            with self.assertRaises(FetchError):self.client.search(query(),consent=True)
        self.now[0]+=601;self.transport.json.side_effect=None
        self.transport.json.return_value={'jobs':[],'meta':{'total':4}}
        with self.assertRaises(ContractError):self.client.search(query(),consent=True)

    def test_expired_cache_or_cold_failure_is_not_success(self):
        self.transport.json.side_effect=FetchError('network_error')
        with self.assertRaises(FetchError):self.client.search(query(),consent=True)
        self.assertFalse(self.client.path.exists())
        self.now[0]+=31;self.transport.json.side_effect=None
        self.client.search(query(),consent=True)
        self.now[0]+=7*86400+1;self.transport.json.side_effect=FetchError('network_error')
        with self.assertRaises(FetchError):self.client.search(query(),consent=True)

    def test_shared_example_limit_and_failure_budget_not_reset(self):
        self.client.search(query(),consent=True)
        example=PublicExample(self.workspace,transport=Mock())
        self.assertEqual(example.ledger.path,self.client.ledger.path)
        with self.assertRaises(RateLimit):example.ledger.reserve(SCOPE,'request')
        self.assertEqual(self.transport.json.call_count,1)

    def test_long_429_is_durable_and_no_early_retry(self):
        self.transport.json.side_effect=FetchError('http_429',retry_after=900)
        with self.assertRaises(FetchError):self.client.search(query(),consent=True)
        self.now[0]+=600
        again=LocalPublicDataClient(self.workspace,transport=self.transport,clock=lambda:self.now[0])
        with self.assertRaises(RateLimit):again.search(query(),consent=True)
        self.assertEqual(self.transport.json.call_count,1)
        self.now[0]+=301;self.transport.json.side_effect=None
        self.assertEqual(again.search(query(),consent=True)['returned_jobs'],2)

    def test_tampered_cache_and_clock_rollback_fail_closed(self):
        self.client.search(query(),consent=True)
        original=self.client.path.read_bytes()
        value=json.loads(original);value['jobs'][0]['source']='boss'
        value['revision']=revision(value['jobs']);atomic_json(self.client.path,value)
        with self.assertRaises(ContractError):self.client.cached(query())
        self.client.path.write_bytes(original);self.now[0]-=1
        with self.assertRaises(ContractError):self.client.cached(query())
        self.assertEqual(self.transport.json.call_count,1)

    def test_registry_does_not_allow_arbitrary_boards(self):
        for change in ({'id':True}, {'absolute_url':'https://127.0.0.1/a'},
                       {'absolute_url':'https://job-boards.greenhouse.io/other/jobs/80000'},
                       {'absolute_url':'https://job-boards.greenhouse.io/anthropic/jobs/999'},
                       {'absolute_url':'https://job-boards.greenhouse.io/anthropic/jobs/80000?token=SECRET'},
                       {'content':'tiny'}, {'location':None}):
            p=payload();p['jobs'][0].update(change)
            with self.subTest(change=change),self.assertRaises(ContractError):parse_board(p,self.now[0])
        p=payload();p['jobs'][1]=copy.deepcopy(p['jobs'][0])
        with self.assertRaises(ContractError):parse_board(p,self.now[0])

    def test_empty_list_is_valid_not_market_coverage(self):
        self.transport.json.return_value=payload(0)
        result=self.client.search(query(),consent=True)
        self.assertEqual(result['matching_jobs'],0);self.assertEqual(result['response']['jobs'],[])


class LocalPublicHTTPTests(unittest.TestCase):
    setUp=http_fixtures.HTTPTests.setUp
    tearDown=http_fixtures.HTTPTests.tearDown
    call=http_fixtures.HTTPTests.call

    def wait(self):
        # This checks task completion, not report-generation performance. Wait
        # on the actual worker rather than repeatedly discovering system proxy
        # settings via state() while it writes the report on a busy CI host.
        worker=self.server.public_tasks._thread
        self.assertIsNotNone(worker)
        worker.join(timeout=30)
        state=self.server.public_tasks.state()['task']
        self.assertFalse(worker.is_alive(),f'local public worker timeout: {state}')
        self.assertNotIn(state['status'],{'queued','running','cancelling'},state)
        return state

    def test_default_local_provider_needs_no_server_or_config_and_is_offline_until_consent(self):
        with patch.object(SafeHTTP,'conditional_json') as network:
            code,_,body=self.call('/api/public/state')
            state=json.loads(body);self.assertEqual(code,200)
            self.assertTrue(state['query_available']);self.assertFalse(state['hybrid_service_configured'])
            self.assertEqual(state['execution_mode'],'local_direct')
            self.assertEqual(state['sources'][0]['id'],SOURCE.key)
            self.assertEqual(self.call('/api/public/search',{'consent':False,'query':query().payload()})[0],400)
            self.assertEqual(self.call('/api/public/search',{'consent':True,'query':query().payload()},authorized=False)[0],403)
            self.assertEqual(self.call('/api/public/search',{'consent':True,'query':query().payload()},headers={'Origin':'https://evil.test'})[0],403)
            self.assertIn('无需自建服务器',self.call('/')[2].decode('utf-8'))
        network.assert_not_called()

    def test_local_query_flows_to_isolated_report_with_provenance_and_paging(self):
        self.server.workspace.add_job(http_fixtures.capture())
        with patch.object(SafeHTTP,'conditional_json',return_value=JSONRepresentation(200,payload(),None)) as network:
            self.assertEqual(self.call('/api/public/search',{'consent':True,'query':query(limit=1).payload()})[0],200)
            first=self.wait();self.assertEqual(first['status'],'completed',first)
            self.assertEqual(first['matching_jobs'],2);self.assertEqual(first['returned_jobs'],1)
            report=json.loads(self.call('/api/report/'+first['report_id'])[2])
            self.assertEqual(report['manifest']['stats']['full_text_job_groups'],1)
            self.assertIn('public_source.json',report['files'])
            audit=json.loads((self.server.workspace.root/'reports'/first['report_id']/'public_source.json').read_text(encoding='utf-8'))
            self.assertEqual(audit['execution_mode'],'local_direct')
            self.assertEqual(audit['jobs'][0]['source'],SOURCE.key)
            self.assertEqual(self.call('/api/public/search',{'consent':True,'query':query(limit=1,cursor=first['next_cursor']).payload()})[0],200)
            second=self.wait();self.assertEqual(second['status'],'completed',second)
            self.assertTrue(second['cache_reused']);self.assertEqual(second['next_cursor'],'')
            self.assertNotEqual(second['report_id'],first['report_id'])
            network.assert_called_once_with(API_URL,etag=None)
        with Store(self.server.workspace.db) as store:self.assertEqual(len(store.records()),3)
