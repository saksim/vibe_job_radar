"""PAC withdrawal must stop origin bytes after blocking connection phases."""
import unittest
from unittest.mock import Mock, patch

from vibe_job_radar.network import FetchError, PinnedHTTPSConnection
from vibe_job_radar.network_policy import NetworkPolicy
from vibe_job_radar.pac import PacSnapshot


HOST = 'connection.fixture.invalid'
IP = '93.184.216.34'


class PacConnectionRevocationTests(unittest.TestCase):
    def connection(self):
        allowed = [True]
        snapshot = PacSnapshot('function FindProxyForURL(url, host) { return "DIRECT"; }',
                               lambda: allowed[0])
        # Freeze the already chosen DIRECT route; no PAC worker or network runs.
        snapshot._cache[HOST] = None
        policy = NetworkPolicy('explicit_workspace', pac=snapshot)
        return allowed, PinnedHTTPSConnection(HOST, IP, 1, network_policy=policy)

    def test_revoke_during_dial_prevents_tls_and_origin_request(self):
        allowed, conn = self.connection()
        raw, secured = Mock(), Mock()
        def dial(*args):
            allowed[0] = False
            return raw
        with patch('socket.create_connection', side_effect=dial) as connect, \
                patch.object(conn._context, 'wrap_socket', return_value=secured) as tls:
            with self.assertRaisesRegex(FetchError, '^pac_revoked$'):
                conn.request('GET', '/jobs')
        self.assertEqual(connect.call_count, 1)
        tls.assert_not_called()
        raw.close.assert_called_once()
        secured.sendall.assert_not_called()
        self.assertIsNone(conn.sock)

    def test_revoke_during_tls_prevents_origin_request(self):
        allowed, conn = self.connection()
        raw, secured = Mock(), Mock()
        def wrap(*args, **kwargs):
            allowed[0] = False
            return secured
        with patch('socket.create_connection', return_value=raw) as connect, \
                patch.object(conn._context, 'wrap_socket', side_effect=wrap):
            with self.assertRaisesRegex(FetchError, '^pac_revoked$'):
                conn.request('GET', '/jobs')
        self.assertEqual(connect.call_count, 1)
        secured.close.assert_called_once()
        secured.sendall.assert_not_called()
        self.assertIsNone(conn.sock)

    def test_revoke_connected_route_prevents_first_request(self):
        allowed, conn = self.connection()
        raw, secured = Mock(), Mock()
        with patch('socket.create_connection', return_value=raw) as connect, \
                patch.object(conn._context, 'wrap_socket', return_value=secured):
            conn.connect()
            allowed[0] = False
            with self.assertRaisesRegex(FetchError, '^pac_revoked$'):
                conn.request('POST', '/jobs', body=b'fixture body')
        self.assertEqual(connect.call_count, 1)
        secured.close.assert_called_once()
        secured.sendall.assert_not_called()
        self.assertIsNone(conn.sock)

    def test_active_route_preserves_one_request_and_original_hostname(self):
        _, conn = self.connection()
        raw, secured = Mock(), Mock()
        try:
            with patch('socket.create_connection', return_value=raw) as connect, \
                    patch.object(conn._context, 'wrap_socket', return_value=secured) as tls:
                conn.request('GET', '/jobs')
            self.assertEqual(connect.call_count, 1)
            tls.assert_called_once_with(raw, server_hostname=HOST)
            secured.sendall.assert_called_once()
            request = secured.sendall.call_args.args[0]
            self.assertTrue(request.startswith(b'GET /jobs HTTP/1.1\r\n'))
            self.assertIn(('Host: ' + HOST).encode(), request)
            self.assertEqual(conn.connected_ip, IP)
        finally:
            conn.close()

    def test_socket_close_error_preserves_revocation_classification(self):
        for phase in ('tls', 'connected'):
            with self.subTest(phase=phase):
                allowed, conn = self.connection()
                raw, secured = Mock(), Mock()
                secured.close.side_effect = OSError('fixture close failure')
                def wrap(*args, **kwargs):
                    if phase == 'tls':
                        allowed[0] = False
                    return secured
                with patch('socket.create_connection', return_value=raw), \
                        patch.object(conn._context, 'wrap_socket', side_effect=wrap):
                    if phase == 'connected':
                        conn.connect()
                        allowed[0] = False
                    with self.assertRaisesRegex(FetchError, '^pac_revoked$'):
                        conn.request('GET', '/jobs')
                secured.close.assert_called_once()
                secured.sendall.assert_not_called()
                self.assertIsNone(conn.sock)


if __name__ == '__main__':
    unittest.main()
