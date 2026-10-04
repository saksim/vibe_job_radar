"""Reobserve a replaced DOM inside the original readiness budget, without input."""
import unittest
from unittest.mock import Mock, patch

from vibe_job_radar.guided.contracts import CrawlError, PageSnapshotChanged
from vibe_job_radar.guided.native_browser import NativeBackend


class NativeReadinessNavigationTests(unittest.TestCase):
    def setUp(self):
        self.backend = NativeBackend.__new__(NativeBackend)
        self.backend._check_error = Mock()
        self.backend.page = Mock()
        self.backend.adapter = Mock()
        self.backend.adapter.challenged.return_value = False
        self.backend.auth_mode = True
        self.body = self.backend.page.locator.return_value
        self.body.count.return_value = 1
        self.body.inner_text.return_value = '当前登录页'

    def test_navigation_during_count_reobserves_current_document(self):
        self.body.count.side_effect = [PageSnapshotChanged(), 1]
        self.backend._settle()
        self.body.inner_text.assert_called_once()
        self.backend.page.wait_for_timeout.assert_called_once_with(100)
        self.backend.page.goto.assert_not_called()

    def test_navigation_during_text_read_does_not_accept_stale_text(self):
        self.body.inner_text.side_effect = [PageSnapshotChanged(), '当前登录页']
        self.backend._settle()
        self.backend.adapter.challenged.assert_called_once_with(
            '当前登录页', self.backend.page.url)
        self.backend.page.goto.assert_not_called()

    def test_navigation_during_search_snapshot_is_read_again(self):
        self.backend.auth_mode = False
        self.backend.adapter.native_ready.return_value = False
        self.backend.observations = Mock(return_value=())
        current = object()
        self.backend.snapshot = Mock(side_effect=[PageSnapshotChanged(), current])
        self.backend.adapter.cards.return_value = ['current card']
        self.backend._settle(search=True)
        self.backend.adapter.cards.assert_called_once_with(current)
        self.backend.page.goto.assert_not_called()

    def test_repeated_navigation_cannot_restart_original_deadline(self):
        self.body.count.side_effect = PageSnapshotChanged()
        with patch('vibe_job_radar.guided.native_browser.time.monotonic',
                   side_effect=[100, 100, 114.9, 115]):
            with self.assertRaisesRegex(CrawlError, 'page_not_ready'):
                self.backend._settle()
        self.assertEqual(self.body.count.call_count, 2)
        self.assertEqual(self.backend.page.wait_for_timeout.call_count, 2)
        self.body.inner_text.assert_not_called()
        self.backend.page.goto.assert_not_called()

    def test_cancellation_after_navigation_prevents_another_dom_read(self):
        self.body.count.side_effect = [PageSnapshotChanged(), 1]
        self.backend._check_error.side_effect = [None, CrawlError('paused')]
        with self.assertRaisesRegex(CrawlError, 'paused'):
            self.backend._settle()
        self.body.count.assert_called_once()
        self.body.inner_text.assert_not_called()

    def test_refusal_received_at_deadline_keeps_original_reason(self):
        self.body.count.side_effect = PageSnapshotChanged()
        self.backend._check_error.side_effect = [None, CrawlError('robots_denied')]
        with patch('vibe_job_radar.guided.native_browser.time.monotonic',
                   side_effect=[100, 100, 115]):
            with self.assertRaisesRegex(CrawlError, 'robots_denied'):
                self.backend._settle()
        self.body.count.assert_called_once()
        self.body.inner_text.assert_not_called()

    def test_new_document_challenge_still_requires_user_action(self):
        self.body.inner_text.side_effect = [PageSnapshotChanged(), '完成验证']
        self.backend.adapter.challenged.return_value = True
        with self.assertRaisesRegex(CrawlError, 'manual_required'):
            self.backend._settle()
        self.backend.page.goto.assert_not_called()
        self.backend.page.wait_for_timeout.assert_called_once_with(100)

    def test_unrelated_page_error_is_not_a_transient_snapshot(self):
        self.body.count.side_effect = CrawlError('page_not_ready')
        with self.assertRaisesRegex(CrawlError, 'page_not_ready'):
            self.backend._settle()
        self.backend.page.wait_for_timeout.assert_not_called()
        self.body.inner_text.assert_not_called()
