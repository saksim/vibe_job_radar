"""Local socket ownership races; no browser, DNS, credentials or upstream traffic."""
import socket
import threading
import unittest
from unittest.mock import patch

from vibe_job_radar.guided.native_tunnel import NativeTunnel
from vibe_job_radar.network_policy import NetworkPolicy


class _CloseGate:
    """Pause only the first close, leaving a concurrent owner close effective."""
    def __init__(self, sock):
        self.sock = sock
        self.entered, self.release = threading.Event(), threading.Event()
        self._lock = threading.Lock()
        self.calls = 0

    def settimeout(self, value):
        return self.sock.settimeout(value)

    def recv(self, size):
        return self.sock.recv(size)

    def shutdown(self, how):
        return self.sock.shutdown(how)

    def close(self):
        with self._lock:
            self.calls += 1
            first = self.calls == 1
        if first:
            self.entered.set()
            if not self.release.wait(5):
                raise RuntimeError('test close gate expired')
        self.sock.close()


class _CloseErrorAfterRelease:
    def __init__(self, sock):
        self.sock = sock

    def close(self):
        self.sock.close()
        raise OSError('controlled cleanup failure after actual close')


class NativeTunnelLifecycleTests(unittest.TestCase):
    def test_close_owns_accepted_socket_before_late_handler_runs(self):
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()
        accepted, worker_errors, reads = {}, [], []
        client = None
        guard = NativeTunnel(('jobs.fixture.test',), NetworkPolicy(), threading.Event())
        original = guard._handle
        real_recv = socket.socket.recv

        def delayed(server_socket):
            accepted.update(socket=server_socket, worker=threading.current_thread())
            entered.set()
            try:
                if not release.wait(5):
                    raise RuntimeError('test handler dispatch gate expired')
                original(server_socket)
            except BaseException as error:
                worker_errors.append(type(error).__name__)
                raise
            finally:
                finished.set()

        def observe_recv(sock, *args, **kwargs):
            if sock is accepted.get('socket'):
                reads.append(True)
            return real_recv(sock, *args, **kwargs)

        try:
            with patch.object(guard, '_handle', delayed), \
                    patch.object(guard, '_open') as upstream, \
                    patch.object(socket.socket, 'recv', observe_recv):
                client = socket.create_connection(guard.server.server_address, 2)
                self.assertTrue(entered.wait(2))
                guard.close()
                # This assertion precedes releasing the worker: eventual
                # cleanup after scheduling does not satisfy close ownership.
                self.assertFalse(release.is_set())
                self.assertEqual(accepted['socket'].fileno(), -1)
                self.assertEqual(guard.server.socket.fileno(), -1)
                self.assertFalse(guard.thread.is_alive())
                self.assertTrue(accepted['worker'].is_alive())
                release.set()
                self.assertTrue(finished.wait(2))
                accepted['worker'].join(2)
                self.assertFalse(accepted['worker'].is_alive())
                self.assertEqual(reads, [])
                self.assertEqual(worker_errors, [])
                upstream.assert_not_called()
                self.assertEqual(guard.connections, 0)
                with guard._lock:
                    self.assertNotIn(accepted['socket'], guard._sockets)
        finally:
            release.set()
            if client is not None:
                client.close()
            if accepted:
                accepted['worker'].join(2)
                accepted['socket'].close()
            guard.close()

    def test_failed_worker_start_closes_socket_restores_slot_and_keeps_error(self):
        guard = NativeTunnel(('jobs.fixture.test',), NetworkPolicy(), threading.Event())
        request, peer = socket.socketpair()
        wrapped = _CloseErrorAfterRelease(request)
        original_error = RuntimeError('controlled worker start failure')
        owned_at_start, acquired = [], []

        def failed_start(_thread):
            with guard._lock:
                owned_at_start.append(wrapped in guard._sockets)
            raise original_error

        try:
            with patch.object(threading.Thread, 'start', failed_start):
                with self.assertRaises(RuntimeError) as caught:
                    guard.server.process_request(wrapped, ('127.0.0.1', 0))
            self.assertIs(caught.exception, original_error)
            self.assertEqual(owned_at_start, [True])
            self.assertEqual(request.fileno(), -1)
            with guard._lock:
                self.assertNotIn(wrapped, guard._sockets)
            for _ in range(16):
                slot = guard._slots.acquire(blocking=False)
                acquired.append(slot)
                self.assertTrue(slot)
            self.assertFalse(guard._slots.acquire(blocking=False))
        finally:
            for slot in acquired:
                if slot:
                    guard._slots.release()
            request.close()
            peer.close()
            guard.close()

    def test_handler_keeps_socket_owned_until_close_has_released_it(self):
        guard = NativeTunnel(('jobs.fixture.test',), NetworkPolicy(), threading.Event())
        request, peer = socket.socketpair()
        wrapped = _CloseGate(request)
        worker_errors = []

        def handle():
            try:
                guard._handle(wrapped)
            except BaseException as error:
                worker_errors.append(type(error).__name__)

        worker = threading.Thread(target=handle, daemon=True)
        try:
            with patch.object(guard, '_open') as upstream:
                # EOF enters the real handler's finally without a CONNECT or
                # dial. Its first close is paused before releasing the socket.
                peer.shutdown(socket.SHUT_WR)
                worker.start()
                self.assertTrue(wrapped.entered.wait(2))
                guard.close()
                self.assertFalse(wrapped.release.is_set())
                self.assertEqual(request.fileno(), -1)
                self.assertTrue(worker.is_alive())
                wrapped.release.set()
                worker.join(2)
                self.assertFalse(worker.is_alive())
                self.assertEqual(worker_errors, [])
                upstream.assert_not_called()
                with guard._lock:
                    self.assertNotIn(wrapped, guard._sockets)
        finally:
            wrapped.release.set()
            peer.close()
            if worker.ident is not None:
                worker.join(2)
            request.close()
            guard.close()


if __name__ == '__main__':
    unittest.main()
