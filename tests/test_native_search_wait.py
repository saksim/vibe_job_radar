"""Native API-only list waits do not repeatedly copy an unusable DOM."""
from dataclasses import replace
import threading
import unittest
from unittest.mock import Mock, patch

from vibe_job_radar.guided.contracts import CrawlError, PageSnapshot
from vibe_job_radar.guided.native_browser import NativeBackend
from test_liepin_native_search import ADAPTER, JOB, SEARCH, payload, snapshot


class NativeSearchWaitTests(unittest.TestCase):
    def backend(self):
        b = object.__new__(NativeBackend)
        b.adapter = ADAPTER
        b.page = Mock(url=SEARCH)
        b.page.locator.return_value.count.return_value = 1
        b.page.locator.return_value.inner_text.return_value = '等待当前搜索结果'
        b.auth_mode = False
        b.error = b.wait_error = None
        b.cancelled = threading.Event()
        b._drain_rejected_pages = Mock()
        b.observations = Mock(return_value=())
        b.snapshot = Mock(side_effect=AssertionError('unusable list DOM copied'))
        self.clock = 0.0
        b.page.wait_for_timeout.side_effect = lambda ms: setattr(self, 'clock', self.clock + ms / 1000)
        return b

    def settle(self, b):
        with patch('vibe_job_radar.guided.native_browser.time.monotonic', side_effect=lambda: self.clock):
            b._settle(search=True)

    def test_missing_or_entry_only_response_keeps_deadline_without_copying_stale_dom(self):
        default = replace(snapshot().business[0], context={'entry_bootstrap': True})
        for observations in ((), (default,)):
            with self.subTest(entry_only=bool(observations)):
                b = self.backend()
                b.observations.return_value = observations
                with self.assertRaisesRegex(CrawlError, '^page_not_ready$'):
                    self.settle(b)
                self.assertGreaterEqual(self.clock, 15)
                self.assertLess(self.clock, 15.2)
                b.snapshot.assert_not_called()
                b.page.goto.assert_not_called()

    def test_current_response_including_empty_finishes_without_waiting_full_deadline(self):
        for data in (payload(), payload([])):
            with self.subTest(empty=not data['data']['data']['jobCardList']):
                b = self.backend()
                b.observations.side_effect = lambda: snapshot(data).business if self.clock >= .2 else ()
                self.settle(b)
                self.assertAlmostEqual(self.clock, .2)
                b.snapshot.assert_not_called()

    def test_challenge_and_refusal_still_stop_before_business_response(self):
        for failure in ('manual_required', 'http_403', 'paused'):
            with self.subTest(failure=failure):
                b = self.backend()
                if failure == 'manual_required':
                    b.page.locator.return_value.inner_text.return_value = '请完成安全验证'
                elif failure == 'paused':
                    b.cancelled.set()
                else:
                    b.error = failure
                with self.assertRaisesRegex(CrawlError, '^' + failure + '$'):
                    self.settle(b)
                self.assertEqual(self.clock, 0)
                b.snapshot.assert_not_called()

    def test_detail_and_other_sites_keep_dom_readiness(self):
        for site, url in (('liepin', JOB), ('boss', 'https://www.zhipin.com/web/geek/job')):
            with self.subTest(site=site):
                b = self.backend()
                b.page.url = url
                b.adapter = Mock(key=site, search_base=ADAPTER.search_base)
                b.adapter.accept_url.return_value = url
                b.adapter.challenged.return_value = False
                b.adapter.native_ready.return_value = False
                b.adapter.cards.side_effect = CrawlError('not_job_list') if site == 'liepin' else None
                b.adapter.cards.return_value = [object()]
                b.snapshot.side_effect = None
                b.snapshot.return_value = PageSnapshot(url, '<h1>完整职位</h1>')
                self.settle(b)
                b.snapshot.assert_called_once()
                if site == 'liepin':
                    b.adapter.detail.assert_called_once_with(b.snapshot.return_value)
                self.assertEqual(self.clock, 0)


if __name__ == '__main__':
    unittest.main()
