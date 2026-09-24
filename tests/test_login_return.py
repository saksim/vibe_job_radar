"""Local return-page continuation; no credentials or live recruitment requests."""
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from vibe_job_radar.guided.adapters import Registry, builtins
from vibe_job_radar.guided.contracts import CrawlError, PageSnapshot, PageSnapshotChanged
from vibe_job_radar.guided.login_return import LoginReturnManager, matching_list_signature
from vibe_job_radar.guided.service import GuidedService
from vibe_job_radar.workspace import Workspace, InputError
from test_liepin_detail_pipeline import ReturnedPageBackend

ADAPTER = builtins().get('liepin')
SEARCH = ADAPTER.search_url('时间序列算法工程师')
HTML = '<a href="/job/123.shtml">时间序列算法工程师</a>'


class MatchingReturnTests(unittest.TestCase):
    def test_exact_task_list(self):
        self.assertTrue(matching_list_signature(ADAPTER, SEARCH, PageSnapshot(SEARCH, HTML)))

    def test_tracking_only_does_not_change_query(self):
        self.assertTrue(matching_list_signature(ADAPTER, SEARCH, PageSnapshot(SEARCH+'&utm_source=local', HTML)))

    def test_different_keyword_is_not_consumed(self):
        self.assertIsNone(matching_list_signature(ADAPTER, SEARCH, PageSnapshot(ADAPTER.search_url('架构师'), HTML)))

    def test_changed_filter_is_not_the_original_query(self):
        self.assertIsNone(matching_list_signature(ADAPTER, SEARCH, PageSnapshot(SEARCH+'&city=010', HTML)))

    def test_duplicate_query_parameter_is_not_accepted(self):
        self.assertIsNone(matching_list_signature(ADAPTER, SEARCH, PageSnapshot(SEARCH+'&key=other', HTML)))

    def test_detail_recommendations_are_not_a_returned_list(self):
        self.assertIsNone(matching_list_signature(ADAPTER, SEARCH, PageSnapshot('https://www.liepin.com/job/123.shtml', HTML)))

    def test_empty_page_is_not_session_success(self):
        self.assertIsNone(matching_list_signature(ADAPTER, SEARCH, PageSnapshot(SEARCH, '<h1>登录成功</h1>')))

    def test_challenge_is_not_skipped(self):
        with self.assertRaises(CrawlError):
            matching_list_signature(ADAPTER, SEARCH, PageSnapshot(SEARCH, '请完成安全验证'+HTML))


class ReturnWatcherTests(unittest.TestCase):
    def setUp(self):
        self.now = 0.0
        self.manager = LoginReturnManager(clock=lambda:self.now, timeout=10)
        self.backend = Mock()
        self.backend.snapshot.return_value = PageSnapshot(SEARCH, HTML)
        self.state = {'id':'task', 'platform':'liepin', 'search_url':SEARCH, 'status':'waiting_manual',
                      'authentication':'manual_pending', 'phase':'search', 'auto_continue_after_login':True}
        self.service = SimpleNamespace(_lock=threading.RLock(),_busy=False,_shutdown=threading.Event(),
            _cancel=threading.Event(),_backends={'task':self.backend},registry=Registry([ADAPTER]),
            _load=lambda _:self.state,_save=lambda state,**kw:state.update(kw),_submit=Mock())
        self.manager.arm(self.state,self.backend)

    def tick(self):
        self.manager.tick(self.service)
        self.now += 1.0

    def test_two_stable_reads_then_exactly_one_capture(self):
        self.tick();self.service._submit.assert_not_called()
        self.tick();self.tick()
        self.service._submit.assert_called_once_with('capture','task')
        self.backend.open.assert_not_called()
        self.assertEqual(self.state['authentication'],'manual_pending')

    def test_pending_collection_resumes_selection_instead_of_new_search(self):
        self.state['phase']='collect'
        self.tick();self.tick()
        self.service._submit.assert_called_once_with('resume','task')

    def test_opt_in_required(self):
        self.state['auto_continue_after_login']=False
        self.assertFalse(self.manager.arm(self.state,self.backend))
        self.tick();self.backend.snapshot.assert_not_called()

    def test_busy_service_cannot_enqueue(self):
        self.service._busy=True
        self.tick();self.tick();self.backend.snapshot.assert_not_called()

    def test_stop_disarms_before_snapshot(self):
        self.service._cancel.set()
        self.tick();self.backend.snapshot.assert_not_called()
        self.assertEqual(self.state['login_continuation'],'cancelled')

    def test_cancellation_during_dom_read_wins(self):
        self.tick()
        def snapshot():
            self.service._cancel.set()
            return PageSnapshot(SEARCH,HTML)
        self.backend.snapshot.side_effect=snapshot
        self.tick();self.service._submit.assert_not_called()

    def test_error_after_pause_does_not_overwrite_new_state(self):
        def snapshot():
            self.manager.disarm('task')
            self.state.update(status='paused', login_continuation='off')
            raise CrawlError('browser_closed')
        self.backend.snapshot.side_effect=snapshot
        self.tick()
        self.assertEqual(self.state['status'],'paused')
        self.assertEqual(self.state['login_continuation'],'off')
        self.service._submit.assert_not_called()

    def test_cancel_while_observer_errors_never_changes_task_status(self):
        def snapshot():
            self.service._cancel.set()
            self.state['status']='paused'
            raise RuntimeError('synthetic')
        self.backend.snapshot.side_effect=snapshot
        self.tick()
        self.assertEqual(self.state['status'],'paused')
        self.assertNotIn('login_continuation',self.state)

    def test_replaced_browser_does_not_inherit_watcher(self):
        self.service._backends['task']=Mock()
        self.tick();self.backend.snapshot.assert_not_called()

    def test_timeout_never_navigates_or_logs_in(self):
        self.now=10
        self.tick();self.backend.snapshot.assert_not_called()
        self.service._submit.assert_not_called()
        self.assertEqual(self.state['login_continuation'],'timed_out')

    def test_challenge_then_return_needs_two_fresh_observations(self):
        self.tick()
        self.backend.snapshot.side_effect=CrawlError('manual_required')
        self.tick()
        self.backend.snapshot.side_effect=None
        self.tick();self.service._submit.assert_not_called()
        self.tick();self.service._submit.assert_called_once()

    def test_network_failure_requires_attention_not_automatic_retry(self):
        self.backend.snapshot.side_effect=CrawlError('http_403')
        self.tick();self.tick()
        self.assertEqual(self.state['login_continuation'],'needs_attention')
        self.backend.snapshot.assert_called_once()
        self.service._submit.assert_not_called()

    def test_navigation_during_local_read_keeps_deadline_and_requires_two_fresh_observations(self):
        self.tick();deadline=self.manager._watches['task'].expires
        self.backend.snapshot.side_effect=PageSnapshotChanged();self.tick()
        self.assertEqual(self.manager._watches['task'].expires,deadline)
        self.backend.snapshot.side_effect=None
        self.tick();self.service._submit.assert_not_called()
        self.tick();self.service._submit.assert_called_once_with('capture','task')
        self.backend.open.assert_not_called()

    def test_repeated_changing_page_expires_without_navigation_or_extending_wait(self):
        self.backend.snapshot.side_effect=PageSnapshotChanged()
        for _ in range(11):self.tick()
        self.assertEqual(self.state['login_continuation'],'timed_out')
        self.assertNotIn('task',self.manager._watches)
        self.assertEqual(self.backend.snapshot.call_count,10)
        self.service._submit.assert_not_called();self.backend.open.assert_not_called()

    def test_unclassified_page_not_ready_still_requires_attention(self):
        self.backend.snapshot.side_effect=CrawlError('page_not_ready');self.tick();self.tick()
        self.assertEqual(self.state['login_continuation'],'needs_attention')
        self.assertEqual(self.state['code'],'page_not_ready')
        self.backend.snapshot.assert_called_once();self.service._submit.assert_not_called()

    def test_rejected_password_is_reported_and_never_retried(self):
        self.backend.snapshot.side_effect=CrawlError('login_credentials_rejected')
        self.tick();self.tick()
        self.assertEqual(self.state['code'],'login_credentials_rejected')
        self.assertEqual(self.state['login_continuation'],'needs_attention')
        self.backend.snapshot.assert_called_once()
        self.service._submit.assert_not_called()

    def test_pause_disarm_cancels_a_stable_candidate(self):
        self.tick();self.manager.disarm('task');self.tick()
        self.service._submit.assert_not_called()

    def test_rate_of_local_observation_is_bounded(self):
        self.manager.tick(self.service);self.manager.tick(self.service)
        self.backend.snapshot.assert_called_once()


class ReturnServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.workspace=Workspace(Path(self.tmp.name))
        class Backend(ReturnedPageBackend):
            def open(self,url,authentication=False):
                if authentication:
                    self.page=PageSnapshot(url,'<h1>正常登录页面</h1>')
                    return self.page
                return super().open(url,authentication=authentication)
        self.service=GuidedService(self.workspace,registry=Registry([ADAPTER]),backend_factory=Backend)
        self.ident=self.service.create({'platform':'liepin','keyword':'时间序列算法工程师','roles':['time_series'],
            'consent':True,'rights_note':'明确的合成页面验收，不是实际猎聘或账号','max_pages':1,'max_jobs':1})['id']
        self.wait()

    def tearDown(self):
        self.service.close();self.tmp.cleanup()

    def wait(self):
        until=time.monotonic()+10
        while self.service.state()['busy'] and time.monotonic()<until:time.sleep(.01)
        self.assertFalse(self.service.state()['busy'])

    def test_actual_worker_continues_return_and_existing_report(self):
        self.service._login_return.interval=.01
        self.service.action({'id':self.ident,'action':'login','auto_continue':True});self.wait()
        backend=self.service._backends[self.ident]
        backend.page=PageSnapshot(SEARCH,HTML)
        until=time.monotonic()+5
        while self.service._load(self.ident).get('login_continuation')!='resumed' and time.monotonic()<until:time.sleep(.02)
        self.wait()
        state=self.service._load(self.ident)
        self.assertEqual(state['status'],'ready',
                         {k:state.get(k) for k in ('code','login_continuation','authentication')})
        self.assertEqual(state['login_continuation'],'resumed')
        self.assertEqual(state['authentication'],'user_resumed')
        self.service.action({'id':self.ident,'action':'collect','selected':[state['cards'][0]['id']]});self.wait()
        state=self.service._load(self.ident)
        self.assertEqual(state['cards'][0]['status'],'ok')
        self.assertTrue(state['report_id'])

    def test_existing_manual_login_remains_default(self):
        self.service.action({'id':self.ident,'action':'login'});self.wait()
        self.assertFalse(self.service._login_return._watches)

    def test_false_and_nonboolean_consent_are_different(self):
        with self.assertRaises(InputError):
            self.service.action({'id':self.ident,'action':'login','auto_continue':'yes'})
        self.service.action({'id':self.ident,'action':'login','auto_continue':False});self.wait()
        self.assertFalse(self.service._login_return._watches)

    def test_auto_continue_cannot_be_added_to_search(self):
        with self.assertRaises(InputError):
            self.service.action({'id':self.ident,'action':'search','auto_continue':True})

    def test_login_api_still_refuses_credentials(self):
        with self.assertRaises(InputError):
            self.service.action({'id':self.ident,'action':'login','auto_continue':True,'password':'synthetic-not-used'})
