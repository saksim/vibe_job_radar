"""Actual worker/cache/report lifecycle; artificial upstream, never market data."""
import copy
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from unittest.mock import Mock, patch

from test_local_public import payload, query
from test_core import job
from test_redirect_recovery import fixture_payload
import test_workbench as http_fixtures
from vibe_job_radar.local_public import API_URL, SOURCE, LocalPublicDataClient
from vibe_job_radar.network import FetchError
from vibe_job_radar.public_example import PublicExample
from vibe_job_radar.public_tasks import PublicTasks
from vibe_job_radar.store import Store
from vibe_job_radar.workspace import InputError, Workspace


class PublicLifecycleTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.workspace = Workspace(tmp.name)
        self.now = [time.time()]
        self.transport = Mock()
        self.transport.json.return_value = payload()
        self.client = LocalPublicDataClient(self.workspace, transport=self.transport, clock=lambda:self.now[0])
        self.tasks = PublicTasks(self.workspace, hybrid_client=self.client)
        self.addCleanup(self.tasks.close)
        self.entered, self.release = threading.Event(), threading.Event()
        self.addCleanup(self.release.set)

    def wait(self, tasks=None):
        tasks = tasks or self.tasks
        tasks._thread.join(timeout=10)
        self.assertFalse(tasks._thread.is_alive(), 'worker did not finish')
        return tasks.state()['task']

    def hold_request(self, url):
        self.entered.set()
        if not self.release.wait(10):
            raise AssertionError('test did not release fixture transport')
        return payload()

    def cancelled_search(self):
        self.transport.json.side_effect = self.hold_request
        task = self.tasks.search({'consent':True, 'query':query().payload()})
        self.assertTrue(self.entered.wait(5))
        self.tasks.cancel({'id':task['id']})
        self.assertEqual(self.tasks.state()['task']['status'], 'cancelling')
        self.release.set()
        self.assertEqual(self.wait()['status'], 'cancelled')
        return task['id']

    def test_cancel_inflight_preserves_cache_then_confirmed_resume_reuses_it(self):
        ident = self.cancelled_search()
        with Store(self.workspace.db) as store:
            self.assertEqual(store.records(), [])
        self.assertEqual(list((self.workspace.root/'reports').iterdir()), [])
        state = self.tasks.state()['task']
        self.assertTrue(state['can_resume'])
        self.assertFalse(state['can_cancel'])
        self.assertEqual(state['report_id'], '')
        self.transport.json.assert_called_once_with(API_URL)
        self.tasks.resume({'id':ident, 'consent':True})
        complete = self.wait()
        self.assertEqual((complete['status'], complete['id'], complete['attempt']), ('completed', ident, 2))
        self.assertEqual(complete['query'], query().payload())
        self.assertTrue(complete['cache_reused'])
        self.assertEqual(complete['network_requests_this_click'], 0)
        self.transport.json.assert_called_once_with(API_URL)
        self.assertEqual(complete['returned_jobs'], 2)
        with Store(self.workspace.db) as store:
            self.assertEqual(len(store.records()), 2)
        # Identical artificial postings remain two stored source rows; the
        # original analysis intentionally groups their identical text once.
        self.assertEqual(self.workspace.report(complete['report_id'])['manifest']['stats']['full_text_job_groups'], 1)

    def test_report_needs_no_temporary_disk_database_and_keeps_exact_batch(self):
        previous = job(text='另一任务已保存的原始正文，不属于当前公开来源批次。')
        with Store(self.workspace.db) as stored:
            stored.add(previous)
        def without_staging_disk(path):
            if str(path) != ':memory:' and Path(path) != self.workspace.db:
                raise OSError('fixture temporary database storage unavailable')
            return Store(path)
        with patch('vibe_job_radar.public_tasks.Store', side_effect=without_staging_disk):
            self.tasks.search({'consent': True, 'query': query().payload()})
            complete = self.wait()
        self.assertEqual(complete['status'], 'completed')
        folder = self.workspace.root / 'reports' / complete['report_id']
        jobs = [json.loads(line) for line in (folder / 'jobs.jsonl').read_text(encoding='utf-8').splitlines()]
        self.assertEqual(len(jobs), 2)
        self.assertNotIn(previous.record_id, {r['record_id'] for r in jobs})
        with Store(self.workspace.db) as stored:
            self.assertEqual(len(stored.records()), 3)
            self.assertIn(previous, stored.records())
        self.transport.json.assert_called_once_with(API_URL)

    def test_restart_is_offline_and_requires_confirmation_of_saved_query(self):
        ident = self.cancelled_search()
        self.tasks._save(status='running')  # Persisted process-exit checkpoint.
        self.tasks.close()
        restarted = PublicTasks(self.workspace, hybrid_client=LocalPublicDataClient(
            self.workspace, transport=self.transport, clock=lambda:self.now[0]))
        self.addCleanup(restarted.close)
        self.assertEqual(restarted.state()['task']['status'], 'interrupted')
        self.assertTrue(restarted.state()['task']['can_resume'])
        self.assertIsNone(restarted._thread)
        self.transport.json.assert_called_once_with(API_URL)
        for data in ({'id':ident}, {'id':ident,'consent':False},
                     {'id':'0'*32,'consent':True}, {'id':ident,'consent':True,'query':query(query='Engineer').payload()}):
            with self.subTest(data=data), self.assertRaises(InputError):
                restarted.resume(data)
        restarted.resume({'id':ident,'consent':True})
        complete = self.wait(restarted)
        self.assertEqual(complete['status'], 'completed')
        self.assertEqual(complete['query'], query().payload())
        self.transport.json.assert_called_once_with(API_URL)

    def test_cancel_before_worker_starts_does_not_request_or_store(self):
        real_run = self.tasks._run
        def held_run(*args):
            self.entered.set()
            self.release.wait(10)
            real_run(*args)
        with patch.object(self.tasks, '_run', side_effect=held_run):
            ident = self.tasks.search({'consent':True,'query':query().payload()})['id']
            self.assertTrue(self.entered.wait(5))
            self.tasks.cancel({'id':ident})
            self.release.set()
            self.assertEqual(self.wait()['status'], 'cancelled')
        self.transport.json.assert_not_called()

    def test_cancel_after_local_commit_starts_retains_complete_report(self):
        real_import = self.tasks._import
        def held_import(*args):
            self.entered.set()
            self.release.wait(10)
            return real_import(*args)
        with patch.object(self.tasks, '_import', side_effect=held_import):
            ident = self.tasks.search({'consent':True,'query':query().payload()})['id']
            self.assertTrue(self.entered.wait(5))
            self.assertEqual(self.tasks.state()['task']['phase'], 'saving')
            self.tasks.cancel({'id':ident})
            self.release.set()
            state = self.wait()
        self.assertEqual(state['status'], 'completed')
        self.assertTrue(state['cancel_requested'])
        self.assertFalse(state['can_resume'])
        self.assertTrue(self.workspace.report(state['report_id']))
        self.transport.json.assert_called_once_with(API_URL)

    def test_stale_page_cannot_cancel_different_task(self):
        self.transport.json.side_effect = self.hold_request
        ident = self.tasks.search({'consent':True,'query':query().payload()})['id']
        self.assertTrue(self.entered.wait(5))
        for value in ({'id':'0'*32}, {'id':ident,'url':'https://bad.test'}, {'id':None}):
            with self.subTest(value=value), self.assertRaises(InputError):
                self.tasks.cancel(value)
        self.assertFalse(self.tasks._cancel.is_set())
        self.release.set()
        self.assertEqual(self.wait()['status'], 'completed')

    def test_changed_source_contract_blocks_resume_without_requests(self):
        ident = self.cancelled_search()
        self.client.registry = {SOURCE.key:replace(SOURCE,contract='changed reviewed contract')}
        self.assertFalse(self.tasks.state()['task']['can_resume'])
        with self.assertRaises(InputError):
            self.tasks.resume({'id':ident,'consent':True})
        self.transport.json.assert_called_once_with(API_URL)

    def test_changed_execution_mode_or_origin_blocks_resume(self):
        ident = self.cancelled_search()
        for attribute, value in (('origin','https://different.fixture.test'), ('execution_mode','remote_service')):
            with self.subTest(attribute=attribute), patch.object(self.client, attribute, value, create=True):
                self.assertFalse(self.tasks.state()['task']['can_resume'])
                with self.assertRaises(InputError):
                    self.tasks.resume({'id':ident,'consent':True})
        self.transport.json.assert_called_once_with(API_URL)

    def test_legacy_or_corrupt_resume_metadata_is_not_guessed(self):
        ident = self.cancelled_search()
        original = copy.deepcopy(self.tasks._state)
        for changes in ({'resume_binding':None}, {'attempt':'2'}, {'attempt':True},
                        {'attempt':0}, {'id':'not-a-task'}, {'query':query(query='Engineer').payload()}):
            self.tasks._save(**{**original, **changes})
            self.assertFalse(self.tasks.state()['task']['can_resume'])
        self.tasks._save(**original)
        self.assertTrue(self.tasks.state()['task']['can_resume'])
        self.assertEqual(self.tasks.state()['task']['id'], ident)

    def test_provider_denial_is_failed_and_not_resumable(self):
        self.transport.json.side_effect = FetchError('http_403')
        ident = self.tasks.search({'consent':True,'query':query().payload()})['id']
        state = self.wait()
        self.assertEqual(state['status'], 'failed')
        self.assertFalse(state['can_resume'])
        with self.assertRaises(InputError):
            self.tasks.resume({'id':ident,'consent':True})
        self.transport.json.assert_called_once_with(API_URL)

    def test_existing_report_retained_when_new_task_is_cancelled(self):
        self.tasks.search({'consent':True,'query':query().payload()})
        previous = self.wait()['report_id']
        manifest = (self.workspace.root/'reports'/previous/'run_manifest.json').read_bytes()
        self.now[0] += 601
        self.cancelled_search()
        self.assertEqual((self.workspace.root/'reports'/previous/'run_manifest.json').read_bytes(), manifest)
        self.assertTrue(self.workspace.report(previous))

    def test_example_cancellation_keeps_attempt_in_persistent_ledger(self):
        def held_example(url):
            self.entered.set()
            self.release.wait(10)
            return fixture_payload()
        transport = Mock(); transport.json.side_effect = held_example
        example = PublicExample(self.workspace, transport=transport)
        self.tasks.factory = lambda workspace:example
        ident = self.tasks.start({'consent':True})['id']
        self.assertTrue(self.entered.wait(5))
        self.tasks.cancel({'id':ident}); self.release.set()
        self.assertEqual(self.wait()['status'], 'cancelled')
        with closing(sqlite3.connect(example.ledger.path)) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM visits').fetchone()[0], 1)
        with Store(self.workspace.db) as store:
            self.assertEqual(store.records(), [])
        self.tasks.resume({'id':ident,'consent':True})
        self.assertEqual(self.wait()['status'], 'failed')  # Existing 30s quota still applies.
        self.assertEqual(transport.json.call_count, 1)

    def test_example_saved_extra_query_is_rejected(self):
        self.tasks._save(id='a'*32, status='interrupted', kind='example', query={},
                        attempt=1,resume_binding=self.tasks._binding('example', {}))
        self.assertTrue(self.tasks.state()['task']['can_resume'])
        self.tasks._save(query={'url':'https://unreviewed.fixture.test'})
        self.assertFalse(self.tasks.state()['task']['can_resume'])


class PublicLifecycleHTTPTests(unittest.TestCase):
    setUp = http_fixtures.HTTPTests.setUp
    tearDown = http_fixtures.HTTPTests.tearDown
    call = http_fixtures.HTTPTests.call

    def test_control_routes_require_existing_token_and_same_origin(self):
        for action in ('cancel','resume'):
            data = {'id':'a'*32, **({'consent':True} if action == 'resume' else {})}
            with self.subTest(action=action), patch.object(self.server.public_tasks, action,
                    return_value={'id':data['id']}) as dispatch:
                self.assertEqual(self.call('/api/public/'+action, data, authorized=False)[0], 403)
                self.assertEqual(self.call('/api/public/'+action, data,
                    headers={'Origin':'https://external.fixture.test'})[0], 403)
                dispatch.assert_not_called()
                self.assertEqual(self.call('/api/public/'+action, data)[0], 200)
                dispatch.assert_called_once_with(data)


if __name__ == '__main__':
    unittest.main()
