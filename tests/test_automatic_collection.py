"""Opt-in search -> selection -> existing report; all data is synthetic."""
import copy
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from vibe_job_radar.guided.adapters import builtins, Registry
from vibe_job_radar.guided.automatic_selection import select_ready_batch
from vibe_job_radar.guided.contracts import CrawlError, PageSnapshot
from vibe_job_radar.guided.rate import RateLedger, Limits, RateLimit
from vibe_job_radar.guided.service import GuidedService
from vibe_job_radar.store import Store
from vibe_job_radar.workspace import InputError, Workspace
from test_liepin_native_search import ObservedBackend, payload

ADAPTER = builtins().get('liepin')
SEARCH = ADAPTER.search_url('时间序列')
BODY = '岗位职责：负责时间序列算法和预测系统。任职要求：熟悉Python和统计学，编写单元测试与代码审查。独立人工测试，不是真实岗位。'


def ready():
    return {'auto_collect': True, 'status': 'ready', 'code': 'ready', 'phase': 'select',
            'max_jobs': 2, 'selection': [], 'cards': [
                {'id': str(n), 'status': 'discovered'} for n in range(3)]}


class AutomaticSelectionTests(unittest.TestCase):
    def test_exact_order_and_limit(self):
        state = ready(); before = copy.deepcopy(state)
        self.assertEqual(select_ready_batch(state), ['0', '1'])
        self.assertEqual(state, before)

    def test_not_enabled_by_default_or_truthy_values(self):
        for value in (False, None, 1, 'true', []):
            state = ready(); state['auto_collect'] = value
            with self.subTest(value=value): self.assertEqual(select_ready_batch(state), [])
        state = ready(); del state['auto_collect']
        self.assertEqual(select_ready_batch(state), [])

    def test_empty_failed_or_unready_does_not_use_stale_cards(self):
        for change in ({'code': 'no_matching_jobs'}, {'code': 'empty_list'},
                       {'code': 'manual_required'}, {'code': 'http_403'},
                       {'status': 'waiting_rate'}, {'status': 'running'},
                       {'phase': 'report'}, {'phase': 'collect'}):
            with self.subTest(change=change):
                self.assertEqual(select_ready_batch({**ready(), **change}), [])

    def test_never_overwrites_a_prior_selection(self):
        self.assertEqual(select_ready_batch({**ready(), 'selection': ['2']}), [])
        self.assertEqual(select_ready_batch({**ready(), 'auto_selection_applied': True}), [])

    def test_invalid_limits_fail(self):
        for value in (0, -1, 21, True, '2', None):
            with self.subTest(value=value), self.assertRaises(CrawlError):
                select_ready_batch({**ready(), 'max_jobs': value})

    def test_invalid_duplicate_or_failed_rows_not_silently_skipped(self):
        for rows in (None, [None], [{'id': '', 'status': 'discovered'}],
                     [{'id': 'a', 'status': 'discovered'}] * 2,
                     [{'id': 'a', 'status': 'http_403'}]):
            with self.subTest(rows=rows), self.assertRaises(CrawlError):
                select_ready_batch({**ready(), 'cards': rows})

    def test_empty_cards_do_not_create_an_empty_report(self):
        self.assertEqual(select_ready_batch({**ready(), 'cards': []}), [])


class MemoryBackend:
    block = ''
    def __init__(self, *_args, **_kwargs):
        self.calls = []; self.page = None; self.auth_mode = False; self.closed = False
    def open(self, url, *, authentication=False):
        self.calls.append((url, authentication))
        if '/zhaopin/' in url:
            html = '<h1>合成列表</h1>' + ''.join(
                f'<a href="/job/{n}.shtml">时间序列算法工程师 {n}</a>' for n in (1, 2, 3))
        else:
            if self.block and '/job/2.shtml' in url:
                if self.block == 'rate_wait':
                    raise RateLimit(60, 'rate_wait', next_allowed_at=time.time() + 60)
                raise CrawlError(self.block)
            html = '<h1>时间序列算法工程师</h1><div class="job-description">'+BODY+'</div>'
        self.page = PageSnapshot(url, html); return self.page
    def snapshot(self): return self.page
    def next_page(self): return False
    def collection_mode(self): self.auth_mode = False
    def alive(self): return not self.closed
    def pump(self): pass
    def close(self): self.closed = True


class AutomaticCollectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.workspace = Workspace(Path(self.tmp.name))
        self.service = GuidedService(self.workspace, registry=Registry([ADAPTER]),
            backend_factory=MemoryBackend)
        self.addCleanup(self.service.close)
        self.service._submit = Mock()
        self.data = {'platform': 'liepin', 'keyword': '时间序列', 'roles': ['time_series'],
                     'max_pages': 1, 'max_jobs': 2, 'consent': True, 'rights_note': '人工测试',
                     'auto_collect': True}

    def create(self, **extra):
        result = self.service.create({**self.data, **extra})
        return self.service._load(result['id'])

    def test_one_search_action_saves_bounded_batch_and_report(self):
        state = self.create(); self.service._run('search', state, None)
        self.assertEqual(state['status'], 'completed')
        self.assertEqual(state['outcome']['saved'], 2)
        self.assertEqual(state['selection_source'], 'query_order')
        self.assertEqual(state['selection'], [r['id'] for r in state['cards'][:2]])
        self.assertEqual(state['cards'][2]['status'], 'discovered')
        self.assertTrue(state['auto_selection_applied'])
        backend = self.service._backends[state['id']]
        self.assertEqual([url for url, _ in backend.calls], [SEARCH,
            'https://www.liepin.com/job/1.shtml', 'https://www.liepin.com/job/2.shtml'])
        self.assertFalse(any(auth for _, auth in backend.calls))
        self.assertEqual(state['authentication'], 'not_checked')
        with Store(self.workspace.db) as store:
            self.assertEqual(len(store.records()), 2)
            self.assertTrue(all(r.text == BODY for r in store.records()))
        self.assertTrue(self.workspace.report(state['report_id']))

    def test_default_still_stops_at_selection(self):
        state = self.create(auto_collect=False); self.service._run('search', state, None)
        self.assertEqual(state['status'], 'ready'); self.assertEqual(state['selection'], [])
        self.assertEqual(len(self.service._backends[state['id']].calls), 1)

    def test_creation_option_is_strict_boolean(self):
        for value in (1, 'true', [], None):
            with self.subTest(value=value), self.assertRaises(InputError): self.create(auto_collect=value)

    def test_option_not_changed_via_action(self):
        state = self.create(auto_collect=False)
        with self.assertRaises(InputError):
            self.service.action({'id': state['id'], 'action': 'search', 'auto_collect': True})

    def test_api_only_search_uses_same_collector(self):
        state = self.create(max_jobs=1)
        with patch.object(self.service, 'factory', ObservedBackend):
            self.service._run('search', state, None)
        self.assertEqual(state['outcome']['saved'], 1)
        self.assertEqual(state['selection_source'], 'query_order')
        self.assertEqual(state['status'], 'completed')

    def test_confirmed_empty_does_not_select_or_login(self):
        state = self.create()
        with patch.object(self.service, 'factory', ObservedBackend), patch.object(ObservedBackend, 'data', payload([])):
            self.service._run('search', state, None)
        self.assertEqual(state['code'], 'no_matching_jobs')
        self.assertEqual(state['selection'], []); self.assertEqual(state['report_id'], '')
        self.assertFalse(state['auto_selection_applied'])

    def test_invalid_business_result_never_auto_collects(self):
        state = self.create()
        with patch.object(self.service, 'factory', ObservedBackend), patch.object(ObservedBackend, 'data', {'flag': 0}):
            with self.assertRaises(CrawlError): self.service._run('search', state, None)
        self.assertFalse(state['selection']); self.assertFalse(state['report_id'])

    def test_login_wall_remains_stopped_and_does_not_skip_to_third_job(self):
        state = self.create()
        with patch.object(MemoryBackend, 'block', 'manual_required'):
            with self.assertRaises(CrawlError) as error: self.service._run('search', state, None)
        self.assertEqual(error.exception.code, 'manual_required')
        partial = self.service._load(state['id'])
        self.assertEqual([r['status'] for r in partial['cards']], ['ok', 'manual_required', 'discovered'])
        self.assertEqual(partial['phase'], 'collect'); self.assertEqual(partial['outcome']['saved'], 1)
        self.assertTrue(partial['report_id'])
        backend = self.service._backends[state['id']]
        self.assertFalse(any(auth for _, auth in backend.calls))
        self.assertFalse(any('/job/3.shtml' in url for url, _ in backend.calls))

    def test_explicit_resume_preserves_selection_and_does_not_refetch_success(self):
        state = self.create()
        with patch.object(MemoryBackend, 'block', 'manual_required'):
            with self.assertRaises(CrawlError): self.service._run('search', state, None)
        selection = list(state['selection']); record = state['cards'][0]['record_id']; old_report = state['report_id']
        self.service._run('resume', state, None)
        self.assertEqual(state['selection'], selection)
        self.assertEqual(state['cards'][0]['record_id'], record)
        self.assertTrue(self.workspace.report(old_report))
        self.assertEqual(state['outcome']['saved'], 2)
        calls = self.service._backends[state['id']].calls
        self.assertEqual(sum(url == SEARCH for url, _ in calls), 1)
        self.assertEqual(sum('/job/1.shtml' in url for url, _ in calls), 1)

    def test_visible_challenge_stops_collection(self):
        state = self.create()
        backend = MemoryBackend(); backend.page = PageSnapshot(SEARCH,
            '<h1>合成页面</h1><div>登录后查看</div><a href="/job/1.shtml">岗位</a>')
        self.service._backends[state['id']] = backend
        with self.assertRaises(CrawlError) as error: self.service._run('capture', state, None)
        self.assertEqual(error.exception.code, 'manual_required')
        self.assertEqual(state['selection'], []); self.assertFalse(backend.calls)

    def test_hidden_login_template_does_not_block_real_collection(self):
        state = self.create()
        backend = MemoryBackend(); backend.page = PageSnapshot(SEARCH,
            '<h1>合成页面</h1><div hidden>登录后查看</div><a href="/job/1.shtml">岗位</a>')
        self.service._backends[state['id']] = backend
        self.service._run('capture', state, None)
        self.assertEqual(state['status'], 'completed')
        self.assertEqual(state['outcome']['saved'], 1)
        self.assertTrue(state['report_id'])
        with Store(self.workspace.db) as store:
            self.assertEqual([r.text for r in store.records()], [BODY])

    def test_original_query_after_manual_login_can_auto_collect(self):
        state = self.create()
        backend = MemoryBackend(); backend.open(SEARCH)
        self.service._backends[state['id']] = backend
        state['authentication'] = 'manual_pending'
        self.service._run('capture', state, None)
        self.assertEqual(state['status'], 'completed')
        self.assertEqual(state['authentication'], 'user_resumed')
        self.assertEqual(sum(url == SEARCH for url, _ in backend.calls), 1)
        self.assertFalse(any(auth for _, auth in backend.calls))

    def test_access_refusal_is_not_skipped_or_retried(self):
        state = self.create()
        with patch.object(MemoryBackend, 'block', 'http_403'):
            with self.assertRaises(CrawlError) as error:
                self.service._run('search', state, None)
        self.assertEqual(error.exception.code, 'http_403')
        calls = self.service._backends[state['id']].calls
        self.assertEqual(sum('/job/2.shtml' in url for url, _ in calls), 1)
        self.assertFalse(any('/job/3.shtml' in url or auth for url, auth in calls))

    def test_restart_then_explicit_resume_keeps_frozen_batch(self):
        state = self.create()
        with patch.object(MemoryBackend, 'block', 'manual_required'):
            with self.assertRaises(CrawlError): self.service._run('search', state, None)
        self.service.close()
        restarted = GuidedService(self.workspace, registry=Registry([ADAPTER]), backend_factory=MemoryBackend)
        try:
            persisted = restarted._load(state['id'])
            restarted._run('resume', persisted, None)
            calls = restarted._backends[state['id']].calls
            self.assertEqual([url for url, _ in calls], ['https://www.liepin.com/job/2.shtml'])
            self.assertEqual(persisted['selection'], state['selection'])
            self.assertEqual(persisted['outcome']['saved'], 2)
        finally: restarted.close()

    def test_cancel_before_selection_does_not_fetch(self):
        state = self.create()
        state.update(status='ready', code='ready', phase='select',
                     cards=[{'id':'one','status':'discovered'}])
        self.service._cancel.set()
        with patch.object(self.service, '_collect') as collect, self.assertRaises(CrawlError):
            self.service._auto_collect_ready(state, MemoryBackend(), ADAPTER)
        collect.assert_not_called(); self.assertEqual(state['selection'], [])

    def test_no_automatic_reselection_on_reread_after_batch(self):
        state = self.create(); self.service._run('search', state, None)
        backend = self.service._backends[state['id']]; before = len(backend.calls)
        state.update(status='ready', code='ready', phase='select')
        self.service._auto_collect_ready(state, backend, ADAPTER)
        self.assertEqual(len(backend.calls), before)

    def test_manual_selection_is_not_overwritten(self):
        state = self.create(auto_collect=False); self.service._run('search', state, None)
        chosen = state['cards'][2]['id']
        self.service.action({'id': state['id'], 'action': 'collect', 'selected': [chosen]})
        saved = self.service._load(state['id'])
        self.assertEqual(saved['selection_source'], 'manual')
        self.service._run('collect', saved, None)
        self.assertEqual(saved['outcome']['saved'], 1)
        self.assertEqual(saved['selection'], [chosen])


class AutomaticWorkerRecoveryTests(unittest.TestCase):
    def test_rate_wait_resumes_collection_not_search(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = Workspace(tmp)
            service = GuidedService(ws, registry=Registry([ADAPTER]), backend_factory=MemoryBackend,
                ledger=RateLedger(Path(tmp)/'rates.sqlite', Limits(page_interval=0, request_interval=0)))
            try:
                with patch.object(MemoryBackend, 'block', 'rate_wait'):
                    result = service.create({'platform':'liepin','keyword':'时间序列','roles':['time_series'],
                        'max_pages':1,'max_jobs':2,'consent':True,'rights_note':'人工测试','auto_collect':True})
                    until = time.monotonic() + 20
                    while service.state()['busy']:
                        if time.monotonic() > until: self.fail('worker did not finish')
                        time.sleep(.02)
                state = service._load(result['id'])
                self.assertEqual(state['status'], 'waiting_rate')
                self.assertEqual(state['retry_action'], 'resume')
                self.assertEqual(state['phase'], 'collect')
                self.assertEqual(state['outcome']['saved'], 1)
                self.assertEqual(len(state['selection']), 2)
                self.assertTrue(state['auto_resume'])
                self.assertTrue(service._cancel.is_set())
            finally: service.close()
