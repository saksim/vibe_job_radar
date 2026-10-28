"""Finite native rejection metadata; no live requests or account data."""
import json
import unittest
from unittest.mock import patch

from vibe_job_radar.guided.contracts import CrawlError
from vibe_job_radar.guided.diagnostic_trace import DiagnosticTrace, observe, safe_target
from vibe_job_radar.guided.native_policy import liepin_bootstrap, NativeOperationRejected
import test_native_acquisition as fixtures


URL = 'https://api-dok.liepin.com/api/com.liepin.bd.p.v4.get-all-dq'
HEADERS = {'origin': 'https://www.liepin.com', 'access-control-request-method': 'GET',
           'access-control-request-headers': 'content-type,x-client-type'}
SECRET = 'private-contract-fixture-13800000000'


class NativeContractDiagnosticTests(unittest.TestCase):
    def setUp(self):
        self.contract = liepin_bootstrap()

    def test_operation_method_resource_and_login_rejections_are_distinct(self):
        cases = [
            (URL + '/' + SECRET, 'GET', 'XHR', 'operation_unmatched'),
            (URL, 'POST', 'XHR', 'method_unreviewed'),
            (URL, 'GET', 'Document', 'resource_unreviewed'),
            ('https://api-passport.liepin.com/api/com.liepin.passport.account.check-login',
             'POST', 'XHR', 'authentication_required'),
        ]
        for url, method, resource, reason in cases:
            with self.subTest(reason=reason), self.assertRaises(NativeOperationRejected) as got:
                self.contract.match(url, method, resource)
            self.assertEqual(got.exception.code, 'native_operation_unreviewed')
            self.assertEqual(got.exception.native_reason, reason)
            self.assertNotIn(SECRET, repr(vars(got.exception)))

    def test_header_rejections_keep_values_private_and_original_error_code(self):
        rule = self.contract.match(URL, 'OPTIONS', 'XHR')
        for change, reason in (
                ({'origin': 'https://' + SECRET + '.test'}, 'origin_mismatch'),
                ({'access-control-request-method': SECRET}, 'preflight_method_mismatch'),
                ({'access-control-request-headers': ''}, 'preflight_headers_missing'),
                ({'access-control-request-headers': SECRET}, 'preflight_headers_unreviewed')):
            with self.subTest(reason=reason), self.assertRaises(NativeOperationRejected) as got:
                rule.validate_headers('OPTIONS', {**HEADERS, **change})
            self.assertEqual(str(got.exception), 'native_operation_unreviewed')
            self.assertEqual(got.exception.native_reason, reason)
            self.assertEqual(got.exception.native_rule, 'liepin_regions_preflight')
            self.assertNotIn(SECRET, repr(vars(got.exception)))

    def test_existing_success_and_login_mode_are_unchanged(self):
        self.assertEqual(self.contract.match(URL, 'GET', 'XHR').key, 'liepin_regions')
        for kind in ('XHR', 'Fetch', 'Preflight', 'Other'):
            rule = self.contract.match(URL, 'OPTIONS', kind)
            self.assertEqual(rule.key, 'liepin_regions_preflight')
            rule.validate_headers('OPTIONS', HEADERS)
        rule = self.contract.match(
            'https://api-passport.liepin.com/api/com.liepin.passport.account.check-login',
            'POST', 'XHR', authentication=True)
        self.assertEqual(rule.key, 'liepin_password_login')

    def test_unknown_metadata_and_original_target_remain_redacted(self):
        trace = DiagnosticTrace('a' * 32, 'liepin')
        error = NativeOperationRejected(SECRET, SECRET)
        with self.assertRaises(CrawlError) as got:
            with observe(trace, 'route', url=URL + '/' + SECRET + '?token=' + SECRET):
                raise error
        self.assertIs(got.exception, error)
        data = trace.snapshot()
        self.assertEqual(data['first_failure']['native_reason'], 'unknown')
        self.assertEqual(data['first_failure']['native_rule'], 'unknown')
        self.assertNotIn(SECRET, json.dumps(data))

    def test_published_city_operations_do_not_grant_unreviewed_methods(self):
        for name in ('com.liepin.bd.p.suggest-dq', 'com.liepin.bd.v3.batch-lookup-dq'):
            url = 'https://api-dok.liepin.com/api/' + name
            target = safe_target(url + '?token=' + SECRET)
            self.assertEqual(target['path_template'], '/api/' + name)
            self.assertNotIn(SECRET, json.dumps(target))
            with self.assertRaises(NativeOperationRejected):
                self.contract.match(url, 'GET', 'XHR')
            self.assertEqual(safe_target(url.replace('api-dok.', 'unreviewed.'))['path_template'], '/api/:segment')


class NativeBackendContractDiagnosticTests(unittest.TestCase):
    setUp = fixtures.NativeControllerTests.setUp

    def event(self):
        self.b.contract = liepin_bootstrap()
        return {'requestId': 'fetch-1', 'networkId': 'net-1', 'frameId': 'frame',
                'resourceType': 'XHR', 'request': {'url': URL, 'method': 'OPTIONS',
                'headers': {**HEADERS, 'access-control-request-method': SECRET}}}

    def test_first_stop_records_rule_and_reason_without_continuing_request(self):
        for direct in (False, True):
            with self.subTest(direct=direct):
                self.b._direct_cdp = direct
                self.b.error = None
                self.b._halted = False
                self.b._diagnostics = DiagnosticTrace('a' * 32, 'liepin')
                self.b._paused('session', self.event())
                self.assertEqual(self.b.error, 'native_operation_unreviewed')
                self.b._send.assert_called_with('session', 'Fetch.failRequest',
                    {'requestId': 'fetch-1', 'errorReason': 'BlockedByClient'})
                data = self.b._diagnostics.snapshot()
                stop = data['first_backend_stop']
                self.assertEqual(stop['native_reason'], 'preflight_method_mismatch')
                self.assertEqual(stop['native_rule'], 'liepin_regions_preflight')
                self.assertTrue(stop['local_block'])
                self.assertNotIn(SECRET, json.dumps(data))

    def test_failed_observer_does_not_change_rejection(self):
        with patch.object(DiagnosticTrace, 'mark', side_effect=RuntimeError(SECRET)):
            self.b._paused('session', self.event())
        self.assertEqual(self.b.error, 'native_operation_unreviewed')
        self.assertEqual(self.b._send.call_args.args[1], 'Fetch.failRequest')
        self.assertGreater(self.b._diagnostics.observer_errors, 0)


if __name__ == '__main__':
    unittest.main()
