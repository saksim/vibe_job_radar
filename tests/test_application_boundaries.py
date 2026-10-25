"""Independent page domains and one explicit collection admission."""
from concurrent.futures import ThreadPoolExecutor
import json
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from vibe_job_radar.application import WorkbenchApplication
from vibe_job_radar.application_http import WorkbenchHTTP
from vibe_job_radar.evidence_ui import EvidenceService
from vibe_job_radar.guided.rate import Limits, RateLedger
from vibe_job_radar.network import SafeHTTP
from vibe_job_radar.workspace import InputError, Workspace
from test_public_category import Wire, job_url
from test_workbench import capture


class ApplicationBoundaryTests(unittest.TestCase):
    def workspace(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        return Workspace(temporary.name)

    def application(self, workspace=None):
        app = WorkbenchApplication(workspace or self.workspace(), public_client=None)
        self.addCleanup(app.close)
        return app

    def test_bad_operational_stores_do_not_hide_settings_or_frozen_reports(self):
        workspace = self.workspace()
        workspace.add_job(capture())
        report = workspace.analyze({'roles': ['time_series']})
        previous = workspace.status()
        service = EvidenceService(workspace)
        source_bytes, evidence_bytes = b'artificial invalid source database', b'artificial invalid evidence database'
        workspace.db.write_bytes(source_bytes)
        service.db.write_bytes(evidence_bytes)
        with self.assertRaises(InputError):
            EvidenceService(workspace)  # Direct callers retain fail-fast validation.
        app = self.application(workspace)
        routes = WorkbenchHTTP(app)
        app.mutation_lock.acquire()
        try:
            response = routes.get('/api/bootstrap')
            self.assertEqual(response.status, 200)
            bootstrap = json.loads(response.body)
            for name in ('roles', 'platforms', 'brave_key_configured'):
                self.assertEqual(bootstrap[name], previous[name])
            self.assertNotIn('records', bootstrap)
            self.assertNotIn('runs', bootstrap)
        finally:
            app.mutation_lock.release()
        self.assertEqual(routes.get('/api/status').status, 400)
        self.assertEqual(routes.get('/api/report/' + report['id']).status, 200)
        self.assertEqual(routes.post('/api/evidence/state', {}).status, 400)
        self.assertEqual(routes.get('/api/evidence/export').status, 400)
        self.assertEqual(workspace.db.read_bytes(), source_bytes)
        self.assertEqual(service.db.read_bytes(), evidence_bytes)

    def test_lazy_first_access_is_shared_and_keeps_replaceable_facade(self):
        app = self.application()
        self.assertFalse((app.workspace.root / 'evidence').exists())
        entered, release = threading.Event(), threading.Event()
        def initialize(workspace):
            entered.set()
            self.assertTrue(release.wait(5), 'test did not release evidence initialization')
            return EvidenceService(workspace)
        with patch('vibe_job_radar.application.EvidenceService', side_effect=initialize) as factory:
            with ThreadPoolExecutor(max_workers=2) as executor:
                first = executor.submit(lambda: app.evidence)
                try:
                    self.assertTrue(entered.wait(5))
                    second = executor.submit(lambda: app.evidence)
                finally:
                    release.set()
                self.assertIs(first.result(30), second.result(30))
            self.assertEqual(factory.call_count, 1)
        self.assertEqual(app.evidence.state()['revision'], 0)
        replacement = SimpleNamespace(state=lambda data: {'revision': 7})
        app.evidence = replacement
        self.assertEqual(app.execute('evidence.state', {})['revision'], 7)
        del app.evidence
        with self.assertRaises(AttributeError):
            _ = app.evidence
        app.evidence = replacement
        self.assertIs(app.evidence, replacement)

    def test_close_does_not_initialize_an_unused_evidence_store(self):
        app = self.application()
        app.close()
        with self.assertRaises(InputError):
            _ = app.evidence
        self.assertFalse((app.workspace.root / 'evidence').exists())
        self.assertEqual(WorkbenchHTTP(app).get('/api/bootstrap').status, 200)

    def test_close_can_join_accepted_worker_that_has_not_read_evidence_yet(self):
        app = self.application()
        service = app.evidence
        entered, release = threading.Event(), threading.Event()
        original_run, original_close = app.report_tasks._run, app.report_tasks.close
        def delayed(*args):
            entered.set()
            if not release.wait(10):
                raise AssertionError('test did not release accepted worker')
            original_run(*args)
        def close_worker():
            # Application.close holds lifecycle here. The worker now reads
            # app.evidence before generating and must be able to finish.
            release.set()
            original_close()
            self.assertFalse(app.report_tasks._thread.is_alive(), 'worker blocked by application shutdown')
        with patch.object(app.workspace, 'report_file', return_value=app.workspace.root / 'synthetic-manifest'), \
                patch.object(service, 'generate', return_value={'id': 'b' * 32}), \
                patch.object(app.report_tasks, '_run', side_effect=delayed), \
                patch.object(app.report_tasks, 'close', side_effect=close_worker):
            try:
                app.execute('report_tasks.start', {'kind': 'evidence', 'run_id': 'a' * 32, 'expected_revision': 0})
                self.assertTrue(entered.wait(5))
                app.close()
            finally:
                release.set()
                if app.report_tasks._thread:
                    app.report_tasks._thread.join(10)
        self.assertEqual(app.report_tasks.state()['task']['status'], 'succeeded')
        self.assertFalse(app.mutation_lock.locked())

    @staticmethod
    def request(**changes):
        collection = dict(mode='urls', urls=job_url(1), roles=['architect'],
            platforms=['liepin'], permit_platforms=['liepin'], consent=True,
            detail_budget=1, rights_note='Artificial application boundary regression only.')
        collection.update(changes)
        return {'collection': collection, 'execution': {'consent': True}}

    def run_collection(self, *, lose_acknowledgement=False):
        app = self.application()
        wire = Wire()
        ledger = RateLedger(app.workspace.root / 'guided/rates.sqlite',
            Limits(page_interval=0, request_interval=0), clock=lambda: 1_000_000.0)
        original_start = app.collection_runner.start
        def admit(request):
            response = original_start(request)
            if lose_acknowledgement:
                raise OSError('PRIVATE-ACKNOWLEDGEMENT-CONTENT')
            return response
        with patch('socket.socket.connect', side_effect=AssertionError('unexpected external request')), \
                patch('vibe_job_radar.collection_rate.RateLedger', return_value=ledger), \
                patch.object(SafeHTTP, 'public_get', side_effect=wire.public_get), \
                patch.object(app.collector, 'list', side_effect=AssertionError('page refresh is not admission')), \
                patch.object(app.collection_runner, 'start', side_effect=admit) as start:
            response = WorkbenchHTTP(app).post('/api/collection/create_and_start', self.request())
            self.assertEqual(response.status, 200)
            value = json.loads(response.body)
            self.assertIs(value['accepted'], None if lose_acknowledgement else True)
            self.assertTrue(value['created'])
            self.assertNotIn(b'PRIVATE-ACKNOWLEDGEMENT-CONTENT', response.body)
            self.assertEqual(start.call_count, 1)
            app.collection_runner._thread.join(30)
            self.assertFalse(app.collection_runner.is_running())
            final = app.collection_runner.state()
            self.assertEqual(final['id'], value['task']['id'])
            self.assertEqual(final['status'], 'completed')
            self.assertEqual(final['task']['saved_detail_count'], 1)
            self.assertTrue(app.workspace.report_file(final['task']['report_id'], 'dashboard.html').is_file())
            self.assertFalse(app.mutation_lock.locked())
        self.assertEqual(wire.calls, ['https://www.liepin.com/robots.txt', job_url(1)])
        self.assertEqual(ledger.summary('liepin')['request']['day'], 2)
        return value

    def test_create_and_run_finishes_without_page_history_or_more_client_commands(self):
        self.assertEqual(self.run_collection()['code'], 'accepted')

    def test_lost_admission_ack_preserves_created_identity_and_never_replays(self):
        value = self.run_collection(lose_acknowledgement=True)
        self.assertEqual(value['code'], 'start_unconfirmed')
        self.assertIsNone(value['runner'])

    def test_rejected_preview_consent_and_busy_gate_create_nothing(self):
        app = self.application()
        routes = WorkbenchHTTP(app)
        with patch.object(app.collector, 'start', wraps=app.collector.start) as create:
            bad = self.request(urls='not a source URL')
            response = routes.post('/api/collection/create_and_start', bad)
            self.assertEqual(response.status, 400)
            value = json.loads(response.body)
            self.assertEqual(value['code'], 'collection_preflight_failed')
            self.assertIs(value['accepted'], False)
            self.assertFalse(value['created'])
            self.assertFalse(value['preview']['ready'])
            bad = self.request()
            bad['execution']['consent'] = False
            self.assertEqual(routes.post('/api/collection/create_and_start', bad).status, 400)
            bad = self.request(mode='feed', endpoint='https://feed.example/jobs',
                contract_ref='https://feed.example/contract')
            self.assertEqual(routes.post('/api/collection/create_and_start', bad).status, 400)
            app.mutation_lock.acquire()
            try:
                self.assertEqual(routes.post('/api/collection/create_and_start', self.request()).status, 409)
            finally:
                app.mutation_lock.release()
            create.assert_not_called()
        self.assertEqual(list(app.collector.root.glob('*.json')), [])

    def test_execution_key_never_enters_saved_task_or_unconfirmed_response(self):
        app = self.application()
        secret = 'ARTIFICIAL-EXECUTION-ONLY-KEY'
        request = self.request(mode='search', search_storage_rights=True, detail_budget=0)
        request['execution']['api_key'] = secret
        with patch.object(app.collection_runner, 'start', side_effect=OSError(secret)) as start:
            response = WorkbenchHTTP(app).post('/api/collection/create_and_start', request)
        self.assertEqual(response.status, 200)
        value = json.loads(response.body)
        self.assertIsNone(value['accepted'])
        self.assertNotIn(secret, response.body.decode())
        self.assertEqual(start.call_count, 1)
        self.assertEqual(start.call_args.args[0]['api_key'], secret)
        for path in app.collector.root.rglob('*'):
            if path.is_file():
                self.assertNotIn(secret.encode(), path.read_bytes())
        self.assertEqual(app.collector.status({'id': value['task']['id']})['search_requests'], 0)
        self.assertFalse(app.mutation_lock.locked())


if __name__ == '__main__':
    unittest.main()
