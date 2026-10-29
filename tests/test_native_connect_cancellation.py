"""Real loopback handshakes stalled before tunnel creation; no upstream traffic."""
import socket
import threading
import time
import unittest
from unittest.mock import Mock, patch

from vibe_job_radar.guided.native_tunnel import NativeTunnel
from vibe_job_radar.guided.native_connect import connect
from vibe_job_radar.loopback_proxy import LoopbackProxy
from vibe_job_radar.loopback_socks import LoopbackSocks5
from vibe_job_radar.network import FetchError
from vibe_job_radar.network_policy import NetworkPolicy


class NativeConnectCancellationTests(unittest.TestCase):
    def stalled_handshake(self, proxy_type):
        listener = socket.socket()
        listener.bind(('127.0.0.1', 0)); listener.listen(1); listener.settimeout(4)
        received, eof = threading.Event(), threading.Event()
        remote, server_errors = [], []

        def proxy_peer():
            try:
                peer, _ = listener.accept(); remote.append(peer)
                with peer:
                    peer.settimeout(8)
                    if peer.recv(8192): received.set()
                    if not peer.recv(1): eof.set()
            except OSError as exc:
                if not received.is_set(): server_errors.append(type(exc).__name__)

        worker = threading.Thread(target=proxy_peer, daemon=True)
        worker.start()
        guard = NativeTunnel(('jobs.fixture.test',), NetworkPolicy('explicit_application',
            proxy_type(*listener.getsockname())), threading.Event(), timeout=20)
        client = None
        try:
            with patch.object(guard, 'resolve_host', return_value=('93.184.216.34',)):
                client = socket.create_connection(guard.server.server_address, 2)
                client.sendall(('CONNECT jobs.fixture.test:443 HTTP/1.1\r\nProxy-Authorization: '
                                + guard._authorization + '\r\n\r\n').encode())
                self.assertTrue(received.wait(3), server_errors)
                started = time.monotonic()
                guard.close()
                self.assertLess(time.monotonic()-started, 2)
                self.assertTrue(eof.wait(1), 'The uncompleted upstream handshake must be closed')
                with guard._lock:
                    self.assertFalse(guard._pending_requests)
                    self.assertFalse(guard._sockets)
                self.assertFalse(guard.thread.is_alive())
                self.assertEqual(guard.connections, 0)
                guard.close()
        finally:
            for peer in remote:
                try: peer.shutdown(socket.SHUT_RDWR)
                except OSError: pass
                peer.close()
            if client: client.close()
            listener.close(); worker.join(3)
            with guard._requests_done:
                guard._requests_done.wait_for(lambda: not guard._pending_requests, timeout=3)
            guard.close()

    def test_close_interrupts_http_connect_response_wait(self):
        self.stalled_handshake(LoopbackProxy)

    def test_close_interrupts_socks_greeting_wait(self):
        self.stalled_handshake(LoopbackSocks5)

    def test_cancel_during_tcp_connect_closes_owned_socket_without_retry(self):
        raw, stopped = Mock(), threading.Event()
        raw.connect_ex.return_value = 10035
        owned = []
        def waiting(*_):
            self.assertEqual(owned, [raw])
            stopped.set()
            return [], [], []
        with patch('vibe_job_radar.guided.native_connect.socket.socket', return_value=raw), \
                patch('vibe_job_radar.guided.native_connect.select.select', side_effect=waiting), \
                patch('socket.getaddrinfo') as dns:
            with self.assertRaisesRegex(FetchError, '^paused$'):
                connect(('93.184.216.34',443), 20, stopped=stopped.is_set, own=owned.append)
        raw.connect_ex.assert_called_once(); raw.close.assert_called_once(); dns.assert_not_called()

    def test_close_before_socket_registration_cannot_start_connect(self):
        guard = NativeTunnel(('jobs.fixture.test',), NetworkPolicy(), threading.Event())
        raw = Mock()
        def allocate(*args):
            # Simulate close winning immediately before registration under the
            # same owner lock. No newly created socket may escape that snapshot.
            guard.close()
            return raw
        try:
            with patch.object(guard, 'resolve_host', return_value=('93.184.216.34',)), \
                    patch('vibe_job_radar.guided.native_connect.socket.socket', side_effect=allocate):
                with self.assertRaisesRegex(FetchError, '^paused$'):
                    guard._open('jobs.fixture.test')
            raw.connect_ex.assert_not_called()
            self.assertTrue(raw.close.called)
            self.assertFalse(guard._sockets)
        finally:
            guard.close()

    def test_authentication_wait_cancels_without_waiting_for_lock_or_sending(self):
        guard = NativeTunnel(('jobs.fixture.test',), NetworkPolicy(), threading.Event())
        proxy, failures = Mock(), []
        def waiting():
            try: guard._open_proxy(proxy, '93.184.216.34', 20)
            except FetchError as exc: failures.append(exc.code)
        worker = threading.Thread(target=waiting, daemon=True)
        guard._proxy_auth_lock.acquire()
        try:
            worker.start(); guard.cancelled.set(); worker.join(1)
            self.assertFalse(worker.is_alive())
            self.assertEqual(failures, ['paused'])
            proxy.open_tunnel.assert_not_called()
        finally:
            guard._proxy_auth_lock.release(); worker.join(2); guard.close()


if __name__ == '__main__':
    unittest.main()
