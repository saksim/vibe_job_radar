"""A temporary missing body during navigation must not abort a normal login."""
import unittest
from unittest.mock import Mock, patch

from vibe_job_radar.guided.contracts import CrawlError
from vibe_job_radar.guided.native_browser import NativeBackend


class NativeReadinessTests(unittest.TestCase):
    def setUp(self):
        self.backend = NativeBackend.__new__(NativeBackend)
        self.backend._check_error = Mock()
        self.backend.page = Mock()
        self.backend.adapter = Mock()
        self.backend.adapter.challenged.return_value = False
        self.backend.auth_mode = True

    def test_missing_body_then_login_ready_without_renavigation(self):
        body = self.backend.page.locator.return_value
        body.count.side_effect = [0, 0, 1]
        body.inner_text.return_value = '正常登录表单'
        self.backend._settle()
        self.assertEqual(self.backend.page.wait_for_timeout.call_count, 2)
        body.inner_text.assert_called_once()
        self.backend.page.goto.assert_not_called()

    def test_missing_body_remains_bounded_by_the_original_deadline(self):
        self.backend.page.locator.return_value.count.return_value = 0
        with patch('vibe_job_radar.guided.native_browser.time.monotonic', side_effect=[100, 100, 116]):
            with self.assertRaisesRegex(CrawlError, 'page_not_ready'):
                self.backend._settle()
        self.backend.page.locator.return_value.inner_text.assert_not_called()
        self.backend.page.goto.assert_not_called()

    def test_cancellation_during_missing_body_stops_before_reading_or_retrying(self):
        self.backend.page.locator.return_value.count.return_value = 0
        self.backend._check_error.side_effect = [None, CrawlError('paused')]
        with self.assertRaisesRegex(CrawlError, 'paused'):
            self.backend._settle()
        self.assertEqual(self.backend.page.wait_for_timeout.call_count, 1)
        self.backend.page.locator.return_value.inner_text.assert_not_called()
