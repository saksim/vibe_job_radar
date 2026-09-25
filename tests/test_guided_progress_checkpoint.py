"""Browser callbacks must not restore the snapshot from an earlier action."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from vibe_job_radar.guided.adapters import Registry
from vibe_job_radar.guided.contracts import CrawlError
from vibe_job_radar.guided.service import GuidedService
from vibe_job_radar.store import Store
from vibe_job_radar.workspace import Workspace
from test_automatic_collection import ADAPTER, BODY, MemoryBackend


class ProgressBackend(MemoryBackend):
    def __init__(self, adapter, ledger, cancelled, progress, **options):
        super().__init__()
        self.progress = progress
        self.observe = lambda: None
        self.fail_detail = False

    def open(self, url, *, authentication=False):
        if '/job/' in url:
            self.progress('rate_wait', 0.4)
            self.observe()
            if self.fail_detail and not authentication:
                raise CrawlError('redirect_requires_attention')
        return super().open(url, authentication=authentication)


class GuidedProgressCheckpointTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.workspace = Workspace(Path(self.tmp.name))
        self.service = GuidedService(self.workspace, registry=Registry([ADAPTER]), backend_factory=ProgressBackend)
        self.addCleanup(self.service.close)
        self.service._submit = Mock()
        self.ident = self.service.create(dict(platform='liepin', keyword='时间序列', roles=['time_series'],
            consent=True, rights_note='Synthetic test', max_pages=1, max_jobs=1))['id']
        self.service._run('search', self.service._load(self.ident), None)
        self.backend = self.service._backends[self.ident]
        self.card = self.service._load(self.ident)['cards'][0]['id']
        self.observed = []
        self.backend.observe = lambda: self.observed.append(self.service._load(self.ident))

    def collect(self):
        self.service.action({'id':self.ident, 'action':'collect', 'selected':[self.card]})
        self.service._run('collect', self.service._load(self.ident), None)
        return self.service._load(self.ident)

    def assert_file_unchanged_by_progress(self):
        before = self.service._path(self.ident).read_bytes()
        self.backend.progress('rate_wait', 0.4)
        self.assertEqual(self.service._path(self.ident).read_bytes(), before)

    def test_wait_during_later_collection_keeps_selection_and_started_attempt(self):
        result = self.collect()
        during = self.observed[0]
        self.assertEqual(during['selection'], [self.card])
        self.assertEqual(during['phase'], 'collect')
        self.assertEqual(during['status'], 'running')
        self.assertEqual(during['cards'][0]['status'], 'opening')
        self.assertEqual(len(during['detail_attempt_history']['attempts']), 1)
        self.assertEqual((during['code'], during['wait_seconds']), ('rate_wait', 0.4))
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['outcome']['saved'], 1)
        self.assertTrue(self.workspace.report(result['report_id']))

    def test_late_login_progress_keeps_original_selection_attempt_and_pending_authentication(self):
        self.backend.fail_detail = True
        with self.assertRaisesRegex(CrawlError, 'redirect_requires_attention'):
            self.collect()
        state = self.service._load(self.ident)
        self.service._save(state, 'redirect_requires_attention', status='waiting_manual')
        self.service.action({'id':self.ident, 'action':'login', 'auto_continue':True})
        self.service._run('login', self.service._load(self.ident), None)
        state = self.service._load(self.ident)
        self.assertEqual(state['selection'], [self.card])
        self.assertEqual(state['authentication'], 'manual_pending')
        self.assertEqual(state['login_continuation'], 'watching')
        self.assertTrue(state['auto_continue_after_login'])
        self.assertEqual(len(state['detail_attempt_history']['attempts']), 1)
        self.assert_file_unchanged_by_progress()

    def test_completed_report_and_saved_jd_survive_late_progress(self):
        state = self.collect()
        self.assert_file_unchanged_by_progress()
        self.assertTrue(self.workspace.report(state['report_id']))
        with Store(self.workspace.db) as store:
            self.assertEqual(len(store.records()), 1)
            self.assertEqual(store.records()[0].text, BODY)

    def test_new_queued_paused_or_stopped_action_wins_over_old_callback(self):
        for status, code in (('queued','opening'),('paused','paused'),('stopped','stopped')):
            with self.subTest(status=status):
                state = self.service._load(self.ident)
                self.service._save(state, code, status=status, selection=[self.card], phase='collect')
                self.assert_file_unchanged_by_progress()

    def test_cancellation_and_shutdown_prevent_progress_rewriting_running_checkpoint(self):
        for flag in (self.service._cancel, self.service._shutdown):
            with self.subTest(flag='cancel' if flag is self.service._cancel else 'shutdown'):
                state = self.service._load(self.ident)
                self.service._save(state, 'opening', status='running', selection=[self.card], phase='collect')
                flag.set()
                try:
                    self.assert_file_unchanged_by_progress()
                finally:
                    flag.clear()


if __name__ == '__main__':
    unittest.main()
