"""CONNECT uses one deadline through slow headers, without upstream requests."""
import io
import unittest
from unittest.mock import patch

from vibe_job_radar.loopback_proxy import LocalProxyError, LoopbackProxy


class ClockSocket:
    def __init__(self, chunks, step=.4):
        self.now = 0
        self.chunks = list(chunks)
        self.step = step
        self.timeout = 1
        self.closed = False
        self.sent = []

    def settimeout(self, value):
        self.timeout = value

    def setsockopt(self, *args):
        pass

    def sendall(self, value):
        if self.step > self.timeout:
            self.now += self.timeout
            raise TimeoutError()
        self.now += self.step
        self.sent.append(value)

    def recv_into(self, buffer):
        if not self.chunks:
            return 0
        if self.step > self.timeout:
            self.now += self.timeout
            raise TimeoutError()
        self.now += self.step
        part = self.chunks.pop(0)
        buffer[:len(part)] = part
        return len(part)

    def makefile(self, *args, **kwargs):
        owner = self
        class Raw(io.RawIOBase):
            def readable(self):
                return True
            def readinto(self, buffer):
                return owner.recv_into(buffer)
        return io.BufferedReader(Raw())

    def close(self):
        self.closed = True


class ProxyDeadlineTests(unittest.TestCase):
    def invoke(self, raw, *, timeout=1, dial_time=0):
        def dial(endpoint, budget, source_address=None):
            self.assertEqual(endpoint, ('127.0.0.1', 1234))
            raw.now += dial_time
            raw.settimeout(budget)
            return raw
        with patch('socket.create_connection', side_effect=dial) as connect, \
                patch('time.monotonic', side_effect=lambda: raw.now):
            try:
                result = LoopbackProxy('127.0.0.1', 1234).open_tunnel('93.184.216.34', timeout)
            finally:
                self.assertEqual(connect.call_count, 1)
        return result

    def test_fragmented_status_and_headers_share_original_deadline(self):
        raw = ClockSocket([b'HTTP/1.1 ', b'200 OK\r\n', *[b'X: kept-alive\r\n'] * 10, b'\r\n'])
        with self.assertRaisesRegex(LocalProxyError, '^local_proxy_connection_failed$'):
            self.invoke(raw)
        self.assertLessEqual(raw.now, 1.000001)
        self.assertTrue(raw.closed)
        self.assertEqual(len(raw.sent), 1)

    def test_dial_time_is_subtracted_before_connect_request_send(self):
        raw = ClockSocket([b'HTTP/1.1 200 OK\r\n\r\n'], step=.4)
        with self.assertRaisesRegex(LocalProxyError, '^local_proxy_connection_failed$'):
            self.invoke(raw, dial_time=.8)
        self.assertLessEqual(raw.now, 1.000001)
        self.assertTrue(raw.closed)
        self.assertEqual(raw.sent, [])

    def test_success_returns_original_socket_with_remaining_budget(self):
        raw = ClockSocket([b'HTTP/1.1 200 OK\r\n\r\n'], step=.1)
        result = self.invoke(raw, dial_time=.1)
        self.assertIs(result, raw)
        self.assertFalse(raw.closed)
        self.assertAlmostEqual(raw.timeout, .7)
        self.assertTrue(raw.sent[0].startswith(b'CONNECT 93.184.216.34:443 HTTP/1.'))


if __name__ == '__main__':
    unittest.main()
