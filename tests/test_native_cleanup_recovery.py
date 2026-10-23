"""Private native-browser cleanup faults; no browser, site or user profile."""
from collections import defaultdict
import io
import subprocess
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from vibe_job_radar.guided.cdp_browser import CDPBrowser
from vibe_job_radar.guided.cdp_connection import CDPCommandTimeout
from vibe_job_radar.guided.cdp_page import CDPPage


class NativeCleanupRecoveryTests(unittest.TestCase):
    def browser(self):
        parent = tempfile.TemporaryDirectory()
        self.addCleanup(parent.cleanup)
        profile = Path(tempfile.mkdtemp(prefix='vibe-native-cdp-', dir=parent.name)).resolve()
        (profile/'fixture').write_text('artificial private profile')
        browser = CDPBrowser.__new__(CDPBrowser)
        browser.profile, browser.profile_parent = profile, profile.parent
        browser.cleanup_failed = False
        browser.contexts = []
        browser.connection = Mock(closed=True)
        browser.process = Mock(pid=12345, stdout=io.BytesIO())
        browser.process.poll.return_value = 0
        return browser

    def test_transport_close_failure_does_not_skip_remaining_owned_cleanup(self):
        browser = self.browser()
        problem = OSError('artificial transport close fault')
        browser.connection.close.side_effect = problem
        page = Mock()
        browser.contexts = [SimpleNamespace(_pages={'page': page})]
        with self.assertRaises(OSError) as caught:
            browser.close()
        self.assertIs(caught.exception, problem)
        self.assertTrue(browser.process.stdout.closed)
        page._did_close.assert_called_once_with()
        self.assertFalse(browser.profile.exists())
        self.assertTrue(browser.cleanup_failed)

    def test_page_close_callback_failure_does_not_skip_other_page_or_profile(self):
        browser = self.browser()
        problem = RuntimeError('artificial page callback fault')
        first, second = Mock(), Mock()
        first._did_close.side_effect = problem
        browser.contexts = [SimpleNamespace(_pages={'first': first, 'second': second})]
        with self.assertRaises(RuntimeError) as caught:
            browser.close()
        self.assertIs(caught.exception, problem)
        second._did_close.assert_called_once_with()
        self.assertFalse(browser.profile.exists())
        self.assertTrue(browser.cleanup_failed)

    def test_startup_primary_timeout_is_not_replaced_by_cleanup_failure(self):
        problem = CDPCommandTimeout()
        connection = Mock()
        connection.root.send.side_effect = problem
        with tempfile.TemporaryDirectory() as temporary, \
                patch('vibe_job_radar.guided.cdp_browser.tempfile.mkdtemp', return_value=temporary), \
                patch('vibe_job_radar.guided.cdp_browser.node_executable', return_value='artificial-node'), \
                patch('vibe_job_radar.guided.cdp_browser.subprocess.Popen'), \
                patch('vibe_job_radar.guided.cdp_browser.PipeTransport'), \
                patch('vibe_job_radar.guided.cdp_browser.CDPConnection', return_value=connection), \
                patch.object(CDPBrowser, 'close', side_effect=OSError('artificial cleanup fault')):
            with self.assertRaises(CDPCommandTimeout) as caught:
                CDPBrowser(executable_path='artificial-browser', headless=True,
                           args=[], proxy={'server': 'http://127.0.0.1:1'})
        self.assertIs(caught.exception, problem)

    def test_failed_owned_process_shutdown_preserves_profile_and_error(self):
        browser = self.browser()
        problem = OSError('artificial process access denied')
        browser.process.poll.return_value = None
        browser.process.wait.side_effect = problem
        with self.assertRaises(OSError) as caught:
            browser.close()
        self.assertIs(caught.exception, problem)
        browser.connection.close.assert_called_once_with()
        self.assertTrue(browser.process.stdout.closed)
        self.assertTrue(browser.profile.exists())
        self.assertTrue(browser.cleanup_failed)

    def test_normal_close_is_repeatable_and_cannot_kill_unrelated_process(self):
        browser = self.browser()
        sibling = browser.profile.parent/'unrelated-user-profile'
        sibling.mkdir()
        marker = sibling/'history'
        marker.write_bytes(b'untouched')
        with patch('vibe_job_radar.guided.cdp_browser.subprocess.run') as command:
            browser.close()
            browser.close()
        command.assert_not_called()
        browser.process.kill.assert_not_called()
        self.assertEqual(marker.read_bytes(), b'untouched')
        self.assertFalse(browser.profile.exists())
        self.assertFalse(browser.cleanup_failed)

    def test_termination_after_transport_failure_targets_only_owned_pid(self):
        browser = self.browser()
        problem = OSError('artificial transport close fault')
        browser.connection.close.side_effect = problem
        browser.process.poll.return_value = None
        browser.process.wait.side_effect = [subprocess.TimeoutExpired('owned', 5),
                                           subprocess.TimeoutExpired('owned', 3), 0]
        def terminated(*args, **kwargs):
            browser.process.poll.return_value = 0
        with patch('vibe_job_radar.guided.cdp_browser.sys.platform', 'win32'), \
                patch('vibe_job_radar.guided.cdp_browser.subprocess.CREATE_NO_WINDOW', 0, create=True), \
                patch('vibe_job_radar.guided.cdp_browser.subprocess.run', side_effect=terminated) as command:
            with self.assertRaises(OSError) as caught:
                browser.close()
        self.assertIs(caught.exception, problem)
        command.assert_called_once()
        self.assertEqual(command.call_args.args[0], ['taskkill', '/PID', '12345', '/T', '/F'])
        self.assertEqual(command.call_args.kwargs['timeout'], 10)
        browser.process.kill.assert_not_called()
        self.assertFalse(browser.profile.exists())
        self.assertTrue(browser.process.stdout.closed)
        browser.connection.close.side_effect = None
        browser.close()

    def test_close_listener_fault_drops_callbacks_and_close_remains_repeatable(self):
        page = CDPPage.__new__(CDPPage)
        page.closed = False
        problem = RuntimeError('artificial listener fault')
        listener = Mock(side_effect=problem)
        page.callbacks = defaultdict(list, close=[listener], request=[Mock()])
        with self.assertRaises(RuntimeError) as caught:
            page._did_close()
        self.assertIs(caught.exception, problem)
        self.assertTrue(page.closed)
        self.assertFalse(page.callbacks)
        page._did_close()
        listener.assert_called_once_with()

    def test_taskkill_failure_is_visible_and_keeps_live_profile(self):
        browser = self.browser()
        problem = OSError('artificial owned termination fault')
        browser.process.poll.return_value = None
        browser.process.wait.side_effect = subprocess.TimeoutExpired('owned', 5)
        page = Mock()
        browser.contexts = [SimpleNamespace(_pages={'page': page})]
        with patch('vibe_job_radar.guided.cdp_browser.sys.platform', 'win32'), \
                patch('vibe_job_radar.guided.cdp_browser.subprocess.CREATE_NO_WINDOW', 0, create=True), \
                patch('vibe_job_radar.guided.cdp_browser.subprocess.run', side_effect=problem) as command:
            with self.assertRaises(OSError) as caught:
                browser.close()
        self.assertIs(caught.exception, problem)
        command.assert_called_once()
        self.assertEqual(command.call_args.args[0], ['taskkill', '/PID', '12345', '/T', '/F'])
        self.assertTrue(browser.cleanup_failed)
        self.assertTrue(browser.profile.exists())
        self.assertTrue(browser.process.stdout.closed)
        page._did_close.assert_called_once_with()

    def test_modified_profile_target_is_never_removed(self):
        browser = self.browser()
        browser.profile = browser.profile.parent/'user-profile'
        browser.profile.mkdir()
        marker = browser.profile/'history'
        marker.write_bytes(b'untouched')
        with self.assertRaisesRegex(Exception, 'native_protocol_error'):
            browser.close()
        self.assertEqual(marker.read_bytes(), b'untouched')


if __name__ == '__main__':
    unittest.main()
