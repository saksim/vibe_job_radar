"""Synthetic multi-page batches: identities, finite pagination and old saves."""
import copy
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from vibe_job_radar.guided.adapters import builtins, Registry
from vibe_job_radar.guided.batch_identity import batch_cards, ENTITY_V1
from vibe_job_radar.guided.contracts import CrawlError, PageSnapshot
from vibe_job_radar.guided.service import GuidedService
from vibe_job_radar.workspace import Workspace

ADAPTER = builtins().get('liepin')
SEARCH = ADAPTER.search_url('时间序列')


def listing(*links):
    return PageSnapshot(SEARCH, ''.join(f'<a href="{link}">时间序列工程师</a>' for link in links))


def job(number, query=''):
    return f'https://www.liepin.com/job/{number}.shtml{query}'


class Pages:
    def __init__(self, pages):
        self.pages, self.index, self.clicks = pages, 0, 0
    def snapshot(self):
        return self.pages[self.index]
    def next_page(self):
        self.clicks += 1
        if self.index + 1 >= len(self.pages):
            return False
        self.index += 1
        return True


class EntityBatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.workspace = Workspace(Path(self.tmp.name))
        self.service = GuidedService(self.workspace, registry=Registry([ADAPTER]))
        self.addCleanup(self.service.close)
        self.service._submit = Mock()

    def create(self, **changes):
        data = dict(platform='liepin', keyword='时间序列', roles=['time_series'],
                    consent=True, rights_note='Synthetic test', max_pages=5, max_jobs=20)
        ident = self.service.create({**data, **changes})['id']
        return self.service._load(ident)

    def gather(self, *pages, state=None):
        state = state if state is not None else self.create()
        backend = Pages(pages)
        self.service._gather(state, backend, ADAPTER)
        return state, backend

    def test_tracking_variants_on_same_page_keep_first_actual_link(self):
        first, later = job(1, '?scene=first'), job(1, '?scene=second')
        state, _ = self.gather(listing(first, later))
        self.assertEqual(state['identity_strategy'], ENTITY_V1)
        self.assertEqual(len(state['cards']), 1)
        self.assertEqual(state['cards'][0]['url'], first)

    def test_cross_page_variants_do_not_inflate_discovered(self):
        state, backend = self.gather(listing(job(1), job(2)),
            listing(job(2, '?scene=next'), job(3)))
        self.assertEqual(len(state['cards']), 3)
        self.assertEqual([r['url'] for r in state['cards']], [job(1), job(2), job(3)])
        self.assertEqual(state['list_end'], 'no_next_button')

    def test_reordered_tracking_variants_stop_without_a_third_click(self):
        state, backend = self.gather(listing(job(1), job(2)),
            listing(job(2, '?scene=next'), job(1, '?scene=next')), listing(job(3)))
        self.assertEqual(backend.clicks, 1)
        self.assertEqual(len(state['pages_seen']), 1)
        self.assertEqual(state['list_end'], 'repeated_page')

    def test_subset_without_new_entities_stops(self):
        state, backend = self.gather(listing(job(1), job(2)), listing(job(2)), listing(job(3)))
        self.assertEqual(backend.clicks, 1)
        self.assertEqual(state['list_end'], 'no_new_entities')
        self.assertEqual(len(state['cards']), 2)

    def test_same_number_in_different_families_is_not_merged(self):
        state, _ = self.gather(listing(job(1), 'https://www.liepin.com/a/1.shtml',
                                      'https://www.liepin.com/lptjob/1'))
        self.assertEqual(len(state['cards']), 3)

    def test_same_number_on_different_hosts_is_not_assumed_alias(self):
        state, _ = self.gather(listing(job(1), 'https://m.liepin.com/job/1.shtml'))
        self.assertEqual(len(state['cards']), 2)

    def test_explicit_page_limit_does_not_click_ahead(self):
        state, backend = self.gather(listing(job(1)), listing(job(2)), state=self.create(max_pages=1))
        self.assertEqual(backend.clicks, 0)
        self.assertEqual(state['list_end'], 'page_limit')

    def test_card_limit_stops_before_another_page(self):
        state, backend = self.gather(listing(*(job(n) for n in range(101))), listing(job(200)))
        self.assertEqual(len(state['cards']), 100)
        self.assertEqual(backend.clicks, 0)
        self.assertEqual(state['list_end'], 'card_limit')

    def test_legacy_task_retains_ids_selection_and_ordered_signature(self):
        state = self.create(); state.pop('identity_strategy')
        page = listing(job(1, '?scene=first'), job(1, '?scene=later'))
        cards = ADAPTER.cards(page)
        self.gather(page, state=state)
        expected = hashlib.sha256('\n'.join(c.id for c in cards).encode()).hexdigest()
        state['selection'] = [cards[1].id]
        self.service._save(state)
        before = copy.deepcopy(state)
        self.gather(page, state=state)
        self.assertEqual(state['pages_seen'], [expected])
        self.assertEqual(state['cards'], before['cards'])
        self.assertEqual(state['selection'], before['selection'])
        self.assertNotIn('identity_strategy', state)

    def test_reloaded_new_task_keeps_selection_and_skips_duplicate_page(self):
        state, _ = self.gather(listing(job(1), job(2)))
        state['selection'] = [state['cards'][0]['id']]
        self.service._save(state)
        loaded = self.service._load(state['id'])
        loaded, backend = self.gather(listing(job(2, '?scene=resume'), job(1)), state=loaded)
        self.assertEqual(loaded['selection'], state['selection'])
        self.assertEqual(loaded['cards'], state['cards'])
        self.assertEqual(backend.clicks, 0)

    def test_unknown_identity_version_never_silently_falls_back(self):
        state = self.create(); state['identity_strategy'] = 'future'
        with self.assertRaises(CrawlError) as caught:
            batch_cards(state, ADAPTER, ADAPTER.cards(listing(job(1))))
        self.assertEqual(caught.exception.code, 'batch_identity_unsupported')

    def test_explicit_empty_last_page_preserves_prior_matches(self):
        from test_liepin_native_search import snapshot, payload
        state, backend = self.gather(listing(job(1)), snapshot(payload([])))
        self.assertEqual(state['code'], 'ready')
        self.assertEqual(state['phase'], 'select')
        self.assertEqual(state['list_end'], 'confirmed_empty')
        self.assertEqual(len(state['cards']), 1)

    def test_explicit_empty_first_page_is_still_zero_matches(self):
        from test_liepin_native_search import snapshot, payload
        state, _ = self.gather(snapshot(payload([])))
        self.assertEqual(state['code'], 'no_matching_jobs')
        self.assertEqual(state['cards'], [])
