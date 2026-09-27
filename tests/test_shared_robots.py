"""Same valid publisher rules across legacy HTTP and both browser backends."""
import unittest
from unittest.mock import Mock

import test_loopback_proxy as tls_fixture
from vibe_job_radar.network import SiteFetcher, SafeHTTP, FetchError, Response
from vibe_job_radar.guided.native_policy import NativeRobots

ORIGIN='https://jobs.fixture.test'
HTML=b'<html><body>Artificial job content for rules tests</body></html>'


def transport(body=b'User-agent: *\nAllow: /', *, mime='text/plain', status=200, pages=1):
    fake=Mock();fake.interval=2
    fake.public_get.side_effect=[Response(status,{'content-type':mime} if mime else {},body,ORIGIN+'/robots.txt'),
        *[Response(200,{'content-type':'text/html'},HTML,ORIGIN+'/job') for _ in range(pages)]]
    return fake


class SharedRobotsTests(unittest.TestCase):
    def test_same_rules_and_targets_make_same_decision_before_job_request(self):
        cases=(
            ('Disallow: /*?*','/search?query=test',False),
            ('Disallow: /job/*?*','/job/1?q=test',False),
            ('Disallow: /job/*?*','/job/1',True),
            ('Disallow: /login$','/login',False),
            ('Disallow: /login$','/login/ok',True),
            ('Disallow: /\nAllow: /job/public','/job/public/1',True),
            ('Disallow: /job/private\nAllow: /job/private','/job/private',True),
            ('Disallow: /中文','/%E4%B8%AD%E6%96%87',False),
            ('Disallow: /a%2fb','/a%2Fb',False),
            ('Disallow: /a%2fb','/a/b',True),
            ('Disallow: /%7euser','/~user',False),
            ('Disallow: /a[0-9]+','/a123',True),
        )
        for rules,path,allowed in cases:
            body=('User-agent: *\n'+rules).encode()
            with self.subTest(rules=rules,path=path):
                self.assertEqual(NativeRobots(200,'text/plain',body).allowed(ORIGIN+path),allowed)
                fake=transport(body);fetcher=SiteFetcher({'jobs.fixture.test'},fake)
                if allowed:self.assertEqual(fetcher.fetch(ORIGIN+path).status,200)
                else:
                    with self.assertRaisesRegex(FetchError,'robots_denied'):fetcher.fetch(ORIGIN+path)
                self.assertEqual(fake.public_get.call_count,2 if allowed else 1)

    def test_specific_agent_groups_merge_without_using_other_crawler_identity(self):
        body=b'User-agent: *\nDisallow: /\nUser-agent: VibeJobRadar\nAllow: /job\nUser-agent: vibeJobRadar\nDisallow: /job/private\nUser-agent: Googlebot\nAllow: /\n'
        fake=transport(body,pages=2);fetcher=SiteFetcher({'jobs.fixture.test'},fake)
        self.assertEqual(fetcher.fetch(ORIGIN+'/job').status,200)
        with self.assertRaisesRegex(FetchError,'robots_denied'):fetcher.fetch(ORIGIN+'/job/private')
        self.assertEqual(fake.public_get.call_count,2)

    def test_denial_is_cached_and_never_fetches_a_job(self):
        fake=transport(b'User-agent: *\nDisallow: /*?*')
        fetcher=SiteFetcher({'jobs.fixture.test'},fake)
        for path in ('/job?query=a','/job?query=b'):
            with self.assertRaisesRegex(FetchError,'robots_denied'):fetcher.fetch(ORIGIN+path)
        self.assertEqual(fake.public_get.call_count,1)

    def test_html_empty_bad_encoding_or_ambiguous_rule_stops_and_stays_cached(self):
        for body,mime in ((b'<html><div id="app"></div></html>','text/plain'),
                         (b'User-agent: *\nAllow: /','text/html'),(b'', 'text/plain'),
                         (b'\xff', 'text/plain'),(b'not a robots file', 'text/plain'),
                         (b'User-agent: *\nDisallow: ?query=*','text/plain')):
            with self.subTest(body=body,mime=mime):
                fake=transport(body,mime=mime);fetcher=SiteFetcher({'jobs.fixture.test'},fake)
                for _ in range(2):
                    with self.assertRaisesRegex(FetchError,'robots_unavailable'):fetcher.fetch(ORIGIN+'/job')
                self.assertEqual(fake.public_get.call_count,1)

    def test_http_missing_file_policy_does_not_expand_with_shared_parser(self):
        for status in (404,410,401,403,429,500,503):
            with self.subTest(status=status):
                fake=transport(status=status);fetcher=SiteFetcher({'jobs.fixture.test'},fake)
                with self.assertRaisesRegex(FetchError,'robots_unavailable'):fetcher.fetch(ORIGIN+'/job')
                self.assertEqual(fake.public_get.call_count,1)

    def test_legacy_headerless_real_utf8_rules_still_work_but_html_does_not(self):
        fake=transport(mime='');self.assertEqual(SiteFetcher({'jobs.fixture.test'},fake).fetch(ORIGIN+'/job').status,200)
        fake=transport(b'<html>User-agent: *\nAllow: /</html>',mime='')
        with self.assertRaisesRegex(FetchError,'robots_unavailable'):SiteFetcher({'jobs.fixture.test'},fake).fetch(ORIGIN+'/job')

    def test_fractional_delay_and_all_rate_constraints_preserve_stricter_interval(self):
        body=b'User-agent: *\nAllow: /\nCrawl-delay: 5.5\nRequest-rate: 2/60\nRequest-rate: 1/40\n'
        fake=transport(body);SiteFetcher({'jobs.fixture.test'},fake).fetch(ORIGIN+'/job')
        self.assertEqual(fake.interval,40)
        fake=transport(body);fake.interval=120
        SiteFetcher({'jobs.fixture.test'},fake).fetch(ORIGIN+'/job');self.assertEqual(fake.interval,120)

    def test_same_origin_redirect_rechecks_rules_before_followup(self):
        fake=transport();fake.public_get.side_effect=[
            Response(200,{'content-type':'text/plain'},b'User-agent: *\nDisallow: /job/*?*',ORIGIN+'/robots.txt'),
            Response(302,{'location':'/job/1?query=test'},b'',ORIGIN+'/job/1')]
        with self.assertRaisesRegex(FetchError,'robots_denied'):SiteFetcher({'jobs.fixture.test'},fake).fetch(ORIGIN+'/job/1')
        self.assertEqual(fake.public_get.call_count,2)

    def test_other_origin_has_its_own_rules(self):
        fake=transport();fake.public_get.side_effect=[
            Response(200,{'content-type':'text/plain'},b'User-agent: *\nAllow: /',ORIGIN+'/robots.txt'),
            Response(200,{'content-type':'text/html'},HTML,ORIGIN+'/job'),
            Response(200,{'content-type':'text/plain'},b'User-agent: *\nDisallow: /*?*','https://other.fixture.test/robots.txt')]
        fetcher=SiteFetcher({'jobs.fixture.test','other.fixture.test'},fake)
        fetcher.fetch(ORIGIN+'/job')
        with self.assertRaisesRegex(FetchError,'robots_denied'):fetcher.fetch('https://other.fixture.test/job?query=x')
        self.assertEqual(fake.public_get.call_count,3)

    def test_actual_tls_denied_path_never_reaches_origin_and_specific_allow_does(self):
        for allowed in (False,True):
            with self.subTest(allowed=allowed):
                fixture=tls_fixture.RealProxyTests();fixture.setUp()
                try:
                    fixture.browser_fixture=True
                    fixture.robots_payload=b'User-agent: *\nDisallow: /job/*?*\n'+(b'Allow: /job/public?query=test$\n' if allowed else b'')
                    fixture.payload=HTML;fixture.content_type='text/html'
                    def request():
                        return SiteFetcher({tls_fixture.HOST},SafeHTTP({tls_fixture.HOST},interval=0)).fetch('https://'+tls_fixture.HOST+'/job/public?query=test')
                    if allowed:self.assertEqual(fixture.perform(request).status,200)
                    else:
                        with self.assertRaisesRegex(FetchError,'robots_denied'):fixture.perform(request)
                    self.assertEqual(len(fixture.requests),2 if allowed else 1)
                finally:fixture.tearDown();fixture.doCleanups()
