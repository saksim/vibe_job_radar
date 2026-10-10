"""A single visible publisher form submission, including refusal and stale data."""
from collections import deque
from dataclasses import replace
import unittest
from unittest.mock import Mock, patch

from vibe_job_radar.guided.contracts import CrawlError, PageSnapshot, PageSnapshotChanged
from vibe_job_radar.guided.liepin_form import search_entry_ready, submit_search
from vibe_job_radar.guided.native_browser import NativeBackend
from vibe_job_radar.guided.native_navigation import NativeDocument
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
        b.document_identity = Mock(return_value=('session', id(b.page), 7, NativeDocument('target','main',1,ADAPTER.search_base,ADAPTER.search_base)))
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


    def test_same_url_document_replacement_during_input_never_submits(self):
        from vibe_job_radar.guided.contracts import PageSnapshotChanged
        for operation in ('wait_for','click','fill'):
            with self.subTest(operation=operation):
                self.setUp()
                original=self.backend.document_identity.return_value
                def replace_document(*_, **__):
                    self.backend.document_identity.return_value=(*original[:3],replace(original[-1],sequence=2))
                getattr(self.field,operation).side_effect=replace_document
                with self.assertRaises(PageSnapshotChanged):
                    submit_search(self.backend,'时间序列')
                self.field.press.assert_not_called()
                self.backend.wire.reserve.assert_not_called()
                self.backend.page.goto.assert_not_called()

    def test_replacement_during_budget_reservation_does_not_invalidate_or_submit(self):
        from vibe_job_radar.guided.contracts import PageSnapshotChanged
        original=self.backend.document_identity.return_value
        def replace_document(*_, **__):
            self.backend.document_identity.return_value=(*original[:3],replace(original[-1],sequence=2))
        self.backend.wire.reserve.side_effect=replace_document
        with self.assertRaises(PageSnapshotChanged):
            submit_search(self.backend,'时间序列')
        self.field.fill.assert_called_once()
        self.field.press.assert_not_called()
        self.assertTrue(self.backend._observations)
        self.assertIsNone(self.backend._pagination_page)


    def test_last_body_read_cannot_submit_into_a_replaced_document_or_page(self):
        from vibe_job_radar.guided.contracts import PageSnapshotChanged
        for replacement in ('document','page'):
            with self.subTest(replacement=replacement):
                self.setUp();original=self.backend.document_identity.return_value
                def body_read(**_):
                    if self.backend.wire.reserve.call_count:
                        if replacement=='document':
                            self.backend.document_identity.return_value=(*original[:3],replace(original[-1],sequence=2))
                        else:
                            self.backend.page=Mock(url=ADAPTER.search_base,is_closed=Mock(return_value=False))
                            self.backend.document_identity.return_value=(original[0],id(self.backend.page),*original[2:])
                    return '合成搜索入口'
                self.body.inner_text.side_effect=body_read
                with self.assertRaises(PageSnapshotChanged):
                    submit_search(self.backend,'时间序列')
                self.field.fill.assert_called_once()
                self.backend.wire.reserve.assert_called_once_with('page')
                self.field.press.assert_not_called()
                self.assertTrue(self.backend._observations)


    def prepare_entry(self):
        b = self.backend
        b.contract = Mock()
        b._load_robots = Mock()
        b.observations = Mock(return_value=[])
        self.body.count.return_value = 1
        self.field.is_enabled.return_value = True
        self.field.get_attribute.return_value = None
        entry = PageSnapshot(ADAPTER.search_base,
            '<input type="text" placeholder="搜索职位、公司">', business_required=True)
        b.snapshot.side_effect = lambda: (entry if b.page.url == ADAPTER.search_base
                                         else replace(snapshot(), business_required=True))
        b.page.goto.side_effect = lambda url, **_: setattr(b.page, 'url', url)
        self.clock = [0.]
        b.page.wait_for_timeout.side_effect = lambda milliseconds: self.clock.__setitem__(
            0, self.clock[0]+milliseconds/1000)
        del b._settle
        return b

    def open_entry(self, **kwargs):
        with patch('vibe_job_radar.guided.native_browser.time.monotonic',
                   side_effect=lambda: self.clock[0]):
            return self.backend.open_search(SEARCH, keyword='时间序列', **kwargs)

    def test_normal_query_does_not_wait_for_unrequested_entry_recommendations(self):
        b = self.prepare_entry()
        observed = self.open_entry()
        self.assertEqual([card.url for card in ADAPTER.cards(observed)], [JOB])
        b.page.goto.assert_called_once_with(ADAPTER.search_base, wait_until='domcontentloaded', timeout=90000)
        self.field.fill.assert_called_once_with('时间序列', timeout=5000)
        self.field.press.assert_called_once_with('Enter', timeout=90000)
        b.wire.reserve.assert_called_once_with('page')
        b.wire.fetch.assert_not_called()

    def test_unusable_entry_field_cannot_be_replaced_by_default_recommendations(self):
        from test_liepin_native_search import request, payload, request_context
        from vibe_job_radar.guided.native_browser import BusinessObservation
        context = request_context(ADAPTER, 'liepin_search', request(key=''), ADAPTER.search_base)
        response = BusinessObservation(1, 'liepin_search', 1, payload(), context)
        self.assertTrue(ADAPTER.native_ready((response,)))
        for unusable in ('absent', 'duplicate', 'disabled', 'readonly'):
            with self.subTest(unusable=unusable):
                self.setUp()
                b = self.prepare_entry()
                b.observations.return_value = (response,)
                if unusable in ('absent', 'duplicate'):
                    self.field.count.return_value = 0 if unusable == 'absent' else 2
                elif unusable == 'disabled':
                    self.field.is_enabled.return_value = False
                else:
                    self.field.get_attribute.return_value = ''
                with self.assertRaisesRegex(CrawlError, '^page_not_ready$'):
                    self.open_entry()
                self.assertLess(self.clock[0], 15.2)
                self.field.fill.assert_not_called()
                self.field.press.assert_not_called()
                b.wire.reserve.assert_not_called()
                b.page.goto.assert_called_once()

    def test_late_entry_field_is_observed_within_original_deadline_and_submitted_once(self):
        b = self.prepare_entry()
        self.field.count.side_effect = [0, 0, 1, 1]
        self.assertEqual([card.url for card in ADAPTER.cards(self.open_entry())], [JOB])
        self.assertAlmostEqual(self.clock[0], 0.2)
        self.field.press.assert_called_once()
        b.page.goto.assert_called_once()

    def test_entry_missing_body_does_not_read_text_until_current_body_exists(self):
        self.prepare_entry()
        self.body.count.side_effect = [0, 1, 1]
        self.assertEqual([card.url for card in ADAPTER.cards(self.open_entry())], [JOB])
        self.assertAlmostEqual(self.clock[0], 0.1)
        self.field.press.assert_called_once()

    def test_entry_challenge_prevents_keyword_input(self):
        b = self.prepare_entry()
        self.body.inner_text.return_value = '请完成安全验证'
        with self.assertRaisesRegex(CrawlError, '^manual_required$'):
            self.open_entry()
        self.field.fill.assert_not_called()
        b.wire.reserve.assert_not_called()

    def test_entry_permission_refusal_and_cancel_during_field_read_keep_original_reason(self):
        for code in ('robots_denied', 'paused', 'resource_domain_blocked'):
            with self.subTest(code=code):
                self.setUp()
                b = self.prepare_entry()
                if code == 'robots_denied':
                    b.ensure_page_access.side_effect = CrawlError(code)
                else:
                    def refuse():
                        b._check_error.side_effect = CrawlError(code)
                        return True
                    self.field.is_enabled.side_effect = refuse
                with self.assertRaisesRegex(CrawlError, '^'+code+'$'):
                    self.open_entry()
                self.field.fill.assert_not_called()
                b.wire.reserve.assert_not_called()

    def test_entry_same_url_document_or_page_replacement_invalidates_readiness(self):
        for replacement in ('document', 'page'):
            with self.subTest(replacement=replacement):
                self.setUp()
                b = self.prepare_entry()
                original = b.document_identity.return_value
                def change():
                    if replacement == 'page':
                        b.page = Mock(url=ADAPTER.search_base, is_closed=Mock(return_value=False))
                    b.document_identity.return_value = (
                        original[0], id(b.page), original[2], replace(original[-1], sequence=2))
                    return True
                self.field.is_enabled.side_effect = change
                with self.assertRaises(PageSnapshotChanged):
                    search_entry_ready(b)
                self.field.fill.assert_not_called()
                b.wire.reserve.assert_not_called()

    def test_replaced_body_challenge_is_discarded_before_classification(self):
        b = self.prepare_entry()
        original = b.document_identity.return_value
        def change(**_):
            b.document_identity.return_value = (*original[:3], replace(original[-1], sequence=2))
            return '请完成安全验证'
        self.body.inner_text.side_effect = change
        with self.assertRaises(PageSnapshotChanged):
            search_entry_ready(b)
        self.field.is_enabled.assert_not_called()

    def test_repeated_entry_replacement_does_not_extend_deadline_or_submit(self):
        b = self.prepare_entry()
        def change():
            original = b.document_identity.return_value
            b.document_identity.return_value = (
                *original[:3], replace(original[-1], sequence=original[-1].sequence+1))
            return True
        self.field.is_enabled.side_effect = change
        with self.assertRaisesRegex(CrawlError, '^page_not_ready$'):
            self.open_entry()
        self.assertLess(self.clock[0], 15.2)
        self.field.fill.assert_not_called()
        b.page.goto.assert_called_once()

    def test_ready_form_does_not_satisfy_post_submit_result_wait(self):
        b = self.prepare_entry()
        self.field.press.side_effect = None  # Publisher leaves entry displayed; no response.
        with self.assertRaisesRegex(CrawlError, '^page_not_ready$'):
            self.open_entry()
        self.field.press.assert_called_once()
        b.wire.reserve.assert_called_once_with('page')
        b.page.goto.assert_called_once()

    def test_explicit_filters_still_require_results_even_with_a_visible_field(self):
        b = self.prepare_entry()
        filtered = SEARCH+'&city=010'
        b.snapshot.side_effect = None
        b.snapshot.return_value = PageSnapshot(filtered, '<p>合成搜索框</p>', business_required=True)
        with patch('vibe_job_radar.guided.native_browser.time.monotonic',
                   side_effect=lambda: self.clock[0]):
            with self.assertRaisesRegex(CrawlError, '^page_not_ready$'):
                b.open_search(filtered, keyword='时间序列')
        b.page.goto.assert_called_once_with(filtered, wait_until='domcontentloaded', timeout=90000)
        self.field.fill.assert_not_called()
        b.wire.reserve.assert_not_called()

    def test_late_default_recommendation_cannot_finish_post_submit_wait(self):
        from test_liepin_native_search import request, payload, request_context
        from vibe_job_radar.guided.native_browser import BusinessObservation
        context = request_context(ADAPTER, 'liepin_search', request(key=''), ADAPTER.search_base)
        default = BusinessObservation(2, 'liepin_search', 1, payload(), context)
        for empty in (False, True):
            with self.subTest(empty=empty):
                self.setUp()
                b = self.prepare_entry()
                b.observations.return_value = (default,)
                def observed():
                    if b.page.url == SEARCH and self.clock[0] >= 0.1:
                        return replace(snapshot(payload([]) if empty else payload()), business_required=True)
                    return PageSnapshot(b.page.url, '<p>等待原查询</p>', (default,), business_required=True)
                b.snapshot.side_effect = observed
                b.observations.side_effect = lambda: observed().business
                page = self.open_entry()
                self.assertEqual([card.url for card in ADAPTER.cards(page)], [] if empty else [JOB])
                self.assertAlmostEqual(self.clock[0], 0.1)
                self.field.press.assert_called_once()
                b.page.goto.assert_called_once()
                b.wire.reserve.assert_called_once_with('page')

    def test_login_open_remains_reachable_without_usable_search_field(self):
        b = self.prepare_entry()
        self.field.count.return_value = 0
        observed = self.open_entry(authentication=True)
        self.assertEqual(observed.url, ADAPTER.search_base)
        self.field.fill.assert_not_called()
        b.wire.reserve.assert_not_called()


if __name__ == '__main__':
    unittest.main()
