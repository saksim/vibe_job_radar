"""An OPTIONS 204 must leave the reused HTTP stream at the next status line."""
from pathlib import Path
import socket
import sys
import tempfile
import unittest
from unittest.mock import patch

with patch.object(sys, 'path', [str(Path(__file__).resolve().parents[1] / 'scripts'), *sys.path]):
    import run_native_liepin_search as fixture


class NativeSearchFixtureFramingTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        # Exercise actual HTTP bytes on loopback. TLS is independently covered
        # by the real browser suite, and no machine trust is changed here.
        with patch.object(fixture.ssl, 'SSLContext') as context:
            context.return_value.wrap_socket.side_effect = lambda sock, **_: sock
            self.server = fixture.SearchFixture(Path(self.directory.name))
        self.addCleanup(self.server.close)
        self.connection = socket.create_connection(self.server.server.server_address, timeout=2)
        self.addCleanup(self.connection.close)

    def request(self, method, path):
        self.connection.sendall((f'{method} {path} HTTP/1.1\r\nHost: {fixture.API_HOST}\r\n'
            'Content-Length: 0\r\nConnection: keep-alive\r\n\r\n').encode('ascii'))

    def headers(self):
        # Do not read ahead: a 204 has no response body to consume or discard.
        data = bytearray()
        while not data.endswith(b'\r\n\r\n'):
            byte = self.connection.recv(1)
            self.assertTrue(byte, 'response ended before complete headers')
            data.extend(byte)
            self.assertLess(len(data), 8192)
        return bytes(data)

    def test_preflight_204_has_no_content_length_or_gzip_representation(self):
        self.request('OPTIONS', fixture.PATH)
        headers = self.headers().lower()
        self.assertTrue(headers.startswith(b'http/1.1 204 '))
        self.assertNotIn(b'content-length:', headers)
        self.assertNotIn(b'content-encoding:', headers)

    def test_following_response_starts_with_http_status_and_keeps_cors(self):
        self.request('OPTIONS', fixture.PATH)
        self.assertTrue(self.headers().startswith(b'HTTP/1.1 204 '))
        self.request('GET', fixture.SUGGEST_PATH)
        headers = self.headers()
        self.assertTrue(headers.startswith(b'HTTP/1.1 200 OK\r\n'),
                        'bytes left after the 204 corrupt the next HTTP response')
        self.assertIn(b'Access-Control-Allow-Origin: '+fixture.URL.encode('ascii')+b'\r\n',headers)
        self.assertIn(b'Content-Type: application/json\r\n',headers)
        size = int(next(line.split(b':',1)[1] for line in headers.split(b'\r\n')
                        if line.lower().startswith(b'content-length:')))
        body = bytearray()
        while len(body) < size:
            data = self.connection.recv(size-len(body))
            self.assertTrue(data)
            body.extend(data)
        self.assertIn(b'suggestList',body)
