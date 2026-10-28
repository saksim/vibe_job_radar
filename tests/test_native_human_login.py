"""Synthetic login handoff regressions: no accounts, SMS or publisher traffic."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from vibe_job_radar.guided.adapters import builtins, Registry
from vibe_job_radar.guided.browser import PlaywrightBackend
from vibe_job_radar.guided.checkpoint import binding, ensure_compatible
from vibe_job_radar.guided.contracts import CrawlError
from vibe_job_radar.guided.liepin_login_policy import native_human_contract
from vibe_job_radar.guided.manual_login_progress import ManualLoginProgress
from vibe_job_radar.guided.native_browser import NativeBackend
from vibe_job_radar.guided.native_documents import document_response_params
from vibe_job_radar.guided.native_login_frames import LoginFrames
from vibe_job_radar.guided.native_login_handoff import prepare
from vibe_job_radar.guided.native_policy import contract_for
from vibe_job_radar.guided.service import GuidedService
from vibe_job_radar.network_policy import current_policy
from vibe_job_radar.store import Store
from vibe_job_radar.workspace import Workspace, InputError
from test_automatic_collection import MemoryBackend, BODY

ADAPTER = builtins().get('liepin')
SMS = 'https://api-passport.liepin.com/api/com.liepin.passport.captcha.send-sms'
LOGIN = 'https://api-passport.liepin.com/api/com.liepin.passport.account.tel-sms-login'
FRAME = 'https://turing.captcha.qcloud.com/template/drag_ele.html'


class ContractTests(unittest.TestCase):
    def test_existing_native_contract_does_not_enable_sms(self):
        base = contract_for(ADAPTER)
        before = deepcopy(base)
        self.assertEqual(base.key, 'liepin_search_login_v6')
        native_human_contract(ADAPTER)
        self.assertEqual(contract_for(ADAPTER), before)
        with self.assertRaises(CrawlError):
            base.match(SMS, 'POST', 'Fetch', authentication=True)

    def test_exact_operations_require_auth_mode_and_original_first_party_origin(self):
        contract = native_human_contract(ADAPTER)
        for url in (SMS, LOGIN):
            with self.assertRaises(CrawlError):
                contract.match(url, 'POST', 'Fetch')
            for origin in ('https://www.liepin.com', 'https://c.liepin.com'):
                headers = {'Origin':origin}
                rule = contract.match(url, 'POST', 'Fetch', authentication=True, headers=headers)
                rule.validate_headers('POST', headers)
                self.assertEqual(rule.cors_origin, origin)
            headers = {'Origin':'https://unrelated.invalid'}
            with self.assertRaises(CrawlError):
                contract.match(url, 'POST', 'Fetch', authentication=True, headers=headers).validate_headers('POST', headers)
        for url in (SMS + '/extra', SMS.replace('send-sms','send-anything'), FRAME.replace('drag_ele','arbitrary')):
            with self.assertRaises(CrawlError):
                contract.match(url, 'POST' if 'passport' in url else 'GET', 'Fetch', authentication=True)

    def test_login_frame_csp_is_additive_and_default_remains_closed(self):
        event = {'requestId':'r', 'resourceType':'Document', 'responseStatusCode':200,
                 'responseHeaders':[{'name':'Content-Security-Policy','value':"default-src 'self'"}]}
        default = document_response_params(event, enabled=True)
        human = document_response_params(event, enabled=True, login_frame=True)
        self.assertEqual(human['responseHeaders'][0], event['responseHeaders'][0])
        self.assertIn("frame-src 'none'", default['responseHeaders'][-1]['value'])
        self.assertIn('frame-src https://turing.captcha.qcloud.com/template/', human['responseHeaders'][-1]['value'])
        self.assertIn("worker-src 'none'", human['responseHeaders'][-1]['value'])


class FrameOwnershipTests(unittest.TestCase):
    def setUp(self):
        page = Mock(); page.is_closed.return_value = False
        page.url = 'https://www.liepin.com/login'
        self.b = SimpleNamespace(human_login=True, auth_mode=True, _direct_cdp=True,
            page=page, _bound_pages={page:'main'}, _sessions={'main':'top'},
            _context_id='ctx', browser=Mock(), _page_sessions={}, _install_target=Mock(),
            _received=Mock(), _detached=Mock())
        self.f = LoginFrames(self.b)
        self.rule = native_human_contract(ADAPTER).match(FRAME, 'GET', 'Document', authentication=True)
        self.event = {'frameId':'child','resourceType':'Document'}
        self.attached = {'sessionId':'protocol-child','targetInfo':{'targetId':'child','type':'iframe','browserContextId':'ctx'}}

    def approve(self):
        self.f.attached_frame('main', {'frameId':'child','parentFrameId':'top'})
        self.f.request('main', self.event, self.rule)

    def test_only_owned_reviewed_document_can_acquire_child_session(self):
        self.assertFalse(self.f.attach_target('main', self.attached))
        self.approve()
        self.assertTrue(self.f.attach_target('main', self.attached))
        self.b._install_target.assert_called_once()
        self.assertEqual(self.f.owner('login-frame:child'), 'main')

    def test_unattached_top_level_nested_and_changed_owner_are_refused(self):
        with self.assertRaises(CrawlError): self.f.request('main', self.event, self.rule)
        self.f.attached_frame('main', {'frameId':'child','parentFrameId':'unknown'})
        with self.assertRaises(CrawlError): self.f.request('main', self.event, self.rule)
        with self.assertRaises(CrawlError): self.f.request('main', {'frameId':'top'}, self.rule)
        self.approve(); self.b._bound_pages[self.b.page] = 'replacement'
        self.assertFalse(self.f.attach_target('main', self.attached))

    def test_worker_popup_and_foreign_context_are_not_admitted(self):
        self.approve()
        for change in ({'type':'worker'}, {'openerId':'top'}, {'browserContextId':'foreign'}):
            event = {**self.attached, 'targetInfo':{**self.attached['targetInfo'], **change}}
            with self.subTest(change=change): self.assertFalse(self.f.attach_target('main', event))

    def test_child_cannot_submit_first_party_sms(self):
        self.approve(); self.f.attach_target('main', self.attached)
        rule = native_human_contract(ADAPTER).match(SMS,'POST','Fetch',authentication=True)
        with self.assertRaises(CrawlError):
            self.f.request('login-frame:child', {'frameId':'child','resourceType':'Fetch'}, rule)

    def test_swap_preserves_reviewed_frame_but_removal_and_collection_revoke_it(self):
        self.approve()
        self.f.detached_frame('main', {'frameId':'child','reason':'swap'})
        self.assertIn(('main','child'), self.f.approved)
        self.b.auth_mode = False
        self.assertFalse(self.f.attach_target('main', self.attached))
        self.f.detached_frame('main', {'frameId':'child','reason':'remove'})
        self.assertNotIn(('main','child'), self.f.approved)


class ProgressTests(unittest.TestCase):
    def setUp(self):
        self.now = 0
        self.p = ManualLoginProgress(clock=lambda:self.now); self.p.start()

    def test_response_is_not_delivery_or_login_success(self):
        self.p.requested('a', SMS, 'POST'); self.p.received('a', 200)
        self.assertEqual(self.p.snapshot()['stage'], 'sms_response_received')
        self.p.requested('b', LOGIN, 'POST'); self.p.received('b', 200)
        self.assertEqual(self.p.snapshot()['stage'], 'login_response_received')
        self.assertTrue(self.p.active)

    def test_late_sms_response_cannot_overwrite_newer_login(self):
        self.p.requested('a', SMS, 'POST'); self.p.requested('b', LOGIN, 'POST')
        self.p.received('b', 200); self.p.received('a', 500)
        self.assertEqual(self.p.snapshot()['stage'], 'login_response_received')

    def test_duplicate_observation_not_double_counted_but_real_repeated_requests_are(self):
        self.p.requested('a', SMS, 'POST'); self.p.requested('a', SMS, 'POST')
        self.p.requested('b', SMS, 'POST')
        self.assertEqual(self.p.snapshot()['sms_requests'], 2)

    def test_cancel_expiry_and_stale_response_cannot_resume(self):
        for stage in ('cancelled','returned'):
            self.p.start(); self.p.requested('a', LOGIN, 'POST'); self.p.stop(stage)
            self.p.received('a', 200)
            self.assertEqual(self.p.snapshot()['stage'], stage)
        self.p.start(); self.now = 601
        self.p.requested('a', SMS, 'POST')
        self.assertEqual(self.p.snapshot()['stage'], 'timed_out')
        self.assertEqual(self.p.snapshot()['sms_requests'], 0)

    def test_no_url_values_or_credentials_persisted_and_unknown_requests_ignored(self):
        self.p.requested('PRIVATE-KEY', SMS+'?private=SECRET', 'POST')
        self.p.requested('x', SMS.replace('api-passport.liepin.com','other.invalid'), 'POST')
        state = json.dumps(self.p.snapshot())
        self.assertNotIn('SECRET', state); self.assertNotIn('PRIVATE', state)
        self.assertEqual(self.p.snapshot()['sms_requests'], 1)


class HandoffBackend(MemoryBackend):
    def __init__(self, adapter, ledger, cancelled, progress, **options):
        super().__init__()
        self.adapter, self.cancelled = adapter, cancelled
        self.wire = SimpleNamespace(network_policy=current_policy(), ensure_robots=Mock())
        self.options = options
        self.error = None
    start_manual_login = PlaywrightBackend.start_manual_login
    def collection_mode(self):
        super().collection_mode()
        self._manual_login_progress.stop('returned')


class OriginalTaskHandoffTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.workspace = Workspace(Path(self.tmp.name))
        self.factory = Mock(side_effect=HandoffBackend)
        self.s = GuidedService(self.workspace, registry=Registry([ADAPTER]),
            backend_factory=MemoryBackend, native_backend_factory=self.factory)
        self.addCleanup(self.s.close)
        self.s._submit = Mock(); self.s._selected_browser = 'msedge'
        made = self.s.create({'platform':'liepin','keyword':'时间序列','roles':['time_series'],
            'max_jobs':2,'max_pages':1,'auto_collect':True,'consent':True,'rights_note':'synthetic'})
        self.state = self.s._load(made['id']); self.ident = made['id']
        self.s._save(self.state, 'manual_required', status='waiting_manual')

    def test_explicit_migration_preserves_original_task_content_and_old_binding_record(self):
        original = deepcopy(self.state)
        for key, value in {'selection':['synthetic'], 'report_id':'existing-report', 'pages_seen':['old-page']}.items():
            original[key] = value
        changed = prepare(original, ADAPTER, 'msedge')
        for key in ('id','keyword','search_url','cards','selection','pages_seen','report_id','auto_collect','max_jobs'):
            self.assertEqual(changed[key], original[key])
        self.assertEqual(changed['native_login_handoff']['previous_binding'], original['execution_binding'])
        self.assertEqual(changed['execution_binding']['query'], original['execution_binding']['query'])
        ensure_compatible(changed, ADAPTER)
        self.assertEqual(original['backend'], 'bridge')
        with self.assertRaises(InputError): prepare(changed, ADAPTER, 'msedge')

    def test_explicit_consent_and_installed_browser_required(self):
        for data in ({}, {'native_consent':False}, {'native_consent':True,'backend':'native'}):
            with self.subTest(data=data), self.assertRaises(InputError):
                self.s.action({'id':self.ident,'action':'login_native',**data})
        self.s._selected_browser = 'bundled'
        with self.assertRaises(InputError):
            self.s.action({'id':self.ident,'action':'login_native','native_consent':True})
        self.factory.assert_not_called()

    def test_uncertain_old_browser_close_keeps_original_binding(self):
        before = deepcopy(self.state)
        with patch.object(self.s, '_close_backend', side_effect=RuntimeError('synthetic close')):
            with self.assertRaises(RuntimeError): self.s._run('login_native', self.state, None)
        self.assertEqual(self.state, before)
        self.assertEqual(self.s._load(self.ident)['execution_binding'], before['execution_binding'])
        self.factory.assert_not_called()

    def test_existing_login_quota_blocks_handoff_before_closing_or_rebinding(self):
        before = deepcopy(self.state)
        with patch.object(self.s.ledger, 'login_availability', return_value={'available':False}), \
                patch.object(self.s, '_close_backend') as close:
            with self.assertRaises(CrawlError) as error:
                self.s._run('login_native', self.state, None)
            self.assertEqual(error.exception.code, 'login_rate_limited')
            close.assert_not_called()
        self.assertEqual(self.state, before)
        self.factory.assert_not_called()

    def test_one_handoff_then_original_task_capture_collects_full_synthetic_jds_and_report(self):
        self.state['auto_continue_after_login'] = True
        self.s._run('login_native', self.state, None)
        backend = self.s._backends[self.ident]
        self.assertTrue(backend.options['human_login'])
        self.assertEqual(self.state['manual_login']['stage'], 'waiting_user')
        self.assertEqual(self.state['status'], 'waiting_manual')
        self.s._run('capture', self.state, None)
        self.assertEqual(self.state['id'], self.ident)
        self.assertEqual(self.state['status'], 'completed')
        self.assertEqual(self.state['outcome']['saved'], 2)
        with Store(self.workspace.db) as store:
            self.assertEqual([r.text for r in store.records()], [BODY, BODY])
        self.assertTrue(self.workspace.report(self.state['report_id']))
        self.assertEqual(self.factory.call_count, 1)
        self.assertEqual(self.state['authentication'], 'user_resumed')

    def test_duplicate_workbench_login_is_a_noop_and_feedback_error_is_nonfatal(self):
        self.s._run('login_native', self.state, None)
        backend = self.s._backends[self.ident]
        self.s._submit.reset_mock()
        self.s.action({'id':self.ident,'action':'login','auto_continue':True})
        self.s._submit.assert_not_called()
        before = self.s._load(self.ident)
        backend.manual_login_status = Mock(side_effect=RuntimeError('synthetic DOM race'))
        self.s._update_manual_login(self.ident, backend)
        self.assertEqual(self.s._load(self.ident), before)

    def test_idle_error_stops_progress_and_cannot_leave_login_button_locked(self):
        self.s._run('login_native', self.state, None)
        backend = self.s._backends[self.ident]
        backend.error = 'native_page_cleared'
        backend.page = Mock(); backend.page.is_closed.return_value = False
        backend.manual_login_status = lambda: PlaywrightBackend.manual_login_status(backend)
        backend.pump = Mock(side_effect=CrawlError('native_page_cleared'))
        self.s._pump_idle()
        state = self.s._load(self.ident)
        self.assertEqual(state['login_continuation'], 'needs_attention')
        self.assertFalse(state['manual_login']['active'])

    def test_old_active_progress_is_historical_without_a_live_backend(self):
        self.state['manual_login'] = {'active':True,'stage':'sms_requested','sms_requests':1,
                                     'login_submissions':0,'http_status':None}
        self.s._save(self.state)
        viewed = next(j for j in self.s.state()['jobs'] if j['id'] == self.ident)
        self.assertFalse(viewed['manual_login']['active'])
        self.assertEqual(viewed['manual_login']['stage'], 'cancelled')
        self.assertTrue(self.s._load(self.ident)['manual_login']['active'])

    def test_failed_initial_navigation_without_backend_error_releases_login_guard(self):
        class FailedNavigation(HandoffBackend):
            def open(self, url, **kwargs):
                self.page = Mock(url='about:blank')
                self.page.is_closed.return_value = False
                raise CrawlError('page_not_ready')
        self.factory.side_effect = FailedNavigation
        self.state['auto_continue_after_login'] = True
        with self.assertRaises(CrawlError): self.s._run('login_native', self.state, None)
        backend = self.s._backends[self.ident]
        self.assertIsNone(backend.error)
        self.assertFalse(self.state['manual_login']['active'])
        self.assertNotIn(self.ident, self.s._login_return._watches)
        self.s._save(self.state, 'page_not_ready', status='waiting_manual')
        self.s._submit.reset_mock()
        self.s.action({'id':self.ident,'action':'login'})
        self.s._submit.assert_called_once_with('login', self.ident, None)

    def test_normal_sms_handoff_keeps_waiting_instead_of_cancelling(self):
        class ManualGate(HandoffBackend):
            def open(self, url, **kwargs): raise CrawlError('login_sms_required')
        self.factory.side_effect = ManualGate
        self.state['auto_continue_after_login'] = True
        with self.assertRaises(CrawlError): self.s._run('login_native', self.state, None)
        self.assertTrue(self.state['manual_login']['active'])
        self.assertIn(self.ident, self.s._login_return._watches)


if __name__ == '__main__':
    unittest.main()
