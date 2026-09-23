"""No new browser may be created by a timer after its owner window closes."""
from types import SimpleNamespace
import json
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from vibe_job_radar.guided.service import GuidedService
from vibe_job_radar.guided.contracts import CrawlError
from vibe_job_radar.guided.deferred_resume import DeferredResume
from vibe_job_radar.guided.rate import RateLimit
from vibe_job_radar.workspace import Workspace


class DeferredBrowserLifecycleTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup)
        self.workspace=Workspace(tmp.name)
        self.service=GuidedService(self.workspace)
        self.addCleanup(self.service.close)
        self.service.ledger.clock=lambda:1000.0
        self.service._selected_browser='bundled'
        with patch.object(self.service,'_submit'):
            task=self.service.create({'platform':'liepin','keyword':'时间序列算法',
                'roles':['time_series'],'consent':True,'rights_note':'ARTIFICIAL OFFLINE TEST'})
        self.state=self.service._load(task['id'])
        self.service._save(self.state,'publisher_wait',status='waiting_rate',
            next_allowed_at=999.0,auto_resume=True,retry_action='search')
        self.backend=SimpleNamespace(alive=Mock(return_value=True),auth_mode=False,close=Mock())
        self.service._backends[task['id']]=self.backend

    def queued(self):
        with patch.object(self.service,'_submit') as submit:
            self.service._resume_due()
        submit.assert_called_once()
        action,ident,token=submit.call_args.args
        self.assertIsInstance(token,DeferredResume)
        self.assertIs(token.backend,self.backend)
        return action,ident,token

    def test_closed_browser_does_not_queue_automatic_recreation(self):
        self.backend.alive.return_value=False
        with patch.object(self.service,'_submit') as submit:
            self.service._resume_due()
        submit.assert_not_called()
        state=self.service._load(self.state['id'])
        self.assertFalse(state['auto_resume'])
        self.assertEqual(state['next_allowed_at'],999.0)
        self.assertEqual(state['status'],'waiting_manual')

    def test_closed_browser_is_not_left_armed_before_deadline(self):
        self.service._save(self.state,next_allowed_at=2000.0)
        self.backend.alive.return_value=False
        with patch.object(self.service,'_submit') as submit:
            self.service._resume_due()
        submit.assert_not_called()
        state=self.service._load(self.state['id'])
        self.assertFalse(state['auto_resume'])
        self.assertEqual(state['next_allowed_at'],2000.0)

    def test_live_browser_is_bound_to_queued_action_without_persistence(self):
        _,ident,token=self.queued()
        self.assertEqual(token.next_allowed_at,999.0)
        self.assertNotIn('backend=',repr(token))
        state=self.service._load(ident)
        self.assertEqual(state['status'],'queued')
        self.assertIsNone(state['next_allowed_at'])
        self.assertNotIn('DeferredResume',self.service._path(ident).read_text(encoding='utf-8'))

    def test_authentication_unknown_and_failing_liveness_stop_auto_resume(self):
        for backend in (SimpleNamespace(close=Mock()),
                        SimpleNamespace(alive=Mock(side_effect=RuntimeError('private diagnostic')),close=Mock()),
                        SimpleNamespace(alive=lambda:True,auth_mode=True,close=Mock())):
            with self.subTest(backend=type(backend).__name__):
                self.service._backends[self.state['id']]=backend
                self.service._save(self.state,status='waiting_rate',auto_resume=True,next_allowed_at=999.0)
                with patch.object(self.service,'_submit') as submit:
                    self.service._resume_due()
                submit.assert_not_called()
                state=self.service._load(self.state['id'])
                self.assertEqual(state['code'],'automatic_resume_unavailable')
                self.assertNotIn('private diagnostic',str(state))

    def test_window_closed_after_queue_does_not_create_or_restore_a_session(self):
        action,ident,token=self.queued()
        self.backend.alive.return_value=False
        with patch.object(self.service,'factory') as factory, \
                patch.object(self.service,'_new_session_lease') as lease, self.assertRaises(CrawlError) as error:
            self.service._run(action,self.service._load(ident),token)
        self.assertEqual(error.exception.code,'automatic_resume_unavailable')
        factory.assert_not_called();lease.assert_not_called()

    def test_fatal_or_inconsistent_backend_error_cannot_be_erased_by_timer(self):
        for code,waiting in (('http_403',None),('native_page_cleared',None),
                             ('private unrecognized diagnostic',None),('publisher_wait',None),
                             ('http_403',RateLimit(1,'publisher_wait')),
                             ('publisher_wait',RateLimit(1,'cooldown')),
                             ('clock_rollback',RateLimit(1,'clock_rollback')),
                             (None,RateLimit(1,'publisher_wait'))):
            with self.subTest(code=code):
                self.backend.error,self.backend.wait_error=code,waiting
                self.service._save(self.state,status='waiting_rate',auto_resume=True,next_allowed_at=999.0)
                with patch.object(self.service,'_submit') as submit:
                    self.service._resume_due()
                submit.assert_not_called()
                state=self.service._load(self.state['id'])
                self.assertEqual(state['code'],'automatic_resume_unavailable')
                self.assertNotIn('private unrecognized diagnostic',str(state))

    def test_valid_publisher_wait_and_background_pause_keep_original_timer(self):
        for code in ('rate_wait','publisher_wait','cooldown','http_429','hourly_limit','daily_limit','paused'):
            with self.subTest(code=code):
                self.backend.error=code
                self.backend.wait_error=None if code=='paused' else RateLimit(1,code)
                self.service._save(self.state,status='waiting_rate',auto_resume=True,next_allowed_at=999.0)
                self.queued()

    def test_fatal_error_after_queue_does_not_reopen_original_browser(self):
        action,ident,token=self.queued()
        self.backend.error='http_403'
        with patch.object(self.service,'factory') as factory,self.assertRaises(CrawlError) as error:
            self.service._run(action,self.service._load(ident),token)
        self.assertEqual(error.exception.code,'automatic_resume_unavailable')
        factory.assert_not_called()

    def test_replaced_backend_is_not_borrowed_by_the_old_timer(self):
        action,ident,token=self.queued()
        replacement=SimpleNamespace(alive=lambda:True,close=Mock())
        self.service._backends[ident]=replacement
        with patch.object(self.service,'factory') as factory,self.assertRaises(CrawlError):
            self.service._run(action,self.service._load(ident),token)
        factory.assert_not_called();replacement.close.assert_not_called()

    def test_second_liveness_check_cannot_fall_through_to_new_browser(self):
        self.backend.alive.side_effect=[True,False]
        with patch.object(self.service,'factory') as factory,self.assertRaises(CrawlError):
            self.service._backend(self.state,required=self.backend)
        factory.assert_not_called()

    def test_explicit_action_still_may_create_a_new_owned_browser(self):
        self.backend.alive.return_value=False
        replacement=SimpleNamespace()
        with patch.object(self.service,'factory',return_value=replacement) as factory:
            self.assertIs(self.service._backend(self.state),replacement)
        factory.assert_called_once()
        # Closing the fixture service only touches this application-owned object.
        replacement.close=Mock()

    def test_worker_preserves_deadline_report_and_quota_when_queued_window_closes(self):
        report=self.workspace.root/'reports'/('a'*32)
        report.mkdir();marker=report/'unchanged.txt';marker.write_text('ARTIFICIAL ORIGINAL REPORT',encoding='utf-8')
        self.service._save(self.state,report_id=report.name)
        self.service.ledger.reserve('liepin','request')
        before=self.service.ledger.summary('liepin')
        action,ident,token=self.queued()
        self.backend.alive.return_value=False
        with patch.object(self.service,'factory') as factory:
            self.service._submit(action,ident,token)
            deadline=time.monotonic()+5
            while self.service.state()['busy'] and time.monotonic()<deadline:
                time.sleep(.01)
            self.assertFalse(self.service.state()['busy'])
        factory.assert_not_called()
        state=self.service._load(ident)
        self.assertEqual(state['code'],'automatic_resume_unavailable')
        self.assertEqual(state['next_allowed_at'],999.0)
        self.assertEqual(state['report_id'],report.name)
        self.assertEqual(marker.read_text(encoding='utf-8'),'ARTIFICIAL ORIGINAL REPORT')
        self.assertEqual(self.service.ledger.summary('liepin'),before)
        self.assertTrue(self.service._cancel.is_set())

    def test_malformed_timer_flags_and_deadlines_do_not_authorize_network(self):
        for changes in ({'auto_resume':'true'},{'next_allowed_at':True},
                        {'next_allowed_at':float('nan')},{'retry_action':'login'}):
            with self.subTest(changes=changes):
                self.service._save(self.state,auto_resume=True,next_allowed_at=999.0,retry_action='search')
                # Deliberately damaged legacy metadata; the normal writer
                # already refuses non-finite JSON numbers.
                self.service._path(self.state['id']).write_text(
                    json.dumps({**self.state,**changes}),encoding='utf-8')
                with patch.object(self.service,'_submit') as submit:
                    self.service._resume_due()
                submit.assert_not_called()


if __name__=='__main__':
    unittest.main()
