"""Status metadata must not hold the lock needed to finish browser actions."""
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from vibe_job_radar.guided.service import GuidedService
from vibe_job_radar.workspace import Workspace
from test_guided import FakeBackend, fixture_adapter
from vibe_job_radar.guided.adapters import Registry


class GuidedStateLockTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.service = GuidedService(Workspace(Path(tmp.name)),
            registry=Registry([fixture_adapter()]), backend_factory=FakeBackend)
        self.addCleanup(self.service.close)

    def test_slow_package_lookup_cannot_hold_up_queued_action_completion(self):
        entered, release, completed = threading.Event(), threading.Event(), threading.Event()
        errors, views = [], []
        def package():
            entered.set()
            if not release.wait(10):
                raise AssertionError('controlled metadata lookup not released')
            return '1.60.0'
        def capture(function):
            try:
                function()
            except Exception as exc:
                errors.append(exc)
        original_done = self.service._queue.task_done
        def done():
            original_done()
            completed.set()
        poll = threading.Thread(target=lambda: capture(lambda: views.append(self.service.state())))
        submit = threading.Thread(target=lambda: capture(lambda: self.service._submit('check_browser')))
        with (patch.object(self.service, '_package', side_effect=package),
                patch.object(self.service, '_check_browser', return_value=None),
                patch.object(self.service._queue, 'task_done', side_effect=done)):
            poll.start()
            try:
                self.assertTrue(entered.wait(5))
                submit.start()
                self.assertTrue(completed.wait(5),
                    'unrelated package lookup kept the action from completing')
                self.assertFalse(release.is_set())
            finally:
                release.set()
                poll.join(5)
                if submit.ident is not None:
                    submit.join(5)
        self.assertFalse(poll.is_alive())
        self.assertFalse(submit.is_alive())
        self.assertEqual(errors, [])
        self.assertFalse(views[0]['busy'])
        self.assertEqual(views[0]['browser_package'], '1.60.0')

    def test_package_is_consistent_within_one_view_and_refreshed_next_time(self):
        with (patch.object(self.service, '_package', side_effect=['1.60.0', '1.61.0']),
                patch.object(self.service._choice, 'historical_view', return_value=None) as history):
            first = self.service.state()
            second = self.service.state()
        self.assertEqual(first['browser_package'], '1.60.0')
        self.assertEqual(second['browser_package'], '1.61.0')
        self.assertEqual([call.args[1] for call in history.call_args_list],
            [first['browser_package'], second['browser_package']])

    def _paused_task(self):
        with patch.object(self.service, '_submit'):
            ident = self.service.create({'platform':'fixture', 'keyword':'人工列表',
                'consent':True, 'rights_note':'Synthetic lock regression; no network'})['id']
        state = self.service._load(ident)
        self.service._save(state, 'paused', status='paused')
        return ident

    def test_slow_login_advice_does_not_block_queued_action_completion(self):
        self._paused_task()
        entered, release, completed = threading.Event(), threading.Event(), threading.Event()
        errors, views = [], []
        advice = {'available':True, 'reason':'', 'next_allowed_at':None, 'wait_seconds':0}
        def lookup(site):
            entered.set()
            if not release.wait(15):
                raise AssertionError('controlled login advice not released')
            return advice
        def capture(function):
            try:
                function()
            except Exception as exc:
                errors.append(exc)
        original_done = self.service._queue.task_done
        def done():
            original_done()
            completed.set()
        poll = threading.Thread(target=lambda: capture(lambda: views.append(self.service.state())))
        submit = threading.Thread(target=lambda: capture(lambda: self.service._submit('check_browser')))
        with (patch.object(self.service.ledger, 'login_availability', side_effect=lookup),
                patch.object(self.service, '_check_browser', return_value=None),
                patch.object(self.service._queue, 'task_done', side_effect=done)):
            poll.start()
            try:
                self.assertTrue(entered.wait(5))
                submit.start()
                self.assertTrue(completed.wait(5),
                    'read-only login advice kept an unrelated action from completing')
                self.assertFalse(release.is_set())
            finally:
                release.set()
                poll.join(5)
                if submit.ident is not None:
                    submit.join(5)
        self.assertFalse(poll.is_alive())
        self.assertFalse(submit.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(views[0]['jobs'][0]['login_availability'], advice)

    def test_slow_login_advice_releases_checkpoint_lock_and_preserves_snapshot(self):
        ident = self._paused_task()
        other = GuidedService(self.service.workspace,
            registry=Registry([fixture_adapter()]), backend_factory=FakeBackend)
        self.addCleanup(other.close)
        state = other._load(ident)
        entered, release, saved = threading.Event(), threading.Event(), threading.Event()
        views, errors = [], []
        def lookup(site):
            entered.set()
            if not release.wait(15):
                raise AssertionError('controlled login advice not released')
            return {'available':False, 'reason':'daily_limit',
                    'next_allowed_at':123456.0, 'wait_seconds':321.0}
        def poll_state():
            try:
                views.append(self.service.state())
            except Exception as exc:
                errors.append(exc)
        def save_state():
            try:
                other._save(state, 'stopped', status='stopped')
                saved.set()
            except Exception as exc:
                errors.append(exc)
        poll = threading.Thread(target=poll_state)
        writer = threading.Thread(target=save_state)
        with patch.object(self.service.ledger, 'login_availability', side_effect=lookup):
            poll.start()
            try:
                self.assertTrue(entered.wait(5))
                writer.start()
                self.assertTrue(saved.wait(5),
                    'login advice kept the other instance from saving its checkpoint')
                self.assertFalse(release.is_set())
            finally:
                release.set()
                poll.join(5)
                if writer.ident is not None:
                    writer.join(5)
        self.assertFalse(poll.is_alive())
        self.assertFalse(writer.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(views[0]['jobs'][0]['status'], 'paused')
        self.assertFalse(views[0]['jobs'][0]['login_availability']['available'])
        self.assertEqual(self.service.state()['jobs'][0]['status'], 'stopped')

    def test_login_advice_is_once_per_site_fresh_per_view_and_not_shared(self):
        self._paused_task()
        self._paused_task()
        first = {'available':True, 'reason':'', 'next_allowed_at':None, 'wait_seconds':0}
        second = {'available':False, 'reason':'daily_limit',
                  'next_allowed_at':123456.0, 'wait_seconds':321.0}
        with (patch.object(self.service.ledger, 'login_availability', side_effect=[first, second]) as lookup,
                patch.object(self.service.ledger, 'reserve') as reserve):
            view = self.service.state()
            self.assertEqual(lookup.call_count, 1)
            self.assertEqual(len(view['jobs']), 2)
            view['jobs'][0]['login_availability']['reason'] = 'local mutation'
            self.assertEqual(view['jobs'][1]['login_availability'], first)
            self.assertEqual(first['reason'], '')
            fresh = self.service.state()
            self.assertEqual(lookup.call_count, 2)
            self.assertEqual([call.args for call in lookup.call_args_list], [('fixture',), ('fixture',)])
            self.assertTrue(all(job['login_availability']==second for job in fresh['jobs']))
            reserve.assert_not_called()

    def test_login_advice_storage_error_stays_unavailable_without_reserving(self):
        from vibe_job_radar.guided.contracts import CrawlError
        self._paused_task()
        with (patch.object(self.service.ledger, 'login_availability',
                           side_effect=CrawlError('rate_storage_error')),
                patch.object(self.service.ledger, 'reserve') as reserve):
            view = self.service.state()
            reserve.assert_not_called()
        self.assertEqual(view['jobs'][0]['login_availability'],
            {'available':False, 'reason':'rate_storage_error',
             'next_allowed_at':None, 'wait_seconds':None})
