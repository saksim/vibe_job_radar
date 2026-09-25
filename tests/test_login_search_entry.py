"""Opted-in query-free native login return; all controls are synthetic."""
from dataclasses import replace
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch

from vibe_job_radar.guided.adapters import Registry,builtins
from vibe_job_radar.guided.contracts import CrawlError,PageSnapshot,PageSnapshotChanged
from vibe_job_radar.guided.login_return import LoginReturnManager

ADAPTER=builtins().get('liepin')
ACCOUNT='#header-quick-menu-user-info:visible'
FIELD='input[type="text"][placeholder="搜索职位、公司"]:visible'
LOGIN='#header-quick-menu-login:visible'
PASSWORD='input[data-nick="login-pwd"]:visible'


class LoginSearchEntryTests(unittest.TestCase):
    def setUp(self):
        self.now=0
        self.manager=LoginReturnManager(clock=lambda:self.now,timeout=60)
        self.counts={ACCOUNT:1,FIELD:1,LOGIN:0,PASSWORD:0}
        self.controls={key:Mock() for key in (*self.counts,'body')}
        for key in self.counts:
            self.controls[key].count.side_effect=lambda key=key:self.counts[key]
        self.controls['body'].inner_text.return_value='合成账号区域与搜索入口'
        self.page=Mock(url=ADAPTER.search_base,ident='synthetic-target',navigation=3)
        self.page.is_closed.return_value=False
        self.page.locator.side_effect=lambda key:self.controls[key]
        self.snapshot=PageSnapshot(ADAPTER.search_base,'<h1>合成入口</h1>',business_required=True)
        self.backend=Mock(adapter=ADAPTER,page=self.page,auth_mode=True)
        self.backend.snapshot.return_value=self.snapshot
        self.state=dict(id='task',platform='liepin',backend='native',keyword='原查询',
            search_url=ADAPTER.search_url('原查询'),query_scope_version=1,
            authentication='manual_pending',status='waiting_manual',phase='search',
            cards=[],selection=[],auto_continue_after_login=True)
        self.service=SimpleNamespace(_lock=threading.RLock(),_busy=False,
            _shutdown=threading.Event(),_cancel=threading.Event(),_backends={'task':self.backend},
            registry=Registry([ADAPTER]),_load=lambda _:self.state,
            _save=lambda state,**kw:state.update(kw),_submit=Mock())
        self.manager.arm(self.state,self.backend)

    def tick(self):
        self.manager.tick(self.service);self.now+=1

    def signature(self):
        from vibe_job_radar.guided.liepin_form import matching_search_entry_signature
        return matching_search_entry_signature(self.backend,self.state,self.snapshot)

    def test_two_stable_account_and_search_controls_enqueue_one_original_search(self):
        self.tick();self.service._submit.assert_not_called()
        self.tick();self.tick()
        self.service._submit.assert_called_once()
        args=self.service._submit.call_args.args
        self.assertEqual(args[:2],('resume_returned_search','task'))
        self.assertIs(args[2].backend,self.backend)
        self.assertEqual(args[2].expected_url,self.state['search_url'])
        self.assertEqual(args[2].keyword,self.state['keyword'])
        self.assertEqual(self.state['authentication'],'manual_pending')
        self.backend.open.assert_not_called()
        self.backend.password_login.assert_not_called()
        self.controls[FIELD].fill.assert_not_called()

    def test_each_missing_or_ambiguous_control_never_submits(self):
        for key,value in ((ACCOUNT,0),(ACCOUNT,2),(FIELD,0),(FIELD,2),(LOGIN,1),(PASSWORD,1)):
            with self.subTest(key=key,value=value):
                previous=self.counts[key];self.counts[key]=value
                self.assertIsNone(self.signature())
                self.counts[key]=previous

    def test_task_consent_scope_existing_results_or_backend_cannot_be_replaced(self):
        for changes in ({'auto_continue_after_login':False},{'backend':'bridge'},
                        {'query_scope_version':0},{'authentication':'not_checked'},
                        {'phase':'collect'},{'cards':[{'id':'existing'}]},
                        {'selection':['existing']},{'effective_search':{'key':'原查询'}},
                        {'search_url':self.state['search_url']+'&city=020'}):
            with self.subTest(changes=changes):
                saved=dict(self.state);self.state.update(changes)
                self.assertIsNone(self.signature());self.state.clear();self.state.update(saved)

    def test_plain_dom_other_url_or_ended_authentication_never_submits(self):
        self.snapshot=replace(self.snapshot,business_required=False)
        self.assertIsNone(self.signature())
        self.snapshot=replace(self.snapshot,business_required=True,url=ADAPTER.search_url('其他'))
        self.assertIsNone(self.signature())
        self.snapshot=replace(self.snapshot,url=ADAPTER.search_base)
        self.backend.auth_mode=False
        self.assertIsNone(self.signature())

    def test_navigation_between_observations_requires_two_fresh_reads(self):
        self.tick();self.page.navigation+=1
        self.tick();self.service._submit.assert_not_called()
        self.tick();self.service._submit.assert_called_once()

    def test_navigation_during_control_read_invalidates_the_snapshot(self):
        def changed():self.page.navigation+=1;return 1
        self.controls[FIELD].count.side_effect=changed
        with self.assertRaises(PageSnapshotChanged):self.signature()

    def test_challenge_or_native_refusal_is_not_cleared(self):
        self.controls['body'].inner_text.return_value='请完成安全验证'
        with self.assertRaises(CrawlError) as error:self.signature()
        self.assertEqual(error.exception.code,'manual_required')
        self.backend._check_error.side_effect=CrawlError('http_403')
        with self.assertRaises(CrawlError) as error:self.signature()
        self.assertEqual(error.exception.code,'http_403')

    def test_cancellation_during_controls_wins(self):
        def cancel():self.service._cancel.set();return 1
        self.controls[FIELD].count.side_effect=cancel
        self.tick();self.tick()
        self.service._submit.assert_not_called()

    def test_positive_entry_does_not_extend_original_wait(self):
        self.tick();self.now=60;self.tick()
        self.assertEqual(self.state['login_continuation'],'timed_out')
        self.service._submit.assert_not_called()

    def make_runner(self):
        from vibe_job_radar.guided.service import GuidedService
        runner=GuidedService.__new__(GuidedService)
        runner.registry=Registry([ADAPTER]);runner._backends={'task':self.backend}
        runner._cancel=threading.Event();runner._selected_records=Mock()
        runner._save=lambda state,code=None,**kw:state.update(kw,**({'code':code} if code else {}))
        runner._backend=Mock(side_effect=AssertionError('must use existing owner'))
        runner._gather=Mock(side_effect=lambda state,*a,**kw:state.update(status='ready'))
        runner._auto_collect_ready=Mock();runner._trace_for=Mock(return_value=None)
        return runner

    def returned(self):
        from vibe_job_radar.guided.login_return import ReturnedSearch
        return ReturnedSearch(self.state['search_url'],self.state['keyword'],self.signature(),self.backend)

    def run_return(self,runner,handoff,submit):
        with patch('vibe_job_radar.guided.service.ensure_compatible'),\
                patch('vibe_job_radar.guided.service.retry_budget'),\
                patch('vibe_job_radar.guided.liepin_form.submit_search',submit):
            runner._run('resume_returned_search',self.state,handoff)

    def test_owner_revalidates_then_submits_once_and_uses_original_gather(self):
        runner=self.make_runner();handoff=self.returned();submit=Mock()
        self.run_return(runner,handoff,submit)
        submit.assert_called_once_with(self.backend,'原查询')
        runner._gather.assert_called_once_with(self.state,self.backend,ADAPTER)
        runner._auto_collect_ready.assert_called_once_with(self.state,self.backend,ADAPTER)
        runner._backend.assert_not_called()
        self.backend.open.assert_not_called()
        self.assertEqual(self.state['login_continuation'],'resumed_search')
        self.assertEqual(self.state['authentication'],'user_resumed')

    def test_changed_control_or_navigation_before_execution_never_inputs(self):
        for change in ('control','navigation','keyword','owner','closed','consent','filters'):
            with self.subTest(change=change):
                self.setUp();runner=self.make_runner();handoff=self.returned();submit=Mock()
                if change=='control':self.counts[ACCOUNT]=0
                elif change=='navigation':self.page.navigation+=1
                elif change=='keyword':self.state['keyword']='变化'
                elif change=='owner':runner._backends['task']=Mock()
                elif change=='closed':self.backend.alive.return_value=False
                elif change=='consent':self.state['auto_continue_after_login']=False
                else:self.state['effective_search']={'key':'原查询'}
                with self.assertRaises(CrawlError) as error:self.run_return(runner,handoff,submit)
                self.assertEqual(error.exception.code,'login_return_changed')
                submit.assert_not_called();runner._backend.assert_not_called()

    def test_quota_or_submission_failure_never_retries_or_gathers(self):
        runner=self.make_runner();handoff=self.returned();submit=Mock(side_effect=CrawlError('daily_limit'))
        with self.assertRaises(CrawlError):self.run_return(runner,handoff,submit)
        submit.assert_called_once();runner._gather.assert_not_called()

    def test_handoff_repr_contains_no_query_or_backend_data(self):
        value=repr(self.returned())
        self.assertNotIn('原查询',value);self.assertNotIn(self.state['search_url'],value)

    def test_empty_handoff_and_no_current_entry_cannot_match(self):
        runner=self.make_runner();handoff=replace(self.returned(),signature=None)
        self.counts[ACCOUNT]=0;submit=Mock()
        with self.assertRaises(CrawlError) as error:self.run_return(runner,handoff,submit)
        self.assertEqual(error.exception.code,'login_return_changed');submit.assert_not_called()
