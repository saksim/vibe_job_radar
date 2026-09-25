import json
import threading
import unittest

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
