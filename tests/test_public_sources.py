"""Artificial catalogs exercise two fixed sources; never live market fixtures."""
import copy
from dataclasses import replace
import json
import os
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from test_local_public import payload, query
from vibe_job_radar.guided.rate import RateLimit
from vibe_job_radar.local_public import LocalPublicDataClient, parse_board
from vibe_job_radar.network import FetchError
from vibe_job_radar.public_boards import ANTHROPIC, CLOUDFLARE
from vibe_job_radar.public_contract import ContractError, validate_batch
from vibe_job_radar.public_schedule import DAY, PublicSchedule
from vibe_job_radar.public_tasks import PublicTasks
from vibe_job_radar.workspace import Workspace


def cloudflare_payload(count=3):
    value=payload(count)
    for job in value['jobs']:
        job['absolute_url']=f"https://boards.greenhouse.io/cloudflare/jobs/{job['id']}?gh_jid={job['id']}"
        job['company_name']='Cloudflare'
    return value


def cloudflare_query(**kw):
    return query(source_scope=(CLOUDFLARE.source.key,),**kw)


class PublicSourcesTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup)
        self.workspace=Workspace(tmp.name);self.now=[time.time()]
        env=patch.dict(os.environ,{k:v for k,v in os.environ.items() if not k.startswith('VIBE_RADAR_')},clear=True)
        env.start();self.addCleanup(env.stop)
        proxies=patch('urllib.request.getproxies',return_value={});proxies.start();self.addCleanup(proxies.stop)
        self.transport=Mock();self.transport.json.side_effect=self.response
        self.client=LocalPublicDataClient(self.workspace,transport=self.transport,clock=lambda:self.now[0])

    def response(self,url):
        self.assertIn(url,(ANTHROPIC.api_url,CLOUDFLARE.api_url))
        return cloudflare_payload() if url==CLOUDFLARE.api_url else payload()

    def wait(self,tasks):
        with tasks._lock:worker=tasks._thread
        worker.join(15);self.assertFalse(worker.is_alive())
        return tasks.snapshot()

    def test_fixed_source_requires_selection_and_never_acquires_on_startup(self):
        self.assertEqual(list(self.client.registry),[ANTHROPIC.source.key,CLOUDFLARE.source.key])
        self.assertIsNone(self.client.cached(cloudflare_query()))
        for sources in ((ANTHROPIC.source.key,CLOUDFLARE.source.key),('unknown_board',)):
            with self.assertRaises(ContractError):self.client.search(query(source_scope=sources),consent=True)
        with self.assertRaises(ValueError):self.client.search(cloudflare_query(),consent=False)
        self.transport.json.assert_not_called()
        self.client.registry[CLOUDFLARE.source.key]=replace(CLOUDFLARE.source,local_access_approved=False)
        with self.assertRaises(ContractError):self.client.search(cloudflare_query(),consent=True)
        self.transport.json.assert_not_called()

    def test_cloudflare_get_carries_no_query_and_retains_actual_company_and_source(self):
        result=self.client.search(cloudflare_query(query='Cursor',region='London'),consent=True)
        self.transport.json.assert_called_once_with(CLOUDFLARE.api_url)
        self.assertEqual(result['available_jobs'],3);self.assertEqual(result['matching_jobs'],2)
        self.assertEqual({j['company'] for j in result['response']['jobs']},{'Cloudflare'})
        self.assertEqual({j['source'] for j in result['response']['jobs']},{CLOUDFLARE.source.key})
        self.assertFalse(CLOUDFLARE.source.distribution_approved)
        with self.assertRaises(ContractError):
            validate_batch(result['response'],cloudflare_query(),self.client.registry)
        self.assertFalse(self.client.path.exists())

    def test_cloudflare_rejects_wrong_company_board_id_domain_or_unreviewed_query(self):
        good=cloudflare_payload();url=good['jobs'][0]['absolute_url']
        changes=[{'company_name':'Anthropic'},{'company_name':None},
            {'absolute_url':url.replace('cloudflare','anthropic')},
            {'absolute_url':url.replace('boards.greenhouse.io','other.example')},
            {'absolute_url':url.replace('/80000?','/80001?')},
            {'absolute_url':url.replace('gh_jid=80000','gh_jid=80001')},
            {'absolute_url':url+'&gh_jid=80000'}, {'absolute_url':url+'&utm_source=unreviewed'},
            {'absolute_url':url+'&token=SECRET'}, {'absolute_url':url.split('?')[0]}]
        for change in changes:
            value=copy.deepcopy(good);value['jobs'][0].update(change)
            with self.subTest(change=change),self.assertRaises(ContractError):parse_board(value,self.now[0],CLOUDFLARE)
        del good['jobs'][0]['company_name']
        with self.assertRaises(ContractError):parse_board(good,self.now[0],CLOUDFLARE)

    def test_source_caches_and_cursors_stay_separate_across_restart(self):
        first=self.client.search(query(limit=1),consent=True);anthropic_bytes=self.client.path.read_bytes()
        cursor=first['response']['next_cursor']
        with self.assertRaises(ContractError):self.client.search(cloudflare_query(limit=1,cursor=cursor),consent=True)
        self.now[0]+=31
        other=self.client.search(cloudflare_query(limit=1),consent=True)
        self.assertEqual(self.client.path.read_bytes(),anthropic_bytes)
        restarted=LocalPublicDataClient(self.workspace,transport=self.transport,clock=lambda:self.now[0])
        for q in (query(limit=1,cursor=other['response']['next_cursor']),cloudflare_query(limit=1,cursor=cursor)):
            with self.assertRaises(ContractError):restarted.search(q,consent=True)
        second=restarted.search(cloudflare_query(limit=1,cursor=other['response']['next_cursor']),consent=True)
        self.assertTrue(second['cache_reused']);self.assertEqual(second['network_requests'],0)
        self.assertEqual(second['response']['jobs'][0]['company'],'Cloudflare')
        self.assertNotEqual(second['response']['jobs'][0]['id'],other['response']['jobs'][0]['id'])
        self.assertEqual(self.transport.json.call_count,2)

    def test_switching_source_cannot_reset_shared_interval_or_persisted_429(self):
        self.client.search(query(),consent=True)
        with self.assertRaises(RateLimit):self.client.search(cloudflare_query(),consent=True)
        self.assertEqual(self.transport.json.call_count,1)
        self.now[0]+=31;self.transport.json.side_effect=FetchError('http_429',retry_after=900)
        with self.assertRaises(FetchError):self.client.search(cloudflare_query(),consent=True)
        self.now[0]+=601
        restarted=LocalPublicDataClient(self.workspace,transport=self.transport,clock=lambda:self.now[0])
        old=restarted.search(query(),consent=True)
        self.assertTrue(old['stale']);self.assertTrue(old['refresh_error'])
        with self.assertRaises(RateLimit):restarted.search(cloudflare_query(),consent=True)
        self.assertEqual(self.transport.json.call_count,2)

    def test_failure_is_retained_for_that_source_without_replacing_another_cache(self):
        cf=self.client.search(cloudflare_query(limit=1),consent=True)
        self.now[0]+=31;self.client.search(query(),consent=True)
        old=self.client.path.read_bytes();self.now[0]+=570
        self.transport.json.side_effect=FetchError('http_403')
        with self.assertRaises(FetchError):self.client.search(cloudflare_query(),consent=True)
        self.assertEqual(self.client.path.read_bytes(),old)
        self.assertIsNone(self.client.cached(query())['refresh_error'])
        self.assertEqual(self.client.cached(cloudflare_query())['refresh_error'],'http_403')
        with self.assertRaises(FetchError):
            self.client.search(cloudflare_query(limit=1,cursor=cf['response']['next_cursor']),consent=True)
        self.assertEqual(self.transport.json.call_count,3)

    def test_each_catalog_compares_only_its_own_complete_observations(self):
        self.client.search(query(),consent=True);self.now[0]+=31
        first=self.client.search(cloudflare_query(),consent=True)
        self.assertEqual(first['catalog_change']['status'],'baseline')
        changed=cloudflare_payload(4);changed['jobs'][0]['content']+='<p>Changed artificial requirement.</p>'
        self.transport.json.side_effect=lambda url:changed if url==CLOUDFLARE.api_url else payload()
        self.now[0]+=601
        cf=self.client.search(cloudflare_query(),consent=True)['catalog_change']
        self.assertEqual(cf['source'],CLOUDFLARE.source.key)
        self.assertEqual((cf['counts']['added'],cf['counts']['updated'],cf['counts']['missing']),(1,1,0))
        self.now[0]+=31
        original=self.client.search(query(),consent=True)['catalog_change']
        self.assertEqual(original['source'],ANTHROPIC.source.key)
        self.assertEqual(original['counts']['unchanged'],3)
        self.assertEqual(original['counts']['added'],0)

    def test_title_match_precedes_body_boilerplate_without_losing_local_pagination(self):
        value=cloudflare_payload(2)
        value['jobs'][0]['title']='Account Executive artificial fixture'
        value['jobs'][0]['content']+='<p>Work alongside Architects, artificial fixture.</p>'
        value['jobs'][1]['title']='Solutions Architect artificial fixture'
        self.transport.json.side_effect=None;self.transport.json.return_value=value
        first=self.client.search(cloudflare_query(limit=1),consent=True)
        self.assertEqual(first['matching_jobs'],2)
        self.assertEqual(first['response']['jobs'][0]['id'],'80001')
        second=self.client.search(cloudflare_query(limit=1,cursor=first['response']['next_cursor']),consent=True)
        self.assertEqual(second['response']['jobs'][0]['id'],'80000')
        self.assertEqual(second['response']['next_cursor'],'')
        self.transport.json.assert_called_once_with(CLOUDFLARE.api_url)

    def test_new_source_preserves_old_plan_binding_and_produces_original_audited_report(self):
        tasks=PublicTasks(self.workspace,hybrid_client=self.client);self.addCleanup(tasks.close)
        before=tasks._binding('search',query().payload())
        with patch.dict(self.client.registry,{ANTHROPIC.source.key:ANTHROPIC.source},clear=True):
            self.assertEqual(tasks._binding('search',query().payload()),before)
        tasks.search({'consent':True,'query':cloudflare_query().payload()});task=self.wait(tasks)
        self.assertEqual(task['status'],'completed');self.assertIn('Cloudflare',task['message'])
        self.assertNotIn('Anthropic',task['message'])
        folder=self.workspace.root/'reports'/task['report_id']
        audit=json.loads((folder/'public_source.json').read_text(encoding='utf-8'))
        self.assertEqual(audit['source_scope'],[CLOUDFLARE.source.key])
        self.assertEqual(audit['catalog_change']['source'],CLOUDFLARE.source.key)
        self.assertTrue(all(j['company']=='Cloudflare' for j in audit['jobs']))
        self.assertIn('Cursor',(folder/'requirements.csv').read_text(encoding='utf-8-sig'))
        manifest=json.loads((folder/'run_manifest.json').read_text(encoding='utf-8'))
        self.assertIn('public_source.json',manifest['output_files_sha256'])

    def test_cloudflare_daily_plan_is_offline_until_due_and_keeps_original_source(self):
        tasks=PublicTasks(self.workspace,hybrid_client=self.client);self.addCleanup(tasks.close)
        schedule=PublicSchedule(self.workspace,tasks,clock=lambda:self.now[0]);self.addCleanup(schedule.close)
        saved=schedule.configure({'query':cloudflare_query().payload(),'consent':True,'revision':0})
        self.assertEqual(saved['query']['source_scope'],[CLOUDFLARE.source.key])
        self.transport.json.assert_not_called()
        self.now[0]+=DAY;schedule.tick();task=self.wait(tasks);schedule.tick()
        self.assertEqual(task['status'],'completed')
        self.assertEqual(schedule.state()['history'][0]['report_id'],task['report_id'])
        self.transport.json.assert_called_once_with(CLOUDFLARE.api_url)


if __name__=='__main__':unittest.main()
