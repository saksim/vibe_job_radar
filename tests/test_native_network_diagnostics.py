"""Production loading failures retain finite request facts, never page secrets."""
import copy
import json
import unittest
from unittest.mock import Mock, patch

import test_native_acquisition as fixture
from vibe_job_radar.guided.diagnostic_trace import DiagnosticTrace, observe


SECRET = 'private-url-body-header-failed-parameter'


class NativeNetworkDiagnosticTests(unittest.TestCase):
    def setUp(self):
        self.helper = fixture.NativeControllerTests()
        self.helper.setUp()
        self.addCleanup(self.helper.doCleanups)
        self.b = self.helper.b

    def admit(self, direct=False):
        self.b._direct_cdp = direct
        if direct:
            self.b._request_pacer = Mock()
        self.b._paused('session', self.helper.req('/api/jobs?token=' + SECRET))
        if direct:
            item = self.b._request_pacer.submit.call_args.args[0]
            self.b._continue_request(item)
        self.b._send.reset_mock()

    def fail(self, error='net::ERR_CONNECTION_RESET', **data):
        self.b._received({'sessionId': 'session', 'message': json.dumps({
            'method': 'Network.loadingFailed', 'params': {'requestId': 'net-1',
            'type': 'XHR', 'errorText': error, **data}})})

    def test_both_controllers_bind_first_stop_to_the_failed_request(self):
        for direct in (False, True):
            with self.subTest(direct=direct):
                self.b.error = None
                self.b._halted = False
                self.b._diagnostics = DiagnosticTrace('a' * 32, 'fixture')
                self.admit(direct)
                # Another observation must not lend its URL/method to the stop.
                with observe(self.b._diagnostics, 'listing', url='https://unrelated.test/search'):
                    self.fail()
                stop = self.b._diagnostics.snapshot()['first_backend_stop']
                self.assertEqual(self.b.error, 'network_error')
                self.assertEqual((stop['host'], stop['path_template'], stop['method'], stop['resource']),
                                 ('jobs.fixture.test', '/api/jobs', 'POST', 'fetch'))
                self.assertEqual(stop['native_stage'], 'before_response')
                self.assertEqual(stop['native_failure']['browser_error'], 'ERR_CONNECTION_RESET')
                self.assertNotIn(SECRET, json.dumps(stop))
                self.assertEqual(self.b._requests, {})
                self.b._send.assert_not_called()

    def test_headers_200_cors_failure_is_not_a_completed_body(self):
        self.admit()
        self.b._paused('session', self.helper.response(200, {'content-type': 'application/json'}))
        self.b._send.reset_mock()
        self.fail('net::ERR_FAILED', canceled=False, blockedReason='origin',
                  corsErrorStatus={'corsError': 'AllowOriginMismatch', 'failedParameter': SECRET})
        stop = self.b._diagnostics.snapshot()['first_backend_stop']
        self.assertEqual((stop['status'], stop['native_stage']), (200, 'response_headers'))
        self.assertEqual(stop['native_failure']['cors_error'], 'AllowOriginMismatch')
        self.assertEqual(stop['native_failure']['blocked_reason'], 'origin')
        self.assertFalse(stop['native_failure']['canceled'])
        self.assertEqual(self.b.error, 'network_error')
        self.assertEqual(self.b.observations(), ())
        self.b._send.assert_not_called()
        self.assertNotIn(SECRET, json.dumps(self.b._diagnostics.snapshot()))

    def test_completed_body_does_not_claim_parsed_job_results(self):
        self.admit()
        self.b._paused('session', self.helper.response(200, {'content-type': 'application/json'}))
        self.b._finished('session', {'requestId': 'net-1'})
        trace = self.b._diagnostics.snapshot()
        self.assertEqual(trace['events'][-1]['native_stage'], 'body_complete')
        self.assertIsNone(trace['first_backend_stop'])
        self.assertEqual(self.b.observations(), ())  # getResponseBody has not replied.

    def test_unknown_values_are_filtered_and_first_stop_survives_noise(self):
        self.admit()
        self.fail(SECRET, canceled=SECRET, blockedReason=SECRET,
                  corsErrorStatus={'corsError': [SECRET], 'failedParameter': SECRET}, url=SECRET)
        first = copy.deepcopy(self.b._diagnostics.snapshot()['first_backend_stop'])
        self.assertEqual(first['native_failure']['browser_error'], 'unknown')
        self.assertEqual(first['native_failure']['blocked_reason'], 'unknown')
        self.assertEqual(first['native_failure']['cors_error'], 'unknown')
        self.assertIsNone(first['native_failure']['canceled'])
        self.fail('net::ERR_ABORTED', canceled=True)
        self.assertEqual(first, self.b._diagnostics.snapshot()['first_backend_stop'])
        self.assertNotIn(SECRET, json.dumps(self.b._diagnostics.snapshot()))

    def test_optional_unowned_and_user_cancelled_events_do_not_add_a_stop(self):
        for mode in ('asset', 'cancelled', 'unowned'):
            with self.subTest(mode=mode):
                self.admit()
                if mode == 'asset':
                    self.b._requests['session', 'net-1']['role'] = 'asset'
                elif mode == 'cancelled':
                    self.b.cancelled.set()
                else:
                    self.b._requests.clear()
                self.fail('net::ERR_ABORTED', canceled=True)
                self.assertIsNone(self.b.error)
                self.assertIsNone(self.b._diagnostics.snapshot()['first_backend_stop'])
                self.b.cancelled.clear()

    def test_observer_faults_cannot_change_error_or_resume_request(self):
        for entrypoint in ('begin', 'mark', 'finish'):
            with self.subTest(entrypoint=entrypoint):
                self.b.error = None
                self.b._halted = False
                self.admit()
                with patch.object(DiagnosticTrace, entrypoint, side_effect=RuntimeError(SECRET)):
                    self.fail('net::ERR_CERT_AUTHORITY_INVALID')
                self.assertEqual(self.b.error, 'tls_verification_failed')
                self.assertTrue(self.b._halted)
                self.b._send.assert_not_called()


if __name__ == '__main__':
    unittest.main()
