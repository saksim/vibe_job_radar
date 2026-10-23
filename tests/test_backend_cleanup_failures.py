"""Owned backend teardown faults; no browser, external request or user profile."""
from collections import deque
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from vibe_job_radar.guided.browser import PlaywrightBackend
from vibe_job_radar.guided.browser_health import BrowserStartupError
from vibe_job_radar.guided.native_browser import NativeBackend
from vibe_job_radar.guided.native_pacing import NativeRequestPacer
from vibe_job_radar.guided.native_tunnel import NativeTunnel
from vibe_job_radar.guided.service import GuidedService
from vibe_job_radar.network_policy import NetworkPolicy


class BackendCleanupFailureTests(unittest.TestCase):
    def native(self):
        backend = NativeBackend.__new__(NativeBackend)
        backend._closing = False
        backend.browser, backend.runtime = Mock(), Mock()
        backend.context, backend.page = Mock(), Mock()
        backend.wire = SimpleNamespace(network_policy=NetworkPolicy())
        backend.tunnel = NativeTunnel(('jobs.fixture.test',), NetworkPolicy(), threading.Event())
        self.addCleanup(backend.tunnel.close)
        for name in ('_sessions', '_pending', '_requests', '_hops', '_latest_business',
                     '_page_sessions', '_bound_pages', '_page_documents'):
            setattr(backend, name, {'owned': object()})
        for name in ('_auth_attempts', '_committed_pages', '_rejected_targets'):
            setattr(backend, name, {'owned'})
        backend._observations = deque([object()])
        backend._pending_rejected_targets = ['owned']
        backend._rejected_pages = [object()]
        return backend

    def assert_released(self, backend, browser, runtime, tunnel):
        browser.close.assert_called_once_with()
        runtime.stop.assert_called_once_with()
        self.assertEqual(tunnel.server.socket.fileno(), -1)
        self.assertFalse(tunnel.thread.is_alive())
        self.assertIsNone(backend.tunnel)
        for name in ('browser', 'runtime', 'context', 'page'):
            self.assertIsNone(getattr(backend, name))
        for name in ('_sessions', '_pending', '_requests', '_hops', '_observations',
                     '_latest_business', '_auth_attempts', '_page_sessions', '_bound_pages',
                     '_page_documents', '_committed_pages', '_rejected_targets',
                     '_pending_rejected_targets', '_rejected_pages'):
            self.assertFalse(getattr(backend, name), name)

    def test_policy_close_failure_still_releases_browser_driver_and_guard(self):
        backend = self.native()
        browser, runtime, tunnel = backend.browser, backend.runtime, backend.tunnel
        problem = OSError('controlled policy close fault')
        pac = SimpleNamespace(close=Mock(side_effect=problem))
        backend.wire.network_policy = NetworkPolicy(pac=pac)
        with self.assertRaises(OSError) as caught:
            backend.close()
        self.assertIs(caught.exception, problem)
        pac.close.assert_called_once_with()
        self.assert_released(backend, browser, runtime, tunnel)

    def test_browser_close_failure_reaches_existing_service_ownership_guard(self):
        backend = self.native()
        browser, runtime, tunnel = backend.browser, backend.runtime, backend.tunnel
        problem = OSError('controlled owned browser close fault')
        browser.close.side_effect = problem
        runtime.stop.side_effect = RuntimeError('controlled later driver close fault')
        service = GuidedService.__new__(GuidedService)
        service._lock = threading.RLock()
        service._backends = {'owned': backend}
        lease = Mock()
        service._session_leases = {'owned': lease}
        service._closing_backends = 0
        service._closure_uncertain = False
        with self.assertRaises(OSError) as caught:
            service._close_backend('owned')
        self.assertIs(caught.exception, problem)
        self.assertTrue(service._closure_uncertain)
        self.assertEqual(service._closing_backends, 0)
        lease.close.assert_called_once_with()
        self.assert_released(backend, browser, runtime, tunnel)

    def test_pacer_detach_failure_discards_queue_and_continues_owned_cleanup(self):
        backend = self.native()
        browser, runtime, tunnel = backend.browser, backend.runtime, backend.tunnel
        pacer = backend._request_pacer = NativeRequestPacer(backend)
        connection = pacer.connection = Mock(closed=False)
        problem = OSError('controlled pump detach fault')
        connection.remove_pump_callback.side_effect = problem
        pacer.queue.append(object())
        pacer.bytes = 17
        with self.assertRaises(OSError) as caught:
            backend.close()
        self.assertIs(caught.exception, problem)
        self.assertIsNone(pacer.connection)
        self.assertFalse(pacer.queue)
        self.assertEqual(pacer.bytes, 0)
        self.assert_released(backend, browser, runtime, tunnel)
        backend.close()
        connection.remove_pump_callback.assert_called_once_with(pacer.pump)
        browser.close.assert_called_once_with()
        runtime.stop.assert_called_once_with()

    def test_guard_close_failure_still_discards_native_state(self):
        backend = self.native()
        browser, runtime, tunnel = backend.browser, backend.runtime, backend.tunnel
        real_close = tunnel.close
        problem = OSError('controlled guard close fault after release')
        def close():
            real_close()
            raise problem
        with patch.object(tunnel, 'close', side_effect=close) as closing:
            with self.assertRaises(OSError) as caught:
                backend.close()
            self.assertIs(caught.exception, problem)
            self.assert_released(backend, browser, runtime, tunnel)
            backend.close()
            closing.assert_called_once_with()

    def test_startup_classification_survives_a_separate_cleanup_error(self):
        problem = OSError('controlled cleanup error')
        policy = NetworkPolicy(pac=SimpleNamespace(close=Mock(side_effect=problem)))
        transport = lambda *args: SimpleNamespace(network_policy=policy)
        with patch('vibe_job_radar.guided.browser.environment_report', return_value={
                'playwright_version': None, 'launch_tested': False, 'ready': False}):
            with self.assertRaises(BrowserStartupError) as caught:
                PlaywrightBackend(None, None, threading.Event(), transport_factory=transport)
        self.assertEqual(caught.exception.code, 'playwright_missing')
        self.assertEqual(caught.exception.report['stage'], 'version')
        self.assertIs(caught.exception.__cause__, problem)
        self.assertIsInstance(problem.__context__, BrowserStartupError)
        self.assertEqual(problem.__context__.code, 'playwright_missing')
        policy.pac.close.assert_called_once_with()


if __name__ == '__main__':
    unittest.main()
