"""Regressions for the eight pre-merge review findings; never contact suppliers."""
from __future__ import annotations

import contextlib
import dataclasses
import io
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from test_guided import FakeBackend, fixture_adapter
from guided_wait_diagnostic import wait_diagnostic
from vibe_job_radar import __version__
from vibe_job_radar.cli import parser
from vibe_job_radar.guided.adapters import Registry
from vibe_job_radar.guided.browser import PlaywrightBackend
from vibe_job_radar.guided.contracts import CrawlError, PageSnapshot
from vibe_job_radar.guided.rate import Limits, RateLedger, RateLimit
from vibe_job_radar.guided.service import GuidedService
from vibe_job_radar.guided.transport import PinnedTransport, WireResponse
from vibe_job_radar.workspace import Workspace


class BrowserReviewTests(unittest.TestCase):
    def backend(self):
        b = object.__new__(PlaywrightBackend)
        b.adapter = fixture_adapter()
        b.page = SimpleNamespace(main_frame=object(), url='https://jobs.fixture.test/search')
        b._pagination_page = None
        b.cancelled = threading.Event()
        b.auth_mode = False
        b.error = None
        b.resource_denials = set()
        b.wire = Mock()
        b.wire.allowed_resource.return_value = True
        b._cookies = Mock()
        b._settle = Mock()
        b.redirects = 0
        return b

    def test_document_pagination_reserves_one_page(self):
        b = self.backend()
        button = Mock()
        button.get_attribute.return_value = ''
        b._visible = Mock(return_value=button)
        button.click.side_effect = lambda **_: b._reserve_document_page(SimpleNamespace(frame=b.page.main_frame))
        self.assertTrue(b.next_page())
        self.assertEqual(b.wire.reserve.call_args_list, [(('page',),)])
        self.assertIsNone(b._pagination_page)
        b._reserve_document_page(SimpleNamespace(frame=b.page.main_frame))
        self.assertEqual(b.wire.reserve.call_count, 2)

    def test_spa_pagination_also_reserves_once_and_discards_credit(self):
        b = self.backend()
        button = Mock()
        button.get_attribute.return_value = ''
        b._visible = Mock(return_value=button)
        b.next_page()
        b.wire.reserve.assert_called_once_with('page')
        self.assertIsNone(b._pagination_page)

    def test_iframe_cannot_consume_main_navigation_credit(self):
        b = self.backend()
        b._pagination_page = b.page
        b._reserve_document_page(SimpleNamespace(frame=object()))
        b.wire.reserve.assert_called_once_with('page')
        self.assertIs(b._pagination_page, b.page)

    def test_timed_out_click_cannot_leak_a_credit(self):
        b = self.backend()
        button = Mock()
        button.get_attribute.return_value = ''
        button.click.side_effect = TimeoutError
        b._visible = Mock(return_value=button)
        with self.assertRaises(TimeoutError):
            b.next_page()
        self.assertIsNone(b._pagination_page)

    def test_idle_poll_aborts_before_wire_without_poisoning_page(self):
        b = self.backend()
        b.cancelled.set()
        route = Mock()
        b._route(route)
        route.abort.assert_called_once()
        b.wire.fetch.assert_not_called()
        self.assertIsNone(b.error)

    def test_optional_denial_does_not_become_page_error(self):
        for kind in ('image', 'font', 'script'):
            for code in ('http_401', 'http_403'):
                with self.subTest(kind=kind, code=code):
                    b = self.backend()
                    route = Mock()
                    route.request.url = 'https://jobs.fixture.test/optional'
                    route.request.resource_type = kind
                    route.request.method = 'GET'
                    route.request.post_data_buffer = None
                    b.wire.fetch.side_effect = CrawlError(code)
                    b._route(route)
                    self.assertFalse(b.wire.fetch.call_args.kwargs['required'])
                    self.assertIsNone(b.error)
                    route.abort.assert_called_once()

    def test_required_api_and_any_429_still_stop(self):
        for kind, code in (('xhr', 'http_403'), ('fetch', 'http_401'), ('image', 'http_429')):
            with self.subTest(kind=kind, code=code):
                b = self.backend()
                route = Mock()
                route.request.url = 'https://jobs.fixture.test/query'
                route.request.resource_type = kind
                route.request.method = 'GET'
                route.request.post_data_buffer = None
                b.wire.fetch.side_effect = CrawlError(code)
                b._route(route)
                self.assertEqual(b.error, code)


class TransportReviewTests(unittest.TestCase):
    def test_only_optional_401_403_skip_site_cooldown(self):
        for required, status in ((False, 401), (False, 403), (True, 401), (True, 403), (False, 429)):
            with self.subTest(required=required, status=status), tempfile.TemporaryDirectory() as tmp:
                ledger = RateLedger(Path(tmp)/'r.sqlite', Limits(request_interval=0))
                wire = PinnedTransport(fixture_adapter(), ledger, threading.Event())
                with patch('vibe_job_radar.guided.transport.validate_public_url', return_value=('jobs.fixture.test', '93.184.216.34', '/')), patch('vibe_job_radar.guided.transport.PinnedHTTPSConnection') as conn:
                    response = conn.return_value.getresponse.return_value
                    response.status = status
                    response.getheaders.return_value = []
                    with self.assertRaises(CrawlError):
                        wire.fetch('https://jobs.fixture.test/optional', required=required)
                blocked = required or status == 429
                self.assertEqual(bool(wire.blocked), blocked)
                if blocked:
                    with self.assertRaises(RateLimit):
                        ledger.reserve('fixture', 'request')
                else:
                    ledger.reserve('fixture', 'request')
                self.assertGreaterEqual(ledger.summary('fixture')['request']['day'], 1)


class SelectorReviewTests(unittest.TestCase):
    def test_selector_excludes_other_matching_job_urls(self):
        adapter = dataclasses.replace(fixture_adapter(), card_selector='#results .job a[href]')
        html = '''<section id="results"><div class="job"><a href="/job/1">结果职位</a></div></section>
            <aside><a href="/job/2">推荐职位</a></aside><footer><a href="/job/3">底部职位</a></footer>'''
        self.assertEqual([c.url for c in adapter.cards(PageSnapshot(adapter.search_url('x'), html))], ['https://jobs.fixture.test/job/1'])

    def test_attribute_equality_comma_and_duplicate_selector(self):
        adapter = dataclasses.replace(fixture_adapter(), card_selector='a[data-type="job card"], a.selected[href]')
        page = PageSnapshot(adapter.search_url('x'), '<a data-type="job card" class="selected" href="/job/1">一</a><a data-type="ad" href="/job/2">二</a>')
        self.assertEqual(len(adapter.cards(page)), 1)

    def test_unsupported_selectors_fail_instead_of_widening_scope(self):
        for selector in ('a:hover', '#results > a', 'a,', '[href', ''):
            with self.subTest(selector=selector), self.assertRaises(CrawlError):
                list(dataclasses.replace(fixture_adapter(), card_selector=selector).cards(PageSnapshot('https://jobs.fixture.test/search', '<a href="/job/1">一</a>')))


class ServiceReviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.workspace = Workspace(self.tmp.name)
        # Deliberately DO NOT add 'fixture' to workspace.config['platforms'].
        self.service = GuidedService(self.workspace, registry=Registry([fixture_adapter()]), backend_factory=FakeBackend)
        self.addCleanup(self.service.close)

    def wait(self):
        deadline = time.monotonic()+15
        while self.service.state()['busy'] and time.monotonic()<deadline:
            time.sleep(.01)
        view = self.service.state()
        self.assertFalse(view['busy'], wait_diagnostic(self.service, view) if view['busy'] else None)

    def create(self, **kw):
        data = dict(platform='fixture', keyword='时间序列', roles=['time_series'], consent=True, rights_note='人工测试样本', max_pages=2, max_jobs=5)
        data.update(kw)
        ident = self.service.create(data)['id']
        self.wait()
        return ident

    def test_ready_freezes_idle_requests_until_next_explicit_action(self):
        ident = self.create()
        self.assertEqual(self.service._load(ident)['status'], 'ready')
        self.assertTrue(self.service._cancel.is_set())
        self.service.action({'id': ident, 'action': 'login'})
        self.wait()
        self.assertFalse(self.service._cancel.is_set())
        self.assertEqual(self.service._load(ident)['authentication'], 'manual_pending')

    def test_repeated_login_does_not_open_or_reset_quota(self):
        ident = self.create()
        self.service.action({'id': ident, 'action': 'login'})
        self.wait()
        backend = self.service._backends[ident]
        count = len(backend.opens)
        self.service.action({'id': ident, 'action': 'login'})
        self.wait()
        self.assertEqual(len(backend.opens), count)
        self.assertEqual(self.service._load(ident)['code'], 'login_rate_limited')
        self.assertEqual(self.service.ledger.summary('fixture')['login']['day'], 1)

    def test_custom_adapter_reports_with_frozen_platform_metadata(self):
        ident = self.create(max_pages=1)
        state = self.service._load(ident)
        self.service.action({'id': ident, 'action': 'collect', 'selected': [c['id'] for c in state['cards']]})
        self.wait()
        state = self.service._load(ident)
        self.assertEqual(state['status'], 'completed', state)
        report = self.workspace.report(state['report_id'])
        self.assertEqual(report['manifest']['project_version'], __version__)
        config = json.loads((self.workspace.root/'reports'/state['report_id']/'effective_config.json').read_text(encoding='utf-8'))
        self.assertIn('fixture', config['platforms'])
        self.assertNotIn('fixture', self.workspace.config['platforms'])

    def test_partial_success_is_reported_before_blocking_error(self):
        ident = self.create()
        state = self.service._load(ident)
        backend = self.service._backends[ident]
        original = backend.open
        def opened(url, **kw):
            if url.endswith('/job/2'):
                raise CrawlError('http_403')
            return original(url, **kw)
        backend.open = opened
        self.service.action({'id': ident, 'action': 'collect', 'selected': [c['id'] for c in state['cards']]})
        self.wait()
        state = self.service._load(ident)
        self.assertEqual(state['status'], 'waiting_manual')
        self.assertEqual(state['code'], 'http_403')
        self.assertEqual(state['phase'], 'collect')
        self.assertEqual([c['status'] for c in state['cards']], ['ok', 'http_403'])
        self.assertTrue(state['report_id'])
        self.assertEqual(self.workspace.report(state['report_id'])['manifest']['stats']['full_text_job_groups'], 1)

    def test_all_failed_does_not_invent_a_report(self):
        ident = self.create(max_pages=1)
        state = self.service._load(ident)
        self.service._backends[ident].open = Mock(side_effect=CrawlError('manual_required'))
        self.service.action({'id': ident, 'action': 'collect', 'selected': [c['id'] for c in state['cards']]})
        self.wait()
        self.assertEqual(self.service._load(ident)['report_id'], '')


class VersionReviewTests(unittest.TestCase):
    def test_cli_reports_same_runtime_version(self):
        capture = io.StringIO()
        with contextlib.redirect_stdout(capture), self.assertRaises(SystemExit) as exit:
            parser().parse_args(['--version'])
        self.assertEqual(exit.exception.code, 0)
        self.assertEqual(capture.getvalue().strip(), 'vibe-job-radar '+__version__)

    def test_packaging_and_candidate_use_the_single_version_source(self):
        root = Path(__file__).resolve().parents[1]
        project = (root/'pyproject.toml').read_text(encoding='utf-8')
        self.assertIn('dynamic = ["version"]', project)
        self.assertIn('vibe_job_radar._version.__version__', project)
        builder = (root/'scripts/build_candidate.py').read_text(encoding='utf-8')
        self.assertIn('src/vibe_job_radar/_version.py', builder)
