"""Bounded post-login return; synthetic requests only, no account traffic."""
from collections import Counter
from copy import deepcopy
from dataclasses import replace
import threading
import unittest
from unittest.mock import Mock

from vibe_job_radar.guided.checkpoint import binding, ensure_compatible
from vibe_job_radar.guided.contracts import CrawlError
from vibe_job_radar.guided.diagnostic_trace import safe_target
from vibe_job_radar.guided.liepin_login_policy import native_human_contract
from vibe_job_radar.guided.login_landing import offer, peek, claim
from vibe_job_radar.guided.manual_login_progress import ManualLoginProgress
from vibe_job_radar.guided.native_browser import NativeBackend
from vibe_job_radar.guided.native_login_handoff import upgrade_known_binding
from vibe_job_radar.guided.password_login import LoginCredentials
from vibe_job_radar.guided.native_navigation import NativeDocument
from vibe_job_radar.store import Store
from test_native_human_login import ADAPTER, LOGIN, SMS, BODY
import test_native_human_login as handoff_tests

MAIN = 'https://www.liepin.com/login'
ROOT = 'https://c.liepin.com/'
FLAG = 'https://api-c.liepin.com/api/com.liepin.cresume.resume-complete-flag'
HEADER = 'https://api-c.liepin.com/api/com.liepin.usercx.pc.user.base-property'


def ready_backend(b=None):
    b = b or NativeBackend.__new__(NativeBackend)
    b.human_login = b.auth_mode = b._direct_cdp = True
    b._halted = b._closing = False
    b.error = b.wait_error = None
    b._epoch = 1
    b.cancelled = threading.Event()
    b.contract = native_human_contract(ADAPTER)
    b.native_counts = Counter()
    b._send = Mock()
    b.wire = Mock()
    b.policy_check = lambda: True
    b.page = Mock(url=MAIN, is_closed=Mock(return_value=False))
    b.document_identity = Mock(return_value=('owned', 1, 2, NativeDocument('target','main',1,MAIN,MAIN)))
    b._manual_login_progress = p = ManualLoginProgress(clock=lambda: 10)
    p.start(); p.requested('login', LOGIN, 'POST'); p.received('login', 200)
    return b


def event(url=ROOT, *, frame='main', method='GET', resource='Document'):
    return {'requestId':'landing', 'networkId':'net', 'frameId':frame,
            'resourceType':resource, 'request':{'url':url,'method':method}}


class PostLoginContractTests(unittest.TestCase):
    def test_exact_read_operations_and_preflights_do_not_become_job_data(self):
        contract = native_human_contract(ADAPTER)
        for url, method in ((FLAG,'GET'), (HEADER,'POST')):
            for auth in ((True,) if url == FLAG else (False, True)):
                headers = {'Origin':'https://www.liepin.com',
                    'Access-Control-Request-Method':method, 'Access-Control-Request-Headers':'x-client-type,content-type'}
                for actual in (method, 'OPTIONS'):
                    rule = contract.match(url, actual, 'XHR', authentication=auth, headers=headers)
                    rule.validate_headers(actual, headers)
                    self.assertEqual(rule.role, 'login')
                for changed in ({**headers,'Origin':'https://c.liepin.com'},
                                {**headers,'Access-Control-Request-Method':'DELETE'},
                                {**headers,'Access-Control-Request-Headers':'x-unreviewed'}):
                    with self.assertRaises(CrawlError):
                        contract.match(url,'OPTIONS','XHR',authentication=auth,headers=changed).validate_headers('OPTIONS',changed)
            for version in (1,):
                with self.assertRaises(CrawlError):
                    native_human_contract(ADAPTER,version=version).match(url,method,'XHR',authentication=True)
        for url, method in ((FLAG,'POST'), (HEADER,'GET'), (FLAG+'/extra','GET'),
                            (ROOT,'GET'), (FLAG.replace('resume-complete-flag','web-resume-detail'),'GET')):
            with self.subTest(url=url,method=method), self.assertRaises(CrawlError):
                contract.match(url,method,'XHR',authentication=True)
        with self.assertRaises(CrawlError):
            contract.match(FLAG, 'GET', 'Fetch', authentication=False)

    def test_diagnostic_names_never_expose_query_values(self):
        for url in (FLAG, HEADER):
            safe = safe_target(url+'?ticket=PRIVATE&user=ACCOUNT')
            self.assertEqual(safe['path_template'], url.split('.com')[1])
            self.assertNotIn('PRIVATE', str(safe)); self.assertNotIn('ACCOUNT', str(safe))


class LandingTests(unittest.TestCase):
    def test_return_aborts_account_navigation_and_is_consumed_only_once(self):
        b = ready_backend()
        b._request_paused = Mock()
        b._paused('owned', event())
        ticket = peek(b)
        self.assertIsNotNone(ticket)
        b._request_paused.assert_not_called()
        b.wire.ensure_robots.assert_not_called(); b.wire.reserve.assert_not_called()
        self.assertEqual(b._send.call_args.args[1], 'Fetch.failRequest')
        self.assertEqual(b._send.call_args.args[2]['errorReason'], 'Aborted')
        self.assertEqual(b._manual_login_progress.snapshot()['stage'], 'return_available')
        self.assertTrue(offer(b,'owned',event()))
        self.assertIs(peek(b),ticket)
        claim(b,ticket)
        with self.assertRaisesRegex(CrawlError,'login_return_changed'): claim(b,ticket)
        self.assertFalse(offer(b,'owned',event()))
        self.assertIsNone(b.error)

    def test_other_destinations_frames_and_non_login_surfaces_never_offer_return(self):
        for e in (event(ROOT+'?ticket=x'), event(ROOT+'#x'), event(ROOT+'login/two-factor'),
                  event(ROOT.replace('c.liepin','c.liepin.com.evil')), event(frame='child'),
                  event(method='POST'), event(resource='XHR')):
            self.assertFalse(offer(ready_backend(),'owned',e))
        self.assertFalse(offer(ready_backend(),'unowned',event()))
        b=ready_backend(); identity=b.document_identity.return_value
        b.document_identity.return_value=(*identity[:3],replace(identity[-1],url=ADAPTER.search_base))
        self.assertFalse(offer(b,'owned',event()))

    def test_no_200_or_cancel_expiry_policy_change_and_new_submit_never_resume(self):
        for change in ('no_response','failure','cancel','expired','policy','halt','normal_mode','new_sms','new_login'):
            with self.subTest(change=change):
                b=ready_backend(); p=b._manual_login_progress
                if change=='no_response': p.last_login_response=None
                elif change=='failure': p.requested('again',LOGIN,'POST'); p.received('again',403)
                elif change=='cancel': b.cancelled.set()
                elif change=='expired': p.clock=lambda: 611
                elif change=='policy': b.policy_check=lambda: False
                elif change=='halt': b._halted=True
                elif change=='normal_mode': b.auth_mode=False
                elif change=='new_sms': p.requested('new',SMS,'POST')
                else: p.requested('new',LOGIN,'POST')
                self.assertFalse(offer(b,'owned',event()))

    def test_ownership_change_after_offer_does_not_replay_navigation(self):
        for change in ('page','epoch','tracker','cancel','error'):
            with self.subTest(change=change):
                b=ready_backend(); offer(b,'owned',event()); ticket=peek(b)
                if change=='page':
                    identity=b.document_identity.return_value
                    b.document_identity.return_value=(*identity[:3],replace(identity[-1],sequence=2))
                elif change=='epoch': b._epoch+=1
                elif change=='tracker': b._manual_login_progress=ManualLoginProgress()
                elif change=='cancel': b.cancelled.set()
                else: b.error='http_403'
                with self.assertRaisesRegex(CrawlError,'login_return_changed'): claim(b,ticket)


class OriginalTaskReturnTests(unittest.TestCase):
    setUp = handoff_tests.OriginalTaskHandoffTests.setUp

    def prepare_return(self, *, auto=True):
        self.state['auto_continue_after_login']=auto
        self.s._run('login_native',self.state,None)
        b=self.s._backends[self.ident]
        wire=b.wire
        ready_backend(b); b.wire=wire
        offer(b,'owned',event())
        return b

    def test_opt_in_return_keeps_task_and_generates_only_full_synthetic_jd_report(self):
        b=self.prepare_return()
        self.s._submit.reset_mock()
        self.s._login_return.tick(self.s)
        self.s._submit.assert_called_once()
        action,ident,secret=self.s._submit.call_args.args
        self.assertEqual(action,'resume_returned_login')
        state=self.s._load(ident)
        self.s._run(action,state,secret)
        self.assertEqual(state['id'],self.ident)
        self.assertEqual(state['status'],'completed')
        self.assertEqual(state['outcome']['saved'],2)
        self.assertEqual(state['authentication'],'user_resumed')
        self.assertEqual(state['login_continuation'],'resumed_after_login')
        with Store(self.workspace.db) as store:
            self.assertEqual([r.text for r in store.records()],[BODY,BODY])
        self.assertTrue(self.workspace.report(state['report_id']))
        self.assertFalse(any(url==ROOT for url,_ in b.calls))
        self.factory.assert_called_once()

    def test_no_opt_in_does_not_queue_any_return(self):
        b=self.prepare_return(auto=False)
        self.s._submit.reset_mock(); self.s._login_return.tick(self.s)
        self.s._submit.assert_not_called()
        self.assertIsNotNone(peek(b))

    def test_phase_change_before_owner_execution_preserves_original_ticket(self):
        b=self.prepare_return(); self.s._submit.reset_mock(); self.s._login_return.tick(self.s)
        action,ident,secret=self.s._submit.call_args.args
        state=self.s._load(ident); state['phase']='collect'
        with self.assertRaisesRegex(CrawlError,'login_return_changed'):
            self.s._run(action,state,secret)
        self.assertIsNotNone(peek(b))

    def test_known_binding_upgrade_preserves_all_other_task_fields(self):
        state=deepcopy(self.state); state.update(backend='native',human_login=True)
        state['execution_binding']=binding(state,ADAPTER,human_contract_version=1)
        before=deepcopy(state)
        self.assertTrue(upgrade_known_binding(state,ADAPTER))
        ensure_compatible(state,ADAPTER)
        self.assertEqual(state['native_login_contract_upgrade']['previous_binding'],before['execution_binding'])
        for key in before:
            if key!='execution_binding': self.assertEqual(state[key],before[key])
        updated=deepcopy(state)
        self.assertFalse(upgrade_known_binding(state,ADAPTER)); self.assertEqual(state,updated)

    def test_legacy_explicit_search_keeps_original_task_and_report_flow(self):
        self.state.update(backend='native', human_login=True)
        self.state['execution_binding']=binding(self.state,ADAPTER,human_contract_version=1)
        self.s._backend(self.state)._manual_login_progress=ManualLoginProgress()
        self.s._run('search',self.state,None)
        self.assertEqual(self.state['id'],self.ident)
        self.assertEqual(self.state['status'],'completed')
        self.assertEqual(self.state['outcome']['saved'],2)
        self.assertTrue(self.workspace.report(self.state['report_id']))
        ensure_compatible(self.state,ADAPTER)

    def test_legacy_explicit_password_login_still_submits_once(self):
        self.state.update(backend='native', human_login=True, auto_continue_after_login=True)
        self.state['execution_binding']=binding(self.state,ADAPTER,human_contract_version=1)
        b=self.s._backend(self.state)
        b.password_login=Mock(return_value='login_password_submitted')
        secret=LoginCredentials('synthetic-user','synthetic-password')
        self.addCleanup(secret.clear)
        self.s._run('login_password',self.state,secret)
        b.password_login.assert_called_once_with(secret)
        self.assertEqual(self.state['status'],'waiting_manual')
        self.assertEqual(self.state['authentication'],'manual_pending')
        ensure_compatible(self.state,ADAPTER)

    def test_unknown_binding_or_changed_scope_is_never_migrated(self):
        for change in ('missing','unknown','keyword','consent'):
            state=deepcopy(self.state); state.update(backend='native',human_login=True)
            state['execution_binding']=binding(state,ADAPTER,human_contract_version=1)
            if change=='missing': state.pop('execution_binding')
            elif change=='unknown': state['execution_binding']['backend']='native:unknown'
            elif change=='keyword': state['keyword']='other'
            else: state['execution_binding']['consent']='unknown'
            before=deepcopy(state)
            with self.assertRaises(CrawlError): upgrade_known_binding(state,ADAPTER)
            self.assertEqual(state,before)


if __name__=='__main__': unittest.main()
