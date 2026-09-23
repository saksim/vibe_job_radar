"""Controlled data exercises real task/storage contracts; no live supplier calls."""
from __future__ import annotations

import dataclasses
import json
import socket
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from vibe_job_radar.guided.adapters import DOMAdapter, Registry, builtins
from vibe_job_radar.guided.contracts import CrawlError, PageSnapshot
from vibe_job_radar.guided.rate import Limits, RateLedger, RateLimit
from vibe_job_radar.guided.service import GuidedService
from vibe_job_radar.guided.transport import PinnedTransport, WireResponse, diagnose_host
from vibe_job_radar.workspace import Workspace, InputError
from vibe_job_radar.store import Store


def fixture_adapter():
    return DOMAdapter('fixture', '人工测试站点', ('jobs.fixture.test',),
        'https://jobs.fixture.test/search', 'q', r'^/job/[a-z0-9-]+$',
        'https://jobs.fixture.test/login', ('jobs.fixture.test',),
        username_selectors=('input[name="username"]',), next_selectors=('a[rel="next"]',))


def listing(page=1):
    return PageSnapshot(f'https://jobs.fixture.test/search?page={page}',
        f'<a href="/job/{page}">时间序列算法工程师 {page}</a><a href="/company/x">公司</a>')


def detail(url):
    return PageSnapshot(url, '<html><h1>时间序列算法工程师</h1><div class="job-description">'
        '要求熟练使用 Cursor 进行 AI 辅助编程，编写单元测试并进行代码审查，负责时间序列预测。'
        '</div><script>const captchaLibrary = true;</script></html>')


class FakeBackend:
    instances = []
    def __init__(self, adapter, ledger, cancelled, progress):
        self.adapter, self.cancelled = adapter, cancelled
        self.page, self.closed, self.opens = listing(), False, []
        self.__class__.instances.append(self)
    def open(self, url, authentication=False):
        self.opens.append(url)
        self.page = detail(url+'-real') if '/job/' in url else listing()
        return self.page
    def snapshot(self): return self.page
    def next_page(self):
        if 'page=1' in self.page.url:
            self.page = listing(2); return True
        return False
    def pump(self): pass
    def alive(self): return not self.closed
    def close(self): self.closed = True


class AdapterTests(unittest.TestCase):
    def setUp(self): self.adapter = fixture_adapter()
    def test_three_real_sites_have_distinct_registry_entries(self):
        self.assertEqual({a['key'] for a in builtins().describe()}, {'boss','liepin','51job'})
        self.assertTrue(all(a['certification']=='not_live_verified' for a in builtins().describe()))
    def test_keywords_are_encoded_not_concatenated(self):
        self.assertIn('q=', self.adapter.search_url('时间序列 & a=b'))
        self.assertIn('%26', self.adapter.search_url('时间序列 & a=b'))
    def test_discover_deduplicates_and_ignores_unrelated_links(self):
        p=PageSnapshot(listing().url,listing().html*2+'<a href="https://evil.test/job/1">bad</a>')
        cards=self.adapter.cards(p)
        self.assertEqual(len(cards),1)
        self.assertEqual(cards[0].url,'https://jobs.fixture.test/job/1')
    def test_preserve_identity_query_and_strip_tracker_fragment(self):
        value=self.adapter.accept_url('https://jobs.fixture.test/job/a?jobId=1&utm_source=ad#foo',detail=True)
        self.assertEqual(value,'https://jobs.fixture.test/job/a?jobId=1')
    def test_credential_and_scheme_boundaries(self):
        for u in ('http://jobs.fixture.test/job/1','https://user:pass@jobs.fixture.test/job/1',
                  'https://jobs.fixture.test:444/job/1','https://jobs.fixture.test/job/1?token=secret',
                  'https://jobs.fixture.test.evil.test/job/1'):
            with self.subTest(url=u),self.assertRaises(CrawlError):self.adapter.accept_url(u,detail=True)
    def test_liepin_key_is_only_allowed_on_its_search_path(self):
        adapter=builtins().get('liepin')
        self.assertIn('key=',adapter.accept_url(adapter.search_url('架构师')))
        with self.assertRaises(CrawlError):adapter.accept_url('https://www.liepin.com/job/a.shtml?key=secret',detail=True)
    def test_detail_uses_isolated_text_and_ignores_unused_script_challenge(self):
        value=self.adapter.detail(detail('https://jobs.fixture.test/job/1'))
        self.assertIn('Cursor',value['text']);self.assertNotIn('captchaLibrary',value['text'])
    def test_detail_refuses_real_challenge_and_list(self):
        with self.assertRaises(CrawlError):self.adapter.detail(PageSnapshot('https://jobs.fixture.test/job/1','<h1>安全验证</h1>'))
        with self.assertRaises(CrawlError):self.adapter.detail(listing())
    def test_explicit_registry_rejects_duplicates(self):
        registry=Registry([self.adapter])
        with self.assertRaises(ValueError):registry.register(self.adapter)
        with self.assertRaises(CrawlError):registry.get('missing')


class RateTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.now=100000.0;self.path=Path(self.tmp.name)/'rates.sqlite'
        self.ledger=RateLedger(self.path,clock=lambda:self.now)
    def test_same_site_same_interval_survives_new_ledger(self):
        self.ledger.reserve('boss','page')
        another=RateLedger(self.path,clock=lambda:self.now)
        with self.assertRaises(RateLimit):another.reserve('boss','page')
        self.now+=15;another.reserve('boss','page')
    def test_other_site_has_independent_quota(self):
        self.ledger.reserve('boss','page');self.ledger.reserve('liepin','page')
    def test_hour_and_day_limits(self):
        small=Limits(page_interval=0,pages_hour=2,pages_day=3)
        ledger=RateLedger(self.path,small,clock=lambda:self.now)
        ledger.reserve('boss','page');ledger.reserve('boss','page')
        with self.assertRaises(RateLimit) as error:ledger.reserve('boss','page')
        self.assertEqual(error.exception.code,'hourly_limit')
        self.now+=3601;ledger.reserve('boss','page')
        with self.assertRaises(RateLimit) as error:ledger.reserve('boss','page')
        self.assertEqual(error.exception.code,'daily_limit')
    def test_failed_attempt_is_not_refunded(self):
        self.ledger.reserve('boss','request')
        self.assertEqual(self.ledger.summary('boss')['request']['day'],1)
    def test_cooldown_survives_new_task_and_process(self):
        self.ledger.cool('boss',600)
        with self.assertRaises(RateLimit) as err:self.ledger.reserve('boss','request')
        self.assertEqual(err.exception.code,'cooldown')
        self.now+=601;RateLedger(self.path,clock=lambda:self.now).reserve('boss','request')
    def test_clock_rollback_fails_closed(self):
        self.ledger.reserve('boss','page');self.now-=30
        with self.assertRaises(RateLimit) as err:self.ledger.reserve('boss','page')
        self.assertEqual(err.exception.code,'clock_rollback')
    def test_login_attempt_limit_is_separate(self):
        self.ledger.reserve('boss','login')
        self.ledger.reserve('boss','request')
        with self.assertRaises(RateLimit):self.ledger.reserve('boss','login')


class NetworkTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.ledger=RateLedger(Path(self.tmp.name)/'rates.sqlite',Limits(request_interval=0,page_interval=0))
        self.wire=PinnedTransport(fixture_adapter(),self.ledger,threading.Event())
    def test_fake_ip_diagnosis_is_specific_and_no_http(self):
        with patch('socket.getaddrinfo',return_value=[(2,1,6,'',('198.18.0.24',443))]):
            result=diagnose_host('www.zhipin.com')
        self.assertFalse(result['passed']);self.assertTrue(result['addresses'][0]['fake_ip_range'])
    def test_bridge_refuses_fake_ip_before_connecting(self):
        with patch('socket.getaddrinfo',return_value=[(2,1,6,'',('198.18.0.24',443))]),patch('vibe_job_radar.guided.transport.PinnedHTTPSConnection') as connection:
            with self.assertRaises(CrawlError) as err:self.wire.fetch('https://jobs.fixture.test/job/1')
        self.assertEqual(err.exception.code,'non_public_address');connection.assert_not_called()
    def test_off_domain_and_private_urls_are_refused(self):
        with self.assertRaises(CrawlError):self.wire.fetch('https://127.0.0.1/secret')
        self.assertFalse(self.wire.allowed_resource('https://jobs.fixture.test.evil.test/a'))
    def test_redirect_response_not_followed_by_transport(self):
        with patch('vibe_job_radar.guided.transport.validate_public_url',return_value=('jobs.fixture.test','93.184.216.34','/a')),patch('vibe_job_radar.guided.transport.PinnedHTTPSConnection') as C:
            response=C.return_value.getresponse.return_value;response.status=302
            response.getheaders.return_value=[('Location','https://127.0.0.1/secret')];response.read.return_value=b''
            value=self.wire.fetch('https://jobs.fixture.test/a')
        self.assertEqual(value.status,302);self.assertEqual(C.call_count,1)
    def test_429_persists_cooldown_and_never_retries(self):
        with patch('vibe_job_radar.guided.transport.validate_public_url',return_value=('jobs.fixture.test','93.184.216.34','/a')),patch('vibe_job_radar.guided.transport.PinnedHTTPSConnection') as C:
            response=C.return_value.getresponse.return_value;response.status=429
            response.getheaders.return_value=[('Retry-After','600')]
            with self.assertRaises(CrawlError):self.wire.fetch('https://jobs.fixture.test/a')
        with self.assertRaises(RateLimit):self.ledger.reserve('fixture','request')
        self.assertEqual(C.call_count,1)
    def test_robots_denied_before_page_fetch(self):
        with patch.object(self.wire,'fetch',return_value=WireResponse(200,{},b'User-agent: *\nDisallow: /')) as fetch:
            with self.assertRaises(CrawlError) as err:self.wire.ensure_robots('https://jobs.fixture.test/job/1')
        self.assertEqual(err.exception.code,'robots_denied');self.assertEqual(fetch.call_count,1)


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.workspace=Workspace(Path(self.tmp.name));self.workspace.config['platforms']['fixture']={'label':'人工测试站点','domains':['jobs.fixture.test']}
        self.service=GuidedService(self.workspace,registry=Registry([fixture_adapter()]),backend_factory=FakeBackend)
        self.addCleanup(self.service.close)
    def create(self,**kw):
        data=dict(platform='fixture',keyword='时间序列',roles=['time_series'],consent=True,rights_note='人工测试样本',max_pages=1,max_jobs=5);data.update(kw)
        ident=self.service.create(data)['id'];self.wait();return ident
    def wait(self):
        deadline=time.monotonic()+10
        while self.service.state()['busy'] and time.monotonic()<deadline:time.sleep(.01)
        self.assertFalse(self.service.state()['busy'],'worker failed to finish')
    def job(self,ident):return self.service._load(ident)
    def test_create_discovers_without_user_detail_urls(self):
        ident=self.create();job=self.job(ident)
        self.assertEqual(job['status'],'ready');self.assertEqual(len(job['cards']),1)
        self.assertIn('/job/1',job['cards'][0]['url'])
    def test_pagination_is_bounded_and_deduplicated(self):
        ident=self.create(max_pages=2);self.assertEqual(len(self.job(ident)['cards']),2)
        self.service.action({'id':ident,'action':'more'});self.wait()
        self.assertEqual(self.job(ident)['code'],'list_page_limit')
    def test_requires_actual_consent_and_rejects_oversized_plan(self):
        for change in ({'consent':False},{'max_jobs':21},{'max_pages':0},{'roles':['missing']}):
            with self.subTest(change=change),self.assertRaises((InputError,CrawlError)):self.create(**change)
    def test_selected_job_real_url_saved_and_report_reused(self):
        ident=self.create();ids=[r['id'] for r in self.job(ident)['cards']]
        self.service.action({'id':ident,'action':'collect','selected':ids});self.wait()
        job=self.job(ident);self.assertEqual(job['status'],'completed',job)
        self.assertTrue(job['report_id']);self.assertEqual(job['cards'][0]['status'],'ok')
        self.assertTrue(job['cards'][0]['resolved_url'].endswith('-real'))
        with Store(self.workspace.db) as store:
            records=store.records();self.assertEqual(len(records),1);self.assertEqual(records[0].source_mode,'browser_fetch')
        self.service.action({'id':ident,'action':'collect','selected':ids});self.wait()
        with Store(self.workspace.db) as store:self.assertEqual(len(store.records()),1)
    def test_cannot_submit_arbitrary_detail_url_or_unknown_id(self):
        ident=self.create()
        with self.assertRaises(InputError):self.service.action({'id':ident,'action':'collect','selected':['https://evil.test']})
    def test_password_fields_are_rejected_before_task_mutation(self):
        ident=self.create();secret='UNIT-TEST-PASSWORD-XYZ'
        with self.assertRaises(InputError):
            self.service.action({'id':ident,'action':'login','username':'test-user','password':secret})
        for p in self.workspace.root.rglob('*.json'):self.assertNotIn(secret,p.read_text(encoding='utf-8'))
        self.assertNotIn(secret,json.dumps(self.service.state()))
        self.service.action({'id':ident,'action':'login'});self.wait()
        self.assertEqual(self.job(ident)['authentication'],'manual_pending')
    def test_automatic_credential_action_is_not_exposed(self):
        ident=self.create()
        with self.assertRaises(InputError):self.service.action({'id':ident,'action':'auto_login','username':'u','password':'p'})
    def test_restart_marks_running_task_interrupted_without_restoring_session(self):
        ident=self.create();job=self.job(ident);self.service._save(job,status='running')
        self.service.close()  # A second live process is not a restart.
        other=GuidedService(self.workspace,registry=Registry([fixture_adapter()]),backend_factory=FakeBackend)
        try:
            self.assertEqual(other.state()['jobs'][0]['status'],'interrupted')
            self.assertFalse(other.state()['jobs'][0]['browser_open'])
        finally:other.close()
    def test_install_uses_fixed_command_same_python_no_shell(self):
        import sys
        from vibe_job_radar.guided.browser_install import CommandResult
        from vibe_job_radar.guided.browser_health import environment_report
        with patch.object(self.service, '_installer', return_value=CommandResult(0, 'installed')) as run, \
             patch.object(self.service, '_health_probe', return_value={**environment_report(), 'ready': True, 'message': 'verified'}) as probe:
            self.service.install({'consent':True});self.wait()
        self.assertEqual(run.call_count,2)
        self.assertEqual(run.call_args_list[0].args[0], [sys.executable, '-m', 'pip', 'install', 'playwright>=1.48,<2', 'packaging>=24.2'])
        self.assertEqual(run.call_args_list[1].args[0], [sys.executable, '-m', 'playwright', 'install', 'chromium'])
        probe.assert_called_once()
        self.assertEqual(self.service.state()['installation'],'installed')
    def test_diagnosis_only_queries_selected_registry_host(self):
        with patch('vibe_job_radar.guided.service.diagnose_host',return_value={'passed':True}) as diagnose:
            self.service.diagnose({'platform':'fixture','url':'https://127.0.0.1/'})
        diagnose.assert_called_once_with('jobs.fixture.test')
    def test_stop_closes_backend_and_preserves_records(self):
        ident=self.create();self.service.action({'id':ident,'action':'stop'});self.wait()
        self.assertEqual(self.job(ident)['status'],'stopped');self.assertFalse(self.service.state()['jobs'][0]['browser_open'])
    def test_completed_resume_is_noop_not_a_new_search(self):
        ident=self.create(); job=self.job(ident)
        self.service._save(job,'completed',status='completed',phase='report')
        before=list(FakeBackend.instances[-1].opens)
        self.service.action({'id':ident,'action':'resume'})
        self.assertFalse(self.service.state()['busy'])
        self.assertEqual(FakeBackend.instances[-1].opens,before)
    def test_task_controls_do_not_cancel_dependency_install(self):
        ident=self.create()
        self.service._busy=True; self.service._active=None
        try:
            with self.assertRaises(InputError): self.service.action({'id':ident,'action':'stop'})
            self.assertIsNone(self.service._stop_ident)
        finally: self.service._busy=False
    def test_second_task_cannot_be_created_while_busy(self):
        with self.service._lock:self.service._busy=True
        try:
            with self.assertRaises(InputError):self.create()
        finally:self.service._busy=False

class GuidedHTTPTests(unittest.TestCase):
    def setUp(self):
        import http.client
        from vibe_job_radar.workbench import LocalServer
        self.http=http.client
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.server=LocalServer(Workspace(self.tmp.name))
        self.thread=threading.Thread(target=self.server.serve_forever,kwargs={'poll_interval':.01},daemon=True);self.thread.start()
        self.addCleanup(self.finish)
    def finish(self):
        self.server.shutdown();self.server.server_close();self.thread.join(timeout=5)
    def call(self,path,payload=None,authorized=True,origin=None):
        conn=self.http.HTTPConnection('127.0.0.1',self.server.server_address[1],timeout=5)
        headers={'X-Radar-Token':self.server.token} if authorized else {}
        if origin:headers['Origin']=origin
        if payload is not None:headers['Content-Type']='application/json'
        conn.request('POST' if payload is not None else 'GET',path,
                     body=json.dumps(payload).encode() if payload is not None else None,headers=headers)
        result=conn.getresponse();code=result.status;body=result.read();conn.close();return code,body
    def test_new_shell_and_packaged_script_available(self):
        self.assertEqual(self.call('/guided',authorized=False)[0],200)
        code,script=self.call('/guided.js',authorized=False)
        self.assertEqual(code,200);self.assertNotIn(b'innerHTML',script)
    def test_state_requires_local_token(self):
        self.assertEqual(self.call('/api/guided/state',authorized=False)[0],403)
        self.assertEqual(self.call('/api/guided/state')[0],200)
    def test_cross_origin_install_cannot_run(self):
        with patch('vibe_job_radar.guided.service.subprocess.run') as run:
            self.assertEqual(self.call('/api/guided/install',{'consent':True},origin='https://evil.test')[0],403)
        run.assert_not_called()
    def test_no_install_without_confirmation(self):
        self.assertEqual(self.call('/api/guided/install',{})[0],400)
    def test_invalid_arbitrary_platform_does_not_query_dns(self):
        with patch('vibe_job_radar.guided.service.diagnose_host') as resolve:
            # No requests are issued by the service for an unknown adapter.
            code,body=self.call('/api/guided/diagnose',{'platform':'http://127.0.0.1'})
        self.assertNotEqual(code,200)
        resolve.assert_not_called()


if __name__=='__main__':
    unittest.main()
