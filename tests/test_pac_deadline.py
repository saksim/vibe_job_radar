
"""Controlled startup/IPC latency; no real PAC worker, DNS or target connection."""
from contextlib import contextmanager
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from vibe_job_radar import pac_native
from vibe_job_radar.loopback_proxy import LocalProxyError
from vibe_job_radar.network import FetchError, SafeHTTP
from vibe_job_radar.network_policy import NetworkPolicy
from vibe_job_radar.pac import PacSnapshot

SCRIPT = 'function FindProxyForURL(url, host) { return "DIRECT"; }'
TARGET = 'https://example.com/'


class PacDeadlineTests(unittest.TestCase):
    @contextmanager
    def execution(self, *, delays=None, raw=b'{"raw":"DIRECT"}', revoked=False):
        delays = {} if delays is None else delays
        elapsed = [0.]
        permission_calls = [0]
        clock = MagicMock()
        clock.monotonic.side_effect = lambda: elapsed[0]
        context, receiver, sender = MagicMock(), MagicMock(), MagicMock()
        process = context.Process.return_value
        process.pid = 42
        process.is_alive.return_value = True
        context.Pipe.return_value = receiver, sender

        def advance(phase):
            elapsed[0] += delays.get(phase, 0.)
        def start():
            advance('start')
        def poll(wait):
            advance('poll')
            return True
        def receive(limit):
            self.assertEqual(limit, 4096)
            advance('receive')
            return raw
        def permission():
            permission_calls[0] += 1
            if permission_calls[0] == 3:
                advance('permission')
                return not revoked
            return True

        process.start.side_effect = start
        receiver.poll.side_effect = poll
        receiver.recv_bytes.side_effect = receive
        with patch.object(pac_native, 'available', return_value=True), \
             patch.object(pac_native, 'time', clock), \
             patch.object(pac_native.multiprocessing, 'get_context', return_value=context), \
             patch('socket.socket.connect', side_effect=AssertionError('No network in these tests')) as connect:
            yield SimpleNamespace(context=context, process=process, receiver=receiver, sender=sender,
                                  elapsed=elapsed, permission=permission, delays=delays)
        self.assertEqual(connect.call_count, 0)
        self.assertEqual(process.close.call_count, process.start.call_count)
        self.assertEqual(receiver.close.call_count, process.start.call_count)
        self.assertEqual(process.terminate.call_count, process.start.call_count)
        # Both original slots must be available after all owned cleanup.
        acquired = 0
        try:
            for _ in range(2):
                self.assertTrue(pac_native._SLOTS.acquire(blocking=False))
                acquired += 1
        finally:
            for _ in range(acquired):
                pac_native._SLOTS.release()

    def evaluate(self, state):
        return pac_native.evaluate(SCRIPT, TARGET, state.permission)

    def test_startup_consumes_the_original_allowance_without_a_fresh_wait(self):
        with self.execution(delays={'start': 9.}) as state:
            with self.assertRaisesRegex(LocalProxyError, 'pac_timeout'):
                self.evaluate(state)
            state.receiver.poll.assert_not_called()
            state.receiver.recv_bytes.assert_not_called()
            self.assertEqual(state.elapsed[0], 9.)

    def test_ready_ipc_after_the_deadline_is_not_a_successful_route(self):
        with self.execution(delays={'poll': 9.}) as state:
            with self.assertRaisesRegex(LocalProxyError, 'pac_timeout'):
                self.evaluate(state)
            state.receiver.recv_bytes.assert_called_once_with(4096)

    def test_time_spent_receiving_the_result_is_counted(self):
        with self.execution(delays={'receive': 9.}) as state:
            with self.assertRaisesRegex(LocalProxyError, 'pac_timeout'):
                self.evaluate(state)

    def test_final_permission_check_cannot_extend_success_acceptance(self):
        with self.execution(delays={'permission': 9.}) as state:
            with self.assertRaisesRegex(LocalProxyError, 'pac_timeout'):
                self.evaluate(state)

    def test_timely_result_retains_original_return_value(self):
        with self.execution(delays={'start': 3., 'poll': 4., 'receive': .5}) as state:
            self.assertEqual(self.evaluate(state), 'DIRECT')
            self.assertEqual(state.elapsed[0], 7.5)

    def test_exact_deadline_is_expired(self):
        with self.execution(delays={'poll': 8.}) as state:
            with self.assertRaisesRegex(LocalProxyError, 'pac_timeout'):
                self.evaluate(state)

    def test_startup_wait_and_receive_share_one_allowance(self):
        with self.execution(delays={'start': 4., 'poll': 3.5, 'receive': .5}) as state:
            with self.assertRaisesRegex(LocalProxyError, 'pac_timeout'):
                self.evaluate(state)
            self.assertEqual(state.elapsed[0], 8.)

    def test_revocation_keeps_precedence_for_a_late_success(self):
        with self.execution(delays={'poll': 9.}, revoked=True) as state:
            with self.assertRaisesRegex(LocalProxyError, 'pac_revoked'):
                self.evaluate(state)

    def test_worker_refusal_is_still_its_original_error(self):
        with self.execution(delays={'poll': 9.}, raw=b'{"error":"pac_invalid_script"}') as state:
            with self.assertRaisesRegex(LocalProxyError, 'pac_invalid_script'):
                self.evaluate(state)

    def test_malformed_reply_still_fails_and_releases_resources(self):
        with self.execution(raw=b'{"raw":7}') as state:
            with self.assertRaisesRegex(LocalProxyError, 'pac_failed'):
                self.evaluate(state)

    def test_expired_route_is_cached_as_failure_before_target_dns(self):
        with self.execution(delays={'poll': 9.}) as state:
            policy = NetworkPolicy('explicit_workspace', pac=PacSnapshot(SCRIPT, lambda: True))
            with patch('socket.getaddrinfo') as dns:
                for _ in range(2):
                    with self.assertRaisesRegex(FetchError, 'pac_timeout'):
                        SafeHTTP({'example.com'}, network_policy=policy).json(TARGET)
            dns.assert_not_called()
            state.process.start.assert_called_once()

    def test_only_a_new_snapshot_can_evaluate_after_the_cached_timeout(self):
        with self.execution(delays={'poll': 9.}) as state:
            original = PacSnapshot(SCRIPT, lambda: True)
            with self.assertRaisesRegex(LocalProxyError, 'pac_timeout'):
                original.for_host('example.com')
            state.delays.clear()
            with self.assertRaisesRegex(LocalProxyError, 'pac_timeout'):
                original.for_host('example.com')
            state.process.start.assert_called_once()
            self.assertIsNone(PacSnapshot(SCRIPT, lambda: True).for_host('example.com'))
            self.assertEqual(state.process.start.call_count, 2)
