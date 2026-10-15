"""Keep startup failure facts through the actual component-check boundaries."""
import json
from pathlib import Path
from types import SimpleNamespace
import sys
import unittest
from unittest.mock import patch

from vibe_job_radar.guided.browser_health import BrowserStartupError
from vibe_job_radar.guided.browser_health import failed_report
from vibe_job_radar.guided.cdp_connection import CDPConnection, CDPCommandTimeout
from vibe_job_radar.guided.contracts import CrawlError
from vibe_job_radar.guided.native_check import check_native_browser


def verifier_module():
    scripts = Path(__file__).resolve().parents[1] / 'scripts'
    with patch.object(sys, 'path', [str(scripts), *sys.path]):
        import verify_windows_portable
    return verify_windows_portable


class NativeStartupDiagnosticTests(unittest.TestCase):
    def test_command_deadline_reaches_startup_ui_as_timeout_without_raw_command(self):
        transport = SimpleNamespace(send=lambda value: None, close=lambda: None)
        connection = CDPConnection(transport)
        with patch('vibe_job_radar.guided.cdp_connection.time.monotonic', side_effect=[0,31]):
            with self.assertRaises(CDPCommandTimeout) as caught:
                connection.root.send('Browser.getVersion', {'PRIVATE': 'DATA'}, timeout=30)
        error = caught.exception
        self.assertEqual(error.code, 'native_protocol_error')
        self.assertFalse(connection.pending)
        report = failed_report({'stage':'launch', 'launch_tested':True}, error)
        self.assertEqual(report['code'], 'browser_launch_timeout')
        row = self.component_failure(report, error)
        self.assertEqual(row['startup_failure']['error_type'], 'CDPCommandTimeout')
        self.assertEqual(row['startup_failure']['cause_code'], 'native_protocol_error')
        self.assertNotIn('PRIVATE', str(report) + str(row))

    def test_protocol_rejection_does_not_become_a_deadline_timeout(self):
        transport = SimpleNamespace(send=lambda value: None, close=lambda: None,
            recv=lambda **kw: '{"id":1,"error":{"code":-32601,"message":"PRIVATE timeout"}}')
        connection = CDPConnection(transport)
        with self.assertRaises(CrawlError) as caught:
            connection.root.send('Browser.getVersion')
        self.assertNotIsInstance(caught.exception, CDPCommandTimeout)
        report = failed_report({'stage':'launch'}, caught.exception)
        self.assertEqual(report['code'], 'browser_launch_failed')
        self.assertNotIn('PRIVATE', str(report))

    def component_failure(self, report, cause=None):
        error = BrowserStartupError(report)
        error.__cause__ = cause
        with patch('vibe_job_radar.guided.native_check.NativeBackend',
                   side_effect=error) as launch:
            row = check_native_browser(channel='chrome')
        launch.assert_called_once()
        self.assertFalse(row['success'])
        self.assertFalse(row['blank_page_check'])
        self.assertFalse(row['cleanup']['attempted'])
        return row

    def test_launch_failure_preserves_original_phase_type_and_fixed_cause(self):
        row = self.component_failure(
            {'code':'browser_launch_failed', 'stage':'launch',
             'error_type':'CrawlError', 'launch_tested':True,
             'error_summary':'PRIVATE RESPONSE', 'executable_path':'PRIVATE PATH'},
            CrawlError('native_protocol_error'))
        self.assertEqual(row['startup_failure'], {
            'stage':'launch', 'error_type':'CrawlError',
            'cause_code':'native_protocol_error', 'launch_tested':True,
            'process_exit_hex':''})
        self.assertNotIn('PRIVATE', str(row))

    def test_context_failure_is_distinguished_from_browser_launch(self):
        row = self.component_failure(
            {'code':'browser_context_failed', 'stage':'context',
             'error_type':'Error', 'launch_tested':True},
            RuntimeError('PRIVATE DATA'))
        self.assertEqual(row['startup_failure']['stage'], 'context')
        self.assertEqual(row['startup_failure']['error_type'], 'Error')
        self.assertEqual(row['startup_failure']['cause_code'], '')
        self.assertNotIn('PRIVATE', str(row))

    def test_unrecognized_values_cannot_enter_startup_evidence(self):
        row = self.component_failure(
            {'code':'browser_launch_failed', 'stage':'PRIVATE STAGE',
             'error_type':'PRIVATE TYPE', 'launch_tested':1,
             'process_exit_hex':'PRIVATE EXIT'},
            CrawlError('PRIVATE CODE'))
        self.assertEqual(row['startup_failure'], {
            'stage':'unknown', 'error_type':'other', 'cause_code':'',
            'launch_tested':None, 'process_exit_hex':''})
        self.assertNotIn('PRIVATE', str(row))

    def portable_failure(self, row):
        verifier = verifier_module()
        evidence = {}
        with patch.object(verifier.subprocess, 'CREATE_NO_WINDOW', 0, create=True), \
                patch.object(verifier.subprocess, 'run', return_value=SimpleNamespace(
                    returncode=2, stdout=json.dumps(row).encode(), stderr=b'PRIVATE STDERR')) as run:
            with self.assertRaises(AssertionError):
                verifier.verify_native_component(Path('owned.exe'), Path('cwd'),
                                                 {}, 'chrome', evidence=evidence)
        run.assert_called_once()
        self.assertEqual(run.call_args.kwargs['timeout'], 60)
        self.assertEqual(evidence['returncode'], 2)
        self.assertNotIn('PRIVATE', str(evidence))
        return evidence

    def test_failed_executable_preserves_diagnostic_before_original_gate_raises(self):
        facts = {'stage':'launch', 'error_type':'CrawlError',
                 'cause_code':'native_protocol_error', 'launch_tested':True,
                 'process_exit_hex':''}
        row = {'success':False, 'stage':'launch', 'code':'browser_launch_failed',
               'startup_failure':{**facts, 'raw_message':'PRIVATE RAW'}}
        evidence = self.portable_failure(row)
        self.assertEqual(evidence['startup_failure'], facts)

    def test_packaged_boundary_filters_unknown_or_mistyped_diagnostic_fields(self):
        evidence = self.portable_failure({
            'success':False, 'startup_failure':{
                'stage':[], 'error_type':{'PRIVATE':'value'}, 'cause_code':'PRIVATE CAUSE',
                'launch_tested':'PRIVATE BOOL', 'process_exit_hex':False,
                'url':'PRIVATE URL'}})
        self.assertEqual(evidence['startup_failure'], {
            'stage':'unknown', 'error_type':'other', 'cause_code':'',
            'launch_tested':None, 'process_exit_hex':''})

    def test_packaged_boundary_retains_already_available_launch_and_exit_facts(self):
        evidence = self.portable_failure({
            'success':False, 'launch_tested':True,
            'process_exit_hex':'0xC0000374', 'code':'browser_native_heap_corruption'})
        self.assertTrue(evidence['launch_tested'])
        self.assertEqual(evidence['process_exit_hex'], '0xC0000374')
