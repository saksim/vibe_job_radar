"""Closed scratch tabs cannot poison the active job or reuse a stale CDP handle."""
import json
from types import MethodType
from unittest import TestCase
from unittest.mock import Mock

import test_native_acquisition as fixture
from vibe_job_radar.guided.contracts import CrawlError
from vibe_job_radar.guided.native_browser import NativeBackend


class NativePageRetirementTests(TestCase):
    def setUp(self):
        helper = fixture.NativeControllerTests()
        helper.setUp()
        self.addCleanup(helper.doCleanups)
        self.b = helper.b
        self.b._command = 0
        self.page, self.client = Mock(), Mock()
        self.session = 'page:scratch'
        self.b._sessions[self.session] = 'scratch'
        self.b._page_sessions[self.session] = self.client
        self.b._bound_pages[self.page] = self.session
        self.b._send = MethodType(NativeBackend._send, self.b)

    def event(self, method, params=None, session=None):
        return {'sessionId': session or self.session,
                'message': json.dumps({'method': method, 'params': params or {}})}

    def test_late_resource_callback_during_close_cannot_send_or_poison_main(self):
        self.b._paused = Mock()
        self.page.close.side_effect = lambda: self.b._received(self.event('Fetch.requestPaused'))
        self.b._close_owned_page(self.page)
        self.b._paused.assert_not_called()
        self.client.send.assert_not_called()
        self.b._cdp.send.assert_not_called()
        self.assertIsNone(self.b.error)
        self.assertEqual(self.b._sessions, {'session': 'frame'})

    def test_late_authentication_after_retirement_never_sends_credentials(self):
        self.b._close_owned_page(self.page)
        self.b._authenticate = Mock()
        self.b._received(self.event('Fetch.authRequired'))
        self.b._authenticate.assert_not_called()
        self.client.send.assert_not_called()
        self.assertIsNone(self.b.error)

    def test_retired_public_handle_is_not_sent_as_a_root_session(self):
        self.b._detached({'sessionId': self.session})
        callback = Mock()
        self.b._send(self.session, 'Fetch.failRequest', {'requestId': 'stale'}, callback)
        self.b._cdp.send.assert_not_called()
        callback.assert_not_called()
        self.assertEqual(self.b._pending, {})

    def test_active_command_error_is_still_an_error(self):
        self.client.send.side_effect = RuntimeError('artificial live protocol failure')
        with self.assertRaises(RuntimeError):
            self.b._send(self.session, 'Fetch.failRequest', {'requestId': 'live'})
        self.assertEqual(self.b._pending, {})

    def test_inflight_command_losing_its_page_is_cancelled_not_global_failure(self):
        def close_while_sending(*args):
            self.b._detached({'sessionId': self.session})
            raise RuntimeError('artificial target closed while yielding')
        self.client.send.side_effect = close_while_sending
        self.b._send(self.session, 'Fetch.failRequest', {'requestId': 'old'})
        self.assertIsNone(self.b.error)
        self.assertEqual(self.b._pending, {})
        self.b._cdp.send.assert_not_called()

    def test_late_response_ack_does_not_run_retired_callback(self):
        callback = Mock()
        self.b._pending[9] = (self.session, callback)
        self.b._close_owned_page(self.page)
        self.b._received({'sessionId': self.session, 'message': '{"id":9,"result":{}}'})
        callback.assert_not_called()
        self.assertEqual(self.b._pending, {})

    def _finished_body(self, body, *, retire):
        self.b._requests[(self.session, 'body')] = {
            'role': 'business', 'json': True, 'epoch': self.b._epoch,
            'status': 200, 'size': 2, 'operation': 'query_jobs',
        }
        def receive_body(*args):
            if retire:
                self.b._detached({'sessionId': self.session})
            return {'body': body}
        self.client.send.side_effect = receive_body
        self.b._finished(self.session, {'requestId': 'body'})
        self.client.send.assert_called_once_with('Network.getResponseBody', {'requestId': 'body'})
        self.assertEqual(self.b._pending, {})

    def test_successful_body_return_after_page_close_cannot_add_observation(self):
        self._finished_body('{"jobs": [{"id": "retired-page"}]}', retire=True)
        self.assertEqual(self.b.observations(), ())
        self.assertEqual(self.b._observed_bytes, 0)
        self.assertIsNone(self.b.error)
        self.assertEqual(self.b._sessions, {'session': 'frame'})

    def test_invalid_body_return_after_page_close_cannot_stop_surviving_job(self):
        self._finished_body('not-json', retire=True)
        self.assertIsNone(self.b.error)
        self.assertFalse(self.b._halted)
        self.assertEqual(self.b.observations(), ())
        self.b._cdp.send.assert_not_called()

    def test_successful_body_return_from_live_page_is_retained(self):
        self._finished_body('{"jobs": [{"id": "live-page"}]}', retire=False)
        observed, = self.b.observations()
        self.assertEqual(observed.operation, 'query_jobs')
        self.assertEqual(observed.payload, {'jobs': [{'id': 'live-page'}]})
        self.assertGreater(self.b._observed_bytes, 0)
        self.assertIsNone(self.b.error)

    def test_retirement_releases_only_own_request_and_authentication_state(self):
        self.b._requests[(self.session, 'old')] = {'size': 1}
        self.b._requests[('session', 'live')] = {'size': 2}
        self.b._hops[(self.session, 'old')] = 1
        self.b._auth_attempts.update({(self.session, 'old'), ('session', 'live')})
        self.b._close_owned_page(self.page)
        self.assertEqual(self.b._requests, {('session', 'live'): {'size': 2}})
        self.assertEqual(self.b._auth_attempts, {('session', 'live')})
        self.assertEqual(self.b._hops, {})
        self.assertEqual(self.b._bound_pages, {})

    def test_failed_close_is_fatal_and_explicitly_closes_retired_target(self):
        self.page.close.side_effect = RuntimeError('artificial close failure')
        with self.assertRaises(CrawlError) as caught:
            self.b._close_owned_page(self.page)
        self.assertEqual(caught.exception.code, 'native_protocol_error')
        self.assertEqual(self.b.error, 'native_protocol_error')
        targets = [call.args[1]['targetId'] for call in self.b._cdp.send.call_args_list
                   if call.args[0] == 'Target.closeTarget']
        self.assertIn('scratch', targets)
        self.assertIn('frame', targets)

    def test_active_request_event_still_reaches_controls(self):
        self.b._paused = Mock()
        self.b._received(self.event('Fetch.requestPaused', {'requestId': 'live'}))
        self.b._paused.assert_called_once_with(self.session, {'requestId': 'live'})

    def test_active_controller_exception_still_stops_the_backend(self):
        self.b._paused = Mock(side_effect=ValueError('artificial'))
        self.b._received(self.event('Fetch.requestPaused'))
        self.assertEqual(self.b.error, 'native_protocol_error')
        self.assertTrue(self.b._halted)
