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
