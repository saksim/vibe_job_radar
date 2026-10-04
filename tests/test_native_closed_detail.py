"""A closed job is a terminal publisher result, not an unfinished page."""
import unittest
from unittest.mock import Mock, patch

from vibe_job_radar.guided.adapters import builtins
from vibe_job_radar.guided.contracts import CrawlError, PageSnapshot, PageSnapshotChanged
from vibe_job_radar.guided.native_browser import NativeBackend
from test_liepin_detail_pipeline import BODY, URL, page_markup


class NativeClosedDetailTests(unittest.TestCase):
    def setUp(self):
        self.backend = NativeBackend.__new__(NativeBackend)
        self.backend._check_error = Mock()
        self.backend.page = Mock()
        self.backend.page.url = URL
        self.backend.page.locator.return_value.count.return_value = 1
        self.backend.page.locator.return_value.inner_text.return_value = '岗位详情'
        self.backend.adapter = builtins().get('liepin')
        self.backend.auth_mode = False
        self.backend.observations = Mock(return_value=())
        self.closed = PageSnapshot(URL, '<h1>人工已停止职位</h1><p>该职位已暂停招聘</p>'
            '<aside><a href="https://www.liepin.com/job/456.shtml">其他推荐架构师</a></aside>',
            business_required=True)

    def test_closed_job_keeps_terminal_reason_without_waiting_or_using_recommendations(self):
        self.backend.snapshot = Mock(return_value=self.closed)
        with patch('vibe_job_radar.guided.native_browser.time.monotonic', side_effect=[100, 100, 116]):
            with self.assertRaisesRegex(CrawlError, '^job_unavailable$'):
                self.backend._settle()
        self.backend.snapshot.assert_called_once()
        self.backend.page.wait_for_timeout.assert_not_called()
        self.backend.page.goto.assert_not_called()

    def test_replaced_snapshot_is_reobserved_then_closed_reason_is_preserved(self):
        self.backend.snapshot = Mock(side_effect=[PageSnapshotChanged(), self.closed])
        with patch('vibe_job_radar.guided.native_browser.time.monotonic', side_effect=[100, 100, 101, 116]):
            with self.assertRaisesRegex(CrawlError, '^job_unavailable$'):
                self.backend._settle()
        self.assertEqual(self.backend.snapshot.call_count, 2)
        self.backend.page.wait_for_timeout.assert_called_once_with(100)
        self.backend.page.goto.assert_not_called()

    def test_incomplete_detail_still_waits_for_the_original_full_description(self):
        pending = PageSnapshot(URL, '<h1>人工岗位</h1><p>加载中</p>', business_required=True)
        complete = PageSnapshot(URL, page_markup(), business_required=True)
        self.backend.snapshot = Mock(side_effect=[pending, complete])
        with patch('vibe_job_radar.guided.native_browser.time.monotonic', side_effect=[100, 100, 101, 116]):
            self.backend._settle()
        self.assertEqual(self.backend.adapter.detail(complete)['text'], BODY)
        self.backend.page.wait_for_timeout.assert_called_once_with(100)
        self.backend.page.goto.assert_not_called()


if __name__ == '__main__':
    unittest.main()
