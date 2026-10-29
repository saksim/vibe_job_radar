"""Cleanup phase attribution is bounded and never changes the primary error."""
import json
import threading
import unittest
from unittest.mock import Mock, patch

import test_native_acquisition as fixture
from vibe_job_radar.guided.browser import PlaywrightBackend
from vibe_job_radar.guided.diagnostic_trace import DiagnosticTrace, observe, notify


class NativeCleanupDiagnosticTests(unittest.TestCase):
    def test_phases_and_unfinished_handlers_preserve_first_error_and_continue_cleanup(self):
        helper = fixture.NativeControllerTests(); helper.setUp(); self.addCleanup(helper.doCleanups)
        b = helper.b
        b._rejected_targets = set(); b._pending_rejected_targets = set(); b._rejected_pages = []
        b._diagnostics = DiagnosticTrace('a'*32, 'fixture')
        first = RuntimeError('PRIVATE arbitrary error text')
        b._request_pacer = Mock(); b._request_pacer.close.side_effect = first
        tunnel = b.tunnel
        tunnel._lock = threading.RLock()
        tunnel._pending_requests = {object()}; tunnel._sockets = set()
        tunnel.thread = Mock(); tunnel.thread.is_alive.return_value = False
        tunnel.close = Mock(side_effect=RuntimeError('native_tunnel_cleanup_incomplete'))
        with patch.object(PlaywrightBackend, 'close') as controller:
            with self.assertRaises(RuntimeError) as caught: b.close()
        self.assertIs(caught.exception, first)
        controller.assert_called_once(); tunnel.close.assert_called_once()
        events = [e for e in b._diagnostics.snapshot()['events'] if e['outcome']=='failure']
        self.assertEqual([e['native_cleanup']['stage'] for e in events], ['pacing','tunnel'])
        self.assertEqual(events[-1]['code'], 'native_tunnel_cleanup_incomplete')
        self.assertEqual(events[-1]['native_cleanup']['pending_handlers'], 1)
        self.assertEqual(events[-1]['native_cleanup']['open_sockets'], 0)
        self.assertFalse(events[-1]['native_cleanup']['listener_alive'])
        self.assertNotIn('PRIVATE', json.dumps(b._diagnostics.snapshot()))

    def test_unknown_cleanup_fields_and_observer_failure_are_not_actions(self):
        trace = DiagnosticTrace('a'*32, 'fixture')
        with observe(trace, 'cleanup', actor='browser'):
            notify(trace, 'mark', native_cleanup={'stage':'PRIVATE','error_type':'PRIVATE',
                'pending_handlers':True, 'open_sockets':9999, 'listener_alive':'PRIVATE',
                'url':'https://private.invalid/?token=PRIVATE'})
        facts = trace.snapshot()['events'][-1]['native_cleanup']
        self.assertEqual(facts, {'stage':'unknown','error_type':'unknown','pending_handlers':None,
                                'open_sockets':None,'listener_alive':None})
        with patch.object(trace, 'mark', side_effect=RuntimeError('PRIVATE')):
            notify(trace, 'mark', native_cleanup={})
        self.assertEqual(trace.observer_errors, 1)
        self.assertNotIn('PRIVATE', json.dumps(trace.snapshot()))
