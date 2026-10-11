"""Developer-only workbench readiness checks; no site or login decision changes."""
import importlib.util
from pathlib import Path
from unittest.mock import Mock
from types import SimpleNamespace
import json
import unittest

class WorkbenchReadinessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = Path(__file__).resolve().parents[1]/'scripts/run_browser_choice_acceptance.py'
        spec = importlib.util.spec_from_file_location('edge_readiness_fixture', path)
        cls.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.module)

    def setup_page(self):
        server = Mock(origin='http://127.0.0.1:12345', entry_url='http://127.0.0.1:12345/#token=SYNTHETIC')
        page = Mock(url=server.origin + '/')
        page.goto.return_value.status = 200
        return page, server

    def test_waits_for_authenticated_ui_without_retry_or_load(self):
        page, server = self.setup_page()
        expect = Mock()
        self.module.open_workbench(page, server, expect)
        page.goto.assert_called_once_with(server.entry_url, wait_until='domcontentloaded', timeout=30000)
        expect.return_value.to_contain_text.assert_called_once_with('真实记录 0', timeout=30000)
        expect.return_value.to_be_visible.assert_called_once()
        page.reload.assert_not_called()

    def test_navigation_error_not_swallowed_or_retried(self):
        page, server = self.setup_page()
        page.goto.side_effect = TimeoutError('synthetic failure')
        with self.assertRaises(TimeoutError):
            self.module.open_workbench(page, server, Mock())
        self.assertEqual(page.goto.call_count, 1)

    def test_200_alone_cannot_pass_without_ui_initialization(self):
        page, server = self.setup_page()
        expect = Mock()
        expect.return_value.to_contain_text.side_effect = AssertionError('no status')
        with self.assertRaises(AssertionError):
            self.module.open_workbench(page, server, expect)

    def test_http_error_or_unconsumed_fragment_cannot_pass(self):
        page, server = self.setup_page()
        page.goto.return_value.status = 403
        with self.assertRaises(AssertionError):
            self.module.open_workbench(page, server, Mock())
        page.goto.return_value.status = 200
        page.url = server.entry_url
        with self.assertRaises(AssertionError):
            self.module.open_workbench(page, server, Mock())


class ReadinessRequestEvidenceTests(unittest.TestCase):
    setUpClass = classmethod(WorkbenchReadinessTests.setUpClass.__func__)
    setup_page = WorkbenchReadinessTests.setup_page
    def test_independent_cold_attempts_and_polling_cannot_hide_status(self):
        page, server = self.setup_page()
        evidence = []
        callbacks = {}
        page.on.side_effect = lambda name, callback: callbacks.__setitem__(name, callback)
        with self.module.observe_workbench_readiness(page, server, evidence):
            for _ in range(1100):
                callbacks['response'](SimpleNamespace(url=server.origin+'/api/poll?private=value', status=200))
        old = json.dumps(evidence[0], sort_keys=True)
        with self.module.observe_workbench_readiness(page, server, evidence):
            request = SimpleNamespace(url=server.origin+'/api/status?private=value')
            callbacks['request'](request)
            callbacks['response'](SimpleNamespace(url=request.url, status=503))
            callbacks['requestfinished'](request)
        self.assertEqual(json.dumps(evidence[0], sort_keys=True), old)
        self.assertTrue(evidence[0]['counter_saturated'])
        self.assertEqual(evidence[0]['routes']['other_local']['responses'], 999)
        status = evidence[1]['routes']['/api/status']
        self.assertEqual((status['started'], status['responses'], status['finished'], status['failed']), (1,1,1,0))
        self.assertEqual(status['last_status'], 503)
        self.assertNotIn('private', json.dumps(evidence))

    def test_response_headers_without_body_completion_remain_pending_after_cleanup(self):
        page, server = self.setup_page()
        evidence = []; callbacks = {}
        page.on.side_effect = lambda name, callback: callbacks.__setitem__(name, callback)
        error = AssertionError('private failure text')
        with self.assertRaises(AssertionError) as caught:
            with self.module.observe_workbench_readiness(page, server, evidence) as row:
                row['phase'] = 'authenticated-counts'
                req = SimpleNamespace(url=server.origin+'/api/status', failure='net::ERR_ABORTED')
                callbacks['request'](req)
                callbacks['response'](SimpleNamespace(url=req.url, status=200))
                raise error
        self.assertIs(caught.exception, error)
        before = json.dumps(evidence, sort_keys=True)
        callbacks['requestfailed'](req); callbacks['close']()
        self.assertEqual(json.dumps(evidence, sort_keys=True), before)
        self.assertEqual(evidence[0]['routes']['/api/status']['finished'], 0)
        self.assertEqual(evidence[0]['routes']['/api/status']['failed'], 0)
        self.assertEqual(evidence[0]['outcome'], 'failed')
        self.assertEqual(page.remove_listener.call_count, 7)

    def test_failed_requests_save_only_fixed_codes_and_page_event_counts(self):
        page, server = self.setup_page()
        evidence = []; callbacks = {}; secret = 'SECRET-URL-OR-ERROR'
        page.on.side_effect = lambda name, callback: callbacks.__setitem__(name, callback)
        with self.module.observe_workbench_readiness(page, server, evidence):
            for path, code in (('/api/status', 'net::ERR_CONNECTION_RESET'), ('/'+secret, secret)):
                req = SimpleNamespace(url=server.origin+path+'?token='+secret+'#'+secret, failure=code)
                callbacks['request'](req); callbacks['requestfailed'](req)
            callbacks['response'](SimpleNamespace(url='https://'+secret+'.invalid/', status=secret))
            callbacks['pageerror'](RuntimeError(secret)); callbacks['crash']()
        self.assertNotIn(secret, json.dumps(evidence))
        row = evidence[0]
        self.assertEqual(row['routes']['/api/status']['last_network_error'], 'net::ERR_CONNECTION_RESET')
        self.assertEqual(row['routes']['other_local']['last_network_error'], 'unknown')
        self.assertIsNone(row['routes']['external']['last_status'])
        self.assertEqual(row['page_errors'], 1)
        self.assertEqual(row['crashes'], 1)

    def test_observer_fault_preserves_original_assertion_and_marks_unavailable(self):
        page, server = self.setup_page()
        evidence = []; callbacks = {}
        page.on.side_effect = lambda name, callback: callbacks.__setitem__(name, callback)
        page.remove_listener.side_effect = RuntimeError('private observer failure')
        error = AssertionError('original')
        with self.assertRaises(AssertionError) as caught:
            with self.module.observe_workbench_readiness(page, server, evidence):
                callbacks['response'](object())
                raise error
        self.assertIs(caught.exception, error)
        self.assertEqual(evidence[0]['outcome'], 'failed')
        self.assertTrue(evidence[0]['observer_failed'])
        self.assertEqual(evidence[0]['routes'], {})
        self.assertNotIn('private', json.dumps(evidence))

    def test_http500_cannot_pass_readiness_or_trigger_an_extra_navigation(self):
        page, server = self.setup_page()
        evidence = []; callbacks = {}; expected = Mock()
        page.on.side_effect = lambda name, callback: callbacks.__setitem__(name, callback)
        def goto(*args, **kwargs):
            req = SimpleNamespace(url=server.origin+'/api/status')
            callbacks['request'](req)
            callbacks['response'](SimpleNamespace(url=req.url, status=500))
            callbacks['requestfinished'](req)
            return SimpleNamespace(status=200)
        page.goto.side_effect = goto
        expected.return_value.to_contain_text.side_effect = AssertionError('status rejected')
        with self.assertRaises(AssertionError):
            self.module.open_workbench(page, server, expected, evidence=evidence)
        self.assertEqual(evidence[0]['outcome'], 'failed')
        self.assertEqual(evidence[0]['phase'], 'authenticated-counts')
        self.assertEqual(evidence[0]['routes']['/api/status']['last_status'], 500)
        page.goto.assert_called_once_with(server.entry_url, wait_until='domcontentloaded', timeout=30000)
        expected.return_value.to_contain_text.assert_called_once_with('真实记录 0', timeout=30000)
        page.reload.assert_not_called()
