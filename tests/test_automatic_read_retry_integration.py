"""A merged auto-selection and finite-read retry must keep one frozen batch."""
from pathlib import Path
import hashlib
import tempfile
import threading
import unittest
from unittest.mock import patch

from test_automatic_collection import ADAPTER, SEARCH, MemoryBackend
from vibe_job_radar.guided.adapters import Registry
from vibe_job_radar.guided.rate import Limits, RateLedger
from vibe_job_radar.guided.read_retry import TransientReadFailure
from vibe_job_radar.guided.service import GuidedService
from vibe_job_radar.store import Store
from vibe_job_radar.workspace import Workspace

FIRST = 'https://www.liepin.com/job/1.shtml'
SECOND = 'https://www.liepin.com/job/2.shtml'


class AutomaticReadRetryIntegrationTests(unittest.TestCase):
    def run_case(self, close_before_due=False):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Workspace(tmp)
            now = [1000.0]
            ledger = RateLedger(Path(tmp)/'rates.sqlite',
                Limits(page_interval=0, request_interval=0), clock=lambda:now[0])
            instances = []
            class Backend(MemoryBackend):
                def __init__(self, *args, **kwargs):
                    super().__init__(*args, **kwargs)
                    self.error = self.wait_error = None
                    self.failed = False
                    instances.append(self)
                def open(self, url, *, authentication=False):
                    self.error = self.wait_error = None
                    ledger.reserve('liepin', 'page')
                    ledger.reserve('liepin', 'request')
                    if url == SECOND and not self.failed:
                        self.failed = True
                        self.calls.append((url, authentication))
                        failure = TransientReadFailure(url, 503, '30')
                        self.error, self.wait_error = failure.code, failure
                        raise failure
                    return super().open(url, authentication=authentication)
            service = GuidedService(workspace, registry=Registry([ADAPTER]),
                                    backend_factory=Backend, ledger=ledger)
            finished = threading.Event()
            task_done = service._queue.task_done
            def done():
                task_done()
                finished.set()
            try:
                with patch.object(service._queue, 'task_done', side_effect=done), \
                     patch('socket.getaddrinfo', side_effect=AssertionError('fixture must not resolve')), \
                     patch('socket.create_connection', side_effect=AssertionError('fixture must not dial')):
                    ident = service.create({'platform':'liepin','keyword':'时间序列',
                        'roles':['time_series'],'max_pages':1,'max_jobs':2,'auto_collect':True,
                        'consent':True,'rights_note':'Independent artificial integration test'})['id']
                    self.assertTrue(finished.wait(30), 'initial batch did not finish')
                    before = service._load(ident)
                    self.assertEqual(before['code'], 'read_retry_wait')
                    self.assertEqual(before['retry_action'], 'resume')
                    self.assertEqual(before['phase'], 'collect')
                    self.assertTrue(before['auto_selection_applied'])
                    self.assertEqual(before['read_retry']['used'], 1)
                    self.assertEqual(before['next_allowed_at'], 1030.0)
                    self.assertEqual(before['outcome']['saved'], 1)
                    self.assertTrue(before['report_id'])
                    selection = list(before['selection'])
                    first_record = before['cards'][0]['record_id']
                    original_report = workspace.root/'reports'/before['report_id']
                    hashes = {p.relative_to(original_report).as_posix():hashlib.sha256(p.read_bytes()).hexdigest()
                              for p in original_report.rglob('*') if p.is_file()}
                    self.assertEqual(instances[0].calls, [(SEARCH,False),(FIRST,False),(SECOND,False)])
                    finished.clear()
                    if close_before_due:
                        changed = threading.Event()
                        original_save = service._save
                        def saved(state, *args, **kwargs):
                            result = original_save(state, *args, **kwargs)
                            if state.get('code') == 'automatic_resume_unavailable': changed.set()
                            return result
                        with patch.object(service, '_save', side_effect=saved):
                            instances[0].closed = True
                            now[0] = 1031.0
                            self.assertTrue(changed.wait(30), 'closed owner was not reported')
                        after = service._load(ident)
                        self.assertEqual(after['status'], 'waiting_manual')
                        self.assertFalse(after['auto_resume'])
                        self.assertEqual(after['report_id'], before['report_id'])
                        self.assertEqual(instances[0].calls, [(SEARCH,False),(FIRST,False),(SECOND,False)])
                    else:
                        now[0] = 1031.0
                        self.assertTrue(finished.wait(30), 'deferred original batch did not finish')
                        after = service._load(ident)
                        self.assertEqual(after['status'], 'completed', after['code'])
                        self.assertEqual(after['outcome']['saved'], 2)
                        self.assertNotEqual(after['report_id'], before['report_id'])
                        self.assertTrue(workspace.report(after['report_id']))
                        self.assertEqual(instances[0].calls,
                            [(SEARCH,False),(FIRST,False),(SECOND,False),(SECOND,False)])
                        with Store(workspace.db) as store:
                            self.assertEqual(len(store.records(latest_only=False)), 2)
                    self.assertEqual(ledger.summary('liepin')['request']['day'], 3 if close_before_due else 4)
                    self.assertEqual(len(instances), 1)
                    self.assertEqual(after['selection'], selection)
                    self.assertEqual(after['cards'][0]['record_id'], first_record)
                    self.assertEqual(after['cards'][2]['status'], 'discovered')
                    self.assertEqual(after['read_retry']['used'], 1)
                    self.assertEqual(hashes, {p.relative_to(original_report).as_posix():hashlib.sha256(p.read_bytes()).hexdigest()
                                             for p in original_report.rglob('*') if p.is_file()})
            finally:
                service.close()

    def test_finite_read_retry_resumes_original_auto_batch_without_replaying_saved_jd(self):
        self.run_case()

    def test_closed_original_browser_preserves_partial_report_without_replacement(self):
        self.run_case(close_before_due=True)


if __name__ == '__main__':
    unittest.main()
