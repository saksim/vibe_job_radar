"""Durable list boundaries under interruption; artificial pages, original file writes."""
import copy
from dataclasses import replace
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from vibe_job_radar.guided.adapters import Registry
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

    def create(self, max_pages=5):
        ident = self.service.create(dict(platform='liepin', keyword='时间序列',
            roles=['time_series'], consent=True, rights_note='ARTIFICIAL CHECKPOINT TEST',
            max_pages=max_pages, max_jobs=2))['id']
        return self.service._load(ident)

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
