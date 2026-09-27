"""Fixed metadata identifies an actual controller stop, never grants access."""
import json
import unittest
from unittest.mock import patch
import test_native_acquisition as fixtures
from vibe_job_radar.guided.diagnostic_trace import safe_target
from vibe_job_radar.guided.contracts import CrawlError

SECRET = 'private-login-value-must-not-leak'

class LoginStopDiagnosticsTests(unittest.TestCase):
    setUp = fixtures.NativeControllerTests.setUp
    req = fixtures.NativeControllerTests.req

    def test_optional_failure_then_preflight_stop_survives_later_blocks(self):
        first = self.req(path='/unreviewed-asset', kind='Other')
        self.b._paused('session', first)
        self.assertIsNone(self.b.error)
        self.assertIsNone(self.b._diagnostics.snapshot()['first_backend_stop'])
        fatal = self.req(path='/api/'+SECRET+'?token='+SECRET, kind='Preflight', method='OPTIONS')
        self.b._paused('session', fatal)
        before = self.b._diagnostics.snapshot()['first_backend_stop']
        self.assertEqual(before['code'], 'native_operation_unreviewed')
        self.assertEqual(before['resource'], 'preflight')
        self.assertEqual(before['method'], 'OPTIONS')
        self.assertEqual(before['path_template'], '/api/:segment')
        for _ in range(110):
            self.b._paused('session', self.req(path='/later', kind='XHR'))
        after = self.b._diagnostics.snapshot()
        self.assertEqual(after['first_backend_stop'], before)
        self.assertGreater(after['dropped_events'], 0)
        self.assertNotIn(SECRET, json.dumps(after))
        self.b._diagnostics.disable()
        self.assertIsNone(self.b._diagnostics.snapshot()['first_backend_stop'])

    def test_only_fixed_public_operation_on_exact_host_is_readable(self):
        path = '/api/com.liepin.passport.account.send-two-factor-sms-code'
        target = safe_target('https://api-passport.liepin.com'+path+'?token='+SECRET)
        self.assertEqual(target['path_template'], path)
        for url in ('https://other.test'+path, 'https://api-passport.liepin.com'+path+'/'+SECRET):
            self.assertNotEqual(safe_target(url)['path_template'], path)
            self.assertNotIn(SECRET, json.dumps(safe_target(url)))
        from vibe_job_radar.guided.adapters import builtins
        from vibe_job_radar.guided.native_policy import contract_for
        with self.assertRaises(CrawlError):
            contract_for(builtins().get('liepin')).match('https://api-passport.liepin.com'+path, 'POST', 'XHR', authentication=True)

    def test_observer_failure_cannot_prevent_stopping(self):
        with patch.object(self.b._diagnostics, 'backend_stop', side_effect=RuntimeError(SECRET)):
            self.b._fatal('native_operation_unreviewed')
        self.assertTrue(self.b._halted)
        self.assertEqual(self.b.error, 'native_operation_unreviewed')
        self.assertEqual(self.b._diagnostics.observer_errors, 1)
        self.assertNotIn(SECRET, json.dumps(self.b._diagnostics.snapshot()))

    def test_stop_without_request_has_no_invented_url(self):
        self.b._fatal('native_protocol_error')
        stop = self.b._diagnostics.snapshot()['first_backend_stop']
        self.assertEqual(stop['stage'], 'network_policy')
        self.assertEqual(stop['host'], '')
        self.assertEqual(stop['code'], 'native_protocol_error')
