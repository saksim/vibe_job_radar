"""Closed selected jobs are recorded without blocking the rest of that batch."""
import copy
import json
import unittest
from unittest.mock import patch

import test_automatic_collection as fixture
from vibe_job_radar.guided import attempt_history
from vibe_job_radar.guided.contracts import CrawlError, PageSnapshot
from vibe_job_radar.guided.login_return import pending_detail_target
from vibe_job_radar.guided.read_retry import TransientReadFailure, action_for
from vibe_job_radar.store import Store

FIRST = 'https://www.liepin.com/job/1.shtml'
SECOND = 'https://www.liepin.com/job/2.shtml'
THIRD = 'https://www.liepin.com/job/3.shtml'


class ClosedBackend(fixture.MemoryBackend):
    closed_urls = (FIRST,)
    native_failure = False
    on_closed = None

    def open(self, url, *, authentication=False):
        if url not in self.closed_urls:
            return super().open(url, authentication=authentication)
        self.calls.append((url, authentication))
        if self.on_closed:
            self.on_closed()
        if self.native_failure:
            raise CrawlError('job_unavailable')
        self.page = PageSnapshot(url, '<h1>人工关闭岗位</h1><p>该职位已暂停招聘</p>'
            '<aside><a href="/job/99.shtml">推荐岗位</a></aside>')
        return self.page


class ClosedJobCollectionTests(unittest.TestCase):
    setUp = fixture.AutomaticCollectionTests.setUp
    create = fixture.AutomaticCollectionTests.create

    def collect(self, **data):
        self.service.factory = ClosedBackend
        state = self.create(**data)
        self.service._run('search', state, None)
        return state, self.service._backends[state['id']]

    def test_closed_first_keeps_denominator_and_reports_only_second_selected_job(self):
        state, backend = self.collect()
        self.assertEqual(state['status'], 'completed')
        self.assertEqual([r['status'] for r in state['cards']], ['job_unavailable', 'ok', 'discovered'])
        self.assertEqual(state['selection'], [r['id'] for r in state['cards'][:2]])
        self.assertEqual((state['outcome']['selected'], state['outcome']['saved'],
            state['outcome']['failed'], state['outcome']['pending']), (2, 1, 1, 0))
        self.assertEqual(backend.calls, [(fixture.SEARCH, False), (FIRST, False), (SECOND, False)])
        with Store(self.workspace.db) as store:
            records = store.records()
            self.assertEqual([r.url for r in records], [SECOND])
            self.assertEqual(records[0].text, fixture.BODY)
        audit = json.loads((self.workspace.root/'reports'/state['report_id']/
            'guided_acquisition.json').read_text(encoding='utf-8'))
        self.assertEqual([r['status'] for r in audit['items']], ['job_unavailable', 'ok'])
        self.assertFalse(audit['items'][0]['full_jd_confirmed'])
        self.assertEqual(audit['items'][0]['result'], 'job_unavailable')
        self.assertEqual([a['outcome'] for a in audit['detail_attempt_history']['attempts']],
                         ['job_unavailable', 'ok'])

    def test_native_open_terminal_error_also_continues(self):
        with patch.object(ClosedBackend, 'native_failure', True):
            state, backend = self.collect()
        self.assertEqual(state['outcome']['saved'], 1)
        self.assertEqual(backend.calls, [(fixture.SEARCH, False), (FIRST, False), (SECOND, False)])

    def test_all_selected_closed_ends_without_report_or_replacement(self):
        with patch.object(ClosedBackend, 'closed_urls', (FIRST, SECOND)):
            state, backend = self.collect()
        self.assertEqual(state['status'], 'completed')
        self.assertEqual(state['report_id'], '')
        self.assertEqual(state['outcome']['status'], 'no_data')
        self.assertEqual((state['outcome']['selected'], state['outcome']['failed']), (2, 2))
        self.assertEqual([a['outcome'] for a in state[attempt_history.KEY]['attempts']],
                         ['job_unavailable', 'job_unavailable'])
        self.assertEqual(backend.calls, [(fixture.SEARCH, False), (FIRST, False), (SECOND, False)])
        with Store(self.workspace.db) as store:
            self.assertEqual(store.records(), [])

    def test_pause_after_closed_then_checkpoint_resume_never_reopens_closed_job(self):
        self.service.factory = ClosedBackend
        state = self.create()
        with patch.object(ClosedBackend, 'on_closed', lambda _backend: self.service._cancel.set()):
            with self.assertRaisesRegex(CrawlError, '^paused$'):
                self.service._run('search', state, None)
        state = self.service._load(state['id'])
        before = copy.deepcopy(state[attempt_history.KEY]['attempts'])
        self.assertEqual([a['outcome'] for a in before], ['job_unavailable'])
        self.service._cancel.clear()  # The worker clears this for an explicit resume.
        self.service._run('resume', state, None)
        backend = self.service._backends[state['id']]
        self.assertEqual(state[attempt_history.KEY]['attempts'][:1], before)
        self.assertEqual(state['outcome']['saved'], 1)
        self.assertEqual(backend.calls, [(fixture.SEARCH, False), (FIRST, False), (SECOND, False)])

    def test_manual_collect_can_recheck_closed_job_without_refetching_success(self):
        state, backend = self.collect()
        old_report = self.workspace.root/'reports'/state['report_id']/'guided_acquisition.json'
        old_bytes = old_report.read_bytes()
        old_history = copy.deepcopy(state[attempt_history.KEY]['attempts'])
        backend.closed_urls = ()
        self.service.action({'id': state['id'], 'action': 'collect', 'selected': state['selection']})
        self.service._submit.assert_called_with('collect', state['id'], None)
        state = self.service._load(state['id'])
        self.assertEqual([r['status'] for r in state['cards']], ['discovered', 'ok', 'discovered'])
        self.service._run('collect', state, None)
        self.assertEqual(state['outcome']['saved'], 2)
        self.assertEqual(state[attempt_history.KEY]['attempts'][:2], old_history)
        self.assertEqual(old_report.read_bytes(), old_bytes)
        self.assertEqual(backend.calls, [(fixture.SEARCH, False), (FIRST, False),
                                        (SECOND, False), (FIRST, False)])

    def test_reselecting_other_jobs_does_not_reset_unselected_closed_result(self):
        state, backend = self.collect()
        self.service.action({'id': state['id'], 'action': 'collect', 'selected': [state['cards'][2]['id']]})
        state = self.service._load(state['id'])
        self.assertEqual(state['cards'][0]['status'], 'job_unavailable')
        self.service._run('collect', state, None)
        self.assertEqual(backend.calls[-1], (THIRD, False))
        self.assertEqual(sum(url == FIRST for url, _ in backend.calls), 1)
        self.assertEqual(state['outcome']['selected'], 1)

    def test_later_login_or_access_gate_still_stops_before_third_selected_job(self):
        for code in ('manual_required', 'http_403', 'http_429', 'redirect_requires_attention'):
            with self.subTest(code=code), patch.object(ClosedBackend, 'block', code):
                self.service.factory = ClosedBackend
                state = self.create(max_jobs=3)
                with self.assertRaisesRegex(CrawlError, '^'+code+'$'):
                    self.service._run('search', state, None)
                backend = self.service._backends[state['id']]
                self.assertEqual([r['status'] for r in state['cards']],
                                 ['job_unavailable', code, 'discovered'])
                self.assertEqual(backend.calls, [(fixture.SEARCH, False), (FIRST, False), (SECOND, False)])
                self.assertEqual(state['report_id'], '')
                if code == 'manual_required':
                    target = pending_detail_target(state)
                    self.assertEqual((target.row_id, target.expected_url),
                                     (state['cards'][1]['id'], SECOND))
                else:
                    self.assertIsNone(pending_detail_target(state))

    def test_transient_retry_targets_second_after_closed_and_preserves_batch(self):
        self.service.factory = ClosedBackend
        state = self.create()
        original = ClosedBackend.open
        def opened(backend, url, **kwargs):
            if url == SECOND:
                backend.calls.append((url, False))
                raise TransientReadFailure(url, 503, '30')
            return original(backend, url, **kwargs)
        with patch.object(ClosedBackend, 'open', opened):
            with self.assertRaises(TransientReadFailure) as failed:
                self.service._run('search', state, None)
        selection = list(state['selection'])
        self.assertEqual(action_for(state, failed.exception), 'resume')
        with self.assertRaisesRegex(CrawlError, '^read_retry_unavailable$'):
            action_for(state, TransientReadFailure(FIRST, 503))
        self.service._run('resume', state, None)
        self.assertEqual(state['selection'], selection)
        self.assertEqual(state['outcome']['saved'], 1)
        self.assertEqual(self.service._backends[state['id']].calls,
            [(fixture.SEARCH, False), (FIRST, False), (SECOND, False), (SECOND, False)])


if __name__ == '__main__':
    unittest.main()
