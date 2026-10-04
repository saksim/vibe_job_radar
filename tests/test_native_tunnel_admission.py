"""Real loopback admission tests; no browser, certificate or upstream traffic."""
import select
import socket
import socketserver
import threading
import time
import unittest
from unittest.mock import patch

from vibe_job_radar.guided.native_tunnel import NativeTunnel
from vibe_job_radar.network_policy import NetworkPolicy


class NativeTunnelAdmissionTests(unittest.TestCase):
    def test_burst_can_connect_before_accept_without_bypassing_authentication(self):
        accepting = threading.Event()
        release = threading.Event()
        original = socketserver.TCPServer.get_request

        def paused_accept(server):
            accepting.set()
            if not release.wait(10):
                raise RuntimeError('test accept gate expired')
            return original(server)

        clients = []
        with patch.object(socketserver.TCPServer, 'get_request', paused_accept):
            guard = NativeTunnel(('jobs.fixture.test',), NetworkPolicy(), threading.Event())
            try:
                with patch.object(guard, '_open') as upstream:
                    # Eight simultaneous TCP handshakes fit within the existing
                    # sixteen-handler limit, even while accept is descheduled.
                    for index in range(8):
                        client = socket.socket()
                        clients.append(client)
                        client.setblocking(False)
                        client.connect_ex(guard.server.server_address)
                        if index == 0:
                            self.assertTrue(accepting.wait(2))
                    pending = set(clients)
                    deadline = time.monotonic() + 2
                    while pending and time.monotonic() < deadline:
                        _, ready, errors = select.select(
                            [], list(pending), list(pending), max(0, deadline - time.monotonic()))
                        for client in set(ready + errors):
                            self.assertEqual(client.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR), 0)
                            pending.remove(client)
                    self.assertEqual(len(pending), 0, 'listener dropped part of a bounded connection burst')
                    release.set()
                    for client in clients:
                        client.settimeout(2)
                        client.sendall(b'CONNECT jobs.fixture.test:443 HTTP/1.1\r\n'
                                       b'Host: jobs.fixture.test:443\r\n\r\n')
                        self.assertTrue(client.recv(1024).startswith(b'HTTP/1.1 407'))
                    upstream.assert_not_called()
                    self.assertEqual(guard.connections, 0)
            finally:
                release.set()
                for client in clients:
                    client.close()
                guard.close()

    def test_larger_listen_queue_keeps_sixteen_active_handler_limit(self):
        release = threading.Event()
        condition = threading.Condition()
        workers = []
        clients = []

        def hold_handler(client):
            with condition:
                workers.append(threading.current_thread())
                condition.notify_all()
            release.wait(10)

        guard = NativeTunnel(('jobs.fixture.test',), NetworkPolicy(), threading.Event())
        try:
            with patch.object(guard, '_handle', hold_handler), patch.object(guard, '_open') as upstream:
                for count in range(1, 17):
                    clients.append(socket.create_connection(guard.server.server_address, 2))
                    with condition:
                        self.assertTrue(condition.wait_for(lambda: len(workers) == count, 2))
                extra = socket.create_connection(guard.server.server_address, 2)
                clients.append(extra)
                try:
                    self.assertEqual(extra.recv(1), b'')
                except ConnectionResetError:
                    pass  # Windows may report the explicit rejection as reset.
                self.assertEqual(len(workers), 16)
                upstream.assert_not_called()
        finally:
            release.set()
            for client in clients:
                client.close()
            guard.close()
            for worker in workers:
                worker.join(2)
                self.assertFalse(worker.is_alive())


if __name__ == '__main__':
    unittest.main()
