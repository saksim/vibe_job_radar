"""Artificial detail attempts exercise real checkpoints/reports, never live jobs."""
import dataclasses
import copy
import json
import tempfile
import unittest
from unittest.mock import patch

from test_guided import detail, fixture_adapter, listing
from vibe_job_radar.guided.adapters import Registry
from vibe_job_radar.guided.contracts import CrawlError
from vibe_job_radar.guided import attempt_history
from vibe_job_radar.guided.rate import RateLimit
from vibe_job_radar.guided.service import GuidedService
from vibe_job_radar.workspace import Workspace, InputError
from vibe_job_radar.utils import atomic_json


class DetailAttemptHistoryTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.workspace = Workspace(tmp.name)
        self.adapter = fixture_adapter()
        self.service = GuidedService(self.workspace, registry=Registry([self.adapter]))
        self.addCleanup(self.service.close)
        with patch.object(self.service, '_submit'):
            result = self.service.create({'platform': 'fixture', 'keyword': '时间序列',
                'roles': ['time_series'], 'max_pages': 1, 'max_jobs': 2,
                'consent': True, 'rights_note': 'ARTIFICIAL OFFLINE TEST ONLY'})
        self.state = self.service._load(result['id'])
        self.state['cards'] = [{**dataclasses.asdict(card), 'status': 'discovered',
                               'record_id': '', 'resolved_url': ''}
                              for page in (listing(1), listing(2)) for card in self.adapter.cards(page)]
        self.state['selection'] = [row['id'] for row in self.state['cards']]
        self.service._save(self.state, 'ready', status='ready', phase='collect')

    def collect(self, *results, returned_detail=None):
        class Backend:
            def __init__(self):
                self.opens = []
                self.results = iter(results)
            def open(self, url):
                self.opens.append(url)
                result = next(self.results)
                if isinstance(result, BaseException):
                    raise result
                return detail(url)
        backend = Backend()
        self.service._collect(self.state, backend, self.adapter, returned_detail=returned_detail)
        return backend

    def history(self):
        return self.service._load(self.state['id']).get('detail_attempt_history', {})

    def test_failed_attempt_is_retained_when_retry_succeeds_between_snapshots(self):
        self.collect(CrawlError('jd_incomplete'), True)
        first_report = self.state['report_id']
        first_bytes = (self.workspace.root / 'reports' / first_report / 'guided_acquisition.json').read_bytes()
        self.collect(True)
        history = self.history()
        self.assertEqual([a['outcome'] for a in history.get('attempts', [])], ['jd_incomplete', 'ok', 'ok'])
        self.assertEqual([a['item_id'] for a in history['attempts']],
                         [self.state['selection'][i] for i in (0, 1, 0)])
        self.assertEqual([row['status'] for row in self.state['cards']], ['ok', 'ok'])
        current = json.loads((self.workspace.root / 'reports' / self.state['report_id'] / 'guided_acquisition.json').read_text(encoding='utf-8'))
        self.assertEqual(current['detail_attempt_history']['attempts'], history['attempts'])
        self.assertEqual(current['detail_attempt_history']['selections'], history['selections'])
        self.assertEqual((self.workspace.root / 'reports' / first_report / 'guided_acquisition.json').read_bytes(), first_bytes)

    def test_refusal_retains_selection_before_remaining_jobs_are_reached(self):
        with self.assertRaisesRegex(CrawlError, 'robots_denied'):
            self.collect(CrawlError('robots_denied'))
        self.state['selection'] = []
        self.service._save(self.state, 'paused', status='paused')
        history = self.history()
        self.assertEqual([a['outcome'] for a in history.get('attempts', [])], ['robots_denied'])
        self.assertEqual(history['selections'][0]['items'], [r['id'] for r in self.state['cards']])
        self.assertEqual(self.state['cards'][1]['status'], 'discovered')

    def test_measured_duration_uses_monotonic_clock_even_if_wall_clock_moves_back(self):
        with patch.object(attempt_history, 'monotonic', side_effect=[100, 100.125, 200, 200.5]), \
                patch.object(attempt_history, 'utc_now', side_effect=['2026-09-01T01:00:00Z',
                    '2026-09-01T00:00:00Z', '2026-09-01T01:00:00Z', '2026-09-01T00:00:00Z']):
            self.collect(True, True)
        self.assertEqual([a['elapsed_ms'] for a in self.history()['attempts']], [125, 500])

    def test_login_returned_detail_records_processing_without_another_navigation(self):
        ident = self.state['selection'][0]
        page = detail(self.state['cards'][0]['url'])
        backend = self.collect(True, returned_detail=(ident, page))
        self.assertEqual(backend.opens, [self.state['cards'][1]['url']])
        self.assertEqual([a['mode'] for a in self.history()['attempts']], ['login_returned_detail', 'navigation'])

    def test_unknown_exception_records_only_fixed_code_and_preserves_original_error(self):
        error = RuntimeError('PRIVATE-PASSWORD-OR-COOKIE-MUST-NOT-BE-RECORDED')
        with self.assertRaises(RuntimeError) as raised:
            self.collect(error)
        self.assertIs(raised.exception, error)
        self.assertEqual(self.history()['attempts'][0]['outcome'], 'operation_error')
        self.assertEqual(self.service._load(self.state['id'])['cards'][0]['status'], 'operation_error')
        self.assertNotIn(str(error), self.service._path(self.state['id']).read_text(encoding='utf-8'))

    def test_rate_refusal_keeps_original_deadline_and_does_not_start_next_job(self):
        error = RateLimit(10, 'hourly_limit', next_allowed_at=1234)
        with self.assertRaises(RateLimit) as raised:
            self.collect(error)
        self.assertIs(raised.exception, error)
        self.assertEqual(raised.exception.next_allowed_at, 1234)
        self.assertEqual([a['outcome'] for a in self.history()['attempts']], ['hourly_limit'])

    def test_cancellation_before_processing_records_selection_but_no_attempt(self):
        self.service._cancel.set()
        with self.assertRaisesRegex(CrawlError, 'paused'):
            self.collect()
        history = self.history()
        self.assertEqual(history['attempts'], [])
        self.assertEqual(history['selections'][0]['items'], self.state['selection'])

    def test_interrupted_attempt_survives_restart_without_fabricated_result_or_duration(self):
        with self.assertRaises(KeyboardInterrupt):
            self.collect(KeyboardInterrupt())
        unfinished = copy.deepcopy(self.history()['attempts'][0])
        self.assertEqual(unfinished['outcome'], 'unfinished')
        self.assertIsNone(unfinished['finished_at'])
        self.assertIsNone(unfinished['elapsed_ms'])
        self.service.close()
        self.service = GuidedService(self.workspace, registry=Registry([self.adapter]))
        self.addCleanup(self.service.close)
        self.state = self.service._load(self.state['id'])
        self.collect(True, True)
        self.assertEqual(self.history()['attempts'][0], unfinished)
        self.assertEqual([a['outcome'] for a in self.history()['attempts']], ['unfinished', 'ok', 'ok'])

    def test_legacy_task_retains_explicit_unknown_prior_history(self):
        self.state.pop(attempt_history.KEY)
        self.service._save(self.state)
        self.collect(True, True)
        self.assertEqual(self.history()['origin'], 'legacy_partial')

    def test_corrupt_history_is_rejected_before_submitting_any_action(self):
        self.collect(True, True)
        original = self.service._path(self.state['id']).read_bytes()
        changes = [lambda h: h.update(version=True), lambda h: h.update(origin='certified'),
                   lambda h: h['attempts'][0].update(sequence=2),
                   lambda h: h['attempts'][0].update(elapsed_ms=True),
                   lambda h: h['attempts'][0].update(elapsed_ms=-1),
                   lambda h: h['attempts'][0].update(item_id='missing'),
                   lambda h: h['attempts'][0].update(password='should never be accepted'),
                   lambda h: h['attempts'][0].update(record_id=''),
                   lambda h: h['selections'].clear()]
        for change in changes:
            with self.subTest(change=change):
                state = json.loads(original)
                change(state[attempt_history.KEY])
                atomic_json(self.service._path(state['id']), state)
                before = self.service._path(state['id']).read_bytes()
                with patch.object(self.service, '_submit') as submit, self.assertRaises(InputError):
                    self.service.action({'id': state['id'], 'action': 'search'})
                submit.assert_not_called()
                self.assertEqual(self.service._path(state['id']).read_bytes(), before)
        self.service._path(self.state['id']).write_bytes(original)

    def test_full_history_stops_before_next_detail_without_truncation(self):
        self.collect(CrawlError('jd_incomplete'), True)
        original = copy.deepcopy(self.history())
        with patch.object(attempt_history, 'MAX_ATTEMPTS', 2), self.assertRaisesRegex(CrawlError, 'attempt_history_limit'):
            self.collect()
        self.assertEqual(self.history()['attempts'], original['attempts'])
        self.assertEqual(self.history()['selections'], original['selections'])

    def test_start_checkpoint_failure_never_calls_backend(self):
        save = self.service._save
        def fail_start(state, *args, **kwargs):
            if state[attempt_history.KEY]['attempts']:
                raise OSError('artificial disk error')
            return save(state, *args, **kwargs)
        with patch.object(self.service, '_save', side_effect=fail_start), self.assertRaises(OSError):
            self.collect()  # An open would raise StopIteration instead.
        self.assertEqual(self.history()['attempts'], [])

    def test_selection_capacity_does_not_erase_old_selection_or_affect_saved_file(self):
        self.state['selection'] = []
        before = self.service._path(self.state['id']).read_bytes()
        with patch.object(attempt_history, 'MAX_SELECTIONS', 1), self.assertRaisesRegex(CrawlError, 'attempt_history_limit'):
            self.service._save(self.state)
        self.assertEqual(self.service._path(self.state['id']).read_bytes(), before)

    def test_attempt_start_is_on_disk_before_backend_runs(self):
        service, state = self.service, self.state
        class Backend:
            def open(self, url):
                saved = service._load(state['id'])['detail_attempt_history']['attempts'][-1]
                self_started = saved['outcome'] == 'unfinished' and saved['finished_at'] is None
                if not self_started:
                    raise AssertionError('network action preceded its checkpoint')
                raise CrawlError('robots_denied')
        with self.assertRaisesRegex(CrawlError, 'robots_denied'):
            service._collect(state, Backend(), self.adapter)

    def test_old_writer_after_rollback_marks_a_gap_without_discarding_known_history(self):
        self.collect(CrawlError('jd_incomplete'), True)
        original = copy.deepcopy(self.history()['attempts'])
        # Old _save retains unknown JSON fields but updates the task timestamp;
        # it cannot update the newer history checkpoint marker.
        self.state['updated_at'] = '2026-01-01T00:00:00Z'
        atomic_json(self.service._path(self.state['id']), self.state)
        observed = attempt_history.snapshot(self.service._load(self.state['id']))
        self.assertEqual(observed['origin'], 'legacy_partial')
        self.collect(True)
        self.assertEqual(self.history()['origin'], 'legacy_partial')
        self.assertEqual(self.history()['attempts'][:2], original)

    def test_capture_keeps_retries_and_deselected_failures_before_first_snapshot(self):
        from vibe_job_radar.live_acceptance import AcceptanceLedger
        from vibe_job_radar.guided.adapters import builtins
        # Register the artificial task's exact shape as Liepin without treating
        # its non-Liepin successful body as live evidence.
        self.state.update(platform='liepin', backend='native', search_url=builtins().get('liepin').search_url('时间序列'))
        clock = ['2026-01-01T00:00:00Z']
        ledger = AcceptanceLedger(self.workspace.root, clock=lambda: clock[0])
        plan = ledger.create({'keyword': '时间序列', 'roles': ['time_series'], 'search_url': '', 'backend': 'native',
            'selection_rule': 'ARTIFICIAL METADATA TEST', 'timezone': 'UTC', 'source_revision': 'a'*40,
            'source_sha256': 'b'*64, 'browser': 'msedge', 'browser_version': 'fixture',
            'planner_os': 'ARTIFICIAL TEST', 'network_mode': 'system', 'consent': True})
        # The controlled task has real current operation times; choose a later
        # observation time solely for this artificial offline reader test.
        clock[0] = '2099-01-01T00:00:00Z'
        with self.assertRaisesRegex(CrawlError, 'robots_denied'):
            self.collect(CrawlError('robots_denied'))
        self.collect(CrawlError('jd_incomplete'), CrawlError('jd_incomplete'))
        self.state['selection'] = []
        self.service._save(self.state, 'paused', status='paused')
        first = ledger.capture(plan['id'])
        second = ledger.capture(plan['id'])
        self.assertEqual(first['selected_items'], 2)
        self.assertEqual(first['detail_attempts'], second['detail_attempts'])
        self.assertEqual(first['detail_attempts']['recorded'], 3)
        self.assertEqual(first['detail_attempts']['outcomes'], {'robots_denied': 1, 'jd_incomplete': 2})
        self.assertEqual(first['detail_attempts']['tasks_with_unknown_prior_history'], 0)
        self.assertEqual(first['certification'], 'not_live_verified')
        self.assertIn('per_request_cost', first['gaps'])
        self.assertIn('runtime_environment', first['gaps'])
        self.assertIn('search_login_and_http_attempts_not_recorded', first['gaps'])
        self.assertNotIn('complete_retry_history_before_capture', first['gaps'])
        # An otherwise valid newer task cannot discard an earlier completed
        # attempt observed by the ledger.
        self.state['detail_attempt_history']['attempts'].pop()
        self.service._save(self.state)
        with self.assertRaises(InputError):
            ledger.capture(plan['id'])


if __name__ == '__main__':
    unittest.main()
