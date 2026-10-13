"""The browser's own address-bar view must not cancel a job at startup."""
from unittest import TestCase
from unittest.mock import Mock

from vibe_job_radar.guided.native_browser import NativeBackend
import test_native_acquisition as fixture


class BrowserUITests(TestCase):
    def setUp(self):
        helper = fixture.NativeControllerTests()
        helper.setUp()
        self.addCleanup(helper.doCleanups)
        self.b = helper.b
        self.b._context_id = 'collection'
        self.info = {'type': 'other', 'targetId': 'browser-ui',
                     'url': 'chrome://omnibox-popup.top-chrome/',
                     'browserContextId': 'collection'}

    def test_known_ui_detaches_without_cancelling_or_installing_controls(self):
        self.b._install_target = Mock()
        self.b._attached({'sessionId': 'ui-session', 'targetInfo': self.info})
        self.b._cdp.send.assert_called_once_with(
            'Target.detachFromTarget', {'sessionId': 'ui-session'})
        self.b._install_target.assert_not_called()
        self.b._send.assert_not_called()
        self.assertFalse(self.b.cancelled.is_set())
        self.assertIsNone(self.b.error)
        self.assertNotIn('ui-session', self.b._sessions)

    def test_page_with_same_internal_url_is_not_a_browser_ui_exception(self):
        self.assertFalse(NativeBackend._browser_chrome_ui({**self.info, 'type': 'page'}))

    def test_worker_with_same_url_is_not_a_browser_ui_exception(self):
        self.assertFalse(NativeBackend._browser_chrome_ui({**self.info, 'type': 'worker'}))

    def test_popup_with_opener_is_not_a_browser_ui_exception(self):
        self.assertFalse(NativeBackend._browser_chrome_ui({**self.info, 'openerId': 'web-page'}))

    def test_network_and_lookalike_urls_are_never_browser_ui(self):
        for url in ('https://omnibox-popup.top-chrome/',
                    'chrome://omnibox-popup.top-chrome.attacker.invalid/',
                    'chrome://omnibox-popup.top-chrome@attacker.invalid/',
                    'chrome://settings/', 'chrome://omnibox-popup.top-chrome/?debug',
                    'chrome://omnibox-popup.top-chrome/extra', '', None):
            with self.subTest(url=url):
                self.assertFalse(NativeBackend._browser_chrome_ui({**self.info, 'url': url}))

    def test_unknown_internal_target_keeps_rejection(self):
        self.b._cdp.send.return_value = {'sessionId': 'quarantine'}
        self.b._attached({'sessionId': 'other', 'targetInfo': {
            **self.info, 'url': 'chrome://unreviewed/'}})
        self.assertTrue(self.b.cancelled.is_set())
        self.assertEqual(self.b.error, 'native_surface_unsupported')
        self.b._send.assert_called_once_with('quarantine', 'Fetch.enable', {
            'patterns': [{'urlPattern': '*', 'requestStage': 'Request'}],
            'handleAuthRequests': True})

    def test_browser_ui_detach_failure_does_not_continue(self):
        self.b._cdp.send.side_effect = RuntimeError('fixture detach error')
        with self.assertRaises(RuntimeError):
            self.b._attached({'sessionId': 'ui', 'targetInfo': self.info})
        self.b._send.assert_not_called()

    def test_closing_backend_does_not_touch_ui(self):
        self.b._closing = True
        self.b._attached({'sessionId': 'ui', 'targetInfo': self.info})
        self.b._cdp.send.assert_not_called()

    def test_exact_browser_ui_type_and_observed_aim_document(self):
        for url in ('chrome://omnibox-popup.top-chrome/',
                    'chrome://omnibox-popup.top-chrome/omnibox_popup_aim.html'):
            with self.subTest(url=url):
                self.assertTrue(NativeBackend._browser_chrome_ui(
                    {**self.info, 'type': 'browser_ui', 'url': url}))

    def test_browser_ui_type_does_not_accept_arbitrary_documents(self):
        for url in ('https://omnibox-popup.top-chrome/',
                    'chrome://omnibox-popup.top-chrome/unreviewed.html',
                    'chrome://omnibox-popup.top-chrome/omnibox_popup_aim.html?token=x'):
            with self.subTest(url=url):
                self.assertFalse(NativeBackend._browser_chrome_ui(
                    {**self.info, 'type': 'browser_ui', 'url': url}))
