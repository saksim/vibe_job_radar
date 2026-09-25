"""Manual navigation must not mix another keyword/filter into a saved batch."""
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from vibe_job_radar.guided.adapters import builtins, Registry
from vibe_job_radar.guided.contracts import CrawlError
from vibe_job_radar.guided.search_scope import conditions, check_scope
from vibe_job_radar.guided.service import GuidedService
from vibe_job_radar.workspace import Workspace, InputError
from test_guided_batch_identity import Pages, listing, job

ADAPTER = builtins().get('liepin')
SEARCH = ADAPTER.search_url('时间序列')


class SearchScopeTests(unittest.TestCase):
    def state(self):
        return dict(query_scope_version=1, search_url=SEARCH, keyword='时间序列')

    def test_first_observed_defaults_are_frozen_and_order_does_not_matter(self):
        state = self.state()
        self.assertEqual(check_scope(state, ADAPTER, SEARCH+'&city=010&currentPage=0'), '0')
        self.assertEqual(check_scope(state, ADAPTER, 'https://www.liepin.com/zhaopin/?city=010&currentPage=1&key=时间序列'), '1')
        self.assertEqual(state['effective_search']['city'], '010')

    def test_changed_removed_or_added_filters_stop(self):
        for tail in ('', '&city=020', '&city=010&salary=10'):
            state = self.state(); check_scope(state, ADAPTER, SEARCH+'&city=010')
            with self.subTest(tail=tail), self.assertRaises(CrawlError): check_scope(state, ADAPTER, SEARCH+tail)
            self.assertEqual(state['effective_search']['city'], '010')

    def test_explicit_seed_filter_cannot_change_even_on_first_page(self):
        state = self.state(); state['search_url'] += '&city=010'
        with self.assertRaises(CrawlError): check_scope(state, ADAPTER, SEARCH+'&city=020')
        self.assertNotIn('effective_search', state)

    def test_missing_wrong_or_duplicate_keyword_is_rejected(self):
        for url in ('https://www.liepin.com/zhaopin/', ADAPTER.search_url('其他'), SEARCH+'&key=时间序列'):
            with self.subTest(url=url), self.assertRaises(CrawlError): conditions(ADAPTER, url, '时间序列')

    def test_bad_cursors_and_duplicate_filters_are_not_normalized_away(self):
        for tail in ('&currentPage=-1', '&currentPage=1001', '&currentPage=1.0', '&currentPage=00',
                     '&currentPage=', '&city=010&city=010'):
            with self.subTest(tail=tail), self.assertRaises(CrawlError): conditions(ADAPTER, SEARCH+tail, '时间序列')

    def test_observed_tracking_and_entry_marker_do_not_change_filters(self):
        state = self.state(); check_scope(state, ADAPTER, SEARCH+'&init=1&utm_source=first')
        check_scope(state, ADAPTER, SEARCH+'&utm_source=second')
        self.assertEqual(state['effective_search'], {'key':'时间序列'})

    def test_old_task_is_not_silently_migrated(self):
        state = self.state(); state.pop('query_scope_version')
        self.assertIsNone(check_scope(state, ADAPTER, ADAPTER.search_url('其他')))
        self.assertNotIn('effective_search', state)


class SearchScopeServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.service = GuidedService(Workspace(Path(self.tmp.name)), registry=Registry([ADAPTER]))
        self.addCleanup(self.service.close); self.service._submit = Mock()

    def create(self, **extra):
        data = dict(platform='liepin', keyword='时间序列', roles=['time_series'], consent=True,
                    rights_note='Synthetic test', max_pages=5, max_jobs=2)
        ident = self.service.create({**data, **extra})['id']
        return self.service._load(ident)

    def test_incompatible_seed_stops_before_saving_or_submitting_task(self):
        for seed in (ADAPTER.search_url('其他'), job(1), 'https://www.liepin.com/zhaopin/'):
            with self.subTest(seed=seed), self.assertRaises(InputError): self.create(list_url=seed)
        self.service._submit.assert_not_called()
        self.assertEqual(self.service.state()['jobs'], [])

    def test_other_keyword_page_never_adds_cards_to_prior_batch(self):
        state = self.create()
        pages = Pages([listing(job(1)), replace(listing(job(2)), url=ADAPTER.search_url('其他'))])
        with self.assertRaises(CrawlError) as caught: self.service._gather(state, pages, ADAPTER)
        self.assertEqual(caught.exception.code, 'search_scope_changed')
        self.assertEqual([c['url'] for c in state['cards']], [job(1)])
        self.assertEqual(len(state['pages_seen']), 1)

    def test_repeated_explicit_cursor_with_different_cards_stops_before_mixing(self):
        state = self.create()
        pages = Pages([replace(listing(job(i)), url=SEARCH+'&currentPage='+str(cursor))
                       for i,cursor in ((1,0),(2,1),(3,1),(4,2))])
        self.service._gather(state, pages, ADAPTER)
        self.assertEqual(state['list_end'], 'repeated_cursor')
        self.assertEqual(state['cursors_seen'], ['0','1'])
        self.assertEqual([c['url'] for c in state['cards']], [job(1), job(2)])
        self.assertEqual(pages.clicks, 2)

    def test_reloaded_conditions_reject_changed_city_without_overwriting_old_selection(self):
        state = self.create(max_pages=1)
        self.service._gather(state, Pages([replace(listing(job(1)), url=SEARCH+'&city=010')]), ADAPTER)
        state['selection'] = [state['cards'][0]['id']]; self.service._save(state)
        loaded = self.service._load(state['id'])
        with self.assertRaises(CrawlError):
            self.service._gather(loaded, Pages([replace(listing(job(2)), url=SEARCH+'&city=020')]), ADAPTER)
        self.assertEqual(loaded['selection'], state['selection'])
        self.assertEqual(loaded['cards'], state['cards'])
