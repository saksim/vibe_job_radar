"""Leading REP wildcards remain rules, including their denial and bounds."""
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

import test_shared_robots as shared
from vibe_job_radar.guided.adapters import builtins
from vibe_job_radar.guided.contracts import CrawlError
from vibe_job_radar.guided.native_browser import NativeControl
from vibe_job_radar.guided.native_policy import contract_for
from vibe_job_radar.guided.rate import Limits, RateLedger
from vibe_job_radar.guided.transport import PinnedTransport, WireResponse
from vibe_job_radar.network import FetchError, Response, SafeHTTP, SiteFetcher
from vibe_job_radar.robots_rules import RobotsError, RobotsRules


def rules(body):
    return RobotsRules(200, 'text/plain', body, user_agent='VibeJobRadar/0.2')


class LeadingWildcardRobotsTests(unittest.TestCase):
    def test_rfc_suffix_example_and_longer_explicit_allow(self):
        policy = rules(b'User-agent: *\nDisallow: *.gif$\nDisallow: /example/\nAllow: /publications/\n')
        for path, allowed in (('/image.gif', False), ('/nested/image.gif', False),
                              ('/image.gif/preview', True), ('/image.gif?size=1', True),
                              ('/example/page', False), ('/publications/image.gif', True)):
            with self.subTest(path=path):
                self.assertEqual(policy.allowed(shared.ORIGIN + path), allowed)

    def test_query_restrictions_are_enforced_before_http_and_remain_cached(self):
        body = b'User-agent: *\nDisallow: *?city=*\nDisallow: /*?*\nDisallow: /*.js*\nDisallow: /job_detail/l*.html\n'
        fake = shared.transport(body)
        fetcher = SiteFetcher({'jobs.fixture.test'}, fake)
        for path in ('/job?city=1', '/search?query=example', '/app.js', '/job_detail/l123.html'):
            with self.subTest(path=path):
                with self.assertRaisesRegex(FetchError, 'robots_denied'):
                    fetcher.fetch(shared.ORIGIN + path)
        self.assertEqual(fake.public_get.call_count, 1)
        self.assertEqual(fetcher.fetch(shared.ORIGIN + '/job').status, 200)
        self.assertEqual(fake.public_get.call_count, 2)

    def test_wildcard_alone_restricts_root_and_all_paths(self):
        policy = rules(b'User-agent: *\nDisallow: *\n')
        for suffix in ('', '/', '/public', '/nested/path?x=1'):
            with self.subTest(suffix=suffix):
                self.assertFalse(policy.allowed(shared.ORIGIN + suffix))

    def test_leading_allow_uses_original_specificity_and_allow_wins_exact_tie(self):
        policy = rules(b'User-agent: *\nDisallow: *\nAllow: *public$\nDisallow: *private-public$\n')
        self.assertTrue(policy.allowed(shared.ORIGIN + '/public'))
        self.assertFalse(policy.allowed(shared.ORIGIN + '/private-public'))
        self.assertFalse(policy.allowed(shared.ORIGIN + '/public/other'))
        tie = rules(b'User-agent: *\nDisallow: *public$\nAllow: *public$\n')
        self.assertTrue(tie.allowed(shared.ORIGIN + '/public'))

    def test_literal_encoded_star_query_unicode_and_end_anchor_keep_their_meaning(self):
        policy = rules('User-agent: *\nDisallow: *%2Asecret$\nDisallow: *中文$\nDisallow: *?query=*\n'.encode())
        for path, allowed in (('/%2Asecret', False), ('/anythingsecret', True),
                              ('/%E4%B8%AD%E6%96%87', False), ('/x?query=1', False),
                              ('/x%3Fquery=1', True), ('/%2Asecret/child', True)):
            with self.subTest(path=path):
                self.assertEqual(policy.allowed(shared.ORIGIN + path), allowed)

    def test_repeated_wildcards_and_invalid_prefixes_preserve_existing_bounds(self):
        policy = rules(b'User-agent: *\nDisallow: **private**$\n')
        self.assertFalse(policy.allowed(shared.ORIGIN + '/private'))
        self.assertTrue(policy.allowed(shared.ORIGIN + '/public'))
        for value in ('private', '?city=*', '%2Fprivate', '*' * 33, '*' + 'a' * 2048):
            with self.subTest(value=value):
                with self.assertRaisesRegex(RobotsError, 'robots_unavailable'):
                    rules(('User-agent: *\nDisallow: ' + value).encode())

    def test_selected_agent_and_publisher_limits_are_unchanged(self):
        body = b'User-agent: *\nDisallow: *\nUser-agent: VibeJobRadar\nDisallow: *private$\nCrawl-delay: 5.5\nRequest-rate: 2/60\n'
        policy = rules(body)
        self.assertTrue(policy.allowed(shared.ORIGIN + '/job'))
        self.assertFalse(policy.allowed(shared.ORIGIN + '/private'))
        self.assertEqual((policy.delay, policy.windows), (5.5, [(2, 60)]))

    def test_both_browser_gates_agree_without_expanding_the_native_contract(self):
        origin = 'https://www.liepin.com'
        body = b'User-agent: *\nDisallow: *?*\n'
        with tempfile.TemporaryDirectory() as directory:
            adapter = builtins().get('liepin')
            ledger = RateLedger(Path(directory) / 'rates.sqlite', Limits(request_interval=0, page_interval=0))
            native = NativeControl(adapter, ledger, threading.Event())
            native.install_robots(origin, 200, 'text/plain', body)
            bridge = PinnedTransport(adapter, ledger, threading.Event())
            with patch.object(bridge, 'fetch', return_value=WireResponse(200, {'content-type': 'text/plain'}, body)) as fetch:
                for gate in (native, bridge):
                    gate.ensure_robots(origin + '/job/123.shtml')
                    with self.assertRaisesRegex(CrawlError, 'robots_denied'):
                        gate.ensure_robots(origin + '/job/123.shtml?query=x')
                fetch.assert_called_once_with(origin + '/robots.txt')
            with self.assertRaisesRegex(CrawlError, 'native_operation_unreviewed'):
                contract_for(adapter).match('https://api-c.liepin.com/api/apply', 'POST', 'Fetch')

    def test_same_origin_redirect_is_denied_before_followup(self):
        fake = shared.transport()
        fake.public_get.side_effect = [
            Response(200, {'content-type': 'text/plain'}, b'User-agent: *\nDisallow: *?*', shared.ORIGIN + '/robots.txt'),
            Response(302, {'location': '/job?query=1'}, b'', shared.ORIGIN + '/job')]
        with self.assertRaisesRegex(FetchError, 'robots_denied'):
            SiteFetcher({'jobs.fixture.test'}, fake).fetch(shared.ORIGIN + '/job')
        self.assertEqual(fake.public_get.call_count, 2)

    def test_real_tls_origin_never_receives_a_denied_path(self):
        for allowed in (False, True):
            with self.subTest(allowed=allowed):
                fixture = shared.tls_fixture.RealProxyTests()
                fixture.setUp()
                try:
                    fixture.browser_fixture = True
                    fixture.robots_payload = b'User-agent: *\nDisallow: *?*\n' + (b'Allow: /job/public?query=test$\n' if allowed else b'')
                    fixture.payload = shared.HTML
                    fixture.content_type = 'text/html'
                    def request():
                        return SiteFetcher({shared.tls_fixture.HOST}, SafeHTTP({shared.tls_fixture.HOST}, interval=0)).fetch('https://' + shared.tls_fixture.HOST + '/job/public?query=test')
                    if allowed:
                        self.assertEqual(fixture.perform(request).status, 200)
                    else:
                        with self.assertRaisesRegex(FetchError, 'robots_denied'):
                            fixture.perform(request)
                    self.assertEqual(len(fixture.requests), 2 if allowed else 1)
                finally:
                    fixture.tearDown()
                    fixture.doCleanups()
