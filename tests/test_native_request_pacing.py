"""Native request pacing must not sleep inside owner-thread CDP callbacks."""
import json
from dataclasses import replace
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import test_native_acquisition as fixture
from test_cdp_connection import Socket
from vibe_job_radar.guided.cdp_connection import CDPConnection
from vibe_job_radar.guided.native_policy import NativeRobots
from vibe_job_radar.guided.rate import RateLimit


class NativeRequestPacingTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixture.NativeControllerTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.b = self.fixture.b
        self.clock = 0.0
        clock_patch = patch('time.monotonic', side_effect=lambda: self.clock)
        clock_patch.start()
        self.addCleanup(clock_patch.stop)
        self.b.wire.ledger.clock = lambda: 1000 + self.clock
        self.b.wire.ledger.limits = replace(self.b.wire.ledger.limits, request_interval=.5)
        self.socket = Socket()
        self.connection = CDPConnection(self.socket)
        self.addCleanup(self.connection.close)
        self.session = self.connection.session('session')
        self.b.browser = SimpleNamespace(connection=self.connection)
        self.b._send = lambda session, method, params=None: self.session.send(method, params)
        self.session.on('Fetch.requestPaused', lambda event: self.b._paused('session', event))

    def queue_two(self):
        self.b._paused('session', self.request(0))
        self.b._paused('session', self.request(1))
        self.assertEqual(self.b.native_counts['asset'], 1)
        self.assertEqual(len(self.b._request_pacer.queue), 1)

    def continued(self):
        return [c['params']['requestId'] for c in self.socket.commands if c['method'] == 'Fetch.continueRequest']

    def request(self, index):
        return self.fixture.req('/fixture.js', 'GET', 'Script',
                                requestId=f'fetch-{index}', networkId=f'net-{index}')

    def wait(self, seconds):
        self.clock += seconds
        return False

    def test_browser_reply_remains_responsive_during_twenty_paced_requests(self):
        def respond(command):
            if command['method'] == 'Runtime.evaluate':
                self.socket.incoming.extend(json.dumps({'sessionId': 'session',
                    'method': 'Fetch.requestPaused', 'params': self.request(i)}) for i in range(20))
            self.socket.incoming.append(json.dumps({'id': command['id'], 'result': {'value': 42}}))
        self.socket.handle = respond
        with patch('time.monotonic', side_effect=lambda: self.clock), \
                patch.object(self.b.cancelled, 'wait', side_effect=self.wait) as waiting:
            self.assertEqual(self.session.send('Runtime.evaluate', timeout=7), {'value': 42})
        waiting.assert_not_called()
        self.assertEqual(self.b.native_counts['asset'], 1)
        self.assertIsNone(self.b.error)
        self.assertFalse(self.connection._events)
        self.assertFalse(self.connection.pending)

    def test_fifo_continuations_keep_half_second_spacing_and_reserve_once(self):
        times = []
        def respond(command):
            if command['method'] == 'Fetch.continueRequest':
                times.append(self.clock)
            self.socket.incoming.append(json.dumps({'id': command['id'], 'result': {}}))
        self.socket.handle = respond
        with patch.object(self.b.cancelled, 'wait') as waiting:
            for i in range(20):
                self.b._paused('session', self.request(i))
            for i in range(1, 20):
                self.clock = i * .5 - .01
                self.connection.pump(0)
                self.assertEqual(len(self.continued()), i)
                self.clock = i * .5
                self.connection.pump(0)
            waiting.assert_not_called()
        self.assertEqual(self.continued(), [f'fetch-{i}' for i in range(20)])
        self.assertEqual(times, [i * .5 for i in range(20)])
        self.assertEqual(self.b.wire.ledger.summary('fixture')['request']['day'], 20)
        self.assertFalse(self.b._request_pacer.queue)
        self.assertEqual(self.b._request_pacer.bytes, 0)

    def test_publisher_delay_still_controls_release(self):
        self.b.wire.ledger.set_publisher('fixture', fixture.URL, delay=1.25)
        self.queue_two()
        self.clock = 1
        self.connection.pump(0)
        self.assertEqual(self.continued(), ['fetch-0'])
        self.clock = 1.25
        self.connection.pump(0)
        self.assertEqual(self.continued(), ['fetch-0', 'fetch-1'])

    def test_other_session_reservation_is_checked_again_at_release(self):
        self.queue_two()
        self.clock = .5
        self.b.wire.ledger.reserve('fixture', 'request', origin=fixture.URL)
        self.connection.pump(0)
        self.assertEqual(self.continued(), ['fetch-0'])
        self.clock = 1
        self.connection.pump(0)
        self.assertEqual(self.continued(), ['fetch-0', 'fetch-1'])
        self.assertEqual(self.b.wire.ledger.summary('fixture')['request']['day'], 3)

    def test_wait_budget_does_not_restart_after_competing_reservations(self):
        self.b.wire.max_inline_wait = 1
        self.queue_two()
        for self.clock in (.5, 1):
            self.b.wire.ledger.reserve('fixture', 'request', origin=fixture.URL)
            self.connection.pump(0)
        self.assertEqual(self.continued(), ['fetch-0'])
        self.assertEqual(self.b.error, 'rate_wait')
        self.assertIsInstance(self.b.wait_error, RateLimit)
        self.assertEqual(self.b.wait_error.next_allowed_at, 1001.5)

    def test_daily_limit_is_durable_and_never_queued_as_a_short_wait(self):
        self.b.wire.ledger.limits = replace(self.b.wire.ledger.limits, requests_day=1)
        self.b._paused('session', self.request(0))
        self.b._paused('session', self.request(1))
        self.assertEqual(self.b.error, 'daily_limit')
        self.assertEqual(self.b.wait_error.next_allowed_at, 87400)
        self.assertFalse(self.b._request_pacer.queue)
        self.assertEqual(self.continued(), ['fetch-0'])

    def test_cancellation_aborts_queued_request_before_due_without_reserving(self):
        self.queue_two()
        self.b.cancelled.set()
        self.connection.pump(0)
        self.assertEqual(self.b.error, 'paused')
        self.assertEqual(self.continued(), ['fetch-0'])
        self.assertEqual(self.b.wire.ledger.summary('fixture')['request']['day'], 1)
        self.assertFalse(self.b._request_pacer.queue)

    def test_policy_revocation_aborts_queued_request(self):
        self.queue_two()
        self.b.policy_check = lambda: False
        self.connection.pump(0)
        self.assertEqual(self.b.error, 'native_policy_changed')
        self.assertEqual(self.continued(), ['fetch-0'])

    def test_new_navigation_discards_old_paused_request_without_poisoning_task(self):
        self.queue_two()
        self.b._epoch += 1
        self.connection.pump(0)
        self.assertIsNone(self.b.error)
        self.assertEqual(self.continued(), ['fetch-0'])
        self.assertFalse(self.b._request_pacer.queue)
        self.assertEqual(self.socket.commands[-1]['params']['errorReason'], 'Aborted')

    def test_detached_session_never_reuses_queued_request(self):
        self.queue_two()
        self.b._detached({'sessionId': 'session'})
        self.clock = .5
        self.connection.pump(0)
        self.assertEqual(self.continued(), ['fetch-0'])
        self.assertFalse(self.b._request_pacer.queue)
        self.assertEqual(self.b._request_pacer.bytes, 0)

    def test_browser_cancelled_request_is_removed_without_replay(self):
        self.queue_two()
        self.b._received({'sessionId': 'session', 'message': json.dumps({
            'method': 'Network.loadingFailed', 'params': {'requestId': 'net-1',
            'canceled': True, 'errorText': 'net::ERR_ABORTED'}})})
        self.clock = .5
        self.connection.pump(0)
        self.assertEqual(self.continued(), ['fetch-0'])
        self.assertFalse(self.b._request_pacer.queue)
        self.assertIsNone(self.b.error)

    def test_queue_does_not_retain_password_body_or_headers(self):
        self.b._paused('session', self.request(0))
        self.b.auth_mode = True
        event = self.fixture.req('/login', 'POST', 'Fetch')
        event['request']['headers'] = {'Cookie': fixture.SECRET, 'Authorization': fixture.SECRET}
        self.b._paused('session', event)
        item = self.b._request_pacer.queue[0]
        self.assertNotIn(fixture.SECRET, json.dumps(vars(item)))
        self.assertNotIn(fixture.SECRET, repr(item))
        self.b.auth_mode = False
        self.clock = .5
        self.connection.pump(0)
        self.assertEqual(self.b.error, 'native_operation_unreviewed')
        self.assertEqual(self.continued(), ['fetch-0'])

    def test_robots_is_rechecked_before_queued_business_continuation(self):
        self.b._paused('session', self.request(0))
        self.b._paused('session', self.fixture.req())
        self.b.wire.rules[fixture.URL] = NativeRobots(200, 'text/plain', b'User-agent: *\nDisallow: /\n')
        self.clock = .5
        self.connection.pump(0)
        self.assertEqual(self.b.error, 'robots_denied')
        self.assertEqual(self.continued(), ['fetch-0'])

    def test_queued_document_consumes_page_budget_once(self):
        self.b._paused('session', self.request(0))
        self.b._paused('session', self.fixture.req('/search', 'GET', 'Document'))
        self.clock = .5
        self.connection.pump(0)
        counts = self.b.wire.ledger.summary('fixture')
        self.assertEqual(counts['page']['day'], 1)
        self.assertEqual(counts['request']['day'], 2)
        self.assertEqual(self.b.native_counts['document'], 1)

    def test_inflight_and_queued_requests_share_the_existing_count_limit(self):
        for i in range(128):
            self.b._paused('session', self.request(i))
        self.assertEqual(len(self.b._requests) + len(self.b._request_pacer.queue), 128)
        self.b._paused('session', self.request(128))
        self.assertEqual(self.b.error, 'native_observation_limit')
        self.assertLessEqual(len(self.b._requests) + len(self.b._request_pacer.queue), 128)
        self.assertEqual(self.continued(), ['fetch-0'])

    def test_queued_metadata_has_a_total_byte_limit(self):
        self.queue_two()
        self.b._request_pacer.MAX_BYTES = self.b._request_pacer.bytes
        self.b._paused('session', self.request(2))
        self.assertEqual(self.b.error, 'native_observation_limit')
        self.assertLessEqual(self.b._request_pacer.bytes, self.b._request_pacer.MAX_BYTES)
        self.assertEqual(self.continued(), ['fetch-0'])

    def test_close_clears_metadata_and_removes_pump_callback(self):
        self.queue_two()
        self.b._request_pacer.close()
        self.clock = 1
        self.connection.pump(0)
        self.assertFalse(self.b._request_pacer.queue)
        self.assertFalse(self.connection._pump_callbacks)
        self.assertEqual(self.continued(), ['fetch-0'])

    def test_pump_work_cannot_recursively_dispatch_or_continue_twice(self):
        self.queue_two()
        calls = []
        def work():
            calls.append('tick')
            self.session.send('Runtime.evaluate')
        self.connection.add_pump_callback(work)
        self.clock = .5
        self.connection.pump(0)
        self.assertEqual(calls, ['tick'])
        self.assertEqual(self.continued(), ['fetch-0', 'fetch-1'])
        self.assertFalse(self.connection.pending)
        self.connection.remove_pump_callback(work)

    def test_deferred_retirement_precedes_due_request_release(self):
        self.queue_two()
        self.session.on('fixture.retire', lambda _: self.b._detached({'sessionId': 'session'}))
        event = {'sessionId': 'session', 'method': 'fixture.retire', 'params': {}}
        size = len(json.dumps(event).encode())
        self.connection._events.append((event, size, 1))
        self.connection._event_bytes = size
        self.clock = .5
        self.connection.pump(0)
        self.assertEqual(self.continued(), ['fetch-0'])
        self.assertFalse(self.b._request_pacer.queue)

    def test_new_queued_business_intent_invalidates_older_results_before_sending(self):
        self.b._paused('session', self.request(0))
        self.b._observations.append(SimpleNamespace(operation='query_jobs'))
        self.b._paused('session', self.fixture.req(requestId='query-1', networkId='query-1'))
        self.b._paused('session', self.fixture.req(requestId='query-2', networkId='query-2'))
        self.assertFalse(self.b._observations)
        self.assertEqual(self.b.native_counts['business'], 0)
        self.assertEqual(self.b._latest_business['query_jobs'], 2)
        self.clock = .5
        self.connection.pump(0)
        self.assertEqual(self.b._requests[('session', 'query-1')]['context']['sequence'], 1)
        self.assertEqual(self.b._latest_business['query_jobs'], 2)
        self.clock = 1
        self.connection.pump(0)
        self.assertEqual(self.b._requests[('session', 'query-2')]['context']['sequence'], 2)
        self.assertEqual(self.b.native_counts['business'], 2)

    def test_business_sequence_is_included_in_queued_metadata_byte_accounting(self):
        self.b._paused('session', self.request(0))
        self.b._paused('session', self.fixture.req())
        item = self.b._request_pacer.queue[0]
        actual = len(json.dumps([item.session, item.request_id, item.key,
            item.origin, item.record], ensure_ascii=False).encode('utf-8'))
        self.assertEqual(item.record['context']['sequence'], 1)
        self.assertEqual(self.b._request_pacer.bytes, actual)
