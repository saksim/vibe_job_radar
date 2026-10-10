"""Resume a verified resident list without submitting the query again."""
import copy
from dataclasses import replace
import unittest
from unittest.mock import patch

import test_gather_checkpoint_boundary as fixture
from test_guided_batch_identity import ADAPTER, SEARCH, Pages, listing, job
from vibe_job_radar.guided.contracts import CrawlError


class ResidentPages(Pages):
    def __init__(self, pages, *, index=0, replay=None):
        super().__init__(pages)
        self.index = index
        self.replay = list(replay if replay is not None else pages)
        self.opens = []
        self.closed = False
        self.snapshot_error = ''
        self.access_error = ''
        self.snapshot_reads = 0
        self.replaced_snapshot = None

    def alive(self):
        return not self.closed

    def close(self):
        self.closed = True

    def collection_mode(self):
        pass

    def open_search(self, url, *, keyword):
        self.opens.append((url, keyword))
        self.pages = list(self.replay)
        self.index = 0

    def snapshot(self):
        self.snapshot_reads += 1
        if self.snapshot_error:
            raise CrawlError(self.snapshot_error)
        if self.snapshot_reads > 1 and self.replaced_snapshot is not None:
            return self.replaced_snapshot
        return super().snapshot()

    def ensure_page_access(self, url):
        if self.access_error:
            raise CrawlError(self.access_error)


class ResidentListResumeTests(unittest.TestCase):
    setUp = fixture.GatherCheckpointBoundaryTests.setUp
    create = fixture.GatherCheckpointBoundaryTests.create
    paused_listing = fixture.GatherCheckpointBoundaryTests.paused_listing
    assert_saved_prefix = fixture.GatherCheckpointBoundaryTests.assert_saved_prefix

    def resident(self, state, pages, **options):
        backend = ResidentPages(pages, index=len(state['pages_seen'])-1, **options)
        self.service._backends[state['id']] = backend
        return backend

    def test_owned_last_page_continues_without_dynamic_query_resubmission(self):
        state, prefix = self.paused_listing(count=2, max_pages=3)
        state['selection'] = [state['cards'][0]['id']]
        self.service._save(state)
        before = copy.deepcopy(state)
        third = replace(listing(job(3)), url=SEARCH+'&currentPage=2')
        changed_search = replace(listing(job(99)), url=prefix[0].url)
        backend = self.resident(state, prefix+[third], replay=[changed_search])
        self.service._run('resume', state, None)
        self.assertEqual(backend.opens, [])
        self.assertEqual(backend.clicks, 1)
        self.assertEqual(state['cards'][:2], before['cards'])
        self.assertEqual(state['selection'], before['selection'])
        self.assertEqual(state['effective_search'], before['effective_search'])
        self.assertEqual(state['cursors_seen'], ['0', '1', '2'])
        self.assertEqual(state['list_end'], 'page_limit')
        self.assertEqual(state['cards'][2]['url'], job(3))

    def test_first_saved_resident_page_also_continues_without_search(self):
        state, prefix = self.paused_listing(count=1, max_pages=2)
        second = replace(listing(job(2)), url=SEARCH+'&currentPage=1')
        backend = self.resident(state, prefix+[second])
        self.service.action({'id': state['id'], 'action': 'resume'})
        self.service._submit.assert_called_with('resume', state['id'], None)
        state = self.service._load(state['id'])
        self.assertEqual(state['status'], 'queued')
        self.service._run('resume', state, None)
        self.assertEqual(backend.opens, [])
        self.assertEqual(backend.clicks, 1)
        self.assertEqual(state['cursors_seen'], ['0', '1'])

    def test_changed_resident_query_cursor_or_cards_retains_checked_replay(self):
        for change in ('query', 'cursor', 'cards', 'empty'):
            with self.subTest(change=change):
                state, prefix = self.paused_listing(count=1, max_pages=2)
                second = replace(listing(job(2)), url=SEARCH+'&currentPage=1')
                current = prefix[0]
                if change == 'query':
                    current = replace(current, url=ADAPTER.search_url('another'))
                elif change == 'cursor':
                    current = replace(current, url=SEARCH+'&currentPage=9')
                elif change == 'cards':
                    current = replace(listing(job(99)), url=current.url)
                else:
                    current = replace(listing(), url=current.url)
                backend = self.resident(state, [current], replay=prefix+[second])
                self.service._run('resume', state, None)
                self.assertEqual(backend.opens, [(SEARCH, '时间序列')])
                self.assertEqual([row['url'] for row in state['cards']], [job(1), job(2)])

    def test_changed_page_between_probe_and_collection_stops_without_click_or_search(self):
        state, prefix = self.paused_listing(count=2, max_pages=3)
        before = copy.deepcopy(state)
        backend = self.resident(state, prefix)
        backend.replaced_snapshot = replace(listing(job(99)), url=prefix[-1].url)
        with self.assertRaisesRegex(CrawlError, '^checkpoint_list_changed$'):
            self.service._run('resume', state, None)
        self.assertEqual(backend.opens, [])
        self.assertEqual(backend.clicks, 0)
        self.assert_saved_prefix(before)

    def test_owned_access_or_login_gate_does_not_fall_back_to_search(self):
        for code in ('manual_required', 'http_403', 'http_429', 'native_policy_changed',
                     'redirect_requires_attention', 'robots_denied', 'paused'):
            with self.subTest(code=code):
                state, prefix = self.paused_listing(count=1, max_pages=2)
                before = copy.deepcopy(state)
                backend = self.resident(state, prefix)
                if code == 'robots_denied':
                    backend.access_error = code
                else:
                    backend.snapshot_error = code
                with self.assertRaisesRegex(CrawlError, '^'+code+'$'):
                    self.service._run('resume', state, None)
                self.assertEqual(backend.opens, [])
                self.assertEqual(backend.clicks, 0)
                self.assert_saved_prefix(before)

    def test_replaced_backend_replays_saved_prefix_instead_of_inheriting_owner(self):
        state, prefix = self.paused_listing(count=2, max_pages=3)
        old = self.resident(state, prefix)
        old.closed = True
        third = replace(listing(job(3)), url=SEARCH+'&currentPage=2')
        replacement = ResidentPages([], replay=prefix+[third])
        with patch.object(self.service, '_backend', return_value=replacement):
            self.service._run('resume', state, None)
        self.assertEqual(replacement.opens, [(SEARCH, '时间序列')])
        self.assertEqual(replacement.clicks, 2)
        self.assertEqual(state['cursors_seen'], ['0', '1', '2'])

    def test_legacy_checkpoint_without_confirmed_url_keeps_full_replay(self):
        state, prefix = self.paused_listing(count=2, max_pages=3)
        del state['checkpoint_list_url']
        third = replace(listing(job(3)), url=SEARCH+'&currentPage=2')
        backend = self.resident(state, prefix+[third])
        self.service._run('resume', state, None)
        self.assertEqual(backend.opens, [(SEARCH, '时间序列')])
        self.assertEqual(backend.clicks, 2)
        self.assertEqual(state['list_end'], 'page_limit')

    def test_current_saved_page_at_budget_finishes_without_extra_navigation(self):
        state, prefix = self.paused_listing(count=2, max_pages=3)
        # This models a previously interrupted boundary at its saved page cap.
        # Updating the cap is not a public API capability.
        state['max_pages'] = 2
        from vibe_job_radar.guided.checkpoint import binding
        state['execution_binding'] = binding(state, ADAPTER)
        self.service._save(state)
        backend = self.resident(state, prefix)
        self.service._run('resume', state, None)
        self.assertEqual(backend.opens, [])
        self.assertEqual(backend.clicks, 0)
        self.assertEqual(state['list_end'], 'page_limit')
        self.assertEqual(len(state['cards']), 2)


    def test_unreadable_probe_keeps_the_original_checked_search_path(self):
        state, prefix = self.paused_listing(count=1, max_pages=2)
        second = replace(listing(job(2)), url=SEARCH+'&currentPage=1')
        backend = self.resident(state, prefix+[second])
        with patch.object(backend, 'snapshot', side_effect=[
                CrawlError('page_not_ready'), prefix[0], second]):
            self.service._run('resume', state, None)
        self.assertEqual(backend.opens, [(SEARCH, '时间序列')])
        self.assertEqual(state['list_end'], 'page_limit')
        self.assertEqual([c['url'] for c in state['cards']], [job(1), job(2)])

    def test_missing_native_observation_never_adopts_dom_cards(self):
        state, prefix = self.paused_listing(count=1, max_pages=2)
        second = replace(listing(job(2)), url=SEARCH+'&currentPage=1')
        pending = replace(prefix[0], business_required=True)
        backend = self.resident(state, [pending], replay=prefix+[second])
        self.service._run('resume', state, None)
        self.assertEqual(backend.opens, [(SEARCH, '时间序列')])
        self.assertEqual(state['list_end'], 'page_limit')

    def test_access_revoked_after_probe_stops_before_advancing(self):
        state, prefix = self.paused_listing(count=2, max_pages=3)
        before = copy.deepcopy(state)
        backend = self.resident(state, prefix)
        with patch.object(backend, 'ensure_page_access',
                          side_effect=[None, CrawlError('robots_denied')]):
            with self.assertRaisesRegex(CrawlError, '^robots_denied$'):
                self.service._run('resume', state, None)
        self.assertEqual(backend.opens, [])
        self.assertEqual(backend.clicks, 0)
        self.assert_saved_prefix(before)


    def test_pending_login_or_authentication_mode_keeps_the_original_search(self):
        for state_pending, browser_auth in ((True, False), (False, True)):
            with self.subTest(state_pending=state_pending, browser_auth=browser_auth):
                state, prefix = self.paused_listing(count=1, max_pages=2)
                second = replace(listing(job(2)), url=SEARCH+'&currentPage=1')
                if state_pending:
                    state['authentication'] = 'manual_pending'
                backend = self.resident(state, prefix+[second])
                backend.auth_mode = browser_auth
                self.service._run('resume', state, None)
                self.assertEqual(backend.opens, [(SEARCH, '时间序列')])
                self.assertEqual(state['list_end'], 'page_limit')


if __name__ == '__main__':
    unittest.main()
