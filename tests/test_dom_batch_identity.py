"""Authored offline URL variants; not BOSS or 51job live-site evidence."""
import copy
from html import escape
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from vibe_job_radar.guided.adapters import Registry, builtins
from vibe_job_radar.guided.automatic_selection import select_ready_batch
from vibe_job_radar.guided.batch_identity import ENTITY_V1, LEGACY
from vibe_job_radar.guided.checkpoint import binding, ensure_compatible
from vibe_job_radar.guided.contracts import CrawlError, PageSnapshot
from vibe_job_radar.guided.service import GuidedService
from vibe_job_radar.workspace import Workspace


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


class DOMBatchIdentityTests(unittest.TestCase):
    def service(self, site):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        adapter = builtins().get(site)
        service = GuidedService(Workspace(Path(temporary.name)), registry=Registry([adapter]))
        self.addCleanup(service.close)
        service._submit = Mock()  # No browser work is submitted by this fixture.
        return service, adapter

    def create(self, service, site):
        ident = service.create(dict(platform=site, keyword='time-series',
            roles=['time_series'], consent=True, rights_note='Authored offline URL fixture',
            max_pages=5, max_jobs=2, auto_collect=True))['id']
        return service._load(ident)

    @staticmethod
    def urls(site):
        if site == 'boss':
            return ('https://www.zhipin.com/job_detail/fixture-one.html',
                    'https://www.zhipin.com/job_detail/fixture-two.html')
        return ('https://we.51job.com/pc/jobdetail?jobId=123',
                'https://we.51job.com/pc/jobdetail?jobId=456')

    @staticmethod
    def variant(url, query):
        return url + ('&' if '?' in url else '?') + query

    @staticmethod
    def listing(adapter, *urls):
        return PageSnapshot(adapter.search_url('time-series'), ''.join(
            '<a href="' + escape(url, quote=True) + '">Fixture job</a>' for url in urls))

    def test_new_batches_stop_tracking_only_repeat(self):
        for site in ('boss', '51job'):
            with self.subTest(site=site):
                service, adapter = self.service(site)
                one, two = self.urls(site)
                first = self.variant(one, 'ka=first')
                second = self.variant(two, 'from=first')
                pages = Pages([self.listing(adapter, first, second),
                    self.listing(adapter, self.variant(two, 'from=next'),
                                 self.variant(one, 'ka=next'))])
                state = self.create(service, site)
                service._gather(state, pages, adapter)
                saved = service._load(state['id'])
                self.assertEqual(len(saved['cards']), 2)
                self.assertEqual(saved['identity_strategy'], ENTITY_V1)
                self.assertEqual(pages.clicks, 1)
                self.assertEqual(saved['list_end'], 'repeated_page')
                self.assertEqual(len(saved['pages_seen']), 1)
                self.assertEqual([row['url'] for row in saved['cards']], [first, second])
                self.assertEqual(select_ready_batch(saved), [row['id'] for row in saved['cards']])

    def test_identity_preserves_unknown_query_and_url_boundaries(self):
        for site in ('boss', '51job'):
            with self.subTest(site=site):
                adapter = builtins().get(site)
                one, two = self.urls(site)
                self.assertEqual(adapter.job_identity(self.variant(one, 'ka=one&from=two')),
                                 adapter.job_identity(one))
                self.assertNotEqual(adapter.job_identity(one), adapter.job_identity(two))
                self.assertNotEqual(adapter.job_identity(self.variant(one, 'publisherValue=a')),
                                    adapter.job_identity(self.variant(one, 'publisherValue=b')))
                self.assertNotEqual(adapter.job_identity(one),
                                    adapter.job_identity(one.replace('https://', 'https://m.', 1)))
                for invalid in ('http://' + one.removeprefix('https://'),
                                'https://unrelated.invalid/job_detail/fixture.html',
                                self.variant(one, 'access_token=private'),
                                one.split('?', 1)[0] + '-preview'):
                    with self.subTest(site=site, invalid=invalid), self.assertRaises(CrawlError):
                        adapter.job_identity(invalid)

    def test_saved_batch_identity_and_selection_are_not_rewritten(self):
        for site in ('boss', '51job'):
            for policy in ('missing', LEGACY, ENTITY_V1):
                with self.subTest(site=site, policy=policy):
                    service, adapter = self.service(site)
                    state = self.create(service, site)
                    if policy == 'missing':
                        state.pop('identity_strategy')
                    else:
                        state['identity_strategy'] = policy
                    state['execution_binding'] = binding(state, adapter)
                    one, _ = self.urls(site)
                    first = self.variant(one, 'ka=first')
                    second = self.variant(one, 'ka=second')
                    page = self.listing(adapter, first, second)
                    service._gather(state, Pages([page]), adapter)
                    self.assertEqual(len(state['cards']), 1 if policy == ENTITY_V1 else 2)
                    state['selection'] = [state['cards'][-1]['id']]
                    service._save(state)
                    before = copy.deepcopy(state)
                    loaded = service._load(state['id'])
                    ensure_compatible(loaded, adapter)
                    backend = Pages([page])
                    service._gather(loaded, backend, adapter)
                    for field in ('cards', 'selection', 'pages_seen', 'execution_binding'):
                        self.assertEqual(loaded[field], before[field])
                    self.assertEqual(loaded.get('identity_strategy'), before.get('identity_strategy'))
                    self.assertEqual(backend.clicks, 0)
                    self.assertEqual(loaded['cards'][0]['url'], first)