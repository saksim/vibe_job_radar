"""Durable list boundaries under interruption; artificial pages, original file writes."""
import copy
from dataclasses import replace
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from vibe_job_radar.guided.adapters import Registry
from vibe_job_radar.guided.contracts import CrawlError
from vibe_job_radar.guided.service import GuidedService
import vibe_job_radar.guided.service as service_module
from vibe_job_radar.workspace import Workspace
from test_guided_batch_identity import ADAPTER, SEARCH, Pages, listing, job


class AfterCommit(Exception):
    pass


class GatherCheckpointBoundaryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.workspace = Workspace(temporary.name)
        self.service = GuidedService(self.workspace, registry=Registry([ADAPTER]))
        self.addCleanup(self.service.close)
        self.service._submit = Mock()

    def create(self, max_pages=5, **changes):
        ident = self.service.create(dict(platform='liepin', keyword='时间序列',
            roles=['time_series'], consent=True, rights_note='ARTIFICIAL CHECKPOINT TEST',
            max_pages=max_pages, max_jobs=2, **changes))['id']
        return self.service._load(ident)

    def paused_listing(self, count=1, max_pages=3, **changes):
        state = self.create(max_pages=max_pages, **changes)
        pages = [replace(listing(job(i+1)), url=SEARCH+'&currentPage='+str(i))
                 for i in range(count)]
        class InterruptedPages(Pages):
            def next_page(self):
                if self.index + 1 == len(self.pages):
                    raise CrawlError('paused')
                return super().next_page()
        with self.assertRaises(CrawlError) as caught:
            self.service._gather(state, InterruptedPages(pages), ADAPTER)
        self.assertEqual(caught.exception.code, 'paused')
        self.service._save(state, 'paused', status='paused')
        return self.service._load(state['id']), pages

    def resume_listing(self, state, pages):
        backend = Pages(pages)
        backend.open_search = Mock()
        with patch.object(self.service, '_backend', return_value=backend):
            self.service._run('resume', state, None)
        backend.open_search.assert_called_once_with(SEARCH, keyword='时间序列')
        return self.service._load(state['id']), backend

    def assert_saved_prefix(self, before):
        after = self.service._load(before['id'])
        for field in ('cards', 'selection', 'pages_seen', 'cursors_seen',
                      'effective_search', 'last_list_url', 'checkpoint_list_url', 'report_id'):
            self.assertEqual(after.get(field), before.get(field), field)

    def test_resume_traverses_multiple_saved_pages_without_replacing_old_cards_or_selection(self):
        state, pages = self.paused_listing(count=2)
        state['selection'] = [state['cards'][0]['id']]
        self.service._save(state)
        before = copy.deepcopy(state)
        pages[0] = replace(listing(job(1, '?scene=again')), url=pages[0].url)
        resumed, backend = self.resume_listing(
            state, pages+[replace(listing(job(3)), url=SEARCH+'&currentPage=2')])
        self.assertEqual([c['url'] for c in resumed['cards']], [job(1), job(2), job(3)])
        self.assertEqual(resumed['cards'][:2], before['cards'])
        self.assertEqual(resumed['selection'], before['selection'])
        self.assertEqual(resumed['effective_search'], before['effective_search'])
        self.assertEqual(resumed['cursors_seen'], ['0','1','2'])
        self.assertEqual(resumed['list_end'], 'page_limit')
        self.assertEqual(resumed['report_id'], '')
        self.assertEqual(backend.clicks, 2)

    def test_resume_changed_missing_or_empty_prefix_never_merges_a_new_list(self):
        for kind in ('first_changed', 'last_changed', 'early_end', 'empty'):
            with self.subTest(kind=kind):
                state, pages = self.paused_listing(count=2)
                state['selection'] = [state['cards'][0]['id']]
                self.service._save(state); before = copy.deepcopy(state)
                if kind == 'first_changed':
                    pages[0] = replace(listing(job(99)), url=pages[0].url)
                elif kind == 'last_changed':
                    pages[1] = replace(listing(job(99)), url=pages[1].url)
                elif kind == 'early_end':
                    pages = pages[:1]
                else:
                    pages[0] = replace(listing(), url=pages[0].url)
                with self.assertRaises(CrawlError) as caught:
                    self.resume_listing(state, pages)
                self.assertEqual(caught.exception.code, 'checkpoint_list_changed')
                self.assert_saved_prefix(before)

    def test_resume_changed_cursor_or_filter_stops_before_next_page(self):
        for suffix, code in (('&currentPage=1', 'checkpoint_list_changed'),
                             ('&currentPage=0&city=010', 'search_scope_changed')):
            with self.subTest(suffix=suffix):
                state, pages = self.paused_listing()
                before = copy.deepcopy(state)
                backend = Pages([replace(pages[0], url=SEARCH+suffix)])
                backend.open_search = Mock()
                with patch.object(self.service, '_backend', return_value=backend), self.assertRaises(CrawlError) as caught:
                    self.service._run('resume', state, None)
                self.assertEqual(caught.exception.code, code)
                self.assertEqual(backend.clicks, 0)
                self.assert_saved_prefix(before)

    def test_resume_unverifiable_old_history_stops_before_backend_allocation(self):
        state, _ = self.paused_listing()
        before = copy.deepcopy(state)
        missing = copy.deepcopy(state)
        missing.pop('checkpoint_list_url'); missing.pop('last_list_url')
        for card in missing['cards']:
            card['source_url'] = ''
        no_scope = copy.deepcopy(state); no_scope.pop('effective_search')
        variants = [
            missing, no_scope,
            {**state, 'checkpoint_list_url': ADAPTER.search_url('another-query')},
            {**state, 'checkpoint_list_url': SEARCH+'&currentPage=9'},
            {**state, 'checkpoint_list_url': None},
            {**state, 'cards': []},
            {**state, 'pages_seen': state['pages_seen']*2},
            {**state, 'cursors_seen': ['0','0']},
            {**state, 'pages_seen': [], 'cursors_seen': ['0']},
        ]
        for changed in variants:
            with self.subTest(keys=sorted(changed)), patch.object(self.service, '_backend') as factory, self.assertRaises(CrawlError) as caught:
                self.service._run('resume', changed, None)
            self.assertEqual(caught.exception.code, 'checkpoint_list_changed')
            factory.assert_not_called()
            self.assert_saved_prefix(before)

    def test_resume_still_terminates_a_true_repeat_after_saved_prefix(self):
        state, pages = self.paused_listing()
        before = copy.deepcopy(state)
        repeated = replace(pages[0], url=SEARCH+'&currentPage=1')
        resumed, backend = self.resume_listing(state, pages+[repeated])
        self.assertEqual(resumed['cards'], before['cards'])
        self.assertEqual(resumed['pages_seen'], before['pages_seen'])
        self.assertEqual(resumed['cursors_seen'], before['cursors_seen'])
        self.assertEqual(resumed['list_end'], 'repeated_page')
        self.assertEqual(backend.clicks, 1)

    def test_resume_at_saved_page_budget_does_not_visit_an_extra_page(self):
        state = self.create(max_pages=2)
        pages = [replace(listing(job(i+1)), url=SEARCH+'&currentPage='+str(i))
                 for i in range(3)]
        self.service._gather(state, Pages(pages), ADAPTER)
        # A legacy interrupted boundary may already contain the full budget
        # without a terminal reason. Verify it without requesting a third page.
        state.pop('list_end'); state['phase'] = 'search'
        self.service._save(state, 'paused', status='paused')
        before = copy.deepcopy(state)
        resumed, backend = self.resume_listing(state, pages)
        self.assertEqual(resumed['cards'], before['cards'])
        self.assertEqual(resumed['pages_seen'], before['pages_seen'])
        self.assertEqual(resumed['list_end'], 'page_limit')
        self.assertEqual(backend.clicks, 1)

    def test_cancellation_while_revisiting_prefix_preserves_checkpoint(self):
        state, pages = self.paused_listing(count=2)
        before = copy.deepcopy(state)
        backend = Pages(pages+[replace(listing(job(3)), url=SEARCH+'&currentPage=2')])
        backend.open_search = Mock()
        original_next = backend.next_page
        def cancel_after_click():
            result = original_next()
            self.service._cancel.set()
            return result
        backend.next_page = cancel_after_click
        try:
            with patch.object(self.service, '_backend', return_value=backend), self.assertRaises(CrawlError) as caught:
                self.service._run('resume', state, None)
            self.assertEqual(caught.exception.code, 'paused')
            self.assertEqual(backend.clicks, 1)
            self.assert_saved_prefix(before)
        finally:
            self.service._cancel.clear()

    def test_login_watcher_capture_continues_saved_prefix_without_refetch(self):
        from vibe_job_radar.guided.login_return import LoginReturnManager
        # Keep both the saved and returned DOM cursor explicit and identical;
        # the return watcher must observe the exact originally requested URL.
        state, pages = self.paused_listing(list_url=SEARCH+'&currentPage=0')
        self.service._save(state, status='waiting_manual', authentication='manual_pending',
                           auto_continue_after_login=True)
        before = copy.deepcopy(state)
        backend = Pages(pages+[replace(listing(job(2)), url=SEARCH+'&currentPage=1')])
        backend.open_search = Mock()
        backend.open = Mock()
        clock = [0.]
        manager = LoginReturnManager(clock=lambda:clock[0])
        self.service._submit.reset_mock()
        with patch.dict(self.service._backends, {state['id']:backend}), patch.object(
                self.service, '_backend', return_value=backend):
            self.assertTrue(manager.arm(state, backend))
            manager.tick(self.service);clock[0]+=1.;manager.tick(self.service)
            self.service._submit.assert_called_once_with('capture', state['id'])
            self.service._run('capture', self.service._load(state['id']), None)
        resumed = self.service._load(state['id'])
        self.assertEqual([c['url'] for c in resumed['cards']], [job(1),job(2)])
        self.assertEqual(resumed['cards'][:1], before['cards'])
        self.assertEqual(resumed['list_end'], 'no_next_button')
        self.assertEqual(resumed['authentication'], 'user_resumed')
        backend.open.assert_not_called();backend.open_search.assert_not_called()

    def test_search_and_returned_search_continue_the_unfinished_saved_list(self):
        from vibe_job_radar.guided.login_return import ReturnedSearch
        for action in ('search','resume_returned_search'):
            with self.subTest(action=action):
                state, pages = self.paused_listing()
                self.service._save(state, authentication='manual_pending')
                before = copy.deepcopy(state)
                backend = Pages(pages+[replace(listing(job(2)), url=SEARCH+'&currentPage=1')])
                backend.open_search = Mock();backend.open = Mock()
                handoff = ReturnedSearch(SEARCH, state['keyword'], 'observed-entry', backend)
                with patch.dict(self.service._backends, {state['id']:backend}), patch.object(
                        self.service, '_backend', return_value=backend), patch(
                        'vibe_job_radar.guided.liepin_form.matching_search_entry_signature',
                        return_value='observed-entry'), patch(
                        'vibe_job_radar.guided.liepin_form.submit_search') as submit:
                    self.service._run(action, state, handoff if action=='resume_returned_search' else None)
                resumed = self.service._load(state['id'])
                self.assertEqual([c['url'] for c in resumed['cards']], [job(1),job(2)])
                self.assertEqual(resumed['cards'][:1], before['cards'])
                self.assertEqual(resumed['list_end'], 'no_next_button')
                backend.open.assert_not_called()
                if action=='resume_returned_search':
                    backend.open_search.assert_not_called()
                    submit.assert_called_once_with(backend, state['keyword'])
                else:
                    backend.open_search.assert_called_once_with(SEARCH, keyword=state['keyword'])
                    submit.assert_not_called()

    def test_unreadable_next_page_recovers_the_last_saved_position(self):
        for legacy, action in ((old, mode) for old in (False, True) for mode in ('resume','capture')):
            with self.subTest(legacy=legacy, action=action):
                state = self.create(max_pages=3)
                first = replace(listing(job(1)), url=SEARCH+'&currentPage=0')
                empty = replace(listing(), url=SEARCH+'&currentPage=1')
                self.service._gather(state, Pages([first, empty]), ADAPTER)
                self.assertEqual(state['code'], 'empty_list')
                self.assertEqual(state['last_list_url'], empty.url)
                self.assertEqual(state['cursors_seen'], ['0'])
                self.assertEqual(state['checkpoint_list_url'], first.url)
                if legacy:
                    state.pop('checkpoint_list_url', None)
                    self.service._save(state)
                before = copy.deepcopy(state)
                readable = replace(listing(job(2)), url=empty.url)
                if action == 'resume':
                    resumed, backend = self.resume_listing(state, [first, readable])
                else:
                    # The same previously attempted page can become readable.
                    # Capture must consume it without navigating to page one.
                    backend = Pages([readable])
                    backend.open_search = Mock();backend.open = Mock()
                    with patch.object(self.service, '_backend', return_value=backend):
                        self.service._run('capture', state, None)
                    resumed = self.service._load(state['id'])
                    backend.open.assert_not_called();backend.open_search.assert_not_called()
                self.assertEqual([c['url'] for c in resumed['cards']], [job(1),job(2)])
                self.assertEqual(resumed['cards'][:1], before['cards'])
                self.assertEqual(resumed['selection'], before['selection'])
                self.assertEqual(resumed['effective_search'], before['effective_search'])
                self.assertEqual(resumed['cursors_seen'], ['0','1'])
                self.assertEqual(resumed['list_end'], 'no_next_button')
                self.assertEqual(backend.clicks, 2 if action=='resume' else 1)

    def test_terminal_state_is_complete_at_the_first_committed_write(self):
        state = self.create(max_pages=1)
        backend = Pages([listing(job(1)), listing(job(2))])
        original = service_module.atomic_json
        fsync_counts = []
        def stop_after_commit(path, value):
            before = fsync.call_count
            original(path, value)
            if Path(path) == self.service._path(state['id']):
                fsync_counts.append(fsync.call_count - before)
                raise AfterCommit()
        with patch('os.fsync', wraps=os.fsync) as fsync, \
             patch.object(service_module, 'atomic_json', side_effect=stop_after_commit), \
             self.assertRaises(AfterCommit):
            self.service._gather(state, backend, ADAPTER)
        saved = self.service._load(state['id'])
        self.assertEqual((saved['code'], saved['status'], saved['phase'], saved.get('list_end')),
                         ('ready', 'ready', 'select', 'page_limit'))
        self.assertEqual([c['url'] for c in saved['cards']], [job(1)])
        self.assertEqual(len(saved['pages_seen']), 1)
        self.assertEqual(saved['last_list_url'], SEARCH)
        self.assertEqual(saved['effective_search'], {'key': '时间序列'})
        self.assertEqual(backend.clicks, 0)
        self.assertEqual(fsync_counts, [1])

    def test_each_navigation_follows_a_durable_current_page(self):
        state = self.create(max_pages=3)
        owner = self
        class ObservedPages(Pages):
            def next_page(self):
                saved = owner.service._load(state['id'])
                owner.assertEqual((saved['code'], saved['status']), ('reading', 'running'))
                owner.assertEqual([c['url'] for c in saved['cards']],
                                  [job(n) for n in range(1, self.index + 2)])
                owner.assertEqual(len(saved['pages_seen']), self.index + 1)
                owner.assertEqual(saved['last_list_url'], self.snapshot().url)
                return super().next_page()
        backend = ObservedPages([listing(job(n)) for n in (1, 2, 3)])
        self.service._gather(state, backend, ADAPTER)
        saved = self.service._load(state['id'])
        self.assertEqual(backend.clicks, 2)
        self.assertEqual(len(saved['cards']), 3)
        self.assertEqual((saved['code'], saved['list_end']), ('ready', 'page_limit'))

    def test_navigation_failure_leaves_a_page_recoverable_after_restart(self):
        state = self.create()
        backend = Pages([listing(job(1))])
        backend.next_page = Mock(side_effect=RuntimeError('artificial navigation failure'))
        with self.assertRaisesRegex(RuntimeError, 'navigation failure'):
            self.service._gather(state, backend, ADAPTER)
        self.service.close()
        restored = GuidedService(self.workspace, registry=Registry([ADAPTER]))
        self.addCleanup(restored.close)
        saved = restored._load(state['id'])
        self.assertEqual([c['url'] for c in saved['cards']], [job(1)])
        self.assertEqual(len(saved['pages_seen']), 1)
        self.assertNotEqual(saved['code'], 'ready')
        self.assertNotIn('list_end', saved)
        backend.next_page.assert_called_once()
        # Preservation alone is not recovery: explicitly resume the persisted
        # task and verify that its still-unread second page is reached.
        before_resume = copy.deepcopy(saved)
        continuation = Pages([listing(job(1)), listing(job(2))])
        continuation.open_search = Mock()
        with patch.object(restored, '_backend', return_value=continuation):
            restored._run('resume', saved, None)
        resumed = restored._load(state['id'])
        self.assertEqual([c['url'] for c in resumed['cards']], [job(1), job(2)])
        self.assertEqual(len(resumed['pages_seen']), 2)
        self.assertEqual((resumed['status'], resumed['phase'], resumed['list_end']),
                         ('ready', 'select', 'no_next_button'))
        self.assertEqual(resumed['cards'][0], before_resume['cards'][0])
        self.assertEqual(resumed['selection'], [])
        self.assertEqual(resumed['report_id'], '')
        continuation.open_search.assert_called_once_with(SEARCH, keyword='时间序列')

    def test_failed_fsync_preserves_prior_checkpoint_without_publishing_ready(self):
        state = self.create(max_pages=1)
        path = self.service._path(state['id'])
        before = path.read_bytes()
        original = service_module.atomic_json
        def fail_flush(target, value):
            if Path(target) == path:
                with patch('os.fsync', side_effect=OSError('artificial fsync failure')):
                    return original(target, value)
            return original(target, value)
        with patch.object(service_module, 'atomic_json', side_effect=fail_flush), \
             self.assertRaisesRegex(OSError, 'fsync failure'):
            self.service._gather(state, Pages([listing(job(1))]), ADAPTER)
        self.assertEqual(path.read_bytes(), before)
        self.assertNotEqual(self.service._load(state['id'])['code'], 'ready')
        self.assertEqual(list(path.parent.glob('.' + path.name + '*')), [])

    def test_all_terminal_paths_keep_one_final_commit_and_navigation_boundaries(self):
        cases = [
            ('page_limit', 1, [listing(job(1)), listing(job(2))], 1, 0, 1),
            ('page_limit', 2, [listing(job(1)), listing(job(2))], 2, 1, 2),
            ('no_next_button', 5, [listing(job(1))], 2, 1, 1),
            ('repeated_page', 5, [listing(job(1)), listing(job(1))], 2, 1, 1),
            ('no_new_entities', 5, [listing(job(1), job(2)), listing(job(2))], 2, 1, 2),
            ('card_limit', 5, [listing(*(job(i) for i in range(101)))], 1, 0, 100),
            ('repeated_cursor', 5,
             [replace(listing(job(i)), url=SEARCH+'&currentPage='+str(c))
              for i, c in ((1, 0), (2, 1), (3, 1))], 3, 2, 2),
        ]
        original = service_module.atomic_json
        for reason, max_pages, pages, writes, clicks, card_count in cases:
            with self.subTest(reason=reason, max_pages=max_pages):
                state = self.create(max_pages=max_pages)
                backend = Pages(pages)
                checkpoints = []
                def observe(path, value):
                    original(path, value)
                    if Path(path) == self.service._path(state['id']):
                        checkpoints.append(copy.deepcopy(value))
                with patch.object(service_module, 'atomic_json', side_effect=observe):
                    self.service._gather(state, backend, ADAPTER)
                self.assertEqual(len(checkpoints), writes)
                self.assertEqual(sum(c['code'] == 'ready' for c in checkpoints), 1)
                saved = self.service._load(state['id'])
                self.assertEqual(checkpoints[-1], saved)
                self.assertEqual((saved['code'], saved['phase'], saved['list_end']),
                                 ('ready', 'select', reason))
                self.assertEqual(len(saved['cards']), card_count)
                self.assertEqual(backend.clicks, clicks)

    def test_resuming_terminal_page_keeps_selection_and_frozen_scope(self):
        state = self.create()
        page = replace(listing(job(1), job(2)), url=SEARCH+'&city=010')
        self.service._gather(state, Pages([page]), ADAPTER)
        state['selection'] = [state['cards'][0]['id']]
        self.service._save(state)
        before = self.service._load(state['id'])
        loaded = copy.deepcopy(before)
        backend = Pages([replace(listing(job(2, '?scene=again'), job(1)), url=page.url)])
        original = service_module.atomic_json
        with patch.object(service_module, 'atomic_json', wraps=original) as write:
            self.service._gather(loaded, backend, ADAPTER)
        saved = self.service._load(state['id'])
        for field in ('cards', 'selection', 'pages_seen', 'effective_search'):
            self.assertEqual(saved[field], before[field])
        self.assertEqual((saved['code'], saved['list_end']), ('ready', 'repeated_page'))
        self.assertEqual(backend.clicks, 0)
        self.assertEqual(write.call_count, 1)


if __name__ == '__main__':
    unittest.main()
