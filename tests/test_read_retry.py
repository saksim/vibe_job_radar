"""Transient document retry limits, actual worker recovery and backend boundaries."""
from datetime import datetime, timezone
from email.utils import format_datetime
import json
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import test_guided_merge_review as bridge_tests
import test_native_acquisition as native_tests
from test_guided import FakeBackend, fixture_adapter, listing, detail
from vibe_job_radar.guided.adapters import Registry
from vibe_job_radar.guided.contracts import CrawlError
from vibe_job_radar.guided.deferred_resume import can_resume
from vibe_job_radar.guided.rate import RateLedger, RateLimit, Limits
from vibe_job_radar.guided.read_retry import (TransientReadFailure, document_failure,
    budget, retry_delay, read_attempt)
from vibe_job_radar.guided.service import GuidedService
from vibe_job_radar.guided.transport import WireResponse
from vibe_job_radar.workspace import Workspace

URL = 'https://jobs.fixture.test/search?q=test'


class ReadRetryPolicyTests(unittest.TestCase):
    def test_only_explicit_main_read_get_statuses_qualify(self):
        b=SimpleNamespace(auth_mode=False,_read_target=URL)
        for status in (500,501,502,503,504,505,403,429):
            for method in ('GET','HEAD','POST'):
                for kind in ('document','xhr','script'):
                    for main in (True,False):
                        with self.subTest(status=status,method=method,kind=kind,main=main):
                            result=document_failure(b,URL,method,kind,status,main=main)
                            self.assertEqual(isinstance(result,TransientReadFailure),
                                status in {502,503,504} and method=='GET' and kind=='document' and main)
        for auth,target in ((True,URL),(False,None),(False,URL+'/redirect')):
            b.auth_mode,b._read_target=auth,target
            self.assertNotIsInstance(document_failure(b,URL,'GET','document',503,main=True),TransientReadFailure)
        b.auth_mode=False;b._read_target=URL;b._read_redirected=True
        self.assertNotIsInstance(document_failure(b,URL,'GET','document',503,main=True),TransientReadFailure)

    def test_open_scope_is_removed_after_failure_and_for_login(self):
        seen=[]
        class Backend:
            @read_attempt
            def open(self,url,*,authentication=False):
                seen.append(self._read_target)
                raise CrawlError('http_403')
        b=Backend()
        for auth in (False,True):
            with self.assertRaises(CrawlError):b.open(URL,authentication=auth)
            self.assertIsNone(b._read_target)
        self.assertEqual(seen,[URL,None])

    def test_bounded_exponential_jitter_and_longer_retry_after(self):
        self.assertEqual([retry_delay(i,'',1000,random=lambda:0) for i in (0,1)],[5,10])
        self.assertEqual([retry_delay(i,'',1000,random=lambda:1) for i in (0,1)],[7.5,15])
        self.assertEqual(retry_delay(0,'600',1000,random=lambda:0),600)
        date=format_datetime(datetime.fromtimestamp(1900,timezone.utc),usegmt=True)
        self.assertEqual(retry_delay(1,date,1000,random=lambda:0),900)
        with self.assertRaisesRegex(CrawlError,'read_retry_exhausted'):retry_delay(2,'',1000)

    def test_ambiguous_invalid_or_unbounded_deadline_never_authorizes_retry(self):
        for value in ('NaN','inf','-1','1.5','600, 1','x'*129,'999999999','Wed, 23 Sep 2026 19:00:00'):
            with self.subTest(value=value),self.assertRaisesRegex(CrawlError,'read_retry_after_invalid'):
                retry_delay(0,value,1000)

    def test_budget_schema_refuses_reset_by_invalid_values(self):
        self.assertEqual(budget({})['used'],0)
        for value in (None,[],{}, {'version':True,'used':0,'last_status':0},
                      {'version':1,'used':True,'last_status':503},
                      {'version':1,'used':-1,'last_status':503},
                      {'version':1,'used':3,'last_status':503},
                      {'version':1,'used':1,'last_status':403},
                      {'version':1,'used':0,'last_status':503}):
            with self.subTest(value=value),self.assertRaisesRegex(CrawlError,'read_retry_state_invalid'):
                budget({'read_retry':value})

    def test_retry_keeps_original_browser_guard_and_fatal_errors(self):
        failure=TransientReadFailure(URL,503)
        b=SimpleNamespace(alive=lambda:True,auth_mode=False,error=failure.code,wait_error=failure)
        self.assertTrue(can_resume(b))
        for key,value in (('error','http_403'),('wait_error',None),('auth_mode',True),('alive',lambda:False)):
            original=getattr(b,key);setattr(b,key,value)
            self.assertFalse(can_resume(b));setattr(b,key,original)


class ReadRetryBackendTests(unittest.TestCase):
    def test_bridge_retains_typed_error_and_blocks_later_requests(self):
        b=bridge_tests.BrowserReviewTests().backend();b._read_target=URL;b.wait_error=None
        route=Mock();route.request.url=URL;route.request.method='GET'
        route.request.resource_type='document';route.request.frame=b.page.main_frame
        b.wire.fetch.return_value=WireResponse(503,{'retry-after':'30'},b'temporarily unavailable')
        b._route(route)
        self.assertIsInstance(b.wait_error,TransientReadFailure)
        self.assertEqual(b.wait_error.retry_after,'30');self.assertEqual(b.wait_error.url,URL)
        route.fulfill.assert_not_called()
        b._route(route)
        self.assertEqual(b.wire.fetch.call_count,1)

    def test_native_requires_accounted_main_get_and_preserves_error(self):
        for kind,method,role in (('Document','GET','document'),('XHR','POST','business')):
            with self.subTest(kind=kind):
                fixture=native_tests.NativeControllerTests();fixture.setUp();self.addCleanup(fixture.doCleanups);b=fixture.b
                event=fixture.req('/search',method,kind);b._read_target=event['request']['url']
                b._requests[('session',event.get('networkId',event['requestId']))]={'role':role}
                event.update(responseStatusCode=503,responseHeaders=[{'name':'Retry-After','value':'30'}])
                b._paused('session',event)
                self.assertEqual(isinstance(b.wait_error,TransientReadFailure),kind=='Document')
                self.assertTrue(b._halted)
                self.assertEqual(b.error,'read_transient_failure' if kind=='Document' else 'remote_server_error')
                self.assertFalse(any(call.args[1]=='Fetch.continueRequest' for call in b._send.call_args_list))

    def test_longer_shared_cooldown_survives_new_ledger_and_does_not_refund(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'rates.sqlite';ledger=RateLedger(path,clock=lambda:1000)
            ledger.reserve('fixture','request');self.assertEqual(ledger.defer('fixture',600),1600)
            self.assertEqual(ledger.defer('fixture',5),1600)
            restored=RateLedger(path,clock=lambda:1100)
            with self.assertRaises(RateLimit) as error:restored.reserve('fixture','request')
            self.assertEqual(error.exception.next_allowed_at,1600)
            self.assertEqual(restored.summary('fixture')['request']['day'],1)
            ledger.cool('other',1)
            with self.assertRaises(RateLimit) as error:ledger.reserve('other','request')
            self.assertEqual(error.exception.next_allowed_at,1300)  # Existing 429 minimum remains.


class ReadRetryWorkerTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup)
        self.workspace=Workspace(tmp.name);self.now=1000.0;self.failures=0;self.instances=[]
        self.ledger=RateLedger(self.workspace.root/'rates.sqlite',Limits(page_interval=0,request_interval=0),clock=lambda:self.now)
        owner=self
        class Backend(FakeBackend):
            def __init__(self,*args,**kwargs):
                super().__init__(*args,**kwargs);self.error=self.wait_error=None;self.auth_mode=False
                owner.instances.append(self)
            def open(self,url,authentication=False):
                self.auth_mode=authentication;self.error=self.wait_error=None
                owner.ledger.reserve('fixture','page');owner.ledger.reserve('fixture','request')
                self.opens.append(url)
                if owner.failures:
                    owner.failures-=1
                    failure=TransientReadFailure(url,503,'30');self.error=failure.code;self.wait_error=failure
                    raise failure
                self.page=detail(url) if '/job/' in url else listing()
                return self.page
        self.factory=Backend
        self.service=GuidedService(self.workspace,registry=Registry([fixture_adapter()]),backend_factory=Backend,ledger=self.ledger)
        self.addCleanup(self.service.close)

    def create(self):
        self.ident=self.service.create({'platform':'fixture','keyword':'时间序列','roles':['time_series'],
            'max_pages':1,'max_jobs':1,'consent':True,'rights_note':'ARTIFICIAL RETRY TEST'})['id']

    def wait(self,code):
        deadline=time.monotonic()+5
        while time.monotonic()<deadline:
            state=self.service._load(self.ident)
            if not self.service.state()['busy'] and state['code']==code:return state
            time.sleep(.01)
        self.fail('worker did not reach '+code+': '+str(state))

    def test_worker_recovers_once_preserving_budget_and_accounting(self):
        self.failures=1;self.create();state=self.wait('read_retry_wait')
        self.assertEqual(state['read_retry']['used'],1);self.assertEqual(state['next_allowed_at'],1030)
        self.assertEqual(len(self.instances[0].opens),1)
        self.now=1031;state=self.wait('ready')
        self.assertEqual(state['read_retry']['used'],1);self.assertEqual(len(self.instances),1)
        self.assertEqual(len(self.instances[0].opens),2)
        self.assertEqual(self.ledger.summary('fixture')['request']['day'],2)

    def test_second_failure_then_exhaustion_never_schedules_fourth_request(self):
        self.failures=9;self.create();self.wait('read_retry_wait')
        self.now=1031
        deadline=time.monotonic()+5
        while time.monotonic()<deadline:
            state=self.service._load(self.ident)
            if not self.service.state()['busy'] and state.get('read_retry',{}).get('used')==2:break
            time.sleep(.01)
        self.assertEqual(state['read_retry']['used'],2)
        self.now=1062;state=self.wait('read_retry_exhausted')
        self.assertFalse(state['auto_resume']);self.assertEqual(len(self.instances[0].opens),3)
        self.assertEqual(state['next_allowed_at'],1092)
        with self.assertRaises(RateLimit):self.ledger.reserve('fixture','request')
        self.now=9999;time.sleep(.2);self.assertEqual(len(self.instances[0].opens),3)

    def test_closed_window_never_replaced_and_old_budget_survives_explicit_resume(self):
        self.failures=1;self.create();self.wait('read_retry_wait')
        self.instances[0].closed=True;self.wait('automatic_resume_unavailable')
        self.now=1031;time.sleep(.2);self.assertEqual(len(self.instances),1)
        self.service.action({'id':self.ident,'action':'resume'});state=self.wait('ready')
        self.assertEqual(len(self.instances),2);self.assertEqual(state['read_retry']['used'],1)

    def test_restart_is_not_automatic_and_retry_after_is_still_shared(self):
        self.failures=1;self.create();self.wait('read_retry_wait');self.service.close()
        restored=GuidedService(self.workspace,registry=Registry([fixture_adapter()]),backend_factory=self.factory,ledger=self.ledger)
        self.addCleanup(restored.close);self.service=restored
        self.assertEqual(restored._load(self.ident)['read_retry']['used'],1)
        self.assertFalse(restored.state()['jobs'][0]['automatic_resume_available'])
        with self.assertRaises(RateLimit):self.ledger.reserve('fixture','request')
        self.now=1031;restored.action({'id':self.ident,'action':'resume'});state=self.wait('ready')
        self.assertEqual(state['read_retry']['used'],1);self.assertEqual(len(self.instances),2)

    def test_pause_disarms_and_keeps_budget(self):
        self.failures=1;self.create();self.wait('read_retry_wait')
        self.service.action({'id':self.ident,'action':'pause'});self.wait('paused')
        self.now=1031;time.sleep(.2)
        self.assertEqual(len(self.instances[0].opens),1)
        self.assertEqual(self.service._load(self.ident)['read_retry']['used'],1)

    def test_invalid_budget_prevents_backend_creation(self):
        with patch.object(self.service,'_submit'):self.create()
        state=self.service._load(self.ident);state['read_retry']={'version':1,'used':-1,'last_status':503}
        self.service._save(state)
        self.service.action({'id':self.ident,'action':'resume'});self.wait('read_retry_state_invalid')
        self.assertEqual(self.instances,[])

    def test_wrong_document_or_login_action_cannot_spend_retry_budget(self):
        self.create();self.wait('ready');state=self.service._load(self.ident);backend=self.instances[0]
        failure=TransientReadFailure(URL+'/different',503);backend.error=failure.code;backend.wait_error=failure
        for action in ('login','more','search'):
            with self.subTest(action=action),self.assertRaisesRegex(CrawlError,'read_retry_unavailable'):
                self.service._defer_read_retry(state,action,failure)
        self.assertNotIn('read_retry',self.service._load(self.ident))

    def test_exhausted_detail_is_failed_not_pending(self):
        self.create();ready=self.wait('ready')
        state=self.service._load(self.ident)
        state['read_retry']={'version':1,'used':2,'last_status':503}
        self.service._save(state);self.failures=1
        self.service.action({'id':self.ident,'action':'collect','selected':[ready['cards'][0]['id']]})
        state=self.wait('read_retry_exhausted')
        self.assertEqual(state['cards'][0]['status'],'read_retry_exhausted')
        self.assertEqual(state['outcome']['failed'],1)
        self.assertEqual(state['outcome']['pending'],0)
        self.assertEqual(state['read_retry']['used'],2)
        self.assertEqual(state['next_allowed_at'],1030)
