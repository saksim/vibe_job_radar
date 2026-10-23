"""Constructor failure closes only its own guard listener; no browser/upstream."""
import socketserver
import threading
import unittest
from unittest.mock import patch

from vibe_job_radar.guided.native_tunnel import NativeTunnel
from vibe_job_radar.network_policy import NetworkPolicy


class NativeTunnelStartupTests(unittest.TestCase):
    def assert_failed_start_is_clean(self, target, *, cleanup_failure=False):
        guard = NativeTunnel.__new__(NativeTunnel)
        original = RuntimeError('controlled guard thread startup failure')
        real_close = socketserver.TCPServer.server_close
        closed = []

        def close(server):
            closed.append(server)
            real_close(server)
            if cleanup_failure:
                raise OSError('controlled cleanup error after actual socket close')

        try:
            with patch(target, side_effect=original), patch.object(socketserver.TCPServer, 'server_close', close):
                with self.assertRaises(RuntimeError) as caught:
                    guard.__init__(('jobs.fixture.test',), NetworkPolicy(), threading.Event())
            self.assertIs(caught.exception, original)
            self.assertEqual(closed, [guard.server])
            self.assertEqual(guard.server.socket.fileno(), -1)
            self.assertTrue(guard._stop.is_set())
        finally:
            if hasattr(guard, 'server'):
                real_close(guard.server)

    def test_thread_allocation_failure_closes_bound_listener(self):
        self.assert_failed_start_is_clean('vibe_job_radar.guided.native_tunnel.threading.Thread')

    def test_thread_start_failure_closes_bound_listener(self):
        self.assert_failed_start_is_clean('threading.Thread.start')

    def test_cleanup_failure_does_not_replace_startup_exception(self):
        self.assert_failed_start_is_clean('threading.Thread.start', cleanup_failure=True)


if __name__ == '__main__':
    unittest.main()
