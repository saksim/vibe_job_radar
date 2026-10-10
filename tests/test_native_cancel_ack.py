"""Known browser cancellation must not race into an unrelated task-wide stop."""
from contextlib import contextmanager
import json
import unittest

import test_native_request_pacing as pacing
from vibe_job_radar.guided.cdp_connection import InterceptionGone
from vibe_job_radar.guided.contracts import CrawlError


class CancelAcknowledgmentTests(unittest.TestCase):
    @contextmanager
    def controller(self):
        case = pacing.NativeRequestPacingTests()
        case.setUp()
        case.b._page_sessions = {'session': case.session}
        case.cancels = []
        def cancelled(event):
            case.cancels.append(event)
            case.b._received({'sessionId': 'session', 'message': json.dumps({
                'method': 'Network.loadingFailed', 'params': event})})
        case.session.on('Network.loadingFailed', cancelled)
        try:
            yield case
        finally:
            case.doCleanups()

    def rejection(self, case, *, event=True, session='session', network='net-1',
                  cancelled=True, reason='net::ERR_ABORTED', code=-32602,
                  message='Invalid InterceptionId.', fetch='fetch-1', after=False):
        def respond(command):
            if command['method'] == 'Fetch.continueRequest' and command['params']['requestId'] == fetch:
                failure = json.dumps({'sessionId': session, 'method': 'Network.loadingFailed',
                    'params': {'requestId': network, 'canceled': cancelled, 'errorText': reason}})
                reply = json.dumps({'id': command['id'], 'error': {'code': code, 'message': message}})
                if event and not after:
                    case.socket.incoming.append(failure)
                case.socket.incoming.append(reply)
                if event and after:
                    case.socket.incoming.append(failure)
            else:
                case.socket.incoming.append(json.dumps({'id': command['id'], 'result': {}}))
        case.socket.handle = respond

    def test_earlier_cancel_retires_one_request_and_keeps_later_pacing_and_quota(self):
        with self.controller() as c:
            c.queue_two()
            c.b._paused('session', c.request(2))
            self.rejection(c)
            c.clock = .5
            c.connection.pump(0)
            self.assertIsNone(c.b.error)
            self.assertNotIn(('session', 'net-1'), c.b._requests)
            self.assertEqual(len(c.connection._events), 1)
            self.assertFalse(c.cancels)  # No recursive observer dispatch.
            self.assertEqual(c.b.wire.ledger.summary('fixture')['request']['day'], 2)
            c.connection.pump(0)
            self.assertEqual(len(c.cancels), 1)  # Notification wasn't consumed by the matcher.
            c.clock = 1
            c.connection.pump(0)
            self.assertEqual(c.continued(), ['fetch-0', 'fetch-1', 'fetch-2'])
            self.assertEqual(c.b.wire.ledger.summary('fixture')['request']['day'], 3)
            self.assertFalse(c.b._request_pacer.queue)
            self.assertIsNone(c.b.error)
            self.assertEqual(c.b.native_counts['responses'], 0)

    def test_canceled_business_does_not_restore_an_old_result_or_invent_a_response(self):
        with self.controller() as c:
            c.b._paused('session', c.request(0))
            c.b._observations.append(type('Observation', (), {'operation': 'query_jobs'})())
            c.b._paused('session', c.fixture.req(requestId='fetch-1', networkId='net-1'))
            self.rejection(c)
            c.clock = .5
            c.connection.pump(0)
            self.assertIsNone(c.b.error)
            self.assertNotIn(('session', 'net-1'), c.b._requests)
            self.assertFalse(c.b._observations)
            self.assertEqual(c.b._latest_business['query_jobs'], 1)
            self.assertEqual(c.b.native_counts['responses'], 0)
            self.assertEqual(c.b.wire.ledger.summary('fixture')['request']['day'], 2)

    def test_unmatched_or_non_cancel_failures_remain_fatal(self):
        for options in ({'session': 'other'}, {'network': 'other'}, {'cancelled': False},
                        {'cancelled': 'true'}, {'reason': 'net::ERR_CERT_AUTHORITY_INVALID'},
                        {'event': False}):
            with self.subTest(options=options), self.controller() as c:
                c.queue_two()
                if options.get('session') == 'other':
                    c.connection.session('other').on('Network.loadingFailed', lambda _: None)
                self.rejection(c, **options)
                c.clock = .5
                c.connection.pump(0)
                self.assertEqual(c.b.error, 'native_protocol_error')
                self.assertEqual(c.continued(), ['fetch-0', 'fetch-1'])

    def test_a_cancel_arriving_after_rejection_does_not_excuse_the_failure(self):
        with self.controller() as c:
            c.queue_two()
            self.rejection(c, after=True)
            c.clock = .5
            c.connection.pump(0)
            self.assertEqual(c.b.error, 'native_protocol_error')
            self.assertFalse(c.cancels)

    def test_other_protocol_rejections_remain_fatal_even_with_matching_cancel(self):
        for options in ({'code': -32000}, {'code': '-32602'},
                        {'message': 'Another failure PRIVATE_VALUE'},
                        {'message': 'Invalid InterceptionId. PRIVATE_VALUE'}):
            with self.subTest(options=options), self.controller() as c:
                c.queue_two()
                self.rejection(c, **options)
                c.clock = .5
                c.connection.pump(0)
                self.assertEqual(c.b.error, 'native_protocol_error')

    def test_immediately_admitted_request_uses_the_same_nonrecursive_cancel_check(self):
        with self.controller() as c:
            self.rejection(c, fetch='fetch-0', network='net-0')
            c.socket.incoming.append(json.dumps({'sessionId': 'session',
                'method': 'Fetch.requestPaused', 'params': c.request(0)}))
            c.connection.pump(0)
            self.assertIsNone(c.b.error)
            self.assertFalse(c.cancels)
            self.assertNotIn(('session', 'net-0'), c.b._requests)
            c.connection.pump(0)
            self.assertEqual(len(c.cancels), 1)
            self.assertEqual(c.continued(), ['fetch-0'])
            self.assertEqual(c.b.wire.ledger.summary('fixture')['request']['day'], 1)

    def test_matching_does_not_consume_events_and_requires_prior_reply_order(self):
        with self.controller() as c:
            event = {'sessionId': 'session', 'method': 'Network.loadingFailed',
                     'params': {'requestId': 'net', 'canceled': True, 'errorText': 'net::ERR_ABORTED'}}
            c.connection._events.append((event, 111, 12))
            c.connection._event_bytes = 111
            self.assertTrue(c.connection.has_pending_cancellation('session', 'net', before=13))
            self.assertFalse(c.connection.has_pending_cancellation('session', 'net', before=12))
            self.assertFalse(c.connection.has_pending_cancellation('session', 'net', before=11))
            self.assertFalse(c.connection.has_pending_cancellation('', 'net', before=13))
            self.assertEqual(list(c.connection._events), [(event, 111, 12)])
            self.assertEqual(c.connection._event_bytes, 111)

    def test_only_exact_continue_rejection_is_typed_without_raw_message_disclosure(self):
        for method, code, message, typed in (
                ('Fetch.continueRequest', -32602, 'Invalid InterceptionId.', True),
                ('Fetch.failRequest', -32602, 'Invalid InterceptionId.', False),
                ('Fetch.fulfillRequest', -32602, 'Invalid InterceptionId.', False),
                ('Fetch.continueRequest', -32000, 'Invalid InterceptionId.', False),
                ('Fetch.continueRequest', -32602, 'PRIVATE_URL_PASSWORD', False)):
            with self.subTest(method=method, code=code, typed=typed), self.controller() as c:
                c.socket.handle = lambda command: c.socket.incoming.append(json.dumps({
                    'id': command['id'], 'error': {'code': code, 'message': message}}))
                with self.assertRaises(CrawlError) as caught:
                    c.session.send(method, {'requestId': 'synthetic'})
                self.assertEqual(type(caught.exception) is InterceptionGone, typed)
                self.assertEqual(str(caught.exception), 'native_protocol_error')
                self.assertFalse(c.connection.pending)
                self.assertFalse(c.connection.responses)
