"""Known DOM task boundaries, using authored offline page snapshots."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from vibe_job_radar.guided.adapters import builtins, Registry
from vibe_job_radar.guided.contracts import CrawlError, PageSnapshot
from vibe_job_radar.guided.search_scope import check_scope
from vibe_job_radar.guided.service import GuidedService
from vibe_job_radar.workspace import Workspace, InputError


class Pages:
    def __init__(self, pages):
        self.pages, self.index = pages, 0

    def snapshot(self):
        return self.pages[self.index]

    def next_page(self):
        if self.index + 1 == len(self.pages):
            return False
        self.index += 1
        return True


class DOMKeywordScopeTests(unittest.TestCase):
    def service(self, site):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        adapter = builtins().get(site)
        service = GuidedService(Workspace(Path(temporary.name)), registry=Registry([adapter]))
        self.addCleanup(service.close)
        service._submit = Mock()
        return service, adapter

    def create(self, service, site, **extra):
        data = dict(platform=site, keyword='time-series', roles=['time_series'], consent=True,
                    rights_note='Authored offline fixture', max_pages=2, max_jobs=2)
        ident = service.create({**data, **extra})['id']
        return service._load(ident)

    def pages(self, adapter, second_keyword):
        paths = ('/job_detail/first.html', '/job_detail/second.html') if adapter.key == 'boss' else (
            '/shanghai/123.html', '/shanghai/456.html')
        return Pages([PageSnapshot(adapter.search_url(keyword), '<a href="'+path+'">Fixture job</a>')
                      for keyword, path in zip(('time-series', second_keyword), paths)])

    def test_changed_keyword_stops_before_persisting_foreign_cards(self):
        for site in ('boss', '51job'):
            with self.subTest(site=site):
                service, adapter = self.service(site)
                state = self.create(service, site)
                self.assertNotIn('query_scope_version', state)
                with self.assertRaises(CrawlError) as caught:
                    service._gather(state, self.pages(adapter, 'another-keyword'), adapter)
                self.assertEqual(caught.exception.code, 'search_scope_changed')
                persisted = service._load(state['id'])
                self.assertEqual(len(persisted['cards']), 1)
                self.assertEqual(len(persisted['pages_seen']), 1)
                self.assertEqual(persisted['cards'], state['cards'])

    def test_same_keyword_keeps_existing_pagination_and_card_collection(self):
        for site in ('boss', '51job'):
            with self.subTest(site=site):
                service, adapter = self.service(site)
                state = self.create(service, site)
                pages = self.pages(adapter, 'time-series')
                pages.pages[1] = PageSnapshot(pages.pages[1].url+'&publisherPage=2', pages.pages[1].html)
                service._gather(state, pages, adapter)
                self.assertEqual(len(service._load(state['id'])['cards']), 2)

    def test_incompatible_seed_stops_before_any_submission_or_task_save(self):
        for site in ('boss', '51job'):
            service, adapter = self.service(site)
            search = adapter.search_url('time-series')
            for seed in (adapter.search_url('different'),
                         search+'&'+adapter.keyword_param+'=time-series'):
                with self.subTest(site=site, seed=seed), self.assertRaises(InputError):
                    self.create(service, site, list_url=seed)
            service._submit.assert_not_called()
            self.assertEqual(service.state()['jobs'], [])

    def test_policy_does_not_borrow_liepin_cursor_semantics_or_mutate_scope(self):
        for site in ('boss', '51job'):
            adapter = builtins().get(site)
            state = dict(search_url=adapter.search_url('time-series'), keyword='time-series')
            before = deepcopy(state)
            self.assertIsNone(check_scope(state, adapter,
                adapter.search_url('time-series')+'&currentPage=publisher-specific&init=opaque'))
            self.assertEqual(state, before)

    def test_old_known_keyword_task_is_protected_without_rebinding(self):
        for site in ('boss', '51job'):
            adapter = builtins().get(site)
            state = dict(search_url=adapter.search_url('time-series'), keyword='time-series',
                         execution_binding={'query': 'unchanged-old-binding'}, selection=['prior-selection'])
            before = deepcopy(state)
            for observed in (adapter.search_url('different'), adapter.search_base,
                             adapter.search_base+'/unrelated?'+adapter.keyword_param+'=time-series',
                             adapter.search_url('time-series').replace('https://', 'https://other.')):
                with self.subTest(site=site, observed=observed), self.assertRaises(CrawlError) as caught:
                    check_scope(state, adapter, observed)
                self.assertEqual(caught.exception.code, 'search_scope_changed')
                self.assertEqual(state, before)

    def test_explicit_manual_list_and_bare_entry_keep_existing_input_contract(self):
        for site in ('boss', '51job'):
            adapter = builtins().get(site)
            origin = adapter.search_base.split('/', 3)[:3]
            for seed in ('/'.join(origin)+'/authored-manual-list', adapter.search_base):
                with self.subTest(site=site, seed=seed):
                    service, adapter = self.service(site)
                    state = self.create(service, site, list_url=seed)
                    binding = deepcopy(state['execution_binding'])
                    pages = self.pages(adapter, 'time-series')
                    pages.pages = [PageSnapshot(seed+('?publisherPage='+str(index)), page.html)
                                   for index, page in enumerate(pages.pages)]
                    service._gather(state, pages, adapter)
                    persisted = service._load(state['id'])
                    self.assertEqual(len(persisted['cards']), 2)
                    self.assertEqual(persisted['execution_binding'], binding)
                    self.assertNotIn('query_scope_version', persisted)

    def test_51job_exact_and_legacy_detail_paths_stay_accepted(self):
        adapter = builtins().get('51job')
        for url in ('https://we.51job.com/pc/jobdetail?jobId=123',
                    'https://jobs.51job.com/shanghai/123.html'):
            self.assertEqual(adapter.accept_url(url, detail=True), url)

    def test_51job_prefix_alias_is_rejected_by_url_cards_and_detail(self):
        adapter = builtins().get('51job')
        url = 'https://we.51job.com/pc/jobdetail-preview?jobId=123'
        html = '<h1>Fixture job</h1><div class="job_msg">岗位职责：数据平台建设。任职要求：熟练掌握Python及数据库设计。</div>'
        for action in (lambda: adapter.accept_url(url, detail=True),
                       lambda: adapter.detail(PageSnapshot(url, html))):
            with self.assertRaises(CrawlError) as caught:
                action()
            self.assertEqual(caught.exception.code, 'not_job_url')
        self.assertEqual(adapter.cards(PageSnapshot(adapter.search_url('time-series'),
            '<a href="'+url+'">Fixture job</a>')), [])
