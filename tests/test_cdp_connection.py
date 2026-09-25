import json
import threading
import unittest
from unittest.mock import patch

from vibe_job_radar.guided.cdp_connection import CDPConnection
from vibe_job_radar.guided.contracts import CrawlError


class Socket:
    def __init__(self):
        self.commands, self.incoming = [], []
        self.closed = False
        self.handle = lambda command: self.incoming.append(json.dumps({'id': command['id'], 'result': {}}))

    def send(self, value):
        command = json.loads(value)
        self.commands.append(command)
        self.handle(command)

    def recv(self, *, timeout):
        if self.incoming:
            return self.incoming.pop(0)
        raise TimeoutError()

    def close(self):
        self.closed = True


class CDPConnectionTests(unittest.TestCase):
    def setUp(self):
        self.socket = Socket()
        self.connection = CDPConnection(self.socket)
        self.addCleanup(self.connection.close)

    def test_request_callback_can_issue_nested_command_without_losing_outer_response(self):
        session = self.connection.session('page')
        def respond(command):
            if command['method'] == 'Page.navigate':
                self.socket.incoming.extend([
                    json.dumps({'sessionId': 'page', 'method': 'Fetch.requestPaused', 'params': {'requestId': 'r'}}),
                    json.dumps({'id': command['id'], 'result': {'loaderId': 'loader'}})])
            else:
                self.socket.incoming.append(json.dumps({'id': command['id'], 'result': {'continued': True}}))
        self.socket.handle = respond
        seen = []
        session.on('Fetch.requestPaused', lambda event: seen.append(session.send('Fetch.continueRequest', event)))
        result = session.send('Page.navigate', {'url': 'https://fixture.test/'})
        self.assertEqual(result, {'loaderId': 'loader'})
        self.assertEqual(seen, [{'continued': True}])
        self.assertEqual([c['sessionId'] for c in self.socket.commands], ['page', 'page'])
        self.assertFalse(self.connection.responses)
        self.assertFalse(self.connection.pending)

    def test_burst_of_paused_requests_does_not_recursively_exhaust_command_limit(self):
        session = self.connection.session('page')
        observed, depth, peak_depth, peak_pending = [], 0, 0, 0
        def respond(command):
            nonlocal peak_pending
            peak_pending = max(peak_pending, len(self.connection.pending))
            if command['method'] == 'Page.navigate':
                self.socket.incoming.extend(json.dumps({'sessionId': 'page',
                    'method': 'Fetch.requestPaused', 'params': {'requestId': str(index)}})
                    for index in range(80))
            self.socket.incoming.append(json.dumps({'id': command['id'], 'result': {'ack': True}}))
        def handle(event):
            nonlocal depth, peak_depth, peak_pending
            depth += 1
            peak_depth = max(peak_depth, depth)
            try:
                result = session.send('Fetch.failRequest', {**event, 'errorReason': 'BlockedByClient'})
                self.assertTrue(result['ack'])
                observed.append(event['requestId'])
            finally:
                depth -= 1
        self.socket.handle = respond
        session.on('Fetch.requestPaused', handle)
        self.assertEqual(session.send('Page.navigate'), {'ack': True})
        self.assertEqual(observed, [str(index) for index in range(80)])
        self.assertEqual(peak_depth, 1)
        self.assertLessEqual(peak_pending, 2)
        self.assertEqual(len(self.socket.commands), 81)
        self.assertFalse(self.socket.incoming)
        self.assertFalse(self.connection.responses)
        self.assertFalse(self.connection.pending)

    def deferred_exchange(self, events, callback):
        session = self.connection.session('page')
        def respond(command):
            if command['method'] == 'Page.navigate':
                self.socket.incoming.append(json.dumps({'sessionId': 'page',
                    'method': 'Fetch.requestPaused', 'params': {'requestId': 'first'}}))
                self.socket.incoming.extend(json.dumps(event) for event in events)
            self.socket.incoming.append(json.dumps({'id': command['id'], 'result': {'ack': True}}))
        self.socket.handle = respond
        session.on('Fetch.requestPaused', callback)
        return session

    def test_deferred_navigation_is_visible_before_outer_command_returns(self):
        order = []
        def paused(event):
            order.append('entered')
            session.send('Fetch.continueRequest', event)
            order.append('acknowledged')
        session = self.deferred_exchange([{'sessionId': 'page', 'method': 'Page.frameNavigated',
            'params': {'frame': {'url': 'https://fixture.test/new'}}}], paused)
        session.on('Page.frameNavigated', lambda event: order.append(event['frame']['url']))
        session.send('Page.navigate')
        self.assertEqual(order, ['entered', 'acknowledged', 'https://fixture.test/new'])
        self.assertFalse(self.connection._events)
        self.assertEqual(self.connection._event_bytes, 0)

    def test_deferred_retirement_discards_later_events_for_the_closed_session(self):
        session = self.deferred_exchange([
            {'method': 'Target.detachedFromTarget', 'params': {'sessionId': 'page'}},
            {'sessionId': 'page', 'method': 'Page.frameNavigated', 'params': {}}],
            lambda event: session.send('Fetch.continueRequest', event))
        session.on('Page.frameNavigated', lambda _: self.fail('retired event delivered'))
        session.send('Page.navigate')
        self.assertTrue(session.detached)
        self.assertNotIn('page', self.connection.sessions)
        self.assertFalse(self.connection._events)
        self.assertEqual(self.connection._event_bytes, 0)

    def test_deferred_events_have_independent_count_and_byte_limits(self):
        for field, limit in (('MAX_DEFERRED_EVENTS', 2), ('MAX_DEFERRED_BYTES', 160)):
            with self.subTest(field=field):
                socket = Socket()
                connection = CDPConnection(socket)
                try:
                    setattr(connection, field, limit)
                    session = connection.session('page')
                    def respond(command):
                        if command['method'] == 'Page.navigate':
                            socket.incoming.extend(json.dumps({'sessionId': 'page',
                                'method': 'Fetch.requestPaused', 'params': {'requestId': str(i)}})
                                for i in range(6))
                        socket.incoming.append(json.dumps({'id': command['id'], 'result': {}}))
                    socket.handle = respond
                    session.on('Fetch.requestPaused', lambda event: session.send('Fetch.failRequest', event))
                    with self.assertRaisesRegex(CrawlError, 'native_observation_limit'):
                        session.send('Page.navigate')
                    self.assertLessEqual(len(connection._events), connection.MAX_DEFERRED_EVENTS)
                    self.assertLessEqual(connection._event_bytes, connection.MAX_DEFERRED_BYTES)
                    self.assertFalse(connection._dispatching)
                    self.assertFalse(connection.pending)
                finally:
                    connection.close()
                self.assertFalse(connection._events)
                self.assertEqual(connection._event_bytes, 0)

    def test_deferred_work_does_not_reset_the_original_command_deadline(self):
        clock = [0.0]
        def paused(event):
            session.send('Fetch.continueRequest', event)
            clock[0] = 2.0
        session = self.deferred_exchange([{'sessionId': 'page', 'method': 'Page.frameNavigated', 'params': {}}], paused)
        session.on('Page.frameNavigated', lambda _: self.fail('deadline had expired'))
        with patch('vibe_job_radar.guided.cdp_connection.time.monotonic', side_effect=lambda: clock[0]):
            with self.assertRaisesRegex(CrawlError, 'native_protocol_error'):
                session.send('Page.navigate', timeout=1)
        self.assertFalse(self.connection.pending)
        self.assertFalse(self.connection.responses)

    def test_unobserved_events_do_not_use_the_deferred_event_budget(self):
        events = [{'sessionId': 'other', 'method': 'Fetch.requestPaused', 'params': {}}] * 80
        events += [{'sessionId': 'page', 'method': 'Runtime.consoleAPICalled', 'params': {}}] * 80
        session = self.deferred_exchange(events, lambda event: session.send('Fetch.failRequest', event))
        self.connection.MAX_DEFERRED_EVENTS = 1
        session.send('Page.navigate')
        self.assertFalse(self.connection._events)
        self.assertEqual(self.connection._event_bytes, 0)

    def test_callback_failure_restores_dispatch_state_and_close_clears_deferred_data(self):
        def paused(event):
            session.send('Fetch.continueRequest', event)
            raise CrawlError('paused')
        session = self.deferred_exchange([{'sessionId': 'page', 'method': 'Page.frameNavigated', 'params': {}}], paused)
        session.on('Page.frameNavigated', lambda _: self.fail('cancelled callback delivered'))
        with self.assertRaisesRegex(CrawlError, 'paused'):
            session.send('Page.navigate')
        self.assertFalse(self.connection._dispatching)
        self.assertFalse(self.connection.pending)
        self.connection.close()
        self.assertFalse(self.connection._events)
        self.assertEqual(self.connection._event_bytes, 0)

    def test_original_pending_command_limit_is_not_raised(self):
        self.connection.pending.update(range(32))
        with self.assertRaisesRegex(CrawlError, 'native_observation_limit'):
            self.connection.root.send('Page.navigate')
        self.assertEqual(self.socket.commands, [])

    def test_session_events_do_not_cross_into_other_page(self):
        a, b = self.connection.session('a'), self.connection.session('b')
        seen = []
        a.on('Fetch.requestPaused', lambda _: self.fail('wrong owner'))
        b.on('Fetch.requestPaused', lambda _: seen.append(True))
        self.socket.incoming.append(json.dumps({'sessionId': 'b', 'method': 'Fetch.requestPaused', 'params': {}}))
        self.connection.pump(0)
        self.assertEqual(seen, [True])

    def test_protocol_error_does_not_expose_form_values(self):
        self.socket.handle = lambda command: self.socket.incoming.append(json.dumps({
            'id': command['id'], 'error': {'code': -32000, 'message': 'secret-account secret-password'}}))
        with self.assertRaises(CrawlError) as caught:
            self.connection.root.send('Runtime.evaluate', {'expression': 'secret-password'})
        self.assertEqual(str(caught.exception), 'native_protocol_error')
        self.assertFalse(self.connection.responses)
        self.assertFalse(self.connection.pending)

    def test_forbidden_runtime_subscriptions_and_security_overrides_are_not_sent(self):
        for method in CDPConnection.FORBIDDEN:
            with self.subTest(method=method), self.assertRaises(CrawlError):
                self.connection.root.send(method)
        self.assertEqual(self.socket.commands, [])

    def test_explicit_evaluation_is_available_without_runtime_subscription(self):
        self.connection.root.send('Runtime.evaluate', {'expression': 'document.title', 'returnByValue': True})
        self.assertEqual([c['method'] for c in self.socket.commands], ['Runtime.evaluate'])

    def test_wrong_owner_thread_cannot_send_browser_commands(self):
        failures = []
        def other():
            try:
                self.connection.root.send('Page.navigate')
            except CrawlError as error:
                failures.append(error.code)
        thread = threading.Thread(target=other)
        thread.start()
        thread.join(timeout=2)
        self.assertEqual(failures, ['native_protocol_error'])
        self.assertEqual(self.socket.commands, [])

    def test_oversized_input_and_response_stop_without_unbounded_buffer(self):
        self.connection.MAX_MESSAGE = 100
        with self.assertRaises(CrawlError):
            self.connection.root.send('Runtime.evaluate', {'expression': 'x' * 101})
        self.assertFalse(self.socket.commands)
        self.socket.incoming.append('x' * 101)
        with self.assertRaises(CrawlError):
            self.connection.pump(0)

    def test_closed_session_drops_callbacks_and_cannot_send(self):
        session = self.connection.session('a')
        session.on('Fetch.requestPaused', lambda _: self.fail('retired'))
        session.detach()
        self.socket.incoming.append(json.dumps({'sessionId': 'a', 'method': 'Fetch.requestPaused', 'params': {}}))
        self.connection.pump(0)
        with self.assertRaises(CrawlError):
            session.send('Fetch.continueRequest')
        self.assertNotIn('a', self.connection.sessions)

    def test_browser_target_retirement_releases_closed_page_listeners(self):
        session = self.connection.session('page')
        session.on('Page.frameNavigated', lambda _: self.fail('retired page'))
        self.socket.incoming.extend([
            json.dumps({'method': 'Target.detachedFromTarget', 'params': {'sessionId': 'page'}}),
            json.dumps({'sessionId': 'page', 'method': 'Page.frameNavigated', 'params': {}})])
        self.connection.pump(0)
        self.connection.pump(0)
        self.assertNotIn('page', self.connection.sessions)
        self.assertFalse(session.callbacks)
        self.assertTrue(session.detached)

    def test_transport_failure_does_not_disclose_command_contents(self):
        def failure(_command):
            raise RuntimeError('secret-account secret-password')
        self.socket.handle = failure
        with self.assertRaises(CrawlError) as caught:
            self.connection.root.send('Runtime.evaluate', {'expression': 'secret-password'})
        self.assertEqual(str(caught.exception), 'browser_closed')


if __name__ == '__main__':
    unittest.main()
