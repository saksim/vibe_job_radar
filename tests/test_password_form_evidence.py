"""Failure receipts must not change the synthetic form's protocol behavior."""
from contextlib import contextmanager, redirect_stdout
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def load_script(name):
    spec = importlib.util.spec_from_file_location('fixture_' + name, ROOT / 'scripts' / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    with patch.object(sys, 'path', [str(ROOT / 'scripts'), str(ROOT / 'src'), *sys.path]):
        spec.loader.exec_module(module)
    return module


module = load_script('password_form_evidence')
driver = load_script('verify_password_form')


class FormEvidenceTests(unittest.TestCase):
    def test_observer_forwards_exact_bytes_and_does_not_retry(self):
        evidence = module.FormEvidence('chrome', True)
        raw = json.dumps({'id': 7, 'method': 'Runtime.evaluate', 'params': {'expression': 'private-value'}})
        reply = json.dumps({'id': 7, 'result': {'private-key': 'private-value'}})
        calls = []
        transport = SimpleNamespace(
            send=lambda value: calls.append(('send', value)),
            recv=lambda **kwargs: (calls.append(('recv', kwargs)), reply)[1],
            close=lambda: calls.append(('close',)))
        observed = evidence.wrap(transport)
        observed.send(raw)
        self.assertEqual(observed.recv(timeout=.05), reply)
        observed.close()
        self.assertEqual(calls, [('send', raw), ('recv', {'timeout': .05}), ('close',)])
        self.assertNotIn('private-', json.dumps(evidence.report(True)))

    def test_protocol_rejection_still_raises_original_error_with_safe_context(self):
        from vibe_job_radar.guided.cdp_connection import CDPConnection
        evidence = module.FormEvidence('chrome', True)
        evidence.enter('page_load', 'unchecked_agreement')
        evidence.received(json.dumps({'method': 'Fetch.requestPaused', 'params': {
            'requestId': 'private-request-id', 'resourceType': 'Other',
            'request': {'url': 'https://secret-user:secret-pass@private.example/favicon.ico?token=private-token'}}}))
        sent = []
        reply = json.dumps({'id': 1, 'error': {'code': -32602, 'message': 'Invalid InterceptionId.'}})
        inner = SimpleNamespace(send=lambda raw: sent.append(raw), recv=lambda **_: reply, close=lambda: None)
        connection = CDPConnection(evidence.wrap(inner))
        with self.assertRaises(driver.CrawlError) as raised:
            connection.session('page').send('Fetch.fulfillRequest', {'requestId': 'private-request-id', 'body': 'private-body'})
        self.assertEqual(raised.exception.code, 'native_protocol_error')
        self.assertEqual(len(sent), 1)
        error = evidence.report(False)['protocol_errors'][0]
        self.assertEqual(error['request'], {'resource_type': 'Other', 'role': 'favicon'})
        self.assertEqual(error['scenario'], 'unchecked_agreement')
        self.assertEqual((error['error_code'], error['error_category']), (-32602, 'invalid_interception_id'))
        self.assertNotIn('private-', json.dumps(evidence.report(False)))
        self.assertNotIn('secret-', json.dumps(evidence.report(False)))

    def test_unknown_protocol_text_and_values_never_escape(self):
        evidence = module.FormEvidence(None, True)
        evidence.enter('password_submit', 'submit_once')
        evidence.sent(json.dumps({'id': 1, 'method': 'Runtime.evaluate',
                                  'params': {'expression': 'secret-expression'}}))
        evidence.received(json.dumps({'id': 1, 'error': {'code': True,
            'message': 'secret-password https://private.example', 'data': {'secret-cookie': 'value'}}}))
        error = evidence.report(False)['protocol_errors'][0]
        self.assertIsNone(error['error_code'])
        self.assertEqual(error['error_category'], 'other_protocol_error')
        self.assertNotIn('secret-', json.dumps(evidence.report(False)))
        self.assertNotIn('private.', json.dumps(evidence.report(False)))

    def test_metadata_is_bounded_when_requests_or_replies_do_not_finish(self):
        evidence = module.FormEvidence('chrome', True)
        for number in range(120):
            evidence.received(json.dumps({'method': 'Fetch.requestPaused', 'params': {
                'requestId': str(number), 'resourceType': 'Document',
                'request': {'url': 'https://private.example'}}}))
            evidence.sent(json.dumps({'id': number, 'method': 'Page.navigate'}))
        self.assertLessEqual(len(evidence.requests), 64)
        self.assertLessEqual(len(evidence.pending), 32)
        for number in range(1000, 1100):
            evidence.sent(json.dumps({'id': number, 'method': 'Page.navigate'}))
            evidence.received(json.dumps({'id': number, 'error': {'code': -1, 'message': 'private-error'}}))
        self.assertLessEqual(len(evidence.report(False)['protocol_errors']), 32)
        self.assertGreater(evidence.report(False)['diagnostic_dropped'], 0)
        self.assertNotIn('private', json.dumps(evidence.report(False)))

    def test_malformed_observations_and_transport_failure_preserve_original_result(self):
        evidence = module.FormEvidence('chrome', True)
        for raw in ['{', '[]', 'null', '"text"', '{"method":[]}', '{"id":[]}',
                    '{"method":"Fetch.requestPaused","params":{"requestId":"x","resourceType":[],"request":{"url":[]}}}',
                    'x' * 64_001]:
            evidence.sent(raw)
            evidence.received(raw)
        error = TimeoutError('private-transport-message')

        def receive(**_):
            raise error

        observed = evidence.wrap(SimpleNamespace(recv=receive))
        with self.assertRaises(TimeoutError) as raised:
            observed.recv(timeout=.05)
        self.assertIs(raised.exception, error)
        self.assertNotIn('private-', json.dumps(evidence.report(False)))

    def test_first_failure_survives_cleanup_and_preserves_completed_prefix(self):
        evidence = module.FormEvidence('chrome', True)
        evidence.checks.append('submit_once')
        evidence.enter('page_load', 'unchecked_agreement')
        evidence.fail(driver.CrawlError('native_protocol_error'))
        evidence.enter('cleanup')
        evidence.fail(RuntimeError('private-cleanup-message'))
        report = evidence.report(False)
        self.assertEqual(report['checks'], ['submit_once'])
        self.assertEqual(report['failure'], {'phase': 'page_load', 'scenario': 'unchecked_agreement',
                                            'code': 'native_protocol_error', 'kind': 'CrawlError'})
        self.assertNotIn('private-', json.dumps(report))

    def run_failing_main(self, write_error=False):
        error = driver.CrawlError('native_protocol_error')

        class Page:
            def goto(self, _):
                raise error

        @contextmanager
        def fixture(*_):
            yield SimpleNamespace(version='154.0.8037.58'), Page()

        @contextmanager
        def runtime():
            yield object()

        api = ModuleType('playwright.sync_api')
        api.sync_playwright = runtime
        package = ModuleType('playwright')
        stream = io.StringIO()
        with tempfile.TemporaryDirectory() as folder:
            previous = Path.cwd()
            try:
                os.chdir(folder)
                with patch.dict(sys.modules, {'playwright': package, 'playwright.sync_api': api}), \
                        patch.object(driver, 'fixture_browser', fixture), \
                        patch.object(sys, 'argv', ['verify_password_form.py', '--channel', 'chrome', '--native']), \
                        redirect_stdout(stream):
                    if write_error:
                        with patch.object(driver.FormEvidence, 'publish', side_effect=OSError('private-path')):
                            with self.assertRaises(driver.CrawlError) as raised:
                                driver.main()
                    else:
                        with self.assertRaises(driver.CrawlError) as raised:
                            driver.main()
                    self.assertIs(raised.exception, error)
                path = Path('browser-acceptance/browser-choice-chrome-native/password-form-results.json')
                if not write_error:
                    self.assertTrue(path.is_file(), 'failure must leave an artifact before the CLI exits')
                    self.assertEqual(json.loads(path.read_text(encoding='utf8')), json.loads(stream.getvalue()))
            finally:
                os.chdir(previous)
        return json.loads(stream.getvalue())

    def test_first_page_failure_is_saved_in_existing_uploaded_directory(self):
        report = self.run_failing_main()
        self.assertFalse(report['success'])
        self.assertEqual(report['checks'], [])
        self.assertEqual(report['failure']['phase'], 'page_load')
        self.assertEqual(report['failure']['scenario'], 'submit_once')
        self.assertEqual(report['failure']['code'], 'native_protocol_error')
        self.assertFalse(report['real_account_tested'])
        self.assertIsNone(report['external_requests'])

    def test_artifact_write_error_keeps_primary_failure_in_stdout(self):
        report = self.run_failing_main(write_error=True)
        self.assertTrue(report['artifact_write_failed'])
        self.assertEqual(report['failure']['code'], 'native_protocol_error')
        self.assertNotIn('private-', json.dumps(report))

    def test_success_retains_original_report_fields_and_order(self):
        evidence = module.FormEvidence('chrome', True)
        evidence.checks = ['submit_once', 'unchecked_agreement', 'unchanged_sms']
        evidence.version = '154.0.8037.58'
        report = evidence.report(True)
        self.assertEqual(report['checks'], evidence.checks)
        self.assertEqual(report['browser_version'], '154.0.8037.58')
        self.assertEqual(report['external_requests'], 0)
        self.assertIsNone(report['failure'])
        self.assertEqual(report['input_backend'], 'minimal_cdp')
        self.assertFalse(report['real_account_tested'])


if __name__ == '__main__':
    unittest.main()
