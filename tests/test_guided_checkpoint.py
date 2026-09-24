"""Persisted corruption/version changes must not silently resume browser work."""
import copy
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from vibe_job_radar.guided.adapters import Registry, builtins
from vibe_job_radar.guided.checkpoint import ensure_compatible
from vibe_job_radar.guided.contracts import CrawlError
from vibe_job_radar.guided.native_policy import contract_for
from vibe_job_radar.guided.service import GuidedService
from vibe_job_radar.workspace import Workspace, InputError
from test_automatic_collection import MemoryBackend

ADAPTER = builtins().get('liepin')


class CheckpointTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.workspace = Workspace(Path(self.tmp.name))
        self.factory = Mock(side_effect=MemoryBackend)
        self.service = GuidedService(self.workspace, registry=Registry([ADAPTER]), backend_factory=self.factory)
        self.addCleanup(self.service.close)
        self.service._submit = Mock()

    def create(self, **extra):
        result = self.service.create(dict(platform='liepin', keyword='时间序列', roles=['time_series'],
            consent=True, rights_note='Synthetic test', max_pages=1, max_jobs=2, **extra))
        return self.service._load(result['id'])

    def write(self, state):
        path = self.service._path(state['id'])
        path.write_text(json.dumps(state), encoding='utf-8')
        return path

    def test_new_binding_survives_success_selection_and_report(self):
        state = self.create(auto_collect=True)
        binding = copy.deepcopy(state['execution_binding'])
        self.service._run('search', state, None)
        loaded = self.service._load(state['id'])
        self.assertEqual(loaded['execution_binding'], binding)
        ensure_compatible(loaded, ADAPTER)
        self.assertEqual(loaded['outcome']['saved'], 2)

    def test_changed_query_budget_roles_or_rights_stop_before_backend(self):
        for changes in ({'keyword':'other'}, {'roles':['ai_product']}, {'max_jobs':3},
                        {'search_url':ADAPTER.search_url('other')}, {'rights_note':'changed'},
                        {'auto_collect':True}, {'identity_strategy':'observed_url_v1'}):
            state = self.create(); state.update(changes)
            with self.subTest(changes=changes), self.assertRaises(CrawlError) as caught:
                self.service._run('resume', state, None)
            self.assertEqual(caught.exception.code, 'checkpoint_incompatible')
        self.factory.assert_not_called()

    def test_adapter_upgrade_does_not_reinterpret_existing_selection(self):
        state = self.create()
        self.service._run('search', state, None)
        before = copy.deepcopy(state['cards'])
        self.service.registry = Registry([replace(ADAPTER, version='next')])
        self.factory.reset_mock()
        with self.assertRaises(CrawlError) as caught: self.service._run('resume', state, None)
        self.assertEqual(caught.exception.code, 'checkpoint_incompatible')
        self.assertEqual(state['cards'], before)
        self.factory.assert_not_called()

    def test_native_contract_changes_require_a_new_compatible_task(self):
        state = self.create(backend='native', native_consent=True)
        contract = contract_for(ADAPTER)
        changed = replace(ADAPTER, native_contract=replace(contract, rules=contract.rules[:-1]))
        with self.assertRaises(CrawlError) as caught: ensure_compatible(state, changed)
        self.assertEqual(caught.exception.code, 'checkpoint_incompatible')

    def test_unversioned_old_task_keeps_explicit_compatibility_status(self):
        state = self.create(); state.pop('execution_binding'); state.pop('identity_strategy')
        self.write(state)
        ensure_compatible(state, ADAPTER)
        item = self.service.state()['jobs'][0]
        self.assertEqual(item['checkpoint_compatibility'], 'legacy_unversioned')
        self.assertNotIn('execution_binding', self.service._load(state['id']))

    def test_bad_json_is_visible_without_echoing_or_overwriting_contents(self):
        state = self.create(); path = self.service._path(state['id'])
        raw = '{INVALID PRIVATE_TEST_VALUE'
        path.write_text(raw, encoding='utf-8')
        view = self.service.state()
        self.assertEqual(view['jobs'], [])
        self.assertTrue(view['checkpoint_warnings'])
        self.assertNotIn('PRIVATE_TEST_VALUE', json.dumps(view))
        self.assertEqual(path.read_text(encoding='utf-8'), raw)

    def test_wrong_id_duplicate_keys_and_future_schema_are_not_loaded(self):
        state = self.create(); path = self.service._path(state['id'])
        bad = [json.dumps({**state, 'id':'0'*32}),
               json.dumps({**state, 'schema_version':2}),
               json.dumps({**state, 'schema_version':True}),
               json.dumps(state)[:-1]+',"status":"running"}', 'null', '[]']
        for raw in bad:
            path.write_text(raw, encoding='utf-8')
            with self.subTest(raw=raw[:40]), self.assertRaises(InputError): self.service._load(state['id'])
            self.assertEqual(path.read_text(encoding='utf-8'), raw)

    def test_unknown_checkpoint_binding_version_rejected(self):
        state = self.create(); state['execution_binding']['version'] = 2
        self.write(state)
        with self.assertRaises(InputError): self.service._load(state['id'])

    def test_oversized_task_does_not_become_an_empty_new_task(self):
        state = self.create(); path = self.service._path(state['id'])
        raw = json.dumps({**state, 'unexpected':'x'*2_000_000})
        path.write_text(raw, encoding='utf-8')
        with self.assertRaises(InputError): self.service._load(state['id'])
        self.assertEqual(path.stat().st_size, len(raw))

    def test_dangling_selection_and_duplicate_cards_are_preserved_as_corruption(self):
        state = self.create(); self.service._run('search', state, None)
        for changes in ({'selection':['missing']}, {'cards':state['cards']*2},
                        {'selection':[state['cards'][0]['id']]*2}):
            broken = {**state, **changes}; path = self.write(broken); before = path.read_bytes()
            with self.subTest(changes=changes), self.assertRaises(InputError): self.service._load(state['id'])
            self.assertEqual(path.read_bytes(), before)

    def test_saved_success_requires_a_record_reference(self):
        state = self.create(); self.service._run('search', state, None)
        state['cards'][0]['status'] = 'ok'; self.write(state)
        with self.assertRaises(InputError): self.service._load(state['id'])

    def test_legacy_foreign_url_cannot_grant_browser_access(self):
        state = self.create(); self.service._run('search', state, None)
        state.pop('execution_binding')
        state['cards'][0]['url'] = 'https://outside.test/job/1.shtml'
        with self.assertRaises(CrawlError): ensure_compatible(state, ADAPTER)

    def test_stopping_incompatible_task_remains_available(self):
        state = self.create(); state['keyword'] = 'changed'
        self.service._run('close', state, None)
        self.assertEqual(state['status'], 'stopped')
        self.factory.assert_not_called()

    def test_missing_saved_record_stops_before_refetch_and_keeps_report(self):
        import sqlite3
        from contextlib import closing
        state = self.create(auto_collect=True); self.service._run('search', state, None)
        previous_report = state['report_id']
        with closing(sqlite3.connect(self.workspace.db)) as db:
            db.execute('DELETE FROM records WHERE record_id=?', (state['cards'][0]['record_id'],))
            db.commit()
        backend = self.service._backends[state['id']]
        calls = list(backend.calls)
        state['phase'] = 'collect'
        with self.assertRaises(CrawlError) as caught: self.service._run('resume', state, None)
        self.assertEqual(caught.exception.code, 'checkpoint_records_missing')
        self.assertEqual(backend.calls, calls)
        self.assertEqual(state['report_id'], previous_report)
        self.assertTrue(self.workspace.report(previous_report))

    def test_saved_text_hash_mismatch_is_not_counted_as_success(self):
        state = self.create(auto_collect=True); self.service._run('search', state, None)
        state['cards'][0]['body_sha256'] = 'f'*64
        with self.assertRaises(CrawlError) as caught: self.service._selected_records(state)
        self.assertEqual(caught.exception.code, 'checkpoint_records_missing')

    def test_reader_holds_service_lock_until_file_handle_is_closed(self):
        import threading
        from vibe_job_radar.guided.checkpoint import decode
        state = self.create(); reading = threading.Event(); release = threading.Event()
        attempted = threading.Event(); saved = threading.Event(); errors = []
        def read(path, ident):
            reading.set()
            if not release.wait(3): raise AssertionError('reader not released')
            return decode(path, ident)
        def reader():
            try: self.service._load(state['id'])
            except Exception as exc: errors.append(exc)
        def writer():
            attempted.set()
            try: self.service._save(state, 'paused', status='paused'); saved.set()
            except Exception as exc: errors.append(exc)
        with patch('vibe_job_radar.guided.service.decode_checkpoint', read):
            read_thread = threading.Thread(target=reader); read_thread.start()
            try:
                self.assertTrue(reading.wait(2))
                write_thread = threading.Thread(target=writer); write_thread.start()
                self.assertTrue(attempted.wait(2))
                self.assertFalse(saved.wait(.05))
            finally:
                release.set(); read_thread.join(3)
                if 'write_thread' in locals(): write_thread.join(3)
        self.assertFalse(errors); self.assertTrue(saved.is_set())
        self.assertEqual(self.service._load(state['id'])['status'], 'paused')
