"""Explicit installed-browser control selection; no fallback or live requests."""
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from vibe_job_radar.guided.native_browser import NativeBackend


class NativeControlChoiceTests(unittest.TestCase):
    def setUp(self):
        self.backend = NativeBackend.__new__(NativeBackend)
        self.backend.runtime = SimpleNamespace(chromium=Mock())

    def test_explicit_edge_uses_owned_cdp_without_changing_network_or_identity_options(self):
        options = {'channel': 'msedge', 'headless': False, 'timeout': 30000,
            'args': ['--disable-quic', '--block-new-web-contents'],
            'proxy': {'server': 'http://127.0.0.1:1234'}}
        with patch('vibe_job_radar.guided.cdp_browser.edge_executable', return_value='installed-edge'), \
                patch('vibe_job_radar.guided.cdp_browser.CDPBrowser') as controller:
            self.assertIs(self.backend._launch_browser(options), controller.return_value)
        controller.assert_called_once_with(executable_path='installed-edge',
            **{k: v for k, v in options.items() if k != 'channel'})
        self.backend.runtime.chromium.launch.assert_not_called()
        self.assertEqual(options['channel'], 'msedge')

    def test_missing_selected_edge_does_not_launch_another_browser(self):
        with patch('vibe_job_radar.guided.cdp_browser.edge_executable', side_effect=FileNotFoundError), \
                patch('vibe_job_radar.guided.cdp_browser.CDPBrowser') as controller:
            with self.assertRaises(FileNotFoundError):
                self.backend._launch_browser({'channel': 'msedge'})
        controller.assert_not_called()
        self.backend.runtime.chromium.launch.assert_not_called()

    def test_bundled_and_http_bridge_keep_their_existing_launch_path(self):
        from vibe_job_radar.guided.browser import PlaywrightBackend
        options = {'headless': True, 'args': []}
        self.backend._launch_browser(options)
        self.backend.runtime.chromium.launch.assert_called_once_with(**options)
        self.backend.runtime.chromium.launch.reset_mock()
        PlaywrightBackend._launch_browser(self.backend, {'channel': 'msedge'})
        self.backend.runtime.chromium.launch.assert_called_once_with(channel='msedge')


if __name__ == '__main__':
    unittest.main()
