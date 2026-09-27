"""Browser acquisition must distinguish a missing robots file from a refusal."""
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from vibe_job_radar.guided.adapters import builtins
from vibe_job_radar.guided.contracts import CrawlError
from vibe_job_radar.guided.native_browser import NativeControl
from vibe_job_radar.guided.native_policy import NativeRobots, contract_for
from vibe_job_radar.guided.rate import Limits, RateLedger, RateLimit
from vibe_job_radar.guided.transport import PinnedTransport, WireResponse


MAIN = 'https://www.liepin.com'
API = 'https://api-c.liepin.com'
SEARCH = API + '/api/com.liepin.searchfront4c.pc-search-job'
RULES = b'User-agent: *\nDisallow: /*?*\nDisallow: /user/\n'


class BrowserRobotsStatusTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.adapter = builtins().get('liepin')
        self.ledger = RateLedger(Path(self.tmp.name) / 'rates.sqlite',
                                 Limits(request_interval=0, page_interval=0))
        self.native = NativeControl(self.adapter, self.ledger, threading.Event())
        self.bridge = PinnedTransport(self.adapter, self.ledger, threading.Event())

    def test_missing_file_allows_only_the_existing_origin_and_operation_contract(self):
        for status in (404, 410):
            with self.subTest(status=status):
                self.native.install_robots(API, status, 'text/html', b'<html>Missing</html>')
                self.native.ensure_robots(SEARCH)
                with self.assertRaisesRegex(CrawlError, 'robots_unavailable'):
                    self.native.ensure_robots('https://api-passport.liepin.com/')
                contract = contract_for(self.adapter)
                self.assertEqual(contract.match(SEARCH, 'POST', 'Fetch').key, 'liepin_search')
                with self.assertRaisesRegex(CrawlError, 'native_operation_unreviewed'):
                    contract.match(API + '/api/apply', 'POST', 'Fetch')

    def test_missing_file_on_api_does_not_override_main_site_rules(self):
        self.native.install_robots(MAIN, 200, 'text/plain', RULES)
        self.native.install_robots(API, 404, 'text/html', b'Not Found')
        self.native.ensure_robots(SEARCH)
        self.native.ensure_robots(MAIN + '/job/123.shtml')
        for path in ('/zhaopin/?key=java', '/user/profile'):
            with self.subTest(path=path), self.assertRaisesRegex(CrawlError, 'robots_denied'):
                self.native.ensure_robots(MAIN + path)

    def test_bridge_missing_file_is_cached_without_fetching_a_content_page(self):
        for status in (404, 410):
            with self.subTest(status=status):
                self.bridge.robots.clear()
                response = WireResponse(status, {'content-type': 'text/html'}, b'<html>Missing</html>')
                with patch.object(self.bridge, 'fetch', return_value=response) as fetch:
                    self.bridge.ensure_robots(MAIN + '/job/123.shtml')
                    self.bridge.ensure_robots(MAIN + '/job/456.shtml')
                fetch.assert_called_once_with(MAIN + '/robots.txt')

    def test_bridge_honors_wildcard_query_rule_and_still_reads_allowed_details(self):
        response = WireResponse(200, {'content-type': 'text/plain'}, RULES)
        with patch.object(self.bridge, 'fetch', return_value=response) as fetch:
            with self.assertRaisesRegex(CrawlError, 'robots_denied'):
                self.bridge.ensure_robots(MAIN + '/zhaopin/?key=java')
            self.bridge.ensure_robots(MAIN + '/job/123.shtml')
        fetch.assert_called_once_with(MAIN + '/robots.txt')

    def test_denied_limited_server_error_redirect_and_html_success_do_not_allow(self):
        for status in (301, 401, 403, 429, 500, 503):
            with self.subTest(status=status):
                with self.assertRaisesRegex(CrawlError, 'robots_unavailable'):
                    NativeRobots(status, 'text/plain', b'User-agent: *\nAllow: /')
                self.bridge.robots.clear()
                with patch.object(self.bridge, 'fetch', return_value=WireResponse(status, {}, b'')):
                    with self.assertRaisesRegex(CrawlError, 'robots_unavailable'):
                        self.bridge.ensure_robots(MAIN + '/job/123.shtml')
                self.assertNotIn(MAIN, self.bridge.robots)
        with self.assertRaisesRegex(CrawlError, 'robots_unavailable'):
            NativeRobots(200, 'text/html', b'<html>Sign in</html>')

    def test_missing_file_does_not_reset_existing_publisher_delay(self):
        now = [1000.0]
        self.ledger.clock = lambda: now[0]
        self.native.install_robots(API, 200, 'text/plain',
                                   b'User-agent: *\nCrawl-delay: 15\nRequest-rate: 2/60\n')
        self.ledger.reserve('liepin', 'request', origin=API)
        self.native.install_robots(API, 404, 'text/html', b'Not Found')
        now[0] += 1
        with self.assertRaises(RateLimit) as raised:
            self.ledger.reserve('liepin', 'request', origin=API)
        self.assertEqual(raised.exception.wait, 14)
        now[0] = 1015.0
        self.ledger.reserve('liepin', 'request', origin=API)
        now[0] = 1030.0
        with self.assertRaises(RateLimit) as raised:
            self.ledger.reserve('liepin', 'request', origin=API)
        self.assertEqual(raised.exception.wait, 30)
