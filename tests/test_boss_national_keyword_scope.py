"""Protect the one observed public BOSS national route with authored pages."""
from copy import deepcopy
import unittest

import test_dom_keyword_scope as fixture_scope
from vibe_job_radar.guided.adapters import builtins
from vibe_job_radar.guided.contracts import CrawlError, PageSnapshot
from vibe_job_radar.guided.search_scope import check_scope
from vibe_job_radar.workspace import InputError

NATIONAL = 'https://www.zhipin.com/c100010000/'
SEED = NATIONAL + '?query=Java&industry=&position='


class BossNationalKeywordScopeTests(unittest.TestCase):
    def service(self):
        fixture = fixture_scope.DOMKeywordScopeTests()
        self.addCleanup(fixture.doCleanups)
        service, adapter = fixture.service('boss')
        return fixture, service, adapter

    def test_national_pages_preserve_only_the_tasks_keyword_cards(self):
        for second_keyword in ('Java', 'Python'):
            with self.subTest(second_keyword=second_keyword):
                fixture, service, adapter = self.service()
                state = fixture.create(service, 'boss', keyword='Java', list_url=SEED)
                binding = deepcopy(state['execution_binding'])
                pages = fixture_scope.Pages([
                    PageSnapshot(SEED, '<a href="/job_detail/first.html">First authored job</a>'),
                    PageSnapshot(NATIONAL+'?query='+second_keyword+'&publisherMarker=opaque',
                                 '<a href="/job_detail/second.html">Second authored job</a>'),
                ])
                if second_keyword == 'Java':
                    service._gather(state, pages, adapter)
                    wanted = 2
                else:
                    with self.assertRaises(CrawlError) as caught:
                        service._gather(state, pages, adapter)
                    self.assertEqual(caught.exception.code, 'search_scope_changed')
                    wanted = 1
                persisted = service._load(state['id'])
                self.assertEqual(len(persisted['cards']), wanted)
                self.assertEqual(len(persisted['pages_seen']), wanted)
                self.assertEqual(persisted['execution_binding'], binding)
                self.assertNotIn('query_scope_version', persisted)

    def test_old_national_task_requires_one_keyword_and_its_original_route(self):
        adapter = builtins().get('boss')
        state = dict(search_url=SEED, keyword='Java',
                     execution_binding={'query': 'unchanged-old-binding'}, selection=['prior-selection'])
        before = deepcopy(state)
        changed = (NATIONAL, NATIONAL+'?query=', NATIONAL+'?query=Python',
                   SEED+'&query=Java', adapter.search_url('Java'),
                   'https://www.zhipin.com/c101010100/?query=Java',
                   'https://www.zhipin.com/web/geek/jobs?query=Java',
                   'https://other.zhipin.com/c100010000/?query=Java')
        for observed in changed:
            with self.subTest(observed=observed), self.assertRaises(CrawlError) as caught:
                check_scope(state, adapter, observed)
            self.assertEqual(caught.exception.code, 'search_scope_changed')
            self.assertEqual(state, before)
        self.assertIsNone(check_scope(state, adapter, SEED+'&publisherMarker=opaque'))
        self.assertEqual(state, before)
        original = dict(search_url=adapter.search_url('Java'), keyword='Java')
        with self.assertRaises(CrawlError) as caught:
            check_scope(original, adapter, SEED)
        self.assertEqual(caught.exception.code, 'search_scope_changed')

    def test_invalid_national_seeds_stop_before_save_bare_and_unknown_routes_stay_manual(self):
        fixture, service, adapter = self.service()
        for seed in (NATIONAL+'?query=Python', NATIONAL+'?query=', SEED+'&query=Java'):
            with self.subTest(seed=seed), self.assertRaises(InputError):
                fixture.create(service, 'boss', keyword='Java', list_url=seed)
        service._submit.assert_not_called()
        self.assertEqual(service.state()['jobs'], [])
        self.assertEqual(adapter.search_url('Java'), 'https://www.zhipin.com/web/geek/job?query=Java')
        # These inputs lack the observed explicit national keyword contract.
        # Do not infer other city paths or the separate jobs route semantics.
        for seed in (NATIONAL, 'https://www.zhipin.com/c101010100/?query=Java',
                     'https://www.zhipin.com/web/geek/jobs?query=Java',
                     'https://www.zhipin.com/authored-manual-list?query=Java'):
            with self.subTest(seed=seed):
                state = dict(search_url=seed, keyword='Java')
                before = deepcopy(state)
                self.assertIsNone(check_scope(state, adapter, NATIONAL+'?query=Python'))
                self.assertEqual(state, before)
