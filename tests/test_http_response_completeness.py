"""Real HTTP framing parser checks; synthetic in-memory wire data, no network."""
import http.client
import io
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from vibe_job_radar.network import FetchError, SafeHTTP
from vibe_job_radar.network_policy import NetworkPolicy
from vibe_job_radar.guided.contracts import CrawlError
from vibe_job_radar.guided.transport import PinnedTransport


class ResponseCompletenessTests(unittest.TestCase):
    def request(self, wire, *, bridge=False, method='GET', limit=100):
        socket = SimpleNamespace(makefile=lambda _: io.BytesIO(wire))
        response = http.client.HTTPResponse(socket, method=method)
        response.begin()
        connection = Mock()
        connection.getresponse.return_value = response
        policy = NetworkPolicy()
        if bridge:
            adapter = SimpleNamespace(key='fixture', domains=('example.com',), resource_domains=())
            client = PinnedTransport(adapter, Mock(), threading.Event())
            client.bind_policy(policy)
            module = 'vibe_job_radar.guided.transport'
            operation = lambda: client.fetch('https://example.com/job', method=method)
        else:
            client = SafeHTTP({'example.com'}, interval=0, max_bytes=limit, network_policy=policy)
            module = 'vibe_job_radar.network'
            operation = lambda: client.request('https://example.com/job', method=method)
        with patch(module + '.validate_public_url', return_value=('example.com', ('8.8.8.8',), '/job')), \
                patch(module + '.PinnedHTTPSConnection', return_value=connection):
            try:
                return operation()
            finally:
                connection.close.assert_called_once()
                response.close()

    def test_public_transport_rejects_premature_eof(self):
        # The prefix is valid JSON/HTML on its own; parsing it cannot detect loss.
        for prefix in (b'{"jobs":[]}', b'<p>First requirement</p>'):
            wire = b'HTTP/1.1 200 OK\r\nContent-Length: 80\r\n\r\n' + prefix
            with self.subTest(prefix=prefix), self.assertRaisesRegex(FetchError, '^network_error: IncompleteRead$'):
                self.request(wire)

    def test_browser_bridge_rejects_premature_eof(self):
        wire = b'HTTP/1.1 200 OK\r\nContent-Length: 80\r\n\r\n<p>First requirement</p>'
        with self.assertRaisesRegex(CrawlError, '^network_error$'):
            self.request(wire, bridge=True)

    def test_complete_and_bodyless_responses_keep_original_semantics(self):
        cases = (
            (b'HTTP/1.1 200 OK\r\nContent-Length: 3\r\n\r\nabc', 'GET', b'abc'),
            (b'HTTP/1.1 200 OK\r\nConnection: close\r\n\r\nabc', 'GET', b'abc'),
            (b'HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n3\r\nabc\r\n0\r\n\r\n', 'GET', b'abc'),
            (b'HTTP/1.1 200 OK\r\nContent-Length: 80\r\n\r\n', 'HEAD', b''),
            (b'HTTP/1.1 204 No Content\r\nContent-Length: 80\r\n\r\n', 'GET', b''),
        )
        for bridge in (False, True):
            for wire, method, expected in cases:
                with self.subTest(bridge=bridge, method=method, wire=wire):
                    self.assertEqual(self.request(wire, bridge=bridge, method=method).body, expected)
        self.assertEqual(self.request(b'HTTP/1.1 304 Not Modified\r\nContent-Length: 80\r\n\r\n', bridge=True).body, b'')

    def test_existing_chunked_failure_and_size_limit_still_stop(self):
        for bridge, error in ((False, FetchError), (True, CrawlError)):
            with self.subTest(bridge=bridge), self.assertRaisesRegex(error, '^network_error'):
                self.request(b'HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n5\r\nabc', bridge=bridge)
        with self.assertRaisesRegex(FetchError, '^response_too_large$'):
            self.request(b'HTTP/1.1 200 OK\r\nContent-Length: 80\r\n\r\nabcdef', limit=3)


if __name__ == '__main__':
    unittest.main()
