"""A single visible publisher form submission, including refusal and stale data."""
from collections import deque
from dataclasses import replace
import unittest
from unittest.mock import Mock, patch

from vibe_job_radar.guided.contracts import CrawlError, PageSnapshot
from vibe_job_radar.guided.liepin_form import submit_search
from vibe_job_radar.guided.native_browser import NativeBackend
from vibe_job_radar.guided.rate import RateLimit
from vibe_job_radar.guided.read_retry import TransientReadFailure, action_for
from test_liepin_native_search import ADAPTER, SEARCH, JOB, snapshot


class SearchFormTests(unittest.TestCase):
    def setUp(self):
        self.backend = NativeBackend.__new__(NativeBackend)
        b = self.backend
        b.adapter = ADAPTER
        b.page = Mock(url=ADAPTER.search_base, is_closed=Mock(return_value=False))
        self.field, self.body = Mock(), Mock()
        self.field.count.return_value = 1
        self.body.inner_text.return_value = '合成搜索入口'
        b.page.locator.side_effect = lambda selector: self.body if selector=='body' else self.field
        b._check_error = Mock()
        b.ensure_page_access = Mock()
        b.wire = Mock()
        b._epoch, b._observed_bytes = 1, 50
        b._observations = deque([object()])
        b._latest_business = {'liepin_search':1}
        b._pagination_page = None
        b._settle = Mock()
        b.snapshot = Mock(return_value=replace(snapshot(), business_required=True))
        self.field.press.side_effect = lambda *_, **__: setattr(b.page, 'url', SEARCH)

    def test_one_normal_input_returns_only_matching_response_and_never_constructs_http(self):
        b = self.backend
        page = submit_search(b, '时间序列')
        self.assertEqual(ADAPTER.cards(page)[0].url, JOB)
        self.field.click.assert_called_once_with(timeout=5000)
        self.field.fill.assert_called_once_with('时间序列', timeout=5000)
        self.field.press.assert_called_once_with('Enter', timeout=90000)
        b._settle.assert_called_once_with(search=True)
        b.wire.reserve.assert_called_once_with('page')
        b.wire.fetch.assert_not_called()
        b.page.goto.assert_not_called()
        self.assertFalse(b._observations)
        self.assertIsNone(b._pagination_page)

    def test_duplicate_field_and_publisher_challenge_never_receive_input(self):
        self.field.count.return_value = 2
        with self.assertRaisesRegex(CrawlError, 'search_form_changed'):
            submit_search(self.backend, '时间序列')
        self.field.fill.assert_not_called()
        self.field.count.return_value = 1
        self.body.inner_text.return_value = '请完成安全验证'
        with self.assertRaisesRegex(CrawlError, 'manual_required'):
            submit_search(self.backend, '时间序列')
        self.field.click.assert_not_called()
        self.backend.wire.reserve.assert_not_called()

    def test_changed_page_before_or_during_input_is_not_submitted(self):
        self.field.click.side_effect = lambda **_: setattr(self.backend.page, 'url', ADAPTER.search_url('其他'))
        with self.assertRaisesRegex(CrawlError, 'search_scope_changed'):
            submit_search(self.backend, '时间序列')
        self.field.fill.assert_not_called()
        self.field.press.assert_not_called()

    def test_cancel_after_fill_prevents_submit_and_keeps_prior_observations(self):
        self.field.fill.side_effect = lambda *_, **__: setattr(self.backend._check_error, 'side_effect', CrawlError('paused'))
        with self.assertRaisesRegex(CrawlError, 'paused'):
            submit_search(self.backend, '时间序列')
        self.field.press.assert_not_called()
        self.backend.wire.reserve.assert_not_called()
        self.assertTrue(self.backend._observations)

    def test_quota_denial_does_not_invalidate_prior_results_or_submit(self):
        self.backend.wire.reserve.side_effect = RateLimit(30)
        with self.assertRaises(RateLimit):
            submit_search(self.backend, '时间序列')
        self.field.press.assert_not_called()
        self.assertTrue(self.backend._observations)
        self.assertIsNone(self.backend._pagination_page)

    def test_response_timeout_never_resubmits_or_leaks_input_values(self):
        self.backend._settle.side_effect = RuntimeError('private synthetic input must not escape')
        with self.assertRaisesRegex(CrawlError, '^search_form_changed$'):
            submit_search(self.backend, '时间序列')
        self.field.press.assert_called_once()
        self.assertIsNone(self.backend._pagination_page)

    def test_missing_current_response_is_not_replaced_by_dom_or_initial_recommendations(self):
        self.backend.snapshot.return_value = PageSnapshot(SEARCH, '<a href="'+JOB+'">旧推荐</a>', business_required=True)
        with self.assertRaisesRegex(CrawlError, 'page_not_ready'):
            submit_search(self.backend, '时间序列')
        self.field.press.assert_called_once()
        self.assertIsNone(self.backend._pagination_page)

    def test_backend_error_during_field_wait_retains_its_original_reason(self):
        def failed_wait(**_):
            self.backend._check_error.side_effect = CrawlError('resource_domain_blocked')
            raise RuntimeError('locator wait failed')
        self.field.wait_for.side_effect = failed_wait
        with self.assertRaisesRegex(CrawlError, 'resource_domain_blocked'):
            submit_search(self.backend, '时间序列')
        self.field.fill.assert_not_called()

    def test_login_entry_keeps_the_form_reachable_without_submitting_search_first(self):
        self.backend.open = Mock()
        with patch('vibe_job_radar.guided.liepin_form.submit_search', return_value=snapshot()) as submit:
            page=self.backend.open_search(SEARCH, keyword='时间序列', authentication=True)
        self.backend.open.assert_called_once_with(ADAPTER.search_base, authentication=True)
        self.assertIs(page,self.backend.open.return_value)
        submit.assert_not_called()

    def test_default_search_opens_entry_then_submits_the_keyword_once(self):
        self.backend.open = Mock()
        with patch('vibe_job_radar.guided.liepin_form.submit_search', return_value=snapshot()) as submit:
            self.backend.open_search(SEARCH, keyword='时间序列')
        self.backend.open.assert_called_once_with(ADAPTER.search_base, authentication=False)
        submit.assert_called_once_with(self.backend,'时间序列')

    def test_explicit_filters_are_not_silently_removed_to_fit_keyword_form(self):
        self.backend.open = Mock()
        with patch('vibe_job_radar.guided.liepin_form.submit_search') as submit:
            self.backend.open_search(SEARCH+'&city=010', keyword='时间序列')
        self.backend.open.assert_called_once_with(SEARCH+'&city=010', authentication=False)
        submit.assert_not_called()

    def test_read_retry_alias_is_only_the_default_native_entry_before_collection(self):
        state = dict(search_url=SEARCH, keyword='时间序列', phase='search')
        failure = TransientReadFailure(ADAPTER.search_base, 503)
        entry = self.backend.search_entry_url(SEARCH, keyword=state['keyword'])
        self.assertEqual(action_for(state, failure, search_entry=entry), 'search')
        for url in (SEARCH+'&city=010', ADAPTER.search_url('其他')):
            with self.subTest(url=url), self.assertRaisesRegex(CrawlError, 'read_retry_unavailable'):
                action_for(dict(state, search_url=url), failure,
                    search_entry=self.backend.search_entry_url(url, keyword=state['keyword']))
        for denied in (ADAPTER.search_base+'?key=其他', JOB, ADAPTER.search_base+'redirect'):
            with self.subTest(url=denied), self.assertRaisesRegex(CrawlError, 'read_retry_unavailable'):
                action_for(state, TransientReadFailure(denied, 503), search_entry=entry)
        with self.assertRaisesRegex(CrawlError, 'read_retry_unavailable'):
            action_for(dict(state, phase='collect', selection=[], cards=[]), failure, search_entry=entry)
        with self.assertRaisesRegex(CrawlError, 'read_retry_unavailable'):
            action_for(state, failure)

    def test_entry_failure_never_reaches_form_and_retains_retry_classification(self):
        failure = TransientReadFailure(ADAPTER.search_base, 503, '30')
        self.backend.open = Mock(side_effect=failure)
        with patch('vibe_job_radar.guided.liepin_form.submit_search') as submit:
            with self.assertRaises(TransientReadFailure) as caught:
                self.backend.open_search(SEARCH, keyword='时间序列')
        self.assertIs(caught.exception, failure)
        submit.assert_not_called()


if __name__ == '__main__':
    unittest.main()
