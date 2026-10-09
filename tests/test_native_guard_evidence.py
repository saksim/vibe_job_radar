"""Real owned loopback traffic and observer transparency; no external source."""
import json
import socket
import sys
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from vibe_job_radar.guided.native_browser import NativeBackend
from vibe_job_radar.guided.native_tunnel import NativeTunnel
from vibe_job_radar.network import FetchError
from vibe_job_radar.network_policy import NetworkPolicy

ROOT = Path(__file__).resolve().parents[1]
with patch.object(sys, 'path', [str(ROOT / 'scripts'), *sys.path]):
    from run_native_auth_probe import ObservedBackend, PROBES
    import native_failure_evidence as evidence

SECRET = 'PRIVATE_FIXTURE_HEADER_AND_PAYLOAD'
HOST = 'jobs.fixture.test'


class NativeGuardEvidenceTests(unittest.TestCase):
    def backend(self, opener=None):
        guard = NativeTunnel((HOST,), NetworkPolicy(), threading.Event())
        self.addCleanup(guard.close)
        if opener is not None:
            guard._open = opener

        def initialize(backend):
            backend.tunnel = guard
            backend.cancelled = guard.cancelled
            backend.native_counts = {}
            backend._sessions = {}
            backend._pending = {}
            backend._requests = {}
            backend.error = None
            backend._closing = False
            backend._halted = False

        before = len(PROBES)
        self.addCleanup(lambda: PROBES.__delitem__(slice(before, None)))
        with patch.object(NativeBackend, '__init__', initialize):
            return ObservedBackend()

    def connect(self, backend, *, authorized=False, host=HOST, extra=''):
        guard = backend.tunnel
        client = socket.create_connection(guard.server.server_address, 3)
        client.settimeout(3)
        self.addCleanup(client.close)
        auth = 'Proxy-Authorization: ' + guard._authorization + '\r\n' if authorized else ''
        client.sendall(('CONNECT ' + host + ':443 HTTP/1.1\r\nHost: ' + host +
                        ':443\r\n' + auth + extra + '\r\n').encode('ascii'))
        response = bytearray()
        while not response.endswith(b'\r\n\r\n') and len(response) < 4096:
            part = client.recv(1)
            if not part:
                break
            response.extend(part)
        return client, bytes(response)

    def facts(self, backend):
        return evidence.facts(backend)['guard_transport']

    def finished(self, backend, count=1):
        deadline = time.monotonic() + 3
        while True:
            value = self.facts(backend)
            if value['available'] and value['operations']['handle']['returned'] >= count:
                return value
            if time.monotonic() >= deadline:
                self.fail('owned loopback handler did not finish')
            time.sleep(.01)

    def test_unauthenticated_and_forbidden_paths_keep_distinct_replies(self):
        for authorized, host, expected in [(False, HOST, 407), (True, 'other.fixture.test', 403)]:
            with self.subTest(status=expected):
                opener = Mock(side_effect=AssertionError('upstream must not run'))
                backend = self.backend(opener)
                client, response = self.connect(backend, authorized=authorized, host=host)
                self.assertTrue(response.startswith(('HTTP/1.1 ' + str(expected)).encode()))
                client.close()
                facts = self.finished(backend)
                self.assertEqual(facts['interval'], 'after_backend_init')
                self.assertTrue(facts['complete'])
                self.assertEqual(facts['operations']['accept']['returned'], 1)
                self.assertEqual(facts['operations']['dispatch']['started'], 1)
                self.assertEqual(facts['operations']['open']['started'], 0)
                self.assertEqual(facts['replies'][str(expected)]['returned'], 1)
                opener.assert_not_called()

    def test_upstream_failure_keeps_502_and_original_fixed_error(self):
        original = FetchError('network_error')
        opener = Mock(side_effect=original)
        backend = self.backend(opener)
        client, response = self.connect(backend, authorized=True)
        self.assertTrue(response.startswith(b'HTTP/1.1 502'))
        client.close()
        facts = self.finished(backend)
        self.assertEqual(facts['operations']['open'], {'started': 1, 'returned': 0, 'raised': 1})
        self.assertEqual(facts['replies']['502']['returned'], 1)
        self.assertEqual(backend.tunnel.last_error, 'network_error')
        self.assertEqual(backend.tunnel.connections, 0)
        opener.assert_called_once_with(HOST)

    def test_successful_connect_preserves_original_opaque_bytes(self):
        upstream, peer = socket.socketpair()
        self.addCleanup(upstream.close)
        self.addCleanup(peer.close)
        peer.settimeout(3)
        opener = Mock(return_value=upstream)
        backend = self.backend(opener)
        client, response = self.connect(backend, authorized=True)
        self.assertEqual(response, b'HTTP/1.1 200 Connection Established\r\n\r\n')
        client.sendall(SECRET.encode())
        self.assertEqual(peer.recv(1024), SECRET.encode())
        peer.sendall(b'unchanged response bytes')
        self.assertEqual(client.recv(1024), b'unchanged response bytes')
        client.close()
        peer.close()
        facts = self.finished(backend)
        self.assertEqual(facts['operations']['open'], {'started': 1, 'returned': 1, 'raised': 0})
        # Successful 200 writes are direct in the original handler, not _reply.
        self.assertEqual(sum(row['started'] for row in facts['replies'].values()), 0)
        self.assertEqual(backend.tunnel.connections, 1)
        opener.assert_called_once_with(HOST)
        self.assertNotIn(SECRET, json.dumps(facts))

    def test_owned_guards_have_separate_intervals_and_counters(self):
        first, second = self.backend(), self.backend()
        client, response = self.connect(first)
        self.assertTrue(response.startswith(b'HTTP/1.1 407'))
        client.close()
        one = self.finished(first)
        two = self.facts(second)
        self.assertTrue(two['available'])
        self.assertEqual(one['replies']['407']['returned'], 1)
        self.assertEqual(two['replies']['407']['started'], 0)
        self.assertEqual(two['operations']['accept']['started'], 0)

    def test_first_failure_snapshot_survives_cleanup_without_request_data(self):
        backend = self.backend()
        client, response = self.connect(backend, authorized=True, host='other.fixture.test',
                                        extra='X-Private-Fixture: ' + SECRET + '\r\n')
        self.assertTrue(response.startswith(b'HTTP/1.1 403'))
        client.close()
        self.finished(backend)
        evidence.fatal_failure(backend, 'local_proxy_connection_failed')
        first = json.loads(json.dumps(backend.probe['first_fatal']))
        self.assertEqual(first['connection_facts']['guard_transport']['replies']['403']['returned'], 1)
        backend.tunnel.close()
        self.assertEqual(backend.probe['first_fatal'], first)
        value = json.dumps(first)
        for secret in (SECRET, HOST, backend.tunnel.endpoint, backend.tunnel.password,
                       backend.tunnel._authorization):
            self.assertNotIn(secret, value)

    def test_missing_observer_is_unavailable_instead_of_zero(self):
        backend = self.backend()
        backend.__dict__.pop('_guard_evidence', None)
        facts = self.facts(backend)
        self.assertFalse(facts['available'])
        self.assertFalse(facts['complete'])
        self.assertIsNone(facts['operations'])
        self.assertIsNone(facts['replies'])

    def test_observer_fault_preserves_original_return_and_exception_identity(self):
        token = object()
        original_error = RuntimeError(SECRET)
        for result in (token, original_error):
            with self.subTest(raises=result is original_error):
                opener = Mock(side_effect=result) if isinstance(result, Exception) else Mock(return_value=result)
                backend = self.backend(opener)
                backend._guard_evidence._note = Mock(side_effect=RuntimeError(SECRET))
                if isinstance(result, Exception):
                    with self.assertRaises(RuntimeError) as caught:
                        backend.tunnel._open(HOST)
                    self.assertIs(caught.exception, result)
                else:
                    self.assertIs(backend.tunnel._open(HOST), result)
                opener.assert_called_once_with(HOST)
                self.assertFalse(self.facts(backend)['complete'])
                self.assertNotIn(SECRET, json.dumps(self.facts(backend)))

    def test_locked_observer_cannot_block_the_original_operation(self):
        token = object()
        opener = Mock(return_value=token)
        backend = self.backend(opener)
        probe = backend._guard_evidence
        seen = {}
        done = threading.Event()

        def run():
            try:
                seen['result'] = backend.tunnel._open(HOST)
                seen['snapshot'] = self.facts(backend)
            finally:
                done.set()

        thread = threading.Thread(target=run)
        probe._lock.acquire()
        try:
            thread.start()
            completed_while_locked = done.wait(2)
        finally:
            probe._lock.release()
            thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertTrue(completed_while_locked)
        self.assertIs(seen['result'], token)
        self.assertFalse(seen['snapshot']['available'])
        self.assertFalse(seen['snapshot']['complete'])
        opener.assert_called_once_with(HOST)

    def test_counters_cap_without_limiting_original_calls(self):
        with patch.object(sys, 'path', [str(ROOT / 'scripts'), *sys.path]):
            import native_guard_evidence as guard_evidence
        token = object()
        opener = Mock(return_value=token)
        backend = self.backend(opener)
        with patch.object(guard_evidence, 'LIMIT', 2):
            for _ in range(4):
                self.assertIs(backend.tunnel._open(HOST), token)
        facts = self.facts(backend)
        self.assertEqual(opener.call_count, 4)
        self.assertTrue(facts['overflow'])
        self.assertFalse(facts['complete'])
        self.assertEqual(facts['operations']['open'], {'started': 2, 'returned': 2, 'raised': 0})

    def test_final_snapshot_keeps_original_close_return_and_exception(self):
        for raises in (False, True):
            with self.subTest(raises=raises):
                backend = self.backend()
                token = RuntimeError(SECRET) if raises else object()
                original = Mock(side_effect=token) if raises else Mock(return_value=token)
                with patch.object(NativeBackend, 'close', original):
                    if raises:
                        with self.assertRaises(RuntimeError) as caught:
                            backend.close()
                        self.assertIs(caught.exception, token)
                    else:
                        self.assertIs(backend.close(), token)
                original.assert_called_once_with()
                self.assertTrue(backend.probe['guard_transport']['available'])
                self.assertNotIn(SECRET, json.dumps(backend.probe))


if __name__ == '__main__':
    unittest.main()
