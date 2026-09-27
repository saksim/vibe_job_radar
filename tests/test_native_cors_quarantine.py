"""Quarantine commands must not grant a popup request or authentication."""
from types import SimpleNamespace
import threading
import unittest
from unittest.mock import Mock

from vibe_job_radar.guided.contracts import CrawlError
from vibe_job_radar.guided.native_cors import quarantine_target
from vibe_job_radar.guided.native_browser import NativeBackend


class CorsQuarantineTests(unittest.TestCase):
    def backend(self):
        cancelled = threading.Event(); cancelled.set()
        root = Mock(); root.send.return_value = {'sessionId': 'quarantine'}
        return SimpleNamespace(cancelled=cancelled, _halted=True,
                               _cdp=root, _send=Mock(), _sessions={})

    def test_only_abort_interception_is_enabled(self):
        backend = self.backend()
        quarantine_target(backend, 'unexpected')
        backend._cdp.send.assert_called_once_with('Target.attachToTarget', {
            'targetId': 'unexpected', 'flatten': False})
        backend._send.assert_called_once_with('quarantine', 'Fetch.enable', {
            'patterns': [{'urlPattern': '*', 'requestStage': 'Request'}],
            'handleAuthRequests': True})
        self.assertEqual(backend._sessions, {'quarantine': 'unexpected'})

    def test_requires_prior_cancellation(self):
        backend = self.backend(); backend.cancelled.clear()
        with self.assertRaises(CrawlError): quarantine_target(backend, 'unexpected')
        backend._cdp.send.assert_not_called()

    def test_requires_prior_halt(self):
        backend = self.backend(); backend._halted=False
        with self.assertRaises(CrawlError): quarantine_target(backend, 'unexpected')
        backend._cdp.send.assert_not_called()

    def test_attachment_failure_stays_cancelled_without_secret(self):
        backend = self.backend(); backend._cdp.send.side_effect=RuntimeError('fixture-secret')
        with self.assertRaises(CrawlError) as caught: quarantine_target(backend, 'unexpected')
        self.assertEqual(str(caught.exception), 'native_protocol_error')
        self.assertTrue(backend.cancelled.is_set()); backend._send.assert_not_called()

    def test_configuration_failure_never_resumes_or_continues(self):
        backend=self.backend(); backend._send.side_effect=RuntimeError('fixture-secret')
        with self.assertRaises(CrawlError): quarantine_target(backend, 'unexpected')
        self.assertTrue(backend.cancelled.is_set())
        self.assertEqual([c.args[1] for c in backend._send.call_args_list], ['Fetch.enable'])

    def test_reentrant_rejection_is_registered_before_quarantine_attach(self):
        backend=NativeBackend.__new__(NativeBackend)
        backend.cancelled=threading.Event(); backend.error=None
        backend._halted=False; backend._native_cors=True; backend._sessions={}
        backend._send=Mock(); backend._cdp=Mock()
        def attach(*args):
            backend._reject_target('popup')
            return {'sessionId':'quarantine'}
        backend._cdp.send.side_effect=attach
        backend._reject_target('popup')
        self.assertEqual(backend._pending_rejected_targets, ['popup'])
        self.assertEqual(backend._cdp.send.call_count,1)
        self.assertEqual(backend.error,'native_surface_unsupported')
