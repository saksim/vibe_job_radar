"""Authored Ashby fixtures, never copied recruiting descriptions."""
import copy
import json
import os
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from test_local_public import payload, query
from test_public_sources import cloudflare_payload
from vibe_job_radar.guided.rate import RateLimit
from vibe_job_radar.local_public import LocalPublicDataClient, parse_board, SCOPE, timestamp
from vibe_job_radar.network import FetchError, JSONRepresentation, SafeHTTP
from vibe_job_radar.public_boards import ANTHROPIC, CLOUDFLARE, CURSOR
from vibe_job_radar.public_contract import ContractError
from vibe_job_radar.public_schedule import PublicSchedule, DAY
from vibe_job_radar.public_tasks import PublicTasks
from vibe_job_radar.workspace import Workspace


def cursor_payload(count=4):
    jobs=[]
    for index in range(count):
        ident=f'aaaaaaaa-0000-4000-8000-{index+1:012d}'
        jobs.append({'id':ident, 'title':'Software Architect 人工测试',
            'location':'San Francisco', 'secondaryLocations':[{'location':'New York'},{'location':'San Francisco'}],
            'isListed':True, 'isRemote':index%2==0,
            'jobUrl':f'https://jobs.ashbyhq.com/cursor/{ident}',
            'applyUrl':f'https://jobs.ashbyhq.com/cursor/{ident}/application',
            'descriptionPlain':f'ARTIFICIAL FIXTURE {index}. NOT MARKET DATA.\nRequirements\n'
                'You must use Cursor for AI-assisted coding and code review. '
                'You must write unit tests and document the architecture. 人工测试，不是实际岗位。',
            'publishedAt':'2020-01-01T00:00:00+00:00'})
    return {'apiVersion':'1','jobs':jobs}


def cursor_query(**kwargs):
    return query(source_scope=(CURSOR.source.key,),**kwargs)


class AshbyTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup)
        self.workspace=Workspace(tmp.name);self.now=[time.time()]
        env=patch.dict(os.environ,{k:v for k,v in os.environ.items() if not k.startswith('VIBE_RADAR_')},clear=True)
        env.start();self.addCleanup(env.stop)
        proxy=patch('urllib.request.getproxies',return_value={});proxy.start();self.addCleanup(proxy.stop)
        self.wire=Mock();self.wire.json.side_effect=self.response
        self.client=LocalPublicDataClient(self.workspace,transport=self.wire,clock=lambda:self.now[0])

    def response(self,url):
        return {CURSOR.api_url:cursor_payload,ANTHROPIC.api_url:payload,CLOUDFLARE.api_url:cloudflare_payload}[url]()

    def fetch(self,**kwargs):
        return self.client.search(cursor_query(**kwargs),consent=True)

    def wait(self,tasks):
        with tasks._lock:thread=tasks._thread
        thread.join(15);self.assertFalse(thread.is_alive())
        return tasks.snapshot()

    def test_explicit_fixed_get_preserves_plain_text_secondary_location_and_remote(self):
        self.assertIsNone(self.client.cached(cursor_query()));self.wire.json.assert_not_called()
        with self.assertRaises(ValueError):self.client.search(cursor_query(),consent=False)
        result=self.fetch(region='New York',remote=True)
        self.wire.json.assert_called_once_with(CURSOR.api_url)
        self.assertEqual(result['available_jobs'],4);self.assertEqual(result['matching_jobs'],2)
        jobs=result['response']['jobs'];self.assertEqual(len(jobs),2)
        self.assertEqual(jobs[0]['text'],cursor_payload()['jobs'][0]['descriptionPlain'])
        self.assertEqual(jobs[0]['location'],'San Francisco; New York')
        self.assertEqual(jobs[0]['collected_at'],timestamp(self.now[0]))
        self.assertNotIn('publishedAt',jobs[0]);self.assertNotIn('applyUrl',jobs[0])
        self.assertFalse(CURSOR.source.distribution_approved)

    def test_unlisted_bodies_and_application_fields_never_enter_cache_or_change_audit(self):
        data=cursor_payload();data['jobs'].append({'isListed':False,'descriptionPlain':'unlisted_secret','id':'hidden'})
        self.wire.json.side_effect=None;self.wire.json.return_value=data
        result=self.fetch();self.assertEqual(result['available_jobs'],4)
        cached=self.client._paths(CURSOR)[0].read_text(encoding='utf-8')
        self.assertNotIn('unlisted_secret',cached);self.assertNotIn('/application',cached)
        self.assertEqual(result['catalog_change']['counts']['current_total'],4)

    def test_strict_version_listing_flag_id_and_content_refuse_partial_acceptance(self):
        for changed in ({'id':'bad'}, {'id':None}, {'isListed':'true'}, {'isListed':None},
                        {'title':''}, {'descriptionPlain':'short'}, {'descriptionPlain':None},
                        {'descriptionPlain':'x'*150001}, {'isRemote':'true'},
                        {'secondaryLocations':None}, {'secondaryLocations':[{'location':''}]},
                        {'secondaryLocations':[{'location':'x'*2001}]},
                        {'location':'x'*2001}):
            data=cursor_payload();data['jobs'][1].update(changed)
            with self.subTest(fields=tuple(changed)),self.assertRaises(ContractError):
                parse_board(data,self.now[0],CURSOR)
        for data in ({'apiVersion':1,'jobs':[]},{'apiVersion':'2','jobs':[]},
                     {'apiVersion':'1','jobs':None},{'apiVersion':'1','jobs':[{'isListed':False}]*10001}):
            with self.assertRaises(ContractError):parse_board(data,self.now[0],CURSOR)

    def test_uuid_and_source_url_must_agree_without_application_or_foreign_path(self):
        raw=cursor_payload()['jobs'][0];original=raw['jobUrl']
        for url in (original.replace('/cursor/','/another-board/'), original+'/application',
                    original+'?token=artificial',original+'#fragment',original.replace('https:','http:'),
                    original.replace('jobs.ashbyhq.com','api.ashbyhq.com'),original.replace(raw['id'],'aaaaaaaa-0000-4000-8000-000000000099')):
            data=cursor_payload();data['jobs'][0]['jobUrl']=url
            with self.subTest(url=url),self.assertRaises(ContractError):parse_board(data,self.now[0],CURSOR)
        data=cursor_payload();data['jobs'][0]['id']=data['jobs'][0]['id'].upper()
        with self.assertRaises(ContractError):parse_board(data,self.now[0],CURSOR)
        data=cursor_payload();data['jobs'].append(copy.deepcopy(data['jobs'][0]))
        with self.assertRaisesRegex(ContractError,'public_duplicate_result'):parse_board(data,self.now[0],CURSOR)

    def test_separate_cache_and_bound_cursor_keep_existing_sources_unchanged(self):
        original={}
        for board in (ANTHROPIC,CLOUDFLARE):
            self.client.search(query(source_scope=(board.source.key,)),consent=True)
            original[board.source.key]=self.client._paths(board)[0].read_bytes();self.now[0]+=31
        first=self.fetch(limit=2);calls=self.wire.json.call_count
        second=self.fetch(limit=2,cursor=first['response']['next_cursor'])
        self.assertEqual(len(second['response']['jobs']),2);self.assertEqual(self.wire.json.call_count,calls)
        self.assertFalse(set(j['id'] for j in first['response']['jobs']) & set(j['id'] for j in second['response']['jobs']))
        with self.assertRaises(ContractError):
            self.client.search(query(cursor=first['response']['next_cursor'],limit=2),consent=True)
        self.assertEqual(self.wire.json.call_count,calls)
        for board in (ANTHROPIC,CLOUDFLARE):
            self.assertEqual(self.client._paths(board)[0].read_bytes(),original[board.source.key])
        self.assertEqual(self.client.ledger.summary(SCOPE)['request']['day'],3)

    def test_conditional_confirmation_preserves_original_body_time_and_cache(self):
        calls=[]
        class Wire:
            def conditional_json(inner,url,*,etag=None):
                calls.append((url,etag))
                return JSONRepresentation(304,None,'"cursor-fixture"') if etag else JSONRepresentation(200,cursor_payload(),'"cursor-fixture"')
        self.client=LocalPublicDataClient(self.workspace,transport=Wire(),clock=lambda:self.now[0])
        first=self.fetch();original=self.client._paths(CURSOR)[0].read_bytes()
        self.now[0]+=601;second=self.fetch()
        self.assertEqual(calls,[(CURSOR.api_url,None),(CURSOR.api_url,'"cursor-fixture"')])
        self.assertTrue(second['not_modified']);self.assertFalse(second['stale'])
        self.assertEqual(second['observed_at'],first['observed_at']);self.assertIsNone(second['catalog_change'])
        self.assertEqual(self.client._paths(CURSOR)[0].read_bytes(),original)

    def test_malformed_refresh_keeps_original_bytes_but_cannot_mask_hard_failure(self):
        self.fetch();original=self.client._paths(CURSOR)[0].read_bytes();self.now[0]+=601
        self.wire.json.side_effect=None;self.wire.json.return_value={'apiVersion':'2','jobs':[]}
        with self.assertRaises(ContractError):self.fetch()
        self.assertEqual(self.client._paths(CURSOR)[0].read_bytes(),original)
        self.assertEqual(self.client.cached(cursor_query())['refresh_error'],'local_public_board_incomplete')

    def test_429_cooldown_is_shared_but_transport_block_is_cleared_for_its_own_host(self):
        wire=SafeHTTP({'boards-api.greenhouse.io','api.ashbyhq.com'})
        calls=[]
        def response(url,*,etag=None):
            calls.append(url)
            if len(calls)==1:
                wire.blocked_hosts.add('boards-api.greenhouse.io')
                raise FetchError('http_429',retry_after=300)
            self.assertNotIn('api.ashbyhq.com' if url==CURSOR.api_url else 'boards-api.greenhouse.io',wire.blocked_hosts)
            return JSONRepresentation(200,self.response(url),None)
        self.client=LocalPublicDataClient(self.workspace,transport=wire,clock=lambda:self.now[0])
        with patch.object(wire,'conditional_json',side_effect=response):
            with self.assertRaises(FetchError):self.client.search(query(),consent=True)
            with self.assertRaises(RateLimit):self.fetch()
            self.assertEqual(calls,[ANTHROPIC.api_url])
            self.now[0]+=301;self.fetch()
            self.assertIn('boards-api.greenhouse.io',wire.blocked_hosts)
            self.now[0]+=31;self.client.search(query(),consent=True)
        self.assertFalse(wire.blocked_hosts);self.assertEqual(len(calls),3)

    def test_task_and_daily_plan_create_original_reports_for_cursor_only(self):
        self.now[0]-=DAY
        tasks=PublicTasks(self.workspace,hybrid_client=self.client);self.addCleanup(tasks.close)
        tasks.search({'query':cursor_query(limit=2).payload(),'consent':True});first=self.wait(tasks)
        self.assertEqual(first['status'],'completed',first)
        report=self.workspace.report(first['report_id'])
        self.assertEqual(report['manifest']['stats']['full_text_job_groups'],2)
        self.assertGreater(report['manifest']['stats']['accepted_positive_requirement_rows'],0)
        previous=(self.workspace.root/'reports'/first['report_id']/'requirements.csv').read_bytes()
        schedule=PublicSchedule(self.workspace,tasks,clock=lambda:self.now[0]);self.addCleanup(schedule.close)
        schedule.configure({'query':cursor_query(limit=2).payload(),'consent':True,'revision':0})
        schedule.close();schedule._stop.clear();self.now[0]+=DAY;schedule.tick();self.wait(tasks);schedule.tick()
        state=schedule.state();self.assertEqual(state['status'],'scheduled');self.assertEqual(len(state['history']),1)
        self.assertEqual(self.workspace.report(state['history'][0]['report_id'])['manifest']['stats']['full_text_job_groups'],2)
        audit=json.loads((self.workspace.root/'reports'/state['history'][0]['report_id']/'public_source.json').read_text(encoding='utf-8'))
        self.assertEqual({job['source'] for job in audit['jobs']},{CURSOR.source.key})
        self.assertEqual((self.workspace.root/'reports'/first['report_id']/'requirements.csv').read_bytes(),previous)
        self.assertEqual(self.wire.json.call_count,2)


if __name__=='__main__':unittest.main()
