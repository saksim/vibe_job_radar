"""Rejected-window close must not run callbacks on retired protocol sessions."""
import json
from unittest import TestCase
from unittest.mock import Mock

import test_native_acquisition as fixture
from vibe_job_radar.guided.contracts import CrawlError


class RejectedTargetRetirementTests(TestCase):
    def setUp(self):
        helper = fixture.NativeControllerTests()
        helper.setUp()
        self.addCleanup(helper.doCleanups)
        self.b = helper.b
        self.b.cancelled.set()
        self.b._halted = True
        self.b.error = 'native_surface_unsupported'
        self.b._sessions['quarantine'] = 'popup'
        self.b._rejected_targets = {'popup'}
        self.b._pending_rejected_targets = ['popup']
        self.b._rejected_pages = []
        self.b._pending[9] = ('quarantine', None)

    def close_targets(self):
        return [c.args[1]['targetId'] for c in self.b._cdp.send.call_args_list
                if c.args[0] == 'Target.closeTarget']

    def test_late_command_error_during_rejected_close_cannot_close_main(self):
        def closing(method, params):
            if method == 'Target.closeTarget' and params['targetId'] == 'popup':
                self.b._received({'sessionId':'quarantine',
                                  'message':json.dumps({'id':9,'error':{'code':-32000}})})
        self.b._cdp.send.side_effect = closing
        self.b._drain_rejected_pages()
        self.assertEqual(self.close_targets(), ['popup'])
        self.assertEqual(self.b._sessions, {'session':'frame'})
        self.assertEqual(self.b.error, 'native_surface_unsupported')
        self.assertTrue(self.b.cancelled.is_set())
        self.assertTrue(self.b._halted)
        self.b._send.assert_not_called()

    def test_late_auth_and_request_events_cannot_reuse_retired_session(self):
        self.b._authenticate = Mock()
        self.b._paused = Mock()
        def closing(*args):
            for method in ('Fetch.authRequired', 'Fetch.requestPaused'):
                self.b._received({'sessionId':'quarantine',
                                  'message':json.dumps({'method':method,'params':{}})})
        self.b._cdp.send.side_effect = closing
        self.b._drain_rejected_pages()
        self.b._authenticate.assert_not_called()
        self.b._paused.assert_not_called()
        self.b._send.assert_not_called()

    def test_all_sessions_of_rejected_target_only_are_retired(self):
        self.b._sessions['second_quarantine'] = 'popup'
        self.b._pending[10] = ('session', Mock())
        self.b._requests[('quarantine','old')] = {'size':1}
        self.b._requests[('session','live')] = {'size':2}
        self.b._hops[('quarantine','old')] = 1
        self.b._auth_attempts.update({('quarantine','old'), ('session','live')})
        self.b._drain_rejected_pages()
        self.assertEqual(self.b._sessions, {'session':'frame'})
        self.assertEqual(set(self.b._pending), {10})
        self.assertEqual(self.b._requests, {('session','live'):{'size':2}})
        self.assertEqual(self.b._hops, {})
        self.assertEqual(self.b._auth_attempts, {('session','live')})

    def test_failure_to_close_rejected_target_still_closes_live_target(self):
        def failed_close(method, params):
            if method == 'Target.closeTarget' and params['targetId'] == 'popup':
                raise RuntimeError('synthetic close failure')
        self.b._cdp.send.side_effect = failed_close
        with self.assertRaises(CrawlError) as exc:
            self.b._drain_rejected_pages()
        self.assertEqual(exc.exception.code, 'native_protocol_error')
        self.assertIn('frame', self.close_targets())
        self.assertTrue(self.b.cancelled.is_set())
        self.assertTrue(self.b._halted)

    def test_active_session_error_is_not_ignored(self):
        self.b._pending[11] = ('session', None)
        self.b._received({'sessionId':'session',
                          'message':json.dumps({'id':11,'error':{'code':-32000}})})
        self.assertIn('frame', self.close_targets())
        self.assertTrue(self.b._halted)

    def test_no_target_retirement_without_close_queue(self):
        self.b._pending_rejected_targets = []
        self.b._drain_rejected_pages()
        self.assertIn('quarantine', self.b._sessions)
        self.assertIn(9, self.b._pending)
        self.b._cdp.send.assert_not_called()
