"""Authored metadata only; no downloaded package or installer is executed."""
import threading
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from vibe_job_radar import release_updates as updates
from vibe_job_radar.release_install import consent_for
from vibe_job_radar.workspace import Workspace, InputError
from test_release_updates import ReleaseFixture


class ReleaseSelectionRaceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.workspace = Workspace(Path(self.temp.name) / 'workspace')
        self.addCleanup(self.workspace.close_network)
        self.fixture = ReleaseFixture()
        self.service = updates.ReleaseUpdates(self.workspace,
            transport_factory=lambda: self.fixture, current_version='0.3.0',
            target_platform='windows-x64')
        self.addCleanup(self.service.close)
        self.check()
        self.request = {'consent': True, 'release_id': self.service._selected['release_id'],
                        'sha256': self.service._selected['sha256']}

    def check(self):
        self.service.check({'consent': True})
        self.service._thread.join(10)
        self.assertFalse(self.service._thread.is_alive())

    def request_thread(self, action):
        errors = []
        def run():
            try: action()
            except Exception as error: errors.append(error)
        thread = threading.Thread(target=run)
        thread.start()
        return thread, errors

    def assert_rejected(self, thread, errors, action):
        thread.join(10)
        self.assertFalse(thread.is_alive())
        if self.service._thread: self.service._thread.join(10)
        self.assertEqual(len(errors), 1, errors)
        self.assertIsInstance(errors[0], InputError)
        action.assert_not_called()
        self.assertFalse((self.workspace.root / 'updates').exists())

    def test_download_rechecks_selection_when_reserving_worker(self):
        reached, resume = threading.Event(), threading.Event()
        original = self.service._start
        caller = []
        def dispatch(*args, **kwargs):
            if threading.current_thread() is not threading.main_thread():
                reached.set()
                if not resume.wait(10): raise AssertionError('request not released')
            return original(*args, **kwargs)
        with patch.object(self.service, '_start', side_effect=dispatch), \
             patch.object(self.service, '_download') as download:
            try:
                thread, errors = self.request_thread(lambda: self.service.download(self.request))
                caller.append(thread)
                self.assertTrue(reached.wait(10))
                self.fixture = ReleaseFixture(entries=[('VibeJobRadar/new.txt', b'new bytes')])
                self.check()
                selected = self.service._selected
            finally:
                resume.set()
                for thread in caller: thread.join(10)
            self.assert_rejected(thread, errors, download)
            self.assertEqual(self.service._selected, selected)
            self.assertEqual(self.service.state()['status'], 'available')

    def install_after_recheck(self, *, fail):
        reached, resume = threading.Event(), threading.Event()
        def preflight():
            reached.set()
            if not resume.wait(10): raise AssertionError('preflight not released')
        self.service.installer = SimpleNamespace(preflight=preflight)
        request = dict(self.request, consent_version=consent_for('windows-x64'))
        callers = []
        with patch.object(self.service, '_install') as install:
            try:
                thread, errors = self.request_thread(lambda: self.service.install(request))
                callers.append(thread)
                self.assertTrue(reached.wait(10))
                if fail:
                    self.fixture.latest = Mock(side_effect=updates.FetchError('dns_error'))
                else:
                    self.fixture = ReleaseFixture(entries=[('VibeJobRadar/new.txt', b'new bytes')])
                self.check()
                selected = self.service._selected
            finally:
                resume.set()
                for thread in callers: thread.join(10)
            self.assert_rejected(thread, errors, install)
            self.assertEqual(self.service._selected, selected)
            self.assertEqual(self.service.state()['status'], 'failed' if fail else 'available')

    def test_install_rechecks_selection_after_preflight(self):
        self.install_after_recheck(fail=False)

    def test_install_rejects_selection_invalidated_by_failed_check(self):
        self.install_after_recheck(fail=True)


if __name__ == '__main__':
    unittest.main()
