"""Literal REP special characters agree across HTTP and browser gates."""
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

import test_shared_robots as shared
from vibe_job_radar.guided.adapters import builtins
from vibe_job_radar.guided.contracts import CrawlError
from vibe_job_radar.guided.native_browser import NativeControl
from vibe_job_radar.guided.native_policy import NativeRobots
from vibe_job_radar.guided.rate import Limits, RateLedger
from vibe_job_radar.guided.transport import PinnedTransport, WireResponse
from vibe_job_radar.network import FetchError, Response, SafeHTTP, SiteFetcher
from vibe_job_radar.robots_rules import RobotsRules

CASES = (
    ('/path/file-with-a-%2A.html', '/path/file-with-a-*.html', False),
    ('/path/foo-%24', '/path/foo-$', False),
    ('/path/file-with-a-%2a.html', '/path/file-with-a-%2A.html', False),
    ('/path/foo-%24', '/path/foo-%24', False),
    ('/literal%2A$', '/literal*', False),
    ('/literal%2A$', '/literalanything', True),
    ('/literal%2A$', '/literal*extra', True),
    ('/price%24$', '/price$', False),
    ('/price%24$', '/price', True),
    ('/price%24$', '/price$tail', True),
    ('/a*$', '/a', False),
    ('/a*$', '/ab', False),
    ('/a*b$', '/a*b', False),
    ('/a*b$', '/ab/c', True),
    ('/a%2Fb', '/a/b', True),
    ('/a%2fb', '/a%2Fb', False),
    ('/%7euser', '/~user', False),
    ('/中文%2A', '/%E4%B8%AD%E6%96%87*', False),
    ('/cost$net', '/cost$net', False),
    ('/cost$net', '/cost%24net', False),
    ('/encoded%252A', '/encoded%2A', True),
    ('/encoded%252A', '/encoded%252A', False),
    ('/search?key=%2A%24', '/search?key=*$', False),
    ('/search?key=%2A%24', '/search?key=abc', True),
    ('/search?tag=%2A%24', '/search?tag=*$', False),
    ('/search?tag=%2A%24', '/search?tag=abc', True),
)


def rules(body):
    return RobotsRules(200, 'text/plain', body, user_agent='VibeJobRadar/0.2')


class EncodedRobotsLiteralTests(unittest.TestCase):
    def test_rfc_special_literals_and_reserved_paths_agree_before_http_request(self):
        for pattern, path, allowed in CASES:
            body = ('User-agent: *\nDisallow: ' + pattern).encode()
            with self.subTest(pattern=pattern, path=path):
                self.assertEqual(rules(body).allowed(shared.ORIGIN + path), allowed)
                self.assertEqual(NativeRobots(200, 'text/plain', body).allowed(shared.ORIGIN + path), allowed)
                fake = shared.transport(body)
                fetcher = SiteFetcher({'jobs.fixture.test'}, fake)
                if '?key=' in path:
                    # The HTTP credential-name guard intentionally runs before robots.
                    # Keep it strict; ordinary tag queries below reach rules matching.
                    with self.assertRaisesRegex(FetchError, 'redirect_credentials_blocked'):
                        fetcher.fetch(shared.ORIGIN + path)
                    self.assertEqual(fake.public_get.call_count, 0)
                    continue
                if allowed:
                    self.assertEqual(fetcher.fetch(shared.ORIGIN + path).status, 200)
                else:
                    with self.assertRaisesRegex(FetchError, 'robots_denied'):
                        fetcher.fetch(shared.ORIGIN + path)
                self.assertEqual(fake.public_get.call_count, 2 if allowed else 1)

    def test_normalized_equivalent_allow_wins_but_broad_wildcard_does_not(self):
        tie = rules(b'User-agent: *\nDisallow: /cost%24net$\nAllow: /cost$net$\n')
        for path in ('/cost$net', '/cost%24net'):
            with self.subTest(path=path):
                self.assertTrue(tie.allowed(shared.ORIGIN + path))
        narrow = rules(b'User-agent: *\nDisallow: /literal%2A$\nAllow: /literal*$\n')
        for path, allowed in (('/literal*', False), ('/literal%2A', False), ('/literalanything', True)):
            with self.subTest(path=path):
                self.assertEqual(narrow.allowed(shared.ORIGIN + path), allowed)

    def test_leading_wildcard_keeps_encoded_specials_literal_and_anchor_terminal(self):
        policy = rules(b'User-agent: *\nDisallow: *%2Asecret$\nDisallow: *foo%24$\n')
        for path, allowed in (('/path/*secret', False), ('/path/%2asecret', False),
                              ('/path/anythingsecret', True), ('/path/*secret/child', True),
                              ('/foo$', False), ('/foo%24', False), ('/foo$tail', True)):
            with self.subTest(path=path):
                self.assertEqual(policy.allowed(shared.ORIGIN + path), allowed)

    def test_both_browser_gates_share_literal_denial_and_cached_rules(self):
        origin = 'https://www.liepin.com'
        body = b'User-agent: *\nDisallow: /job/123.shtml?tag=%2A$\nDisallow: /job/124.shtml?tag=%24$\n'
        with tempfile.TemporaryDirectory() as directory:
            adapter = builtins().get('liepin')
            ledger = RateLedger(Path(directory) / 'rates.sqlite', Limits(request_interval=0, page_interval=0))
            native = NativeControl(adapter, ledger, threading.Event())
            native.install_robots(origin, 200, 'text/plain', body)
            bridge = PinnedTransport(adapter, ledger, threading.Event())
            with patch.object(bridge, 'fetch', return_value=WireResponse(200, {'content-type': 'text/plain'}, body)) as fetch:
                for gate in (native, bridge):
                    gate.ensure_robots(origin + '/job/125.shtml')
                    for path in ('/job/123.shtml?tag=*', '/job/123.shtml?tag=%2A',
                                 '/job/124.shtml?tag=$', '/job/124.shtml?tag=%24'):
                        with self.subTest(gate=type(gate).__name__, path=path):
                            with self.assertRaisesRegex(CrawlError, 'robots_denied'):
                                gate.ensure_robots(origin + path)
                fetch.assert_called_once_with(origin + '/robots.txt')

    def test_redirect_to_raw_literal_is_denied_before_followup(self):
        for literal, encoded in (('*', '%2A'), ('$', '%24')):
            with self.subTest(literal=literal):
                fake = shared.transport()
                body = ('User-agent: *\nDisallow: /job/' + encoded + '$\n').encode()
                fake.public_get.side_effect = [
                    Response(200, {'content-type': 'text/plain'}, body, shared.ORIGIN + '/robots.txt'),
                    Response(302, {'location': '/job/' + literal}, b'', shared.ORIGIN + '/job')]
                with self.assertRaisesRegex(FetchError, 'robots_denied'):
                    SiteFetcher({'jobs.fixture.test'}, fake).fetch(shared.ORIGIN + '/job')
                self.assertEqual(fake.public_get.call_count, 2)

    def test_real_tls_denied_literal_never_reaches_origin_and_exact_allow_does(self):
        for literal, encoded in (('*', '%2A'), ('$', '%24')):
            for allowed in (False, True):
                with self.subTest(literal=literal, allowed=allowed):
                    fixture = shared.tls_fixture.RealProxyTests()
                    fixture.setUp()
                    try:
                        fixture.browser_fixture = True
                        rule = '/job/' + encoded + '$'
                        body = 'User-agent: *\nDisallow: ' + rule + '\n'
                        if allowed:
                            body += 'Allow: ' + rule + '\n'
                        fixture.robots_payload = body.encode()
                        fixture.payload = shared.HTML
                        fixture.content_type = 'text/html'
                        def request():
                            return SiteFetcher({shared.tls_fixture.HOST}, SafeHTTP({shared.tls_fixture.HOST}, interval=0)).fetch('https://' + shared.tls_fixture.HOST + '/job/' + literal)
                        if allowed:
                            self.assertEqual(fixture.perform(request).status, 200)
                        else:
                            with self.assertRaisesRegex(FetchError, 'robots_denied'):
                                fixture.perform(request)
                        self.assertEqual(len(fixture.requests), 2 if allowed else 1)
                    finally:
                        fixture.tearDown()
                        fixture.doCleanups()
