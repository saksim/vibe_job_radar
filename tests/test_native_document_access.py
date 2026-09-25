"""History updates do not invent document fetches or waive actual request rules."""
from dataclasses import replace
import unittest
from unittest.mock import Mock

import test_native_acquisition as fixtures
from vibe_job_radar.guided.adapters import builtins
from vibe_job_radar.guided.cdp_page import CDPPage, Frame
from vibe_job_radar.guided.contracts import CrawlError, PageSnapshotChanged
from vibe_job_radar.guided.native_policy import contract_for


class DocumentAddressTests(unittest.TestCase):
    def test_history_and_child_events_cannot_replace_committed_main_document(self):
        page = CDPPage.__new__(CDPPage)
        page.main_frame = Frame(page, 'main')
        page.navigation = 0
        page._emit = Mock()
        page._navigated({'frame': {'id':'main', 'url':'https://fixture.test/zhaopin/', 'loaderId':'one'}})
        page._within_document({'frameId':'main', 'url':'https://fixture.test/zhaopin/?key=example'})
        self.assertEqual(page.document_url, 'https://fixture.test/zhaopin/')
        self.assertEqual(page.url, 'https://fixture.test/zhaopin/?key=example')
        page._navigated({'frame': {'id':'child', 'parentId':'main', 'url':'https://fixture.test/other'}})
        self.assertEqual(page.document_url, 'https://fixture.test/zhaopin/')
        page._navigated({'frame': {'id':'main', 'url':'https://fixture.test/zhaopin/?key=example', 'loaderId':'two'}})
        self.assertEqual(page.document_url, page.url)  # An actual navigation loses the entry exception.


class NativeDocumentAccessTests(unittest.TestCase):
    def setUp(self):
        helper = fixtures.NativeControllerTests()
        helper.setUp()
        self.addCleanup(helper.doCleanups)
        self.backend = helper.b
        self.adapter = builtins().get('liepin')
        self.backend.adapter = self.adapter
        self.backend.contract = contract_for(self.adapter)
        self.backend.wire.adapter = self.adapter
        self.base = self.adapter.search_base
        self.query = self.adapter.search_url('人工合成')
        self.backend.wire.rules['https://www.liepin.com'] = fixtures.robots('User-agent: *\nDisallow: /*?*\n')
        self.backend.page = Mock(url=self.query, document_url=self.base, is_closed=Mock(return_value=False))
        self.backend._bound_pages = {self.backend.page: 'session'}

    def test_same_document_search_can_be_read_without_a_network_request_or_budget_reset(self):
        before = dict(self.backend.native_counts)
        with self.assertRaisesRegex(CrawlError, 'robots_denied'):
            self.backend.wire.ensure_robots(self.query)
        self.backend.ensure_page_access(self.query)
        self.assertEqual(self.backend.native_counts, before)
        self.backend._send.assert_not_called()

    def test_real_query_document_request_remains_denied_before_it_is_sent(self):
        event = {'requestId':'denied-query', 'networkId':'query', 'frameId':'frame', 'resourceType':'Document',
                 'request': {'url':self.query, 'method':'GET'}}
        self.backend._paused('session', event)
        self.assertEqual(self.backend.error, 'robots_denied')
        self.assertEqual(self.backend.native_counts['document'], 0)
        self.backend._send.assert_called_with('session', 'Fetch.failRequest',
                                             {'requestId':'denied-query', 'errorReason':'BlockedByClient'})

    def test_missing_changed_or_unrelated_document_does_not_authorize_query(self):
        for document in (None, self.query, 'https://www.liepin.com/', self.base+'?init=1',
                         'https://other.test/zhaopin/', self.base+'#fragment'):
            self.backend.page.document_url = document
            with self.subTest(document=document), self.assertRaisesRegex(CrawlError, 'robots_denied'):
                self.backend.ensure_page_access(self.query)

    def test_history_path_or_origin_change_cannot_borrow_search_entry_permission(self):
        for url in ('https://www.liepin.com/job/123.shtml?city=010',
                    'https://passport.liepin.com/zhaopin/?key=example',
                    'https://other.test/zhaopin/?key=example'):
            self.backend.page.url = url
            with self.subTest(url=url), self.assertRaises(CrawlError):
                self.backend.ensure_page_access(url)

    def test_denied_entry_stays_denied_after_history_change(self):
        self.backend.wire.rules['https://www.liepin.com'] = fixtures.robots('User-agent: *\nDisallow: /zhaopin/\n')
        with self.assertRaisesRegex(CrawlError, 'robots_denied'):
            self.backend.ensure_page_access(self.query)

    def test_retired_page_cannot_borrow_a_previously_committed_entry(self):
        self.backend._bound_pages.clear()
        with self.assertRaisesRegex(CrawlError, 'robots_denied'):
            self.backend.ensure_page_access(self.query)

    def test_other_adapters_do_not_inherit_the_search_history_case(self):
        self.backend.adapter = replace(self.adapter, key='other')
        with self.assertRaisesRegex(CrawlError, 'robots_denied'):
            self.backend.ensure_page_access(self.query)

    def test_stale_snapshot_closed_page_and_policy_revocation_are_rejected(self):
        with self.assertRaises(PageSnapshotChanged):
            self.backend.ensure_page_access(self.query+'&currentPage=1')
        self.backend.page.is_closed = lambda: True
        with self.assertRaisesRegex(CrawlError, 'browser_closed'):
            self.backend.ensure_page_access(self.query)
        self.backend.page.is_closed = lambda: False
        self.backend.policy_check = lambda: False
        with self.assertRaisesRegex(CrawlError, 'native_policy_changed'):
            self.backend.ensure_page_access(self.query)


if __name__ == '__main__':
    unittest.main()
