"""A native check must not call a partial startup, network access or failed cleanup ready."""
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from vibe_job_radar.guided.browser_health import BrowserStartupError
from vibe_job_radar.guided.native_check import check_native_browser, probe_native_browser
from vibe_job_radar.workspace import InputError


class NativeComponentCheckTests(unittest.TestCase):
    def setUp(self):
        self.browser = Mock(version='156.0.0.0'); self.browser.is_connected.return_value = True
        self.tunnel = SimpleNamespace(connections=0, _closed=False, thread=Mock())
        self.tunnel.thread.is_alive.return_value = True
        self.page = Mock()
        self.page.evaluate.side_effect = [{'url':'about:blank','value':42}, 'blocked']
        self.backend = SimpleNamespace(browser=self.browser, tunnel=self.tunnel, page=self.page,
            context=SimpleNamespace(pages=[self.page]), _bound_pages={self.page:'session'},
            _requests={}, _observations=[], _halted=True, error='resource_domain_blocked',
            native_counts={k:0 for k in ('document','business','asset','robots','login','responses')},
            startup_report={'launch_tested':True})
        self.backend.native_counts['blocked'] = 1
        def close():
            self.browser.is_connected.return_value = False
            self.tunnel._closed = True
            self.tunnel.thread.is_alive.return_value = False
        self.backend.close = Mock(side_effect=close)
        p = patch('vibe_job_radar.guided.native_check.NativeBackend', return_value=self.backend)
        self.factory = p.start(); self.addCleanup(p.stop)
        self.browser.profile.exists.return_value = False
        self.browser.cleanup_failed = False
        self.browser.process.poll.return_value = 0
        def factory(*args, **kwargs):
            self.browser.minimal_events = kwargs.get('channel') == 'chrome'
            return self.backend
        self.factory.side_effect = factory

    def test_selected_channel_uses_no_request_rule_and_proves_guard_and_cleanup(self):
        r = check_native_browser(channel='chrome', headless=False)
        self.assertTrue(r['success']); self.assertFalse(r['live_sites_certified'])
        self.assertEqual(r['external_connections'], 0)
        self.assertTrue(r['blank_page_check'] and r['request_guard_check'] and r['cleanup_verified'])
        self.assertEqual(self.factory.call_args.kwargs, {'headless':False,'channel':'chrome'})
        contract = self.factory.call_args.args[0].native_contract
        self.assertEqual(contract.hosts, ('native-check.invalid',)); self.assertEqual(contract.rules, ())
        self.backend.close.assert_called_once()

    def test_explicit_bundled_uses_default_channel_without_install_or_fallback(self):
        self.assertTrue(check_native_browser(channel='bundled')['success'])
        self.assertEqual(self.factory.call_args.kwargs, {'headless':True})
        self.factory.assert_called_once()

    def test_a_still_connected_browser_cannot_be_called_ready(self):
        self.backend.close.side_effect = lambda: None
        r = check_native_browser(channel='chrome')
        self.assertFalse(r['success']); self.assertFalse(r['cleanup_verified'])
        self.assertEqual(r['stage'], 'cleanup')
        self.factory.assert_called_once()

    def test_cleanup_exception_cannot_report_ready_and_is_not_serialized(self):
        self.backend.close.side_effect = OSError('PRIVATE-PATH')
        r = check_native_browser(channel='chrome')
        self.assertFalse(r['success']); self.assertFalse(r['cleanup']['close_returned'])
        self.assertNotIn('PRIVATE-PATH', str(r)); self.factory.assert_called_once()

    def test_network_or_false_guard_proof_fails_even_when_cleanup_succeeds(self):
        self.tunnel.connections = 1
        r = check_native_browser(channel='chrome')
        self.assertFalse(r['success']); self.assertEqual(r['external_connections'], 1)
        self.assertTrue(r['cleanup_verified']); self.assertEqual(r['stage'], 'request_guard')

    def test_startup_failure_keeps_fixed_code_and_launch_facts_without_fallback(self):
        self.factory.side_effect = BrowserStartupError({'code':'browser_channel_missing',
            'launch_tested':False, 'error_summary':'PRIVATE-PATH', 'process_exit_hex':'PRIVATE'})
        r = probe_native_browser(channel='chrome')
        self.assertFalse(r['ready']); self.assertFalse(r['launch_tested'])
        self.assertEqual(r['code'], 'browser_channel_missing')
        self.assertEqual(r['network_backend'], 'native')
        self.assertNotIn('PRIVATE', str(r['native_component']))
        self.factory.assert_called_once()

    def test_native_crash_keeps_exact_fixed_exit_code_for_history(self):
        self.factory.side_effect = BrowserStartupError({'code':'browser_native_heap_corruption',
            'launch_tested':True, 'process_exit_hex':'0xC0000374'})
        r = probe_native_browser(channel='chrome')
        self.assertFalse(r['ready']); self.assertTrue(r['launch_tested'])
        self.assertEqual(r['process_exit_hex'], '0xC0000374'); self.factory.assert_called_once()

    def test_unrecognized_channel_never_launches(self):
        for channel in ('chrome-beta', True, '/private/path', {'channel':'chrome'}):
            with self.subTest(channel=channel), self.assertRaises(InputError):
                check_native_browser(channel=channel)
        self.factory.assert_not_called()


    def test_owned_profile_left_behind_cannot_qualify_direct_chrome(self):
        self.browser.profile.exists.return_value = True
        r=check_native_browser(channel='chrome')
        self.assertFalse(r['success']);self.assertFalse(r['cleanup_verified'])
        self.assertFalse(r['cleanup']['profile_removed'])

    def test_owned_bridge_still_alive_cannot_qualify_direct_chrome(self):
        self.browser.process.poll.return_value = None
        r=check_native_browser(channel='chrome')
        self.assertFalse(r['success']);self.assertFalse(r['cleanup']['bridge_exited'])

    def test_silent_profile_cleanup_error_cannot_qualify_direct_chrome(self):
        self.browser.cleanup_failed=True
        r=check_native_browser(channel='chrome')
        self.assertFalse(r['success']);self.assertFalse(r['cleanup']['profile_cleanup_ok'])

    def test_wrong_controller_cannot_claim_selected_native_chrome(self):
        self.factory.side_effect=None;self.browser.minimal_events=False
        r=check_native_browser(channel='chrome')
        self.assertFalse(r['success']);self.assertFalse(r['minimal_controller'])
        self.page.evaluate.assert_not_called()
