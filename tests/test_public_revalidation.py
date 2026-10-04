"""Conditional public GETs and real local reports; all source responses artificial."""
import copy
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock,patch

from test_local_public import payload,query
from test_public_sources import cloudflare_payload,cloudflare_query
from vibe_job_radar.local_public import API_URL,SCOPE,LocalPublicDataClient,timestamp
from vibe_job_radar.network import FetchError,JSONRepresentation,Response,SafeHTTP,valid_etag
from vibe_job_radar.public_boards import ANTHROPIC,CLOUDFLARE
from vibe_job_radar.public_contract import ContractError
from vibe_job_radar.public_schedule import DAY,PublicSchedule
from vibe_job_radar.public_tasks import PublicTasks
from vibe_job_radar.utils import atomic_json
from vibe_job_radar.workspace import Workspace,InputError


class ConditionalHTTPTests(unittest.TestCase):
    def test_invalid_conditions_fail_before_dns_or_socket(self):
        client=SafeHTTP({'example.com'},interval=0)
        with patch('socket.getaddrinfo') as dns,patch('socket.create_connection') as socket:
            for tag in ('*','"a", "b"','"a"\r\nCookie: secret','unquoted','"'+'x'*512+'"',False,[],'"汉字"'):
                with self.subTest(tag=tag),self.assertRaisesRegex(FetchError,'invalid_conditional_request'):
                    client.conditional_json('https://example.com/catalog',etag=tag)
            for options in ({'method':'POST'},{'body':b''},{'headers':{'Cookie':'private'}},
                            {'headers':{'Authorization':'secret'}},{'return_redirect':True}):
                with self.subTest(options=options),self.assertRaisesRegex(FetchError,'invalid_conditional_request'):
                    client.request('https://example.com/catalog',if_none_match='"fixture"',**options)
            dns.assert_not_called();socket.assert_not_called()

    def test_only_conditional_get_accepts_matching_304_without_reading_a_body(self):
        raw=Mock(status=304);raw.getheaders.return_value=[('ETag','W/"fixture"')]
        connection=Mock();connection.getresponse.return_value=raw
        with patch('socket.getaddrinfo',return_value=[(2,1,6,'',('8.8.8.8',443))]),\
             patch('vibe_job_radar.network.PinnedHTTPSConnection',return_value=connection):
            client=SafeHTTP({'example.com'},interval=0)
            result=client.conditional_json('https://example.com/catalog',etag='"fixture"')
            self.assertEqual(result,JSONRepresentation(304,None,'W/"fixture"'))
            headers=connection.request.call_args.kwargs['headers']
            self.assertEqual(headers['If-None-Match'],'"fixture"')
            self.assertNotIn('Cookie',headers);self.assertNotIn('Authorization',headers)
            raw.read.assert_not_called();connection.close.assert_called()
            with self.assertRaisesRegex(FetchError,'redirect_not_followed'):client.json('https://example.com/catalog')
            raw.getheaders.return_value=[('ETag','"other"'),('ETag','"fixture"')]
            with self.assertRaisesRegex(FetchError,'invalid_not_modified'):
                client.conditional_json('https://example.com/catalog',etag='"fixture"')
            for returned in ('"another"','*',None):
                raw.getheaders.return_value=[] if returned is None else [('ETag',returned)]
                with self.assertRaisesRegex(FetchError,'invalid_not_modified'):
                    client.conditional_json('https://example.com/catalog',etag='"fixture"')
            raw.status=302;raw.getheaders.return_value=[('Location','https://example.com/elsewhere')]
            with self.assertRaisesRegex(FetchError,'redirect_not_followed'):
                client.conditional_json('https://example.com/catalog',etag='"fixture"')

    def test_optional_bad_200_etag_is_ignored_but_304_payload_is_rejected(self):
        client=SafeHTTP({'example.com'},interval=0)
        with patch.object(client,'request',return_value=Response(200,{'etag':'bad'},b'{"jobs":[]}','https://example.com')):
            self.assertEqual(client.conditional_json('https://example.com').etag,None)
        for response in (Response(304,{'etag':'"a"'},b'body','https://example.com'),
                         Response(304,{},b'','https://example.com')):
            with patch.object(client,'request',return_value=response),self.assertRaises(FetchError):
                client.conditional_json('https://example.com',etag='"a"')


class FixtureTransport:
    def __init__(self):self.calls=[];self.answer=JSONRepresentation(200,payload(5),'"fixture-v1"')
    def conditional_json(self,url,*,etag=None):
        self.calls.append((url,etag))
        if isinstance(self.answer,Exception):raise self.answer
        return copy.deepcopy(self.answer)


class PublicRevalidationTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup)
        self.workspace=Workspace(tmp.name);self.now=[time.time()];self.wire=FixtureTransport()
        self.client=self.fresh_client()
    def fresh_client(self):
        return LocalPublicDataClient(self.workspace,transport=self.wire,clock=lambda:self.now[0])
    def fetch(self,client=None,**options):
        return (client or self.client).search(query(**options),consent=True)
    def unchanged(self):
        self.now[0]+=601;self.wire.answer=JSONRepresentation(304,None,'W/"fixture-v1"')
        return self.fetch()

    def test_304_preserves_full_cache_body_time_revision_cursor_and_shared_budget(self):
        first=self.fetch(limit=1);before=self.client.path.read_bytes()
        second=self.unchanged()
        self.assertEqual(self.client.path.read_bytes(),before)
        self.assertTrue(second['cache_reused']);self.assertTrue(second['not_modified']);self.assertFalse(second['stale'])
        self.assertEqual(second['observed_at'],first['observed_at']);self.assertEqual(second['checked_at'],self.now[0])
        self.assertEqual(second['network_requests'],1);self.assertIsNone(second['catalog_change'])
        self.assertEqual(second['response']['jobs'][0]['collected_at'],timestamp(first['observed_at']))
        next_page=self.fetch(self.fresh_client(),limit=1,cursor=first['response']['next_cursor'])
        self.assertEqual(next_page['response']['jobs'][0]['id'],'80002')
        self.assertEqual(next_page['network_requests'],0);self.assertEqual(len(self.wire.calls),2)
        self.assertEqual(self.client.ledger.summary(SCOPE)['request']['day'],2)

    def test_restart_freshness_uses_check_time_and_expiry_still_refreshes(self):
        self.fetch();self.unchanged();checked=self.now[0]
        self.now[0]+=599
        value=self.fetch(self.fresh_client());self.assertFalse(value['stale']);self.assertEqual(value['checked_at'],checked)
        self.assertEqual(len(self.wire.calls),2)
        self.now[0]+=1
        self.fetch(self.fresh_client());self.assertEqual(len(self.wire.calls),3)

    def test_changed_200_advances_real_body_time_and_compares_prior_full_catalog(self):
        first=self.fetch();self.unchanged();self.now[0]+=601
        changed=payload(6);changed['jobs'][0]['content']+='<p>Changed artificial requirement.</p>'
        self.wire.answer=JSONRepresentation(200,changed,'"fixture-v2"')
        value=self.fetch();self.assertFalse(value['not_modified']);self.assertFalse(value['cache_reused'])
        self.assertGreater(value['observed_at'],first['observed_at'])
        self.assertEqual(value['catalog_change']['counts']['added'],1)
        self.assertEqual(value['catalog_change']['counts']['updated'],1)
        self.assertEqual(self.client._validation(ANTHROPIC).read(self.client._cached(self.now[0]),self.now[0])['etag'],'"fixture-v2"')

    def test_hard_failure_disables_condition_and_304_cannot_restore_permission(self):
        self.fetch();before=self.client.path.read_bytes();self.now[0]+=601
        self.wire.answer=FetchError('http_403')
        with self.assertRaisesRegex(FetchError,'http_403'):self.fetch()
        self.now[0]+=601;self.wire.answer=JSONRepresentation(304,None,'"fixture-v1"')
        with self.assertRaisesRegex(FetchError,'invalid_not_modified'):self.fetch(self.fresh_client())
        self.assertIsNone(self.wire.calls[-1][1]);self.assertEqual(self.client.path.read_bytes(),before)
        self.assertTrue(self.client.failure_guard.read(self.now[0]))
        self.now[0]+=601;self.wire.answer=JSONRepresentation(200,payload(5),'"fixture-v2"')
        self.assertFalse(self.fetch(self.fresh_client())['not_modified'])
        self.assertIsNone(self.client.failure_guard.read(self.now[0]))

    def test_429_keeps_confirmation_time_and_persists_shared_cooldown(self):
        self.fetch();self.unchanged();checked=self.now[0]
        self.now[0]+=601;self.wire.answer=FetchError('http_429',retry_after=600)
        stale=self.fetch();self.assertTrue(stale['stale']);self.assertEqual(stale['checked_at'],checked)
        self.assertEqual(stale['refresh_error'],'http_429');count=len(self.wire.calls)
        self.now[0]+=1
        waiting=self.fetch(self.fresh_client());self.assertEqual(waiting['refresh_error'],'cooldown')
        self.assertEqual(len(self.wire.calls),count);self.assertEqual(waiting['network_requests'],0)

    def test_missing_body_and_seven_day_limit_require_full_200(self):
        self.wire.answer=JSONRepresentation(304,None,'"fixture-v1"')
        with self.assertRaisesRegex(FetchError,'invalid_not_modified'):self.fetch()
        self.now[0]+=601;self.wire.answer=JSONRepresentation(200,payload(5),'"fixture-v1"');self.fetch()
        self.now[0]+=7*DAY+1;self.fetch()
        self.assertIsNone(self.wire.calls[-1][1])
        self.assertEqual(self.client._cached(self.now[0])['observed_at'],self.now[0])

    def test_missing_legacy_metadata_fetches_full_then_optional_validator_is_removed(self):
        self.fetch();metadata=self.client._validation(ANTHROPIC).path;metadata.unlink()
        self.now[0]+=601;self.fetch(self.fresh_client());self.assertIsNone(self.wire.calls[-1][1])
        self.now[0]+=601;self.wire.answer=JSONRepresentation(200,payload(5),None);self.fetch()
        self.now[0]+=601;self.fetch();self.assertIsNone(self.wire.calls[-1][1])

    def test_wrong_source_malformed_or_future_metadata_never_authorizes_request(self):
        self.fetch();path=self.client._validation(ANTHROPIC).path;original=json.loads(path.read_text(encoding='utf-8'))
        for field,value in (('api',CLOUDFLARE.api_url),('checked_at',self.now[0]+600),('etag','"x"\r\nCookie: secret')):
            broken={**original,field:value};atomic_json(path,broken)
            with self.subTest(field=field),self.assertRaises(ContractError):self.fetch(self.fresh_client())
        self.assertEqual(len(self.wire.calls),1)
        atomic_json(path,{**original,'revision':'another-full-snapshot'})
        self.now[0]+=601;self.fetch();self.assertIsNone(self.wire.calls[-1][1])

    def test_sources_keep_separate_validators_under_one_request_budget(self):
        self.fetch();self.now[0]+=31;self.wire.answer=JSONRepresentation(200,cloudflare_payload(),'"cloudflare-fixture"')
        self.client.search(cloudflare_query(),consent=True)
        self.assertEqual(self.wire.calls[-1],(CLOUDFLARE.api_url,None))
        self.now[0]+=601;self.wire.answer=JSONRepresentation(304,None,'"fixture-v1"')
        self.fetch();self.assertEqual(self.wire.calls[-1],(API_URL,'"fixture-v1"'))
        self.assertEqual(self.client.ledger.summary(SCOPE)['request']['day'],3)

    def test_corrupt_full_body_and_symlink_metadata_cannot_use_a_validator(self):
        self.fetch();original=self.client.path.read_bytes();cache=json.loads(original)
        cache['jobs'][0]['text']='invalid truncated body';atomic_json(self.client.path,cache)
        self.now[0]+=601
        with self.assertRaises(ContractError):self.fetch()
        self.client.path.write_bytes(original)
        metadata=self.client._validation(ANTHROPIC).path
        original_check=Path.is_symlink
        with patch.object(Path,'is_symlink',lambda path:path==metadata or original_check(path)):
            with self.assertRaises(InputError):self.fetch()
        self.assertEqual(len(self.wire.calls),1)

    def test_daily_304_creates_original_report_with_two_honest_timestamps(self):
        first=self.fetch();original=self.client.path.read_bytes()
        tasks=PublicTasks(self.workspace,hybrid_client=self.client);self.addCleanup(tasks.close)
        schedule=PublicSchedule(self.workspace,tasks,clock=lambda:self.now[0]);self.addCleanup(schedule.close)
        schedule.configure({'query':query().payload(),'consent':True,'revision':0});schedule.close();schedule._stop.clear()
        self.now[0]+=DAY;self.wire.answer=JSONRepresentation(304,None,'"fixture-v1"');schedule.tick()
        with tasks._lock:worker=tasks._thread
        worker.join(15);self.assertFalse(worker.is_alive());schedule.tick()
        task=tasks.state()['task'];self.assertEqual(task['status'],'completed',task)
        self.assertFalse(task['stale']);self.assertTrue(task['not_modified'])
        self.assertEqual(task['collected_at'],timestamp(first['observed_at']))
        self.assertEqual(task['source_checked_at'],timestamp(self.now[0]))
        self.assertIn('确认目录未变化',task['message']);self.assertEqual(schedule.state()['status'],'scheduled')
        folder=self.workspace.root/'reports'/task['report_id'];audit=json.loads((folder/'public_source.json').read_text(encoding='utf-8'))
        self.assertTrue(audit['not_modified']);self.assertEqual(audit['source_checked_at'],task['source_checked_at'])
        self.assertIn('Cursor',(folder/'requirements.csv').read_text(encoding='utf-8-sig'))
        self.assertFalse((folder/'catalog_changes.json').exists());self.assertEqual(self.client.path.read_bytes(),original)


if __name__=='__main__':unittest.main()
