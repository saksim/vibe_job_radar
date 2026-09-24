"""Preserve native transport failure causes without printing raw errors."""
import json
from unittest import TestCase
from unittest.mock import Mock

import test_native_acquisition as fixture
from vibe_job_radar.guided.contracts import CrawlError
from vibe_job_radar.guided.native_errors import native_failure_code, native_transport_failure


class NativeFailureClassificationTests(TestCase):
    def setUp(self):
        self.helper = fixture.NativeControllerTests()
        self.helper.setUp()
        self.addCleanup(self.helper.doCleanups)
        self.b = self.helper.b

    def test_known_browser_net_errors_are_fixed_codes(self):
        for token, code in {
            'ERR_CERT_COMMON_NAME_INVALID': 'tls_verification_failed',
            'ERR_CERT_AUTHORITY_INVALID': 'tls_verification_failed',
            'ERR_SSL_PROTOCOL_ERROR': 'tls_handshake_failed',
            'ERR_INVALID_AUTH_CREDENTIALS': 'native_proxy_auth_failed',
            'ERR_PROXY_CONNECTION_FAILED': 'local_proxy_connection_failed',
            'ERR_NAME_NOT_RESOLVED': 'dns_error',
            'ERR_BLOCKED_BY_ADMINISTRATOR': 'native_administrator_blocked',
        }.items():
            with self.subTest(token=token):
                self.assertEqual(native_failure_code('Page.goto: net::'+token+' at https://secret.test/?token=secret'), code)

    def test_arbitrary_exception_or_url_cannot_invent_a_cause(self):
        for value in ['text ERR_CERT_AUTHORITY_INVALID', 'https://example.test/ERR_CERT_AUTHORITY_INVALID',
                      'Page.goto: timeout at https://example.test/ERR_CERT_AUTHORITY_INVALID',
                      'timeout\nERR_CERT_AUTHORITY_INVALID', None]:
            with self.subTest(value=value):
                self.assertEqual(native_failure_code(value), '')

    def test_tunnel_noise_preserves_upstream_auth_and_specific_tls_still_wins(self):
        self.assertEqual(native_transport_failure('net::ERR_TUNNEL_CONNECTION_FAILED','local_proxy_auth_failed'),
                         'local_proxy_auth_failed')
        self.assertEqual(native_transport_failure('net::ERR_TUNNEL_CONNECTION_FAILED','local_socks_truncated_reply'),
                         'local_socks_truncated_reply')
        self.assertEqual(native_transport_failure('net::ERR_CERT_AUTHORITY_INVALID','local_proxy_auth_failed'),
                         'tls_verification_failed')
        self.assertEqual(native_transport_failure('net::ERR_INVALID_AUTH_CREDENTIALS','local_proxy_auth_failed'),
                         'native_proxy_auth_failed')

    def test_loading_failed_keeps_actual_upstream_authentication_refusal(self):
        self.b._paused('session',self.helper.req())
        self.b.tunnel.last_error='local_proxy_auth_failed'
        self.b._received({'sessionId':'session','message':json.dumps({'method':'Network.loadingFailed',
            'params':{'requestId':'net-1','errorText':'net::ERR_TUNNEL_CONNECTION_FAILED'}})})
        self.assertEqual(self.b.error,'local_proxy_auth_failed')

    def test_failed_response_preserves_browser_error_not_a_new_client_block(self):
        self.b._paused('session', self.helper.req())
        event = self.helper.req(responseErrorReason='Failed')
        self.b._paused('session', event)
        self.b._send.assert_called_with('session', 'Fetch.continueRequest', {'requestId':'fetch-1'})
        self.assertIsNone(self.b.error)
        self.assertEqual(self.b.native_counts['responses'], 0)

    def test_failed_unaccounted_response_still_stops(self):
        self.b._paused('session', self.helper.req(responseErrorReason='Failed'))
        self.assertEqual(self.b.error, 'native_unaccounted_response')
        self.assertEqual(self.b._send.call_args.args[1], 'Fetch.failRequest')

    def test_loading_failed_reports_original_certificate_code(self):
        self.b._paused('session', self.helper.req())
        self.b._received({'sessionId':'session', 'message': json.dumps({
            'method':'Network.loadingFailed', 'params':{'requestId':'net-1', 'errorText':'net::ERR_CERT_COMMON_NAME_INVALID'}})})
        self.assertEqual(self.b.error, 'tls_verification_failed')

    def test_robots_navigation_keeps_proxy_auth_failure(self):
        self.b.page = Mock()
        self.b.context = Mock()
        self.b._new_page = self.b.context.new_page
        self.b.wire.rules.clear()
        self.b.context.new_page.return_value.goto.side_effect = RuntimeError('Page.goto: net::ERR_INVALID_AUTH_CREDENTIALS at https://secret.test/')
        with self.assertRaises(CrawlError) as raised:
            self.b._load_robots()
        self.assertEqual(raised.exception.code, 'native_proxy_auth_failed')
        self.assertNotIn('secret.test', str(raised.exception))

    def test_existing_policy_failure_wins_over_transport_noise(self):
        self.b.page = Mock()
        self.b.context = Mock()
        self.b._new_page = self.b.context.new_page
        self.b.wire.rules.clear()
        self.b.error = 'robots_denied'
        self.b.context.new_page.return_value.goto.side_effect = RuntimeError('Page.goto: net::ERR_INVALID_AUTH_CREDENTIALS')
        with self.assertRaises(CrawlError) as raised:
            self.b._load_robots()
        self.assertEqual(raised.exception.code, 'robots_denied')

    def test_denied_http_response_is_not_resumed_by_error_preservation(self):
        self.b._paused('session', self.helper.req())
        self.b._paused('session', self.helper.response(403))
        self.assertEqual(self.b.error, 'http_403')
        self.assertEqual(self.b._send.call_args.args[1], 'Fetch.failRequest')
