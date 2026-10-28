"""A form/history search never authorizes fetching a robots-denied document."""
from dataclasses import replace
import json
import threading
import unittest
from unittest.mock import Mock, patch

from vibe_job_radar.guided.adapters import builtins
from vibe_job_radar.guided.browser import PlaywrightBackend
from vibe_job_radar.guided.bridge_search import BridgeSearch
from vibe_job_radar.guided.contracts import CrawlError, PageSnapshot, PageSnapshotChanged
from vibe_job_radar.guided.diagnostic_trace import DiagnosticTrace
from vibe_job_radar.guided.native_navigation import NativeDocument
from vibe_job_radar.guided.transport import WireResponse
import test_liepin_native_search as search_tests
import test_login_search_entry as return_tests


class BridgeSearchTests(unittest.TestCase):
    def setUp(self):
        self.b = PlaywrightBackend.__new__(PlaywrightBackend)
        self.b.adapter = builtins().get('liepin')
        self.b.bridge_search_enabled = True
        self.b.error = self.b.wait_error = None
        self.b.cancelled = threading.Event()
        self.b.wire = Mock()
        self.b.page = Mock(url=search_tests.SEARCH)
        self.b.page.is_closed.return_value = False
        self.search = self.b._bridge_search = BridgeSearch(self.b)
        self.search.sessions[self.b.page] = ('target', Mock())
        self.document = NativeDocument('target', 'main', 1, self.b.adapter.search_base, search_tests.SEARCH)
        self.search.documents[self.b.page] = self.document

    def request(self):
        request = Mock()
        request.frame = self.b.page.main_frame
        request.post_data_buffer = search_tests.request()['postData'].encode()
        return request

    def response(self):
        return WireResponse(200, {'content-type':'application/json'}, json.dumps(search_tests.payload()).encode())

    def test_history_read_checks_original_document_but_real_query_navigation_stays_denied(self):
        def robots(url):
            if '?' in url: raise CrawlError('robots_denied')
        self.b.wire.ensure_robots.side_effect = robots
        self.b.ensure_page_access(search_tests.SEARCH)
        self.b.wire.ensure_robots.assert_called_once_with(self.b.adapter.search_base)
        self.search.documents[self.b.page] = replace(self.document, document_url=search_tests.SEARCH)
        with self.assertRaisesRegex(CrawlError, 'robots_denied'):
            self.b.ensure_page_access(search_tests.SEARCH)

    def test_different_path_does_not_inherit_entry_permission(self):
        self.b.page.url = search_tests.JOB + '?d_sfrom=synthetic'
        self.search.documents[self.b.page] = replace(self.document, url=self.b.page.url)
        self.b.ensure_page_access(self.b.page.url)
        self.b.wire.ensure_robots.assert_called_once_with(self.b.page.url)

    def test_document_change_during_access_check_invalidates_observation(self):
        def changed(_):
            self.search.documents[self.b.page] = replace(self.document, sequence=2)
        self.b.wire.ensure_robots.side_effect = changed
        with self.assertRaises(PageSnapshotChanged): self.b.ensure_page_access(search_tests.SEARCH)

    def test_only_current_query_reply_can_supply_cards(self):
        prepared = self.search.prepare(self.request())
        self.search.complete(prepared, self.response())
        page = PageSnapshot(search_tests.SEARCH, '<h1>Artificial results</h1>',
                            business=self.search.observations(), business_required=True)
        self.assertEqual(len(self.b.adapter.cards(page)), 1)
        self.search.prepare(self.request())
        self.assertEqual(self.search.observations(), ())
        with self.assertRaisesRegex(CrawlError, 'page_not_ready'):
            self.b.adapter.cards(replace(page, business=self.search.observations()))

    def test_changed_document_or_newer_request_rejects_late_reply(self):
        for cause in ('document', 'request'):
            with self.subTest(cause=cause):
                prepared = self.search.prepare(self.request())
                if cause == 'document':
                    current = self.search.documents[self.b.page]
                    self.search.documents[self.b.page] = replace(current, sequence=current.sequence+1)
                else: self.search.prepare(self.request())
                with self.assertRaises(PageSnapshotChanged): self.search.complete(prepared, self.response())
                self.assertEqual(self.search.observations(), ())

    def test_child_frame_and_other_query_requests_cannot_supply_main_results(self):
        request = self.request(); request.frame = Mock()
        with self.assertRaisesRegex(CrawlError, 'native_surface_unsupported'): self.search.prepare(request)
        request = self.request(); request.post_data_buffer = search_tests.request(key='another query')['postData'].encode()
        with self.assertRaisesRegex(CrawlError, 'liepin_search_query_mismatch'): self.search.prepare(request)

    def test_bad_response_does_not_become_an_empty_success(self):
        prepared = self.search.prepare(self.request())
        for response in (WireResponse(200, {'content-type':'text/html'}, b'<p>login</p>'),
                         WireResponse(200, {'content-type':'application/json'}, b'[]')):
            with self.subTest(response=response), self.assertRaisesRegex(CrawlError, 'native_business_response_invalid'):
                self.search.complete(prepared, response)

    def test_search_and_login_use_query_free_entry_without_dropping_explicit_filters(self):
        self.b.open = Mock(return_value='entry')
        with patch('vibe_job_radar.guided.liepin_form.submit_search', return_value='results') as submit:
            self.assertEqual(self.b.open_search(search_tests.SEARCH, keyword='时间序列'), 'results')
            self.b.open.assert_called_once_with(self.b.adapter.search_base)
            submit.assert_called_once_with(self.b, '时间序列')
            submit.reset_mock(); self.b.open.reset_mock()
            self.assertEqual(self.b.open_search(search_tests.SEARCH, keyword='时间序列', authentication=True), 'entry')
            submit.assert_not_called()
            self.b.open.assert_called_once_with(
                'https://www.liepin.com/login?backUrl=https%3A%2F%2Fwww.liepin.com%2Fzhaopin%2F', authentication=True)
            self.b.open.reset_mock()
            explicit = search_tests.SEARCH + '&city=020'
            self.b.open_search(explicit, keyword='时间序列')
            self.b.open.assert_called_once_with(explicit, authentication=False)
            submit.assert_not_called()

    def test_owned_cdp_reload_retires_result_even_for_same_url(self):
        client = Mock()
        client.send.side_effect = lambda method: ({'targetInfo':{'targetId':'target'}} if method=='Target.getTargetInfo'
            else {'frameTree':{'frame':{'id':'main','url':search_tests.SEARCH}}} if method=='Page.getFrameTree' else {})
        self.b.context = Mock(); self.b.context.new_cdp_session.return_value = client
        self.search.bind(self.b.page)
        self.search.complete(self.search.prepare(self.request()), self.response())
        callback = next(c.args[1] for c in client.on.call_args_list if c.args[0]=='Page.frameNavigated')
        callback({'frame':{'id':'main','url':search_tests.SEARCH}})
        self.assertEqual(self.search.observations(), ())

    def test_committed_main_document_clearing_stops_without_undoing_navigation(self):
        client = Mock()
        client.send.side_effect = lambda method: ({'targetInfo': {'targetId': 'target'}} if method == 'Target.getTargetInfo'
            else {'frameTree': {'frame': {'id': 'main', 'url': 'about:blank'}}} if method == 'Page.getFrameTree' else {})
        self.b.context = Mock(); self.b.context.new_cdp_session.return_value = client
        self.search.documents.clear()
        self.search.bind(self.b.page)
        self.assertIsNone(self.b.error)  # Initial blank page is expected.
        callback = next(c.args[1] for c in client.on.call_args_list if c.args[0] == 'Page.frameNavigated')
        callback({'frame': {'id': 'main', 'url': search_tests.SEARCH}})
        self.search.complete(self.search.prepare(self.request()), self.response())
        callback({'frame': {'id': 'child', 'parentId': 'main', 'url': 'about:blank'}})
        self.assertIsNone(self.b.error)
        callback({'frame': {'id': 'main', 'url': 'about:blank'}})
        self.assertEqual(self.b.error, 'native_page_cleared')
        self.assertEqual(self.search.observations(), ())
        self.b.page.goto.assert_not_called()
        self.b.page.evaluate.assert_not_called()
        with self.assertRaisesRegex(CrawlError, 'native_page_cleared'): self.search.identity()

    def blank_callbacks(self):
        client = Mock()
        client.send.side_effect = lambda method: ({'targetInfo': {'targetId': 'target'}} if method == 'Target.getTargetInfo'
            else {'frameTree': {'frame': {'id': 'main', 'url': search_tests.SEARCH}}} if method == 'Page.getFrameTree' else {})
        self.b.context = Mock(); self.b.context.new_cdp_session.return_value = client
        self.b._diagnostics = DiagnosticTrace('a' * 32, 'liepin')
        self.search.bind(self.b.page)
        return client, {call.args[0]: call.args[1] for call in client.on.call_args_list}

    def test_blank_commit_records_browser_reason_without_changing_navigation(self):
        for reason in ('scriptInitiated', 'metaTagRefresh', 'httpHeaderRefresh'):
            with self.subTest(reason=reason):
                self.setUp()
                client, events = self.blank_callbacks()
                commands = list(client.send.call_args_list)
                events['Page.frameRequestedNavigation']({'frameId': 'main', 'url': 'about:blank',
                    'reason': reason, 'disposition': 'currentTab'})
                self.assertIsNone(self.b.error)  # A request need not commit.
                events['Page.frameNavigated']({'frame': {'id': 'main', 'url': 'about:blank'}})
                self.assertEqual(self.b.error, 'native_page_cleared')
                self.assertEqual(self.b._diagnostics.snapshot()['events'][-1]['navigation_reason'], reason)
                self.assertEqual(client.send.call_args_list, commands)
                self.b.page.evaluate.assert_not_called()
                self.b.page.goto.assert_not_called()

    def test_blank_reason_is_not_inherited_from_children_other_tabs_or_retired_document(self):
        for change in ({'frameId': 'child'}, {'disposition': 'newTab'}, {'url': 'about:blank#private'}):
            with self.subTest(change=change):
                self.setUp()
                _, events = self.blank_callbacks()
                event = {'frameId': 'main', 'url': 'about:blank', 'reason': 'scriptInitiated', 'disposition': 'currentTab'}
                events['Page.frameRequestedNavigation']({**event, **change})
                events['Page.frameNavigated']({'frame': {'id': 'main', 'url': 'about:blank'}})
                self.assertEqual(self.b._diagnostics.snapshot()['events'][-1]['navigation_reason'], 'unknown')
        self.setUp()
        _, events = self.blank_callbacks()
        events['Page.frameScheduledNavigation']({'frameId': 'main', 'url': 'about:blank', 'reason': 'scriptInitiated'})
        events['Page.frameNavigated']({'frame': {'id': 'main', 'url': search_tests.SEARCH}})
        events['Page.frameNavigated']({'frame': {'id': 'main', 'url': 'about:blank'}})
        self.assertEqual(self.b._diagnostics.snapshot()['events'][-1]['navigation_reason'], 'unknown')

    def test_unknown_blank_reason_and_unrelated_event_fields_are_not_logged(self):
        _, events = self.blank_callbacks()
        secret = 'private-marker-never-export'
        events['Page.frameScheduledNavigation']({'frameId': 'main', 'url': 'about:blank',
            'reason': secret, 'stack': secret})
        events['Page.frameNavigated']({'frame': {'id': 'main', 'url': 'about:blank'}})
        snapshot = self.b._diagnostics.snapshot()
        self.assertEqual(snapshot['events'][-1]['navigation_reason'], 'unknown')
        self.assertNotIn(secret, json.dumps(snapshot))

    def test_cancelled_schedule_and_invalid_events_cannot_supply_a_blank_reason(self):
        _, events = self.blank_callbacks()
        events['Page.frameRequestedNavigation'](None)
        events['Page.frameScheduledNavigation']({'frameId': 'main', 'url': 'about:blank', 'reason': 'scriptInitiated'})
        events['Page.frameClearedScheduledNavigation']({'frameId': 'main'})
        self.assertIsNone(self.b.error)
        events['Page.frameNavigated']({'frame': {'id': 'main', 'url': 'about:blank'}})
        self.assertEqual(self.b._diagnostics.snapshot()['events'][-1]['navigation_reason'], 'unknown')

    def test_blank_observer_failure_cannot_prevent_stopping(self):
        _, events = self.blank_callbacks()
        with patch.object(self.b._diagnostics, 'mark', side_effect=RuntimeError('observer failed')):
            events['Page.frameNavigated']({'frame': {'id': 'main', 'url': 'about:blank'}})
        self.assertEqual(self.b.error, 'native_page_cleared')
        self.assertEqual(self.b._diagnostics.observer_errors, 1)


class BridgeReturnTests(unittest.TestCase):
    def test_bridge_requires_its_real_search_observer_before_returning_to_original_query(self):
        fixture = return_tests.LoginSearchEntryTests(); fixture.setUp()
        fixture.state['backend'] = 'bridge'
        fixture.backend.bridge_search_enabled = False
        self.assertIsNone(fixture.signature())
        fixture.backend.bridge_search_enabled = True
        fixture.tick(); fixture.service._submit.assert_not_called()
        fixture.tick()
        self.assertEqual(fixture.service._submit.call_args.args[:2], ('resume_returned_search', 'task'))
        fixture.controls[return_tests.FIELD].fill.assert_not_called()
