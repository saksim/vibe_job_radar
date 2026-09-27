"""Keep obtained API data when no pagination action was performed.

All documents and observations are artificial; no network or real accounts.
"""
from collections import deque
from types import SimpleNamespace
import threading
import unittest
from unittest.mock import Mock

from vibe_job_radar.guided.contracts import CrawlError, PageSnapshot
from vibe_job_radar.guided.native_browser import NativeBackend
from vibe_job_radar.guided.rate import RateLimit
from test_liepin_native_search import ADAPTER, SEARCH, snapshot, payload


def backend_with_data(*, empty=False):
    b = NativeBackend.__new__(NativeBackend)
    observed = snapshot(payload([]) if empty else None)
    b.adapter = ADAPTER
    b.page = SimpleNamespace(url=SEARCH)
    b._epoch = 1
    b._observations = deque(observed.business, maxlen=20)
    b._latest_business = {'liepin_search': 1}
    b._observed_bytes = 120
    b._pagination_page = None
    b.auth_mode = False
    b.error = b.wait_error = None
    b.redirects = 0
    b.cancelled = threading.Event()
    b._check_error = Mock()
    b._visible = Mock(return_value=None)
    b._settle = Mock()
    b.wire = Mock()
    return b


def current_cards(b):
    return ADAPTER.cards(PageSnapshot(SEARCH, '<h1>人工无链接列表</h1>', b.observations()))


class ObservationRetentionTests(unittest.TestCase):
    def test_absent_next_does_not_erase_returned_candidates(self):
        b = backend_with_data()
        old = b.observations()
        self.assertEqual(len(current_cards(b)), 1)
        self.assertFalse(b.next_page())
        self.assertEqual(b.observations(), old)
        self.assertEqual(len(current_cards(b)), 1)
        self.assertEqual((b._epoch, b._observed_bytes), (1, 120))
        self.assertEqual(b._latest_business, {'liepin_search': 1})
        b.wire.reserve.assert_not_called()

    def test_disabled_next_preserves_candidates_without_click(self):
        for attributes in ({'aria-disabled': 'true'}, {'class': 'btn disabled'}):
            with self.subTest(attributes=attributes):
                b = backend_with_data()
                button = Mock()
                button.get_attribute.side_effect = attributes.get
                b._visible.return_value = button
                self.assertFalse(b.next_page())
                self.assertEqual(len(current_cards(b)), 1)
                button.click.assert_not_called()
                b.wire.reserve.assert_not_called()

    def test_confirmed_empty_remains_explicitly_empty_after_no_next(self):
        b = backend_with_data(empty=True)
        self.assertFalse(b.next_page())
        page = PageSnapshot(SEARCH, '<h1>人工零结果</h1>', b.observations())
        self.assertTrue(ADAPTER.confirmed_empty(page))
        self.assertEqual(ADAPTER.cards(page), [])

    def test_repeated_read_only_probe_never_discards_api_observation(self):
        b = backend_with_data()
        for _ in range(3):
            self.assertFalse(b.next_page())
            self.assertEqual(len(current_cards(b)), 1)
        self.assertEqual(b._epoch, 1)
        b._settle.assert_not_called()

    def test_robots_refusal_does_not_clear_current_data_or_click(self):
        b = backend_with_data()
        button = Mock()
        button.get_attribute.return_value = None
        b._visible.return_value = button
        b.wire.ensure_robots.side_effect = CrawlError('robots_denied')
        with self.assertRaisesRegex(CrawlError, 'robots_denied'):
            b.next_page()
        self.assertEqual(len(current_cards(b)), 1)
        button.click.assert_not_called()
        b.wire.reserve.assert_not_called()

    def test_quota_refusal_does_not_clear_current_data_or_click(self):
        b = backend_with_data()
        button = Mock()
        button.get_attribute.return_value = None
        b._visible.return_value = button
        b.wire.reserve.side_effect = RateLimit(30)
        with self.assertRaises(RateLimit):
            b.next_page()
        self.assertEqual(len(current_cards(b)), 1)
        button.click.assert_not_called()

    def test_actual_click_invalidates_before_any_new_response(self):
        b = backend_with_data()
        button = Mock()
        button.get_attribute.return_value = None
        b._visible.return_value = button
        def click(**kwargs):
            self.assertEqual(b._epoch, 2)
            self.assertEqual(b.observations(), ())
            self.assertEqual(b._latest_business, {})
            self.assertEqual(b._observed_bytes, 0)
            self.assertIs(b._pagination_page, b.page)
        button.click.side_effect = click
        self.assertTrue(b.next_page())
        b.wire.reserve.assert_called_once_with('page')
        button.click.assert_called_once_with(timeout=90000)
        b._settle.assert_called_once()
        self.assertIsNone(b._pagination_page)

    def test_failed_click_does_not_resurrect_previous_page_data(self):
        b = backend_with_data()
        button = Mock()
        button.get_attribute.return_value = None
        b._visible.return_value = button
        button.click.side_effect = CrawlError('page_not_ready')
        with self.assertRaisesRegex(CrawlError, 'page_not_ready'):
            b.next_page()
        self.assertEqual(b.observations(), ())
        self.assertEqual(b._epoch, 2)
        self.assertIsNone(b._pagination_page)

    def test_cancel_after_reservation_never_clicks_or_discards_data(self):
        b = backend_with_data()
        button = Mock()
        button.get_attribute.return_value = None
        b._visible.return_value = button
        b._check_error.side_effect = [None, CrawlError('paused')]
        with self.assertRaisesRegex(CrawlError, 'paused'):
            b.next_page()
        self.assertEqual(len(current_cards(b)), 1)
        button.click.assert_not_called()
        self.assertIsNone(b._pagination_page)

    def test_refused_action_does_not_fetch_old_or_new_responses(self):
        b = backend_with_data()
        b._send = Mock()
        b._check_error.side_effect = CrawlError('http_429')
        with self.assertRaisesRegex(CrawlError, 'http_429'):
            b.next_page()
        b._send.assert_not_called()
        b.wire.reserve.assert_not_called()

    def test_prior_error_is_not_reset_by_noop_pagination(self):
        b = backend_with_data()
        b._check_error.side_effect = CrawlError('http_403')
        with self.assertRaisesRegex(CrawlError, 'http_403'):
            b.next_page()
        b._visible.assert_not_called()
        self.assertEqual(len(current_cards(b)), 1)


if __name__ == '__main__':
    unittest.main()
