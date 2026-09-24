"""HTTP failure -> confirmed browser task; all site content is a fixture."""
import copy
import http.client
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from vibe_job_radar.collection import Collector
from vibe_job_radar.collection_handoff import CollectionHandoff
from vibe_job_radar.guided.adapters import builtins
from vibe_job_radar.guided.contracts import PageSnapshot
from vibe_job_radar.guided.service import GuidedService
from vibe_job_radar.guided.ownership import GuidedTaskBusy
from vibe_job_radar.store import Store
from vibe_job_radar.workbench import LocalServer
from vibe_job_radar.workspace import Workspace, InputError

A = 'https://www.zhipin.com/job_detail/fixture-a.html'
B = 'https://www.zhipin.com/job_detail/fixture-b.html'
C = 'https://www.zhipin.com/job_detail/fixture-c.html'


class DetailBackend:
    opens = []
    def __init__(self, *args): pass
    def open(self, url, **kwargs):
        self.opens.append(url)
        return PageSnapshot(url, '<h1>时间序列算法工程师</h1><div class="job-sec-text">'
                            '人工测试数据，不是市场职位。要求使用 Cursor 进行 AI 辅助编程，编写单元测试和代码审查。'
                            '</div>')
    def pump(self): pass
    def close(self): pass


class HandoffTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.workspace = Workspace(tmp.name)
        self.collector = Collector(self.workspace)
        self.guided = GuidedService(self.workspace, backend_factory=DetailBackend)
        self.addCleanup(self.guided.close)
        self.bridge = CollectionHandoff(self.collector, self.guided)
        self.state = self.collector.start({'mode': 'urls', 'platforms': ['boss'], 'permit_platforms': ['boss'],
            'roles': ['time_series'], 'urls': A+'\n'+B+'\n'+C, 'detail_budget': 2,
            'rights_note': '仅限人工测试与本机报告', 'consent': True})
        self.state['details'][0]['status'] = 'redirect_login_required'
        self.state['details'][1]['status'] = 'ok'
        self.state['details'][2]['status'] = 'budget_skipped'
        self.state.update(status='needs_attention', phase='report', detail_attempts=2,
                          blocked_hosts=['www.zhipin.com'])
        self.collector._save(self.state)

    def save(self): self.collector._save(self.state)
    def preview(self): return self.bridge.handoff_preview({'id': self.state['id']})
    def request(self, **changes):
        value = {'id': self.state['id'], 'fingerprint': self.preview()['fingerprint'],
                 'platform': 'boss', 'indices': [0], 'consent': True}
        value.update(changes); return value

    def test_preview_offline_and_excludes_saved_and_budget_items(self):
        before = self.collector._path(self.state['id']).read_bytes()
        with patch('socket.create_connection', side_effect=AssertionError('no network')):
            p = self.preview()
        self.assertEqual([x['index'] for x in p['groups'][0]['items']], [0])
        self.assertEqual(p['budget'], {'used': 2, 'limit': 2, 'remaining': 0})
        self.assertFalse(p['network_started']); self.assertEqual(p['rights_note'], self.state['rights_note'])
        self.assertEqual(before, self.collector._path(self.state['id']).read_bytes())
        self.assertEqual(list(self.guided.root.glob('*.json')), [])

    def test_requires_explicit_consent_before_task_or_browser(self):
        for value in (False, None, 1, 'true'):
            with self.subTest(value=value), self.assertRaises(InputError):
                self.bridge.handoff_start(self.request(consent=value))
        self.assertEqual(list(self.guided.root.glob('*.json')), [])

    def test_creates_selected_detail_task_not_new_search(self):
        before = self.collector._path(self.state['id']).read_bytes()
        with patch.object(self.guided, '_submit') as submit:
            result = self.bridge.handoff_start(self.request())
        submit.assert_called_once_with('collect', result['id'])
        child = self.guided._load(result['id'])
        self.assertEqual(child['phase'], 'collect'); self.assertEqual(child['search_url'], A)
        self.assertEqual(child['roles'], self.state['roles']); self.assertEqual(child['max_jobs'], 1)
        self.assertEqual(child['rights_note'], self.state['rights_note'])
        self.assertEqual(child['handoff']['http_budget']['remaining'], 0)
        self.assertEqual(child['handoff']['additional_browser_budget'], 1)
        self.assertEqual(before, self.collector._path(self.state['id']).read_bytes())
        self.assertEqual([x['url'] for x in child['cards']], [A])

    def test_duplicate_confirmation_never_submits_twice(self):
        data = self.request()
        with patch.object(self.guided, '_submit') as submit:
            one = self.bridge.handoff_start(data); two = self.bridge.handoff_start(data)
        self.assertEqual(one['id'], two['id']); self.assertTrue(two['reused'])
        self.assertEqual(submit.call_count, 1)
        self.assertEqual(self.preview()['groups'][0]['existing_url'], one['url'])

    def test_handoff_respects_foreign_browser_owner_before_child_creation(self):
        other=GuidedService(self.workspace,backend_factory=DetailBackend)
        self.addCleanup(other.close)
        other._ownership.acquire()
        before=self.collector._path(self.state['id']).read_bytes()
        with patch.object(self.guided,'_submit') as submit:
            with self.assertRaises(GuidedTaskBusy):self.bridge.handoff_start(self.request())
            submit.assert_not_called()
        self.assertEqual(list(self.guided.root.glob('*.json')),[])
        self.assertEqual(self.collector._path(self.state['id']).read_bytes(),before)

    def test_handoff_holds_claim_across_child_save_and_enqueue(self):
        other=GuidedService(self.workspace,backend_factory=DetailBackend)
        self.addCleanup(other.close);observed=[]
        def queued(*args):
            with self.assertRaises(GuidedTaskBusy):other._ownership.acquire()
            observed.append(True)
        with patch.object(self.guided,'_submit',side_effect=queued):
            self.bridge.handoff_start(self.request())
        self.assertEqual(observed,[True])
        other._ownership.acquire()  # No queue/backend was created by this fixture.

    def test_existing_task_survives_service_restart_without_replay(self):
        data = self.request()
        with patch.object(self.guided, '_submit'):
            result = self.bridge.handoff_start(data)
        other = GuidedService(self.workspace, backend_factory=DetailBackend); self.addCleanup(other.close)
        with patch.object(other, '_submit') as submit:
            repeat = CollectionHandoff(self.collector, other).handoff_start(data)
        self.assertEqual(repeat['id'], result['id']); submit.assert_not_called()

    def test_overlapping_selection_cannot_create_another_task(self):
        self.state['details'][2]['status'] = 'parse_error'; self.save()
        with patch.object(self.guided, '_submit'):
            self.bridge.handoff_start(self.request())
            with self.assertRaises(InputError): self.bridge.handoff_start(self.request(indices=[0, 2]))
        self.assertEqual(len(list(self.guided.root.glob('*.json'))), 1)

    def test_changed_source_invalidates_confirmation(self):
        data = self.request(); self.state['rights_note'] = 'changed'; self.save()
        with self.assertRaises(InputError): self.bridge.handoff_start(data)
        self.assertEqual(list(self.guided.root.glob('*.json')), [])

    def test_known_refusals_stop_whole_host_even_if_another_row_needs_login(self):
        for status in ('http_401', 'http_403', 'http_429', 'robots_denied', 'robots_unavailable',
                       'tls_verification_failed', 'interrupted_uncertain', 'redirect_verification_required',
                       'login_or_challenge', 'host_circuit_open'):
            with self.subTest(status=status):
                self.state['details'][2]['status'] = status; self.save()
                self.assertEqual(self.preview()['groups'], [])
                with self.assertRaises(InputError): self.bridge.handoff_start(self.request())

    def test_unknown_stop_or_missing_permission_cannot_be_overridden(self):
        self.state['details'][0]['status'] = 'parse_error'; self.save()
        self.assertEqual(self.preview()['groups'], [])
        self.state['blocked_hosts'] = []; self.state['permit_platforms'] = []; self.save()
        self.assertEqual(self.preview()['groups'], [])

    def test_scope_indices_types_duplicates_and_budget_are_server_validated(self):
        for change in ({'indices': [1]}, {'indices': [2]}, {'indices': [-1]}, {'indices': [True]},
                       {'indices': []}, {'indices': [0, 0]}, {'indices': list(range(21))},
                       {'indices': ['0']}, {'platform': 'liepin'}):
            with self.subTest(change=change), self.assertRaises(InputError):
                self.bridge.handoff_start(self.request(**change))
        self.assertEqual(list(self.guided.root.glob('*.json')), [])

    def test_no_client_urls_credentials_or_rights_override(self):
        for field in ('url', 'urls', 'password', 'cookie', 'api_key', 'headers', 'rights_note', 'roles'):
            with self.subTest(field=field), self.assertRaises(InputError) as error:
                self.bridge.handoff_start(self.request(**{field: 'DO-NOT-LEAK'}))
            self.assertNotIn('DO-NOT-LEAK', str(error.exception))
        with self.assertRaises(InputError): self.bridge.handoff_preview({'id': self.state['id'], 'url': A})

    def test_unsupported_and_credential_detail_urls_never_transferred(self):
        for url in ('https://www.zhipin.com/web/user/', A+'?token=SECRET',
                    'https://www.zhipin.com.evil.test/job_detail/a.html', 'https://u:SECRET@www.zhipin.com/job_detail/a.html'):
            self.state['details'][0]['url'] = url; self.save()
            with self.subTest(url=url): self.assertEqual(self.preview()['groups'], [])

    def test_normalized_saved_urls_excluded(self):
        self.state['details'][1]['url'] = A+'?utm_campaign=tracking'; self.save()
        self.assertEqual(self.preview()['groups'], [])

    def test_inflight_and_feed_and_bad_rights_stop_without_writes(self):
        original = copy.deepcopy(self.state)
        for change in ({'in_flight': {'queue': 'details', 'index': 0}}, {'status': 'running'},
                       {'mode': 'feed'}, {'rights_note': ''}, {'roles': ['unknown']}):
            self.state = {**copy.deepcopy(original), **change}; self.save()
            with self.subTest(change=change), self.assertRaises(InputError): self.preview()

    def test_busy_browser_does_not_leave_orphan_child(self):
        self.guided._busy = True
        with self.assertRaises(InputError): self.bridge.handoff_start(self.request())
        self.guided._busy = False
        self.assertEqual(list(self.guided.root.glob('*.json')), [])

    def test_platform_groups_and_confirmation_cannot_mix_sources(self):
        self.state['platforms'].append('liepin'); self.state['permit_platforms'].append('liepin')
        self.state['details'].append({'url': 'https://www.liepin.com/job/fixture.shtml',
            'platform': 'liepin', 'status': 'parse_error', 'record_id': ''}); self.save()
        self.assertEqual({g['platform'] for g in self.preview()['groups']}, {'boss', 'liepin'})
        with self.assertRaises(InputError): self.bridge.handoff_start(self.request(indices=[0, 3]))
        with patch.object(self.guided, '_submit'):
            result = self.bridge.handoff_start(self.request(platform='liepin', indices=[3]))
        self.assertEqual(self.guided._load(result['id'])['platform'], 'liepin')

    def test_existing_conflicting_record_never_overwritten(self):
        data = self.request()
        with patch.object(self.guided, '_submit'):
            result = self.bridge.handoff_start(data)
        path = self.guided._path(result['id']); child = json.loads(path.read_text(encoding='utf-8'))
        child['handoff']['parent_id'] = 'another'; path.write_text(json.dumps(child, ensure_ascii=False), encoding='utf-8')
        before = path.read_bytes()
        with self.assertRaises(InputError): self.preview()
        self.assertEqual(before, path.read_bytes())

    def test_rights_note_not_truncated_to_make_handoff_pass(self):
        self.state['rights_note'] = '条' * 2001; self.save()
        with self.assertRaises(InputError): self.preview()
        self.assertEqual(self.collector._load(self.state['id'])['rights_note'], self.state['rights_note'])

    def test_parent_secrets_diagnostics_not_copied(self):
        self.state['private_debug'] = 'DO-NOT-COPY'
        self.state['details'][0]['fetch_diagnostic'] = {'private': 'DO-NOT-COPY'}; self.save()
        with patch.object(self.guided, '_submit'):
            r = self.bridge.handoff_start(self.request())
        self.assertNotIn('DO-NOT-COPY', self.guided._path(r['id']).read_text(encoding='utf-8'))
        self.assertNotIn('DO-NOT-COPY', json.dumps(self.preview()))

    def test_non_ascii_scope_round_trips_under_legacy_default(self):
        self.state['rights_note'] = '本地研究：中文、café、设计 🧪'
        self.save()
        original = Path.read_text

        def legacy_default(path, *args, **kwargs):
            # Reproduce Windows' legacy default only for missing encodings;
            # explicit UTF-8 in the product and test must still take effect.
            if not args and kwargs.get('encoding') is None:
                kwargs['encoding'] = 'cp1252'
            return original(path, *args, **kwargs)

        with patch.object(Path, 'read_text', legacy_default), patch.object(self.guided, '_submit'):
            result = self.bridge.handoff_start(self.request())
            child = self.guided._load(result['id'])
            self.assertEqual(child['rights_note'], self.state['rights_note'])
            self.assertTrue(self.bridge.handoff_start(self.request())['reused'])
            self.assertEqual(self.preview()['rights_note'], self.state['rights_note'])
        raw = self.guided._path(result['id']).read_bytes()
        self.assertIn(self.state['rights_note'].encode('utf-8'), raw)
        self.assertEqual(json.loads(raw.decode('utf-8'))['rights_note'], self.state['rights_note'])

    def test_actual_worker_produces_isolated_report_without_redoing_saved_row(self):
        DetailBackend.opens = []
        # Wait for the actual queue completion, not a five-second snapshot of
        # _busy. On Windows the JD can be saved while its report is still writing.
        # This bound is test synchronization only, never a source retry or a
        # change to the application's rate/timeout policy.
        finished = threading.Event()
        task_done = self.guided._queue.task_done
        def signal_done():
            task_done()
            finished.set()
        with patch.object(self.guided._queue, 'task_done', side_effect=signal_done):
            r = self.bridge.handoff_start(self.request())
            self.assertTrue(finished.wait(30), 'handoff worker did not finish')
        child = self.guided._load(r['id'])
        self.assertEqual(child['status'], 'completed', child)
        self.assertEqual(DetailBackend.opens, [A]); self.assertTrue(child['report_id'])
        with Store(self.workspace.db) as store:
            self.assertEqual(len(store.records(latest_only=False)), 1)
        before = list(DetailBackend.opens)
        self.assertTrue(self.bridge.handoff_start(self.request())['reused'])
        self.assertEqual(before, DetailBackend.opens)

    def test_saved_detail_is_not_worker_completion_until_report_finishes(self):
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()
        report, task_done = self.guided._finalize_report, self.guided._queue.task_done
        def held_report(*args, **kwargs):
            entered.set()
            if not release.wait(30):
                raise RuntimeError('test report gate did not release')
            return report(*args, **kwargs)
        def signal_done():
            task_done()
            finished.set()
        with patch.object(self.guided, '_finalize_report', side_effect=held_report), \
                patch.object(self.guided._queue, 'task_done', side_effect=signal_done):
            result = self.bridge.handoff_start(self.request())
            try:
                self.assertTrue(entered.wait(30), 'worker did not reach report')
                child = self.guided._load(result['id'])
                self.assertEqual(child['cards'][0]['status'], 'ok')
                self.assertEqual(child['status'], 'running')
                self.assertFalse(child['report_id'])
                self.assertFalse(finished.is_set())
            finally:
                release.set()
            self.assertTrue(finished.wait(30), 'worker did not finish its real report')
        child = self.guided._load(result['id'])
        self.assertEqual(child['status'], 'completed', child)
        self.assertTrue(child['report_id'])


class HandoffHTTPTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.server = LocalServer(Workspace(self.tmp.name))
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': .01}, daemon=True)
        self.thread.start(); self.addCleanup(self.close)
        task = self.server.collector.start({'mode': 'urls', 'platforms': ['boss'], 'permit_platforms': ['boss'],
            'roles': ['architect'], 'urls': A, 'detail_budget': 1, 'rights_note': '人工测试', 'consent': True})
        task['details'][0]['status'] = 'parse_error'; self.server.collector._save(task); self.ident = task['id']

    def close(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join(timeout=3)

    def call(self, path, data, *, token=True, origin=None):
        conn = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=3)
        try:
            headers = {'Content-Type': 'application/json'}
            if token: headers['X-Radar-Token'] = self.server.token
            if origin: headers['Origin'] = origin
            conn.request('POST', path, body=json.dumps(data).encode(), headers=headers)
            r = conn.getresponse(); return r.status, json.loads(r.read())
        finally: conn.close()

    def test_preview_and_confirmation_through_real_local_api(self):
        status, p = self.call('/api/collection/handoff_preview', {'id': self.ident})
        self.assertEqual(status, 200)
        with patch.object(self.server.guided, '_submit') as submit:
            status, child = self.call('/api/collection/handoff_start', {'id': self.ident,
                'fingerprint': p['fingerprint'], 'platform': 'boss', 'indices': [0], 'consent': True})
        self.assertEqual(status, 200); submit.assert_called_once_with('collect', child['id'])

    def test_authentication_and_origin_apply_to_both_endpoints(self):
        for name in ('handoff_preview', 'handoff_start'):
            path = '/api/collection/' + name
            self.assertEqual(self.call(path, {'id': self.ident}, token=False)[0], 403)
            self.assertEqual(self.call(path, {'id': self.ident}, origin='https://evil.test')[0], 403)
