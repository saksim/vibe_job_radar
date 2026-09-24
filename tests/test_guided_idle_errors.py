"""Late browser failures are visible without another user action or replay."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from vibe_job_radar.guided.adapters import builtins, Registry
from vibe_job_radar.guided.browser import PlaywrightBackend
from vibe_job_radar.guided.contracts import CrawlError
from vibe_job_radar.guided.native_browser import NativeBackend
from vibe_job_radar.guided.service import GuidedService
from vibe_job_radar.workspace import Workspace


class IdleErrorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.service = GuidedService(Workspace(Path(self.tmp.name)), registry=Registry([builtins().get('liepin')]))
        self.addCleanup(self.service.close); self.service._submit = Mock()
        self.ident = self.service.create(dict(platform='liepin', keyword='时间序列', roles=['time_series'],
            consent=True, rights_note='Synthetic test', max_pages=1, max_jobs=2))['id']
        state = self.service._load(self.ident)
        self.service._save(state, 'manual_browser_open', status='waiting_manual', authentication='manual_pending')
        self.backend = Mock(); self.backend.pump.side_effect = CrawlError('native_page_cleared')
        self.service._backends[self.ident] = self.backend

    def test_late_blank_page_reports_without_click_or_network_retry(self):
        self.service._pump_idle()
        state = self.service._load(self.ident)
        self.assertEqual(state['code'], 'native_page_cleared')
        self.assertEqual(state['status'], 'waiting_manual')
        self.assertEqual(state['login_continuation'], 'needs_attention')
        self.assertFalse(state['auto_resume'])
        self.assertEqual(self.backend.method_calls, [('pump', (), {})])

    def test_unchanged_error_does_not_rewrite_file_every_idle_tick(self):
        self.service._pump_idle()
        with patch.object(self.service, '_save') as save:
            self.service._pump_idle(); save.assert_not_called()

    def test_new_ui_action_wins_while_pump_yields(self):
        def pump():
            self.service._busy = True
            raise CrawlError('native_page_cleared')
        self.backend.pump.side_effect = pump
        self.service._pump_idle(); self.service._busy = False
        self.assertEqual(self.service._load(self.ident)['code'], 'manual_browser_open')

    def test_pause_or_replaced_backend_wins_over_old_error(self):
        for replacement in (False, True):
            self.service._cancel.clear(); self.service._backends[self.ident] = self.backend
            def pump():
                if replacement: self.service._backends[self.ident] = Mock()
                else: self.service._cancel.set()
                raise CrawlError('native_page_cleared')
            self.backend.pump.side_effect = pump
            self.service._pump_idle()
            self.assertEqual(self.service._load(self.ident)['code'], 'manual_browser_open')

    def test_completed_report_is_not_relabelled_by_background_error(self):
        state = self.service._load(self.ident)
        self.service._save(state, 'completed', status='completed', report_id='a'*32)
        self.service._pump_idle()
        loaded = self.service._load(self.ident)
        self.assertEqual(loaded['code'], 'completed'); self.assertEqual(loaded['report_id'], 'a'*32)

    def test_unexpected_exception_text_is_not_persisted(self):
        self.backend.pump.side_effect = RuntimeError('PRIVATE_TEST_VALUE')
        self.service._pump_idle()
        self.assertEqual(self.service._load(self.ident)['code'], 'operation_error')
        self.assertNotIn('PRIVATE_TEST_VALUE', self.service._path(self.ident).read_text(encoding='utf-8'))


class NativePumpTests(unittest.TestCase):
    def test_fatal_event_during_pump_reaches_service(self):
        backend = object.__new__(NativeBackend)
        backend._closing = False; backend.error = None; backend.wait_error = None
        backend._drain_rejected_pages = Mock()
        def event(_): backend.error = 'native_page_cleared'
        with patch.object(PlaywrightBackend, 'pump', event), self.assertRaises(CrawlError) as caught:
            backend.pump()
        self.assertEqual(caught.exception.code, 'native_page_cleared')

    def test_normal_idle_page_needs_no_request_or_snapshot(self):
        backend = object.__new__(NativeBackend)
        backend._closing = False; backend.error = None; backend.wait_error = None
        backend._drain_rejected_pages = Mock()
        with patch.object(PlaywrightBackend, 'pump') as pump:
            backend.pump(); pump.assert_called_once()
