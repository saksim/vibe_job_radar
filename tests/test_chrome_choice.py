"""The installed Chrome option keeps isolated sessions and the existing policy."""
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import test_browser_choice as fixtures
from vibe_job_radar.guided.browser import PlaywrightBackend
from vibe_job_radar.guided.browser_choice import BrowserChoice
from vibe_job_radar.guided.browser_health import BrowserStartupError, failed_report
from vibe_job_radar.guided.cdp_browser import chrome_executable
from vibe_job_radar.guided.diagnostic_trace import DiagnosticTrace
from vibe_job_radar.guided.native_browser import NativeBackend

class ChromeBackendTests(unittest.TestCase):
    setUp = fixtures.BackendChannelTests.setUp
    backend = fixtures.BackendChannelTests.backend

    def test_chrome_launch_uses_fresh_context_without_bundled_browser(self):
        backend = self.backend(channel='chrome'); self.addCleanup(backend.close)
        options = self.runtime.chromium.launch.call_args.kwargs
        self.assertEqual(options['channel'], 'chrome')
        self.assertNotIn('executable_path', options)
        self.assertNotIn('user_data_dir', options)
        self.runtime.chromium.launch_persistent_context.assert_not_called()
        self.assertEqual(self.runtime.chromium.launch.return_value.new_context.call_args.kwargs,
                         {'service_workers': 'block', 'accept_downloads': False})
        self.assertEqual(backend.startup_report['browser_channel'], 'chrome')

    def test_missing_chrome_does_not_retry_another_browser(self):
        self.runtime.chromium.launch.side_effect = RuntimeError("Executable doesn't exist for chrome")
        with self.assertRaises(BrowserStartupError) as error:
            self.backend(channel='chrome')
        self.assertEqual(error.exception.report['code'], 'browser_channel_missing')
        self.runtime.chromium.launch.assert_called_once()

    def test_native_collector_resolves_the_selected_chrome_executable(self):
        backend = NativeBackend.__new__(NativeBackend)
        backend.runtime = self.runtime
        with patch('vibe_job_radar.guided.cdp_browser.chrome_executable', return_value='/installed/chrome') as resolve, \
             patch('vibe_job_radar.guided.cdp_browser.edge_executable', side_effect=AssertionError), \
             patch('vibe_job_radar.guided.cdp_browser.CDPBrowser') as launch:
            NativeBackend._launch_browser(backend, {'channel': 'chrome', 'headless': False, 'proxy': {'server': 'synthetic'}})
        resolve.assert_called_once()
        launch.assert_called_once_with(executable_path='/installed/chrome', headless=False, proxy={'server': 'synthetic'})

class ChromePreferenceTests(unittest.TestCase):
    def test_saved_channel_and_diagnostic_are_chrome_not_bundled(self):
        with tempfile.TemporaryDirectory() as tmp:
            BrowserChoice(Path(tmp)).record(fixtures.ok(), 'chrome', select=True)
            self.assertEqual(BrowserChoice(Path(tmp)).read()['selected'], 'chrome')
        trace = DiagnosticTrace('a'*32, 'liepin', browser='chrome')
        self.assertEqual(trace.snapshot()['browser'], 'chrome')

    def test_native_resolver_only_checks_application_locations(self):
        seen = []
        def exists(path):
            seen.append(path.as_posix())
            return path.as_posix() == 'C:/Programs/Google/Chrome/Application/chrome.exe'
        with patch('vibe_job_radar.guided.cdp_browser.sys.platform', 'win32'), \
             patch.dict('os.environ', {'PROGRAMFILES': 'C:/Programs'}, clear=True), \
             patch.object(Path, 'is_file', exists):
            found = chrome_executable()
        self.assertEqual(Path(found).as_posix(), 'C:/Programs/Google/Chrome/Application/chrome.exe')
        self.assertEqual(seen, ['C:/Programs/Google/Chrome/Application/chrome.exe'])

    def test_missing_native_browser_is_a_channel_error(self):
        with patch.object(Path, 'is_file', return_value=False):
            with self.assertRaises(FileNotFoundError) as error:
                chrome_executable()
        report = failed_report({'stage': 'launch', 'browser_channel': 'chrome'}, error.exception)
        self.assertEqual(report['code'], 'browser_channel_missing')

class ChromeServiceTests(unittest.TestCase):
    setUp = fixtures.ServiceChoiceTests.setUp
    wait = fixtures.ServiceChoiceTests.wait
    choose = fixtures.ServiceChoiceTests.choose

    def test_choice_reaches_collection_and_preserves_source_quota(self):
        before = self.service.ledger.summary('liepin')
        self.choose('chrome')
        self.assertEqual(self.service.state()['browser_choice']['selected'], 'chrome')
        self.probe.assert_called_once_with(channel='chrome')
        factory = Mock(); factory.return_value.startup_report = fixtures.ok()
        self.service.factory = factory
        self.service._backend({'id': 'test', 'platform': 'liepin'})
        self.assertEqual(factory.call_args.kwargs, {'channel': 'chrome'})
        self.service._backends.clear()
        self.assertEqual(self.service.ledger.summary('liepin'), before)
        self.installer.assert_not_called()

    def test_live_browser_is_not_replaced_by_chrome_choice(self):
        from vibe_job_radar.workspace import InputError
        original = Mock(); self.service._backends['existing'] = original
        with self.assertRaises(InputError):
            self.choose('chrome')
        self.assertIs(self.service._backends['existing'], original)
        original.close.assert_not_called(); self.probe.assert_not_called()
        self.service._backends.clear()

    def test_native_choice_uses_native_probe_and_records_its_scope(self):
        report = {**fixtures.ok(), 'network_backend': 'native'}
        with patch('vibe_job_radar.guided.native_check.probe_native_browser', return_value=report) as native:
            self.service.check_browser({'channel': 'chrome', 'backend': 'native', 'consent': True})
            self.wait()
        native.assert_called_once_with(channel='chrome')
        self.probe.assert_not_called()
        state = self.service.state()
        self.assertEqual(state['browser_choice']['selected'], 'chrome')
        self.assertEqual(state['browser_health']['network_backend'], 'native')
        self.assertEqual(state['browser_choice']['last_check']['network_backend'], 'native')

    def test_native_check_failure_preserves_the_original_choice(self):
        with patch('vibe_job_radar.guided.native_check.probe_native_browser', return_value=fixtures.crash()):
            self.service.check_browser({'channel': 'chrome', 'backend': 'native', 'consent': True})
            self.wait()
        self.assertEqual(self.service.state()['browser_choice']['selected'], 'bundled')
        self.probe.assert_not_called()

    def test_invalid_backend_and_nonboolean_consent_never_launch(self):
        from vibe_job_radar.workspace import InputError
        for value in ({}, [], True, 'unreviewed'):
            with self.assertRaises(InputError):
                self.service.check_browser({'channel': 'chrome', 'backend': value, 'consent': True})
        with self.assertRaises(InputError):
            self.service.check_browser({'channel': 'chrome', 'backend': 'native', 'consent': 1})
        self.probe.assert_not_called()
