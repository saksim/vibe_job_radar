"""Finite report lifecycle; artificial offline inputs, no user workspace."""
import copy
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from vibe_job_radar.evidence_ui import EvidenceService
from vibe_job_radar.report_tasks import ReportTaskBusy, ReportTasks
from vibe_job_radar.workspace import InputError, Workspace


class ReportTaskTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.workspace = Workspace(self.temporary.name)
        self.evidence = EvidenceService(self.workspace)
        self.app = SimpleNamespace(workspace=self.workspace, evidence=self.evidence,
                                   mutation_lock=threading.Lock())
        self.services = []
        self.releases = []
        self.tasks = self.service(self.app)

    def tearDown(self):
        for event in self.releases:
            event.set()
        for service in self.services:
            service.close()
            if service._thread:
                service._thread.join(5)
        self.workspace.close_network()
        self.temporary.cleanup()

    def service(self, app):
        service = ReportTasks(app)
        self.services.append(service)
        return service

    def wait(self, service=None):
        service = service or self.tasks
        if service._thread:
            service._thread.join(10)
            self.assertFalse(service._thread.is_alive(), 'controlled report did not finish')
        return service.state()

    def blocked(self, operation):
        entered, release = threading.Event(), threading.Event()
        self.releases.append(release)
        def run(data):
            entered.set()
            if not release.wait(10):
                raise AssertionError('controlled report not released')
            return operation(data)
        return run, entered, release

    def test_real_analysis_and_personal_report_keep_original_report_flow(self):
        from test_workbench import capture
        self.workspace.add_job(capture())
        with patch('socket.create_connection', side_effect=AssertionError('network forbidden')):
            accepted = self.tasks.start({'kind': 'analysis', 'dataset': 'real', 'roles': ['time_series']})
            first = self.wait()['task']
            self.assertEqual(first['id'], accepted['task']['id'])
            self.assertEqual(first['status'], 'succeeded')
            source = self.workspace.report(first['report_id'])
            self.assertEqual(source['manifest']['stats']['full_text_job_groups'], 1)
            self.assertIsNone(source['manifest']['filters']['platforms'])
            original = self.workspace.report_file(first['report_id'], 'run_manifest.json').read_bytes()
            self.tasks.start({'kind': 'evidence', 'run_id': first['report_id'], 'expected_revision': 0})
            second = self.wait()['task']
            self.assertEqual(second['status'], 'succeeded')
            self.assertEqual(second['source_run_id'], first['report_id'])
            self.assertEqual(second['revision'], 0)
            self.assertNotEqual(second['report_id'], first['report_id'])
            personal = self.workspace.report(second['report_id'])
            self.assertIn('evidence_revision.json', personal['files'])
            self.assertEqual(self.workspace.report_file(first['report_id'], 'run_manifest.json').read_bytes(), original)
        reopened = self.service(SimpleNamespace(workspace=self.workspace, evidence=self.evidence,
                                                mutation_lock=threading.Lock()))
        self.assertEqual(reopened.state()['task'], second)
        self.assertFalse(self.app.mutation_lock.locked())

    def test_accepted_input_is_copied_and_both_application_and_workspace_are_exclusive(self):
        observed = []
        operation, entered, release = self.blocked(lambda data: observed.append(copy.deepcopy(data)) or {'id': 'a' * 32})
        other_app = SimpleNamespace(workspace=self.workspace, evidence=self.evidence, mutation_lock=threading.Lock())
        observer = self.service(other_app)
        supplied = {'kind': 'analysis', 'dataset': 'demo', 'roles': ['time_series']}
        with patch.object(self.workspace, 'analyze', side_effect=operation) as analyze:
            accepted = self.tasks.start(supplied)
            self.assertTrue(entered.wait(3))
            supplied['roles'].append('architect')
            self.assertTrue(self.app.mutation_lock.locked())
            self.assertEqual(observer.state()['task']['id'], accepted['task']['id'])
            self.assertTrue(observer.state()['owned_elsewhere'])
            self.assertTrue(observer.is_running())
            before = self.tasks.path.read_bytes()
            with self.assertRaises(ReportTaskBusy):
                self.tasks.start({'kind': 'analysis', 'dataset': 'demo'})
            with self.assertRaises(ReportTaskBusy):
                observer.start({'kind': 'analysis', 'dataset': 'demo'})
            self.assertFalse(other_app.mutation_lock.locked())
            self.assertEqual(before, self.tasks.path.read_bytes())
            release.set()
            self.assertEqual(self.wait()['task']['status'], 'succeeded')
            analyze.assert_called_once()
        self.assertEqual(observed[0]['roles'], ['time_series'])
        self.assertFalse(self.app.mutation_lock.locked())
        self.assertFalse(observer.is_running())

    def test_invalid_start_is_rejected_without_task_or_credential_persistence(self):
        bad = [None, {}, {'kind': []}, {'kind': 'analysis', 'dataset': []},
               {'kind': 'analysis', 'api_key': 'PRIVATE-TEST-KEY'},
               {'kind': 'analysis', 'dataset': 'demo', 'roles': []},
               {'kind': 'evidence', 'run_id': 'a' * 32, 'expected_revision': True},
               {'kind': 'evidence', 'run_id': '../private', 'expected_revision': 0}]
        with patch.object(self.workspace, 'analyze') as analyze, patch.object(self.evidence, 'generate') as generate:
            for data in bad:
                with self.subTest(data=data), self.assertRaises(InputError):
                    self.tasks.start(data)
                self.assertFalse(self.app.mutation_lock.locked())
            analyze.assert_not_called()
            generate.assert_not_called()
        self.assertFalse(self.tasks.path.exists())
        self.assertIsNone(self.tasks.state()['task'])

    def test_dead_owner_is_observed_as_interrupted_without_write_or_replay(self):
        saved = {'id': 'a' * 32, 'kind': 'analysis', 'status': 'running', 'report_id': '',
                 'source_run_id': '', 'revision': None, 'error': None}
        self.tasks._save(saved)
        before = self.tasks.path.read_bytes()
        observer = self.service(self.app)
        with patch.object(self.workspace, 'analyze') as analyze:
            first = observer.state()
            self.assertEqual(first['task']['status'], 'interrupted')
            self.assertFalse(first['busy'])
            self.assertFalse(observer.is_running())
            analyze.assert_not_called()
        self.assertEqual(before, self.tasks.path.read_bytes())

    def test_worker_rechecks_original_evidence_revision_and_does_not_save_raw_error(self):
        path = Path(self.temporary.name) / 'source-manifest.json'
        path.write_text('{}', encoding='utf-8')
        original = self.evidence.generate
        entered, release = threading.Event(), threading.Event()
        self.releases.append(release)
        def delayed(data):
            entered.set()
            self.assertTrue(release.wait(5))
            return original(data)
        with patch.object(self.workspace, 'report_file', return_value=path), patch.object(self.evidence, 'generate', side_effect=delayed):
            self.tasks.start({'kind': 'evidence', 'run_id': 'b' * 32, 'expected_revision': 0})
            self.assertTrue(entered.wait(3))
            with patch.object(self.evidence, 'state', return_value={'revision': 1}):
                release.set()
                result = self.wait()['task']
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['error']['code'], 'input_changed')
        self.assertEqual(result['report_id'], '')
        self.assertFalse(self.app.mutation_lock.locked())

    def test_failure_releases_owners_and_keeps_exception_text_private(self):
        secret = 'PRIVATE-TEXT-FROM-DOCUMENT'
        with patch.object(self.workspace, 'analyze', side_effect=OSError(secret)):
            self.tasks.start({'kind': 'analysis', 'dataset': 'demo'})
            result = self.wait()
        self.assertEqual(result['task']['status'], 'failed')
        self.assertNotIn(secret, json.dumps(result))
        self.assertNotIn(secret.encode(), self.tasks.path.read_bytes())
        self.assertFalse(self.app.mutation_lock.locked())
        self.assertFalse(self.tasks.is_running())
        with patch.object(self.workspace, 'analyze', return_value={'id': 'c' * 32}):
            self.tasks.start({'kind': 'analysis', 'dataset': 'demo'})
            self.assertEqual(self.wait()['task']['status'], 'succeeded')

    def test_close_is_bounded_and_cannot_release_a_live_worker_owner(self):
        operation, entered, release = self.blocked(lambda data: {'id': 'd' * 32})
        observer = self.service(SimpleNamespace(workspace=self.workspace, evidence=self.evidence, mutation_lock=threading.Lock()))
        with patch.object(self.workspace, 'analyze', side_effect=operation):
            self.tasks.start({'kind': 'analysis', 'dataset': 'demo'})
            self.assertTrue(entered.wait(3))
            # Inspect the requested bound without spending five idle seconds.
            with patch.object(self.tasks._thread, 'join') as join:
                self.tasks.close()
                join.assert_called_once_with(timeout=5)
            self.assertTrue(self.tasks.is_running())
            self.assertTrue(self.app.mutation_lock.locked())
            with self.assertRaises(ReportTaskBusy):
                observer.start({'kind': 'analysis', 'dataset': 'demo'})
            release.set()
            self.assertEqual(self.wait()['task']['status'], 'succeeded')
        with self.assertRaises(InputError):
            self.tasks.start({'kind': 'analysis', 'dataset': 'demo'})
        self.assertFalse(self.app.mutation_lock.locked())

    def test_terminal_storage_error_is_uncertain_and_never_removes_published_report(self):
        ident = 'e' * 32
        published = self.workspace.root / 'reports' / ident
        published.mkdir()
        marker = published / 'run_manifest.json'
        marker.write_text('original-published-report', encoding='utf-8')
        save = self.tasks._save
        def writing(task):
            if task['status'] == 'succeeded':
                raise OSError('simulated terminal receipt storage failure')
            return save(task)
        with patch.object(self.workspace, 'analyze', return_value={'id': ident}) as analyze, patch.object(self.tasks, '_save', side_effect=writing):
            self.tasks.start({'kind': 'analysis', 'dataset': 'demo'})
            result = self.wait()['task']
            analyze.assert_called_once()
        self.assertEqual(result['status'], 'interrupted')
        self.assertEqual(result['report_id'], ident)
        self.assertEqual(result['error']['code'], 'storage_error')
        self.assertEqual(marker.read_text(encoding='utf-8'), 'original-published-report')
        self.assertEqual(json.loads(self.tasks.path.read_text(encoding='utf-8'))['task']['status'], 'running')
        observer = self.service(self.app)
        self.assertEqual(observer.state()['task']['status'], 'interrupted')
        self.assertFalse(self.app.mutation_lock.locked())
        durable = self.tasks.path.read_bytes()
        self.tasks.path.write_text('{broken', encoding='utf-8')
        with self.assertRaises(InputError):
            self.tasks.state()
        self.tasks.path.write_bytes(durable)
        next_report = 'f' * 32
        operation, entered, release = self.blocked(lambda data: {'id': next_report})
        with patch.object(self.workspace, 'analyze', side_effect=operation):
            accepted = observer.start({'kind': 'analysis', 'dataset': 'demo'})
            self.assertTrue(entered.wait(2))
            current = self.tasks.state()
            self.assertEqual(current['task']['id'], accepted['task']['id'])
            self.assertEqual(current['task']['status'], 'running')
            self.assertTrue(current['busy'])
            self.assertTrue(current['owned_elsewhere'])
            release.set()
            completed = self.wait(observer)
        self.assertEqual(self.tasks.state()['task'], completed['task'])
        self.assertEqual(completed['task']['report_id'], next_report)
        self.assertIsNone(self.tasks._uncertain)

    def test_thread_start_failure_and_invalid_receipt_release_mutation_lock(self):
        with patch('vibe_job_radar.report_tasks.threading.Thread.start', side_effect=RuntimeError('PRIVATE start detail')):
            with self.assertRaises(InputError):
                self.tasks.start({'kind': 'analysis', 'dataset': 'demo'})
        self.assertFalse(self.app.mutation_lock.locked())
        self.assertFalse(self.tasks.is_running())
        self.assertEqual(self.tasks.state()['task']['error']['code'], 'start_failed')
        self.assertNotIn('PRIVATE', self.tasks.path.read_text(encoding='utf-8'))
        self.tasks.path.write_text('{broken', encoding='utf-8')
        with self.assertRaises(InputError):
            self.tasks.start({'kind': 'analysis', 'dataset': 'demo'})
        self.assertFalse(self.app.mutation_lock.locked())
        self.assertEqual(self.tasks.path.read_text(encoding='utf-8'), '{broken')


if __name__ == '__main__':
    unittest.main()
