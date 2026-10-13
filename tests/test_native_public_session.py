"""Public page CDP transport and owned-blank-tab boundary, without networking."""
from types import MethodType, SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

import test_native_acquisition as fixture
from vibe_job_radar.guided.contracts import CrawlError
from vibe_job_radar.guided.native_browser import NativeBackend


class NativePublicSessionTests(TestCase):
    def setUp(self):
        self.helper = fixture.NativeControllerTests()
        self.helper.setUp()
        self.addCleanup(self.helper.doCleanups)
        self.b = self.helper.b
        self.b._command = 0
        self.b._adopting = set()
        self.b._native_user_agent = 'ActualBrowser/1 VibeJobRadar/0.1'
        # Lifecycle tests isolate the separately tested abort-only attachment.
        quarantine = patch('vibe_job_radar.guided.native_cors.quarantine_target')
        self.quarantine = quarantine.start()
        self.addCleanup(quarantine.stop)

    def test_command_and_ack_use_public_page_session_not_root_relay(self):
        client, callback = Mock(), Mock()
        client.send.return_value = {'ok': True}
        self.b._page_sessions['session'] = client
        self.b._send = MethodType(NativeBackend._send, self.b)
        self.b._send('session', 'Fetch.enable', {'handleAuthRequests': True}, callback)
        client.send.assert_called_once_with('Fetch.enable', {'handleAuthRequests': True})
        callback.assert_called_once_with({'ok': True})
        self.b._cdp.send.assert_not_called()
        self.assertEqual(self.b._pending, {})

    def test_public_command_error_does_not_leave_stale_pending_entry(self):
        client = Mock()
        client.send.side_effect = RuntimeError('fixture')
        self.b._page_sessions['session'] = client
        self.b._send = MethodType(NativeBackend._send, self.b)
        with self.assertRaises(RuntimeError):
            self.b._send('session', 'Fetch.enable')
        self.assertEqual(self.b._pending, {})

    def test_temporary_blank_attachment_never_enables_fetch_or_auth(self):
        self.b._install_target('temporary', {'targetId': 'blank'})
        self.b._send.assert_called_once_with('temporary', 'Runtime.runIfWaitingForDebugger')

    def test_unsolicited_page_is_closed_before_any_protocol_install(self):
        self.b._install_target = Mock()
        self.b._attached({'sessionId': 'automatic', 'targetInfo': {
            'targetId':'popup', 'type':'page', 'url':'about:blank'}})
        self.b._cdp.send.assert_not_called()
        self.assertEqual(self.b._pending_rejected_targets, ['popup'])
        self.b._drain_rejected_pages()
        self.b._cdp.send.assert_called_once_with('Target.closeTarget', {'targetId':'popup'})
        self.b._install_target.assert_not_called()

    def test_opener_page_cannot_steal_an_application_creation_slot(self):
        self.b._page_creation = 1
        self.b._install_target = Mock()
        self.b._attached({'sessionId': 'automatic', 'targetInfo': {
            'targetId':'popup', 'type':'page', 'url':'about:blank', 'openerId':'another-page'}})
        self.b._cdp.send.assert_not_called()
        self.assertEqual(self.b._pending_rejected_targets, ['popup'])
        self.b._drain_rejected_pages()
        self.b._cdp.send.assert_called_once_with('Target.closeTarget', {'targetId':'popup'})
        self.b._install_target.assert_not_called()

    def test_nonblank_target_is_not_released_during_creation(self):
        self.b._page_creation = 1
        self.b._install_target = Mock()
        self.b._attached({'sessionId': 'automatic', 'targetInfo': {
            'targetId':'popup', 'type':'page', 'url':fixture.URL+'/apply'}})
        self.b._cdp.send.assert_not_called()
        self.assertEqual(self.b._pending_rejected_targets, ['popup'])
        self.b._drain_rejected_pages()
        self.b._cdp.send.assert_called_once_with('Target.closeTarget', {'targetId':'popup'})
        self.b._install_target.assert_not_called()

    def test_page_binding_retires_temporary_session_before_fetch(self):
        self.b._page_creation = 1
        page, client, self.b.context = Mock(url='about:blank'), Mock(), Mock()
        self.b.context.new_cdp_session.return_value = client
        client.send.return_value = {'targetInfo':{'targetId':'frame'}}
        order=[]
        self.b._cdp.send.side_effect=lambda *a:order.append('detach-temporary')
        self.b._install_target=Mock(side_effect=lambda *a:order.append('install-public'))
        self.b._bind_page(page)
        self.assertEqual(order,['detach-temporary','install-public'])
        self.b._install_target.assert_called_once_with('page:frame',{'targetId':'frame'})
        self.assertIs(self.b._page_sessions['page:frame'],client)
        self.b._bind_page(page)
        self.b.context.new_cdp_session.assert_called_once_with(page)

    def test_page_not_admitted_by_browser_target_guard_is_closed(self):
        self.b._page_creation = 1
        page, client, self.b.context = Mock(url='about:blank'), Mock(), Mock()
        self.b.context.new_cdp_session.return_value = client
        client.send.return_value={'targetInfo':{'targetId':'not-admitted'}}
        self.b._bind_page(page)
        page.close.assert_called_once()
        self.assertEqual(self.b.error,'native_surface_unsupported')
        self.assertEqual(self.b._page_sessions,{})

    def test_page_creation_clears_slot_even_when_browser_fails(self):
        self.b.context=Mock()
        self.b.context.new_page.side_effect=RuntimeError('fixture')
        with self.assertRaises(RuntimeError):self.b._new_page()
        self.assertEqual(self.b._page_creation,0)

    def test_detached_page_releases_transport_and_page_references(self):
        page=Mock()
        self.b._page_sessions['session']=Mock()
        self.b._bound_pages[page]='session'
        self.b._detached({'sessionId':'session'})
        self.assertEqual(self.b._page_sessions,{})
        self.assertEqual(self.b._bound_pages,{})

    def test_owned_page_event_defers_binding_to_its_caller(self):
        self.b._page_creation = 1
        self.b._bind_page = Mock()
        page = Mock(url='about:blank')
        self.b._page_created(page)
        self.b._bind_page.assert_not_called()
        page.close.assert_not_called()

    def test_unsolicited_page_event_stops_without_reentrant_close(self):
        self.b._bind_page = Mock()
        page = Mock(url='about:blank')
        self.b._page_created(page)
        self.b._bind_page.assert_not_called()
        page.close.assert_not_called()
        self.assertTrue(self.b.cancelled.is_set())
        self.assertEqual(self.b._rejected_pages, [page])
        page.is_closed.return_value = False
        self.b._drain_rejected_pages()
        page.close.assert_called_once()
        self.assertEqual(self.b.error, 'native_surface_unsupported')

    def test_creation_event_and_return_initialize_the_page_only_once(self):
        page = Mock(url='about:blank')
        def create():
            self.b._page_created(page)
            return page
        self.b.context = Mock()
        self.b.context.new_page.side_effect = create
        self.b._bind_page = Mock()
        self.assertIs(self.b._new_page(), page)
        self.b._bind_page.assert_called_once_with(page)
        self.assertEqual(self.b._page_creation, 0)


    def test_same_rejected_target_is_closed_only_once_across_callbacks(self):
        self.b._reject_target('popup')
        self.b._reject_target('popup')
        self.b._cdp.send.assert_not_called()
        self.b._drain_rejected_pages()
        self.b._cdp.send.assert_called_once_with('Target.closeTarget', {'targetId':'popup'})

    def test_reentrant_rejection_is_deduplicated_before_send(self):
        self.b._cdp.send.side_effect = lambda *_: self.b._reject_target('popup')
        self.b._reject_target('popup')
        self.b._drain_rejected_pages()
        self.assertEqual(self.b._cdp.send.call_count, 1)

    def test_browser_closed_popup_needs_no_second_close(self):
        page=Mock()
        self.b._page_created(page)
        page.is_closed.return_value=True
        self.b._drain_rejected_pages()
        page.close.assert_not_called()
        self.assertEqual(self.b._rejected_pages, [])

    def test_closing_context_does_not_close_again_in_page_event(self):
        self.b._closing=True
        page=Mock()
        self.b._page_created(page)
        page.close.assert_not_called()

    def test_rejected_target_tracking_has_a_hard_bound(self):
        self.b._rejected_targets={str(n) for n in range(128)}
        self.b._reject_target('overflow')
        self.assertTrue(self.b.cancelled.is_set())
        self.assertEqual(len(self.b._rejected_targets), 128)
        self.assertEqual(self.b.error, 'native_surface_unsupported')

    def test_rejected_target_is_never_resumed_while_waiting_for_owner(self):
        self.b._reject_target('popup')
        self.b._cdp.send.assert_not_called()
        self.b._send.assert_not_called()
        self.assertTrue(self.b.cancelled.is_set())
        self.assertEqual(self.b.error, 'native_surface_unsupported')
        self.b._drain_rejected_pages()
        self.b._cdp.send.assert_called_once_with('Target.closeTarget', {'targetId':'popup'})

    def test_rejected_target_close_error_is_not_ignored(self):
        self.b._reject_target('popup')
        self.b._cdp.send.side_effect=RuntimeError('fixture')
        with self.assertRaises(CrawlError) as caught:
            self.b._drain_rejected_pages()
        self.assertEqual(caught.exception.code,'native_protocol_error')
        self.assertTrue(self.b.cancelled.is_set())


    def test_rejected_default_context_uses_abort_only_target_controls(self):
        self.b._native_cors = self.b._direct_cdp = False
        self.b._reject_target('popup')
        self.quarantine.assert_called_once_with(self.b, 'popup')
        self.assertTrue(self.b.cancelled.is_set())
        self.assertTrue(self.b._halted)


class NativeInterceptionOwnerTests(TestCase):
    def test_all_native_contexts_leave_http_interception_to_native_controller(self):
        for direct, cors in ((False, False), (False, True), (True, False), (True, True)):
            with self.subTest(direct=direct, cors=cors):
                backend = NativeBackend.__new__(NativeBackend)
                backend.contract = SimpleNamespace(rules=[SimpleNamespace(cors_origin=cors)])
                backend.context = Mock()
                root = Mock()
                root.send.side_effect = lambda method, *args: (
                    {'browserContextIds': ['only-context']} if method == 'Target.getBrowserContexts'
                    else {'userAgent': 'ActualBrowser/1'} if method == 'Browser.getVersion' else {})
                backend.browser = SimpleNamespace(minimal_events=direct,
                                                   new_browser_cdp_session=lambda: root)
                backend._configure_context()
                backend.context.route.assert_not_called()
                if direct:
                    backend.context.route_web_socket.assert_not_called()
                else:
                    backend.context.route_web_socket.assert_called_once()
                    websocket = Mock()
                    backend.context.route_web_socket.call_args.args[1](websocket)
                    websocket.close.assert_called_once_with()
                root.send.assert_any_call('Target.setAutoAttach', {
                    'autoAttach': True, 'waitForDebuggerOnStart': True, 'flatten': True})
