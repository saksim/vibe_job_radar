"""Owned handler completion, using local sockets and a gated upstream call."""
from contextlib import contextmanager
import socket
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from vibe_job_radar.guided.native_tunnel import NativeTunnel
from vibe_job_radar.guided.service import GuidedService
from vibe_job_radar.network_policy import NetworkPolicy


class NativeTunnelInflightTests(unittest.TestCase):
    @contextmanager
    def opening(self):
        guard = NativeTunnel(('jobs.fixture.test',), NetworkPolicy(), threading.Event())
        request, peer = socket.socketpair()
        upstream, other = socket.socketpair()
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()
        workers = []
        original = guard.server.process_request_thread

        def process(*args):
            workers.append(threading.current_thread())
            try:
                return original(*args)
            finally:
                finished.set()

        def open_upstream(_host):
            entered.set()
            if not release.wait(8):
                raise RuntimeError('controlled upstream gate expired')
            return upstream

        try:
            with patch.object(guard.server, 'process_request_thread', process), \
                    patch.object(guard, '_open', open_upstream):
                guard.server.process_request(request, ('127.0.0.1', 0))
                peer.sendall(('CONNECT jobs.fixture.test:443 HTTP/1.1\r\nProxy-Authorization: '
                              + guard._authorization + '\r\n\r\n').encode('ascii'))
                self.assertTrue(entered.wait(2))
                yield SimpleNamespace(guard=guard, request=request, upstream=upstream,
                                      release=release, finished=finished, workers=workers)
        finally:
            release.set()
            finished.wait(2)
            for worker in workers:
                worker.join(2)
            request.close(); peer.close(); upstream.close(); other.close()
            guard.close()
            self.assertTrue(all(not worker.is_alive() for worker in workers))

    def test_unfinished_open_keeps_service_ownership_and_repeated_close_uncertain(self):
        with self.opening() as case:
            service = GuidedService.__new__(GuidedService)
            service._lock = threading.RLock()
            service._backends = {'fixture': case.guard}
            service._session_leases = {}
            service._closing_backends = service._owner_depth = 0
            service._busy = service._closure_uncertain = False
            service._ownership = Mock()
            with self.assertRaisesRegex(RuntimeError, '^native_tunnel_cleanup_incomplete$'):
                service._close_backend('fixture')
            self.assertTrue(service._closure_uncertain)
            service._release_owner_if_idle()
            service._ownership.release.assert_not_called()
            self.assertEqual(service._closing_backends, 0)
            self.assertFalse(case.release.is_set())
            self.assertTrue(case.workers[0].is_alive())
            self.assertEqual(case.request.fileno(), -1)
            self.assertNotEqual(case.upstream.fileno(), -1)
            with self.assertRaisesRegex(RuntimeError, '^native_tunnel_cleanup_incomplete$'):
                case.guard.close()
            case.release.set()
            self.assertTrue(case.finished.wait(2))
            case.guard.close()
            self.assertEqual(case.upstream.fileno(), -1)
            self.assertEqual(case.guard.connections, 0)

    def test_finished_handler_releases_socket_and_slot_before_close_succeeds(self):
        with self.opening() as case:
            original = case.guard.server.server_close
            def close_listener():
                original()
                case.release.set()
            with patch.object(case.guard.server, 'server_close', close_listener):
                case.guard.close()
            self.assertEqual(case.request.fileno(), -1)
            self.assertEqual(case.upstream.fileno(), -1)
            self.assertEqual(case.guard.connections, 0)
            with case.guard._lock:
                self.assertFalse(case.guard._pending_requests)
                self.assertFalse(case.guard._sockets)
            acquired = []
            try:
                for _ in range(16):
                    acquired.append(case.guard._slots.acquire(blocking=False))
                    self.assertTrue(acquired[-1])
                self.assertFalse(case.guard._slots.acquire(blocking=False))
            finally:
                for slot in acquired:
                    if slot:
                        case.guard._slots.release()
            case.guard.close()

    def test_first_shutdown_error_wins_and_handler_wait_shares_join_budget(self):
        with self.opening() as case:
            original = case.guard.server.shutdown
            failure = OSError('controlled shutdown failure after actual shutdown')
            def shutdown():
                original()
                raise failure
            # The original listener join has consumed the two-second budget.
            # No second two-second wait may be assigned to a pending handler.
            with patch.object(case.guard.server, 'shutdown', shutdown), \
                    patch('vibe_job_radar.guided.native_tunnel.time.monotonic', side_effect=[10., 12.]), \
                    patch.object(case.guard.thread, 'join', wraps=case.guard.thread.join) as join, \
                    patch.object(case.guard._requests_done, 'wait') as wait:
                with self.assertRaises(OSError) as caught:
                    case.guard.close()
                self.assertIs(caught.exception, failure)
                join.assert_called_once_with(timeout=2)
                wait.assert_not_called()
            self.assertFalse(case.release.is_set())
            self.assertEqual(case.request.fileno(), -1)
            with self.assertRaisesRegex(RuntimeError, '^native_tunnel_cleanup_incomplete$'):
                case.guard.close()


if __name__ == '__main__':
    unittest.main()
