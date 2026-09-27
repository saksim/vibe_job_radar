"""Controlled public-data fixtures; no production service or site certification."""
import copy
import io
import json
import os
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

import test_workbench as http_fixtures
import test_redirect_recovery as example_fixtures
from vibe_job_radar.public_contract import PublicSource, PublicQuery, ContractError, validate_batch, as_record
from vibe_job_radar.public_gateway import PublicGateway
from vibe_job_radar.public_data import PublicDataClient
from vibe_job_radar.public_tasks import PublicTasks
from vibe_job_radar.workspace import Workspace, InputError
from vibe_job_radar.network import FetchError, SafeHTTP, Response, retry_after_seconds
from vibe_job_radar.store import Store
from vibe_job_radar.utils import utc_now

SOURCE=PublicSource('fixture','TEST FIXTURE ONLY',('jobs.fixture.test',),'Artificial data; tests only',True)
REGISTRY={'fixture':SOURCE}

def job(ident='one',**changes):
    value={'id':ident,'source':'fixture','title':'AI Architect','company':'TEST FIXTURE NOT MARKET DATA',
           'location':'Remote','remote':True,'text':('Requires Python and Cursor assisted software development. '+
           'Candidates design reliable services, write unit tests and review source code. ')*3,
           'url':'https://jobs.fixture.test/jobs/'+ident,'final_url':'https://jobs.fixture.test/jobs/'+ident,
           'collected_at':'2026-01-01T00:00:00Z','completeness':'full_text','adapter_version':'test-only-v1'}
    value.update(changes);return value

def query(**changes):
    return PublicQuery.from_dict({'query':'AI','source_scope':['fixture'],**changes})

class ContractTests(unittest.TestCase):
    def batch(self,rows=None):
        return {'schema_version':1,'jobs':rows if rows is not None else [job()],
                'next_cursor':'','generated_at':'2026-01-02T00:00:00Z'}

    def test_only_minimum_fields_can_leave_device(self):
        for extra in ('cookie','password','authorization','headers','url','resume','evidence','api_key'):
            with self.subTest(extra=extra),self.assertRaises(ContractError):
                query(**{extra:'SECRET'})
        self.assertEqual(set(query().payload()),{'query','region','remote','source_scope','limit','cursor'})

    def test_query_types_limits_scope_and_cursor(self):
        for change in ({'limit':True},{'limit':51},{'query':''},{'query':[]},{'remote':'yes'},
                       {'source_scope':[]},{'source_scope':['fixture','fixture']},{'cursor':'https://127.0.0.1'},
                       {'query':'\ud800'}):
            with self.subTest(change=change),self.assertRaises(ContractError):query(**change)

    def test_complete_batch_validates_before_acceptance(self):
        bad=job('two',source='boss')
        with self.assertRaises(ContractError):validate_batch(self.batch([job(),bad]),query(),REGISTRY)

    def test_source_domain_and_signed_links_rejected(self):
        for url in ('http://jobs.fixture.test/one','https://jobs.fixture.test.evil.test/one',
                    'https://secret:SECRET@jobs.fixture.test/one','https://jobs.fixture.test/one?access_token=SECRET',
                    'https://jobs.fixture.test/one?sig=SECRET','https://jobs.fixture.test/one#SECRET'):
            with self.subTest(url=url),self.assertRaises(ContractError) as error:
                validate_batch(self.batch([job(url=url)]),query(),REGISTRY)
            self.assertNotIn('SECRET',str(error.exception))

    def test_unknown_response_fields_and_duplicate_ids_rejected(self):
        for data in ({**self.batch(),'cookie':'SECRET'}, self.batch([job(),job()]), self.batch([job(password='SECRET')])):
            with self.assertRaises(ContractError):validate_batch(data,query(),REGISTRY)

    def test_cannot_promote_snippet_or_empty_full_text(self):
        data=validate_batch(self.batch([job(completeness='snippet',text='Partial role description')]),query(),REGISTRY)
        self.assertEqual(as_record(data['jobs'][0],SOURCE).evidence_level,'snippet')
        with self.assertRaises(ContractError):validate_batch(self.batch([job(text='small')]),query(),REGISTRY)

    def test_timestamps_timezone_and_future_collection(self):
        for stamp in ('2026-01-01','bad','2026-12-01T00:00:00Z'):
            with self.assertRaises(ContractError):validate_batch(self.batch([job(collected_at=stamp)]),query(),REGISTRY)

    def test_unreviewed_distribution_is_disabled(self):
        with self.assertRaises(ContractError):validate_batch(self.batch(),query(),{'fixture':replace(SOURCE,distribution_approved=False)})

    def test_boolean_schema_and_mismatching_remote_filter_rejected(self):
        with self.assertRaises(ContractError):validate_batch({**self.batch(),'schema_version':True},query(),REGISTRY)
        with self.assertRaises(ContractError):validate_batch(self.batch(),query(remote=False),REGISTRY)

    def test_public_source_does_not_allow_private_literal_hosts(self):
        with self.assertRaises(ContractError):replace(SOURCE,domains=('127.0.0.1',))

class GatewayTests(unittest.TestCase):
    def setUp(self):
        self.clock=[2_000_000_000.0]
        self.gateway=PublicGateway(REGISTRY,[job(str(i)) for i in range(5)],cursor_secret=b'a'*32,clock=lambda:self.clock[0])

    def call(self,path,method='GET',data=None,**extra):
        body=json.dumps(data).encode() if data is not None else b''
        env={'PATH_INFO':path,'REQUEST_METHOD':method,'CONTENT_TYPE':'application/json',
             'CONTENT_LENGTH':str(len(body)),'wsgi.input':io.BytesIO(body),**extra}; statuses=[]
        value=b''.join(self.gateway(env,lambda s,h:statuses.append(s)))
        return statuses[0],json.loads(value)

    def test_catalog_and_detail_use_reviewed_snapshot_without_network(self):
        with patch('socket.create_connection',side_effect=AssertionError('network forbidden')):
            status,data=self.call('/v1/catalog/sources')
            self.assertEqual(status,'200 OK');self.assertEqual(data['sources'][0]['id'],'fixture')
            status,data=self.call('/v1/jobs/fixture.0')
            self.assertEqual(data['jobs'][0]['id'],'0')

    def test_query_filters_do_not_replace_requested_source(self):
        self.assertEqual(self.gateway.search(query(region='nowhere').payload())['jobs'],[])
        with self.assertRaises(ContractError):self.gateway.search(query(source_scope=['boss']).payload())

    def test_cursor_tied_to_query_revision_and_expiry(self):
        first=self.gateway.search(query(limit=2).payload());cursor=first['next_cursor']
        second=self.gateway.search(query(limit=2,cursor=cursor).payload())
        self.assertEqual([j['id'] for j in second['jobs']],['2','3'])
        for q in (query(limit=3,cursor=cursor),query(query='Architect',limit=2,cursor=cursor),
                  query(limit=2,cursor=cursor+'0')):
            with self.assertRaises(ContractError):self.gateway.search(q.payload())
        changed=PublicGateway(REGISTRY,[job('different')],cursor_secret=b'a'*32,clock=lambda:self.clock[0])
        with self.assertRaises(ContractError):changed.search(query(limit=2,cursor=cursor).payload())
        self.clock[0]+=3601
        with self.assertRaises(ContractError):self.gateway.search(query(limit=2,cursor=cursor).payload())

    def test_http_rejects_credentials_arbitrary_url_and_large_body(self):
        q=query().payload()
        for fields in ({'HTTP_COOKIE':'SECRET'},{'HTTP_AUTHORIZATION':'SECRET'},
                       {'CONTENT_LENGTH':'999999'},{'CONTENT_TYPE':'text/plain'},
                       {'HTTP_TRANSFER_ENCODING':'chunked'}):
            status,data=self.call('/v1/jobs/search','POST',q,**fields)
            self.assertEqual(status,'400 Bad Request');self.assertNotIn('SECRET',str(data))
        self.assertEqual(self.call('/v1/jobs/search','POST',{**q,'url':'https://127.0.0.1'})[0],'400 Bad Request')
        self.assertEqual(self.call('/v1/jobs/fixture.missing')[0],'404 Not Found')

    def test_snapshot_never_mutates_caller_or_accepts_duplicate(self):
        values=[job()];before=copy.deepcopy(values)
        gateway=PublicGateway(REGISTRY,values)
        values[0]['text']='modified'
        self.assertEqual(gateway.jobs[0],before[0])
        with self.assertRaises(ContractError):PublicGateway(REGISTRY,[job(),job()])

    def test_missing_approval_or_registry_alias_blocks_ingestion(self):
        with self.assertRaises(ContractError):PublicGateway({'fixture':replace(SOURCE,distribution_approved=False)},[job()])
        with self.assertRaises(ContractError):PublicGateway({'boss':SOURCE},[])

class ClientTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.now=[2_000_000_000.0]
        self.gateway=PublicGateway(REGISTRY,[job()])
        self.transport=Mock();self.transport.json.side_effect=lambda url,**kw:self.gateway.search(kw['payload'])
        self.client=PublicDataClient(Path(self.tmp.name),'https://service.fixture.test',REGISTRY,
                                     transport=self.transport,clock=lambda:self.now[0])

    def test_consent_required_before_any_external_call_or_cache(self):
        with self.assertRaises(InputError):self.client.search(query())
        self.transport.json.assert_not_called()
        self.assertFalse(list(Path(self.tmp.name).glob('*.json')))

    def test_payload_contains_no_ambient_credentials_or_local_files(self):
        with patch.dict(os.environ,{'BRAVE_SEARCH_API_KEY':'SECRET','RADAR_FEED_TOKEN':'SECRET'}):
            self.client.search(query(),consent=True)
        args,kw=self.transport.json.call_args
        self.assertEqual(args[0],'https://service.fixture.test/v1/jobs/search')
        self.assertEqual(set(kw),{'method','payload'});self.assertNotIn('SECRET',repr(kw))

    def test_cache_preserves_origin_timestamp_and_completeness(self):
        first=self.client.search(query(),consent=True)
        self.now[0]+=20;second=self.client.search(query(),consent=True)
        self.assertEqual(self.transport.json.call_count,1)
        self.assertTrue(second['cache_reused']);self.assertFalse(second['stale'])
        self.assertEqual(second['response'],first['response'])
        self.assertEqual(second['network_requests'],0)

    def test_outage_returns_explicit_stale_cache_not_fake_live_success(self):
        first=self.client.search(query(),consent=True);self.now[0]+=700
        self.transport.json.side_effect=FetchError('network_error')
        result=self.client.search(query(),consent=True)
        self.assertTrue(result['stale']);self.assertTrue(result['cache_reused'])
        self.assertEqual(result['refresh_error'],'network_error')
        self.assertEqual(first['response'],result['response'])

    def test_cache_only_survives_restart_without_network(self):
        first=self.client.search(query(),consent=True)
        other=PublicDataClient(Path(self.tmp.name),'https://service.fixture.test',REGISTRY,transport=self.transport,clock=lambda:self.now[0])
        self.assertEqual(other.cached(query())['response'],first['response'])
        self.assertEqual(self.transport.json.call_count,1)

    def test_no_cache_or_expired_cache_does_not_invent_results(self):
        self.transport.json.side_effect=FetchError('network_error')
        with self.assertRaises(FetchError):self.client.search(query(),consent=True)
        self.transport.json.side_effect=lambda url,**kw:self.gateway.search(kw['payload'])
        self.now[0]+=10;self.client.search(query(),consent=True);self.now[0]+=8*86400
        self.transport.json.side_effect=FetchError('network_error')
        with self.assertRaises(FetchError):self.client.search(query(),consent=True)

    def test_permission_tls_and_invalid_response_not_masked_by_cache(self):
        self.client.search(query(),consent=True);self.now[0]+=700
        for code in ('http_401','http_403','tls_verification_failed'):
            self.now[0]+=10;self.transport.json.side_effect=FetchError(code)
            with self.assertRaises(FetchError):self.client.search(query(),consent=True)
        self.now[0]+=10;self.transport.json.side_effect=lambda *a,**k:{'jobs':[]}
        with self.assertRaises(ContractError):self.client.search(query(),consent=True)

    def test_source_scope_and_service_change_cannot_reuse_wrong_cache(self):
        self.client.search(query(),consent=True)
        with self.assertRaises(ContractError):self.client.search(query(source_scope=['boss']),consent=True)
        other=PublicDataClient(Path(self.tmp.name),'https://other.fixture.test',REGISTRY,transport=self.transport,clock=lambda:self.now[0])
        self.assertIsNone(other.cached(query()))

    def test_invalid_cache_and_clock_rollback_fail_closed(self):
        self.client.search(query(),consent=True);self.now[0]-=10
        with self.assertRaises(ContractError):self.client.cached(query())
        self.now[0]+=20;self.client._path(query()).write_text('{broken',encoding='utf-8')
        with self.assertRaises(ContractError):self.client.search(query(),consent=True)

    def test_private_or_credential_service_origin_rejected(self):
        for origin in ('http://service.fixture.test','https://127.0.0.1','https://a:b@service.fixture.test',
                       'https://service.fixture.test?token=SECRET','https://service.fixture.test/path'):
            with self.assertRaises(ContractError):PublicDataClient(Path(self.tmp.name),origin,REGISTRY)

    def test_publisher_429_hint_is_durable_and_circuit_can_resume_after_due(self):
        real=SafeHTTP({'service.fixture.test'},interval=0)
        self.client.client=real
        def first(*args,**kw):
            real.blocked_hosts.add('service.fixture.test')
            raise FetchError('http_429',retry_after=700)
        with patch.object(real,'request',side_effect=first):
            with self.assertRaises(FetchError):self.client.search(query(),consent=True)
        self.now[0]+=400
        from vibe_job_radar.guided.rate import RateLimit
        with self.assertRaises(RateLimit):self.client.search(query(),consent=True)
        self.now[0]+=300
        def recovered(*args,**kw):
            self.assertNotIn('service.fixture.test',real.blocked_hosts)
            return Response(200,{},json.dumps(self.gateway.search(query().payload())).encode(),args[0])
        with patch.object(real,'request',side_effect=recovered):
            self.assertFalse(self.client.search(query(),consent=True)['cache_reused'])

    def test_retry_after_metadata_survives_safe_http_without_header_leak(self):
        with patch('vibe_job_radar.network.validate_public_url',return_value=('service.fixture.test','8.8.8.8','/jobs')), patch('vibe_job_radar.network.PinnedHTTPSConnection') as connection:
            response=connection.return_value.getresponse.return_value
            response.status=429;response.getheaders.return_value=[('Retry-After','900'),('X-Private','SECRET')]
            with self.assertRaises(FetchError) as error:SafeHTTP({'service.fixture.test'},interval=0).json('https://service.fixture.test/jobs')
            self.assertEqual(error.exception.retry_after,900)
            self.assertNotIn('SECRET',str(error.exception));self.assertEqual(connection.call_count,1)

    def test_retry_after_date_and_malformed_values_are_bounded_and_finite(self):
        from email.utils import formatdate
        import math
        self.assertEqual(retry_after_seconds(formatdate(1000000+1200,usegmt=True),now=1000000),1200)
        for value in ('inf','NaN','bad','-1'):
            self.assertTrue(math.isfinite(retry_after_seconds(value)))
            self.assertGreaterEqual(retry_after_seconds(value),300)

class PublicHTTPTests(unittest.TestCase):
    setUp=http_fixtures.HTTPTests.setUp
    tearDown=http_fixtures.HTTPTests.tearDown
    call=http_fixtures.HTTPTests.call

    def wait(self):
        deadline=time.monotonic()+10
        while time.monotonic()<deadline:
            state=self.server.public_tasks.state()['task']
            if state['status'] not in {'queued','running'}:return state
            time.sleep(.02)
        self.fail('public task did not settle')

    def test_shell_and_status_are_offline_and_token_protected(self):
        with patch.object(SafeHTTP,'json') as network:
            self.assertIn('public-example',self.call('/')[2].decode())
            self.assertEqual(self.call('/api/public/state',authorized=False)[0],403)
            self.assertEqual(self.call('/api/public/state')[0],200)
        network.assert_not_called()

    def test_public_example_actual_http_store_and_report_without_commands(self):
        with patch.object(SafeHTTP,'json',return_value=example_fixtures.fixture_payload()) as network:
            self.assertEqual(self.call('/api/public/start',{'consent':True})[0],200)
            result=self.wait();self.assertEqual(result['status'],'completed',result)
            self.assertTrue(result['report_id'])
            self.assertEqual(self.call('/api/report/'+result['report_id'])[0],200)
            self.assertEqual(self.call('/api/public/start',{'consent':True})[0],200)
            again=self.wait();self.assertTrue(again['cache_reused'])
            self.assertEqual(result['report_id'],again['report_id'])
        self.assertEqual(network.call_count,1)

    def test_consent_origin_and_extra_fields_rejected_before_network(self):
        with patch.object(SafeHTTP,'json') as network:
            for data in ({'consent':False},{'consent':True,'url':'https://127.0.0.1'}, {'consent':True,'password':'SECRET'}):
                self.assertEqual(self.call('/api/public/start',data)[0],400)
            self.assertEqual(self.call('/api/public/start',{'consent':True},headers={'Origin':'https://evil.test'})[0],403)
        network.assert_not_called()

    def test_unconfigured_service_honestly_rejects_search(self):
        self.server.public_tasks.hybrid=None  # Explicitly disabled instance, not the default local provider.
        code,_,body=self.call('/api/public/search',{'consent':True,'query':query().payload()})
        self.assertEqual(code,400);self.assertIn('已停用',json.loads(body)['error'])

    def test_configured_hybrid_query_reaches_gateway_and_isolated_local_report(self):
        gateway=PublicGateway(REGISTRY,[job(collected_at=utc_now())]);transport=Mock()
        transport.json.side_effect=lambda url,**kw:gateway.search(kw['payload'])
        self.server.public_tasks.hybrid=PublicDataClient(Path(self.tmp.name)/'public-cache','https://service.fixture.test',REGISTRY,transport=transport)
        self.server.workspace.add_job(http_fixtures.capture())
        code,_,body=self.call('/api/public/search',{'consent':True,'query':query().payload()})
        self.assertEqual(code,200,body)
        task=self.wait();self.assertEqual(task['status'],'completed',task)
        report=json.loads(self.call('/api/report/'+task['report_id'])[2])
        self.assertEqual(report['manifest']['stats']['full_text_job_groups'],1)
        self.assertIn('public_source.json',report['files'])
        with Store(self.server.workspace.db) as store:self.assertEqual(len(store.records()),2)

    def test_task_unknown_exception_never_echoes_secrets_or_claims_success(self):
        with patch.object(SafeHTTP,'json',side_effect=RuntimeError('SECRET CREDENTIAL')):
            self.call('/api/public/start',{'consent':True});state=self.wait()
        self.assertEqual(state['status'],'failed');self.assertNotIn('SECRET',json.dumps(state))
        self.assertEqual(state['report_id'],'')

    def test_duplicate_requests_do_not_spawn_parallel_source_requests(self):
        import threading
        release=threading.Event();entered=threading.Event()
        def delayed(*a,**kw):
            entered.set();release.wait(timeout=5);return example_fixtures.fixture_payload()
        with patch.object(SafeHTTP,'json',side_effect=delayed) as network:
            try:
                self.call('/api/public/start',{'consent':True});self.assertTrue(entered.wait(2))
                self.assertEqual(self.call('/api/public/start',{'consent':True})[0],409)
            finally:release.set()
            self.wait()
        self.assertEqual(network.call_count,1)
