"""Bounded local coordination and HTTP error parsing; no live accounts."""
import threading
import unittest
from unittest.mock import patch

import test_team_service as fixtures
from vibe_job_radar.guided.contracts import CrawlError
from vibe_job_radar.guided.rate import RateLimit
from vibe_job_radar.network import FetchError, SafeHTTP, retry_after_seconds
from vibe_job_radar.team_client import TeamConnection
from vibe_job_radar.team_ownership import TaskLease


class RequestPacingTests(unittest.TestCase):
    setUp = fixtures.TeamTests.setUp
    connection = fixtures.TeamTests.connection

    def test_renewal_and_peer_connection_share_one_http_slot(self):
        owner = self.connection('alice')
        peer = TeamConnection(self.root/'alice', transport=self.http)
        entered, second, release = (threading.Event() for _ in range(3))
        calls = []
        real = self.http.json

        class Gate:
            def json(inner, url, **kwargs):
                calls.append(url)
                if len(calls) == 1:
                    entered.set()
                    if not release.wait(3):
                        raise AssertionError('bounded fixture gate expired')
                else:
                    second.set()
                return real(url, **kwargs)

        outcomes = []
        def run(operation):
            try:
                operation()
                outcomes.append('ok')
            except Exception as error:
                outcomes.append(getattr(error, 'code', type(error).__name__))

        with TaskLease(owner, 'liepin') as lease:
            owner.transport = peer.transport = Gate()
            left = threading.Thread(target=run, args=(lease.confirm,))
            right = threading.Thread(target=run, args=(lambda:peer.request('/v1/team/state'),))
            left.start()
            try:
                self.assertTrue(entered.wait(2))
                right.start()
                overlap = second.wait(.3)
            finally:
                release.set()
                left.join(5)
                if right.ident is not None:right.join(5)
            self.assertFalse(overlap, 'a queued peer entered while renewal was in flight')
            self.assertFalse(left.is_alive() or right.is_alive())
            self.assertEqual(outcomes, ['ok', 'ok'])
            self.assertEqual(len(calls), 2)  # No hidden retry.
            lease.check()

    def test_slow_workspace_does_not_hold_other_workspace(self):
        left = self.connection('alice')
        right = self.connection('bob')
        entered, release = threading.Event(), threading.Event()
        outcomes = []
        class Slow:
            def json(inner, *args, **kwargs):
                entered.set()
                if not release.wait(3):raise AssertionError('bounded fixture gate expired')
                return {'schema_version':1}
        left.transport = Slow()
        worker = threading.Thread(target=lambda:outcomes.append(left.request('/v1/team/state')))
        worker.start()
        try:
            self.assertTrue(entered.wait(2))
            self.assertEqual(right.request('/v1/team/state')['member'], 'bob')
            self.assertFalse(release.is_set())
        finally:
            release.set();worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(outcomes, [{'schema_version':1}])

    def test_failure_releases_slot_without_retry_and_rechecks_credentials(self):
        client = self.connection('alice')
        peer = TeamConnection(self.root/'alice', transport=self.http)
        class Failed:
            calls = 0
            def json(inner, *args, **kwargs):
                inner.calls += 1
                raise FetchError('network_error')
        failed = Failed();client.transport = failed
        with self.assertRaisesRegex(CrawlError, 'team_unavailable'):
            client.request('/v1/team/state')
        self.assertEqual(failed.calls, 1)
        self.assertEqual(peer.request('/v1/team/state')['member'], 'alice')
        before = len(self.http.calls)
        peer.path.write_bytes(peer.path.read_bytes()+b' ')
        with self.assertRaisesRegex(ValueError, 'team_connection_changed'):
            peer.request('/v1/team/state')
        self.assertEqual(len(self.http.calls), before)

    def test_team_429_uses_service_wait_and_still_stops_after_one_request(self):
        client = self.connection('alice')
        client.transport = None
        calls = []
        class Raw:
            status = 429
            def getheaders(inner):return [('Retry-After', '1')]
        class Conn:
            def __init__(inner, *args, **kwargs):pass
            def request(inner, *args, **kwargs):calls.append(args)
            def getresponse(inner):return Raw()
            def close(inner):pass
        with patch('vibe_job_radar.network.validate_public_url', return_value=(
                'team.fixture.test', ('8.8.8.8',), '/v1/team/state')), patch(
                'vibe_job_radar.network.PinnedHTTPSConnection', Conn):
            with self.assertRaises(RateLimit) as caught:
                client.request('/v1/team/state')
        self.assertEqual(caught.exception.wait, 1)
        self.assertEqual(caught.exception.code, 'team_rate_wait')
        self.assertEqual(len(calls), 1)


class RetryAfterTests(unittest.TestCase):
    def test_service_floor_preserves_long_wait_dates_and_conservative_invalid_values(self):
        for value, expected in [('1',1), ('900',900), ('0',1), ('-5',1),
                                ('invalid',300), ('NaN',86400), ('inf',86400), (None,300)]:
            with self.subTest(value=value):
                self.assertEqual(retry_after_seconds(value, minimum=1), expected)
        self.assertEqual(retry_after_seconds('Thu, 01 Jan 1970 00:00:02 GMT', now=0, minimum=1), 2)
        self.assertEqual(retry_after_seconds('Thu, 01 Jan 1970 00:00:02 GMT', now=10, minimum=1), 1)

    def test_default_publisher_floor_and_circuit_stop_are_unchanged(self):
        client = SafeHTTP({'example.com'}, interval=0)
        calls = []
        class Raw:
            status = 429
            def getheaders(inner):return [('Retry-After', '1')]
        class Conn:
            def __init__(inner, *args, **kwargs):pass
            def request(inner, *args, **kwargs):calls.append(args)
            def getresponse(inner):return Raw()
            def close(inner):pass
        with patch('vibe_job_radar.network.validate_public_url', return_value=(
                'example.com', ('8.8.8.8',), '/')), patch(
                'vibe_job_radar.network.PinnedHTTPSConnection', Conn):
            with self.assertRaises(FetchError) as caught:client.request('https://example.com/')
            self.assertEqual(caught.exception.retry_after, 300)
            with self.assertRaisesRegex(FetchError, 'host_circuit_open'):
                client.request('https://example.com/')
        self.assertEqual(len(calls), 1)
        self.assertEqual(retry_after_seconds('900'), 900)

    def test_invalid_floor_is_rejected_before_network(self):
        for value in [0, -1, True, None, '1', float('nan'), float('inf')]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                SafeHTTP({'example.com'}, retry_after_floor=value)


if __name__ == '__main__':
    unittest.main()
