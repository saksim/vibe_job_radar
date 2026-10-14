import json
import tempfile
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch

from vibe_job_radar.config import load_config
from vibe_job_radar.discovery import build_plan, discover
from vibe_job_radar.network import FetchError, Response, SafeHTTP, SiteFetcher, validate_public_url
from vibe_job_radar.store import Store


class FakeAPI:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []

    def json(self, url, **kwargs):
        self.calls.append((url, kwargs))
        result = next(self.responses)
        if isinstance(result, Exception):
            raise result
        return result


class FakeSite:
    interval = 0
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []
    def request(self, url):
        self.calls.append(url)
        return next(self.responses)


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.conf = load_config()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name)/"test.db"

    def result(self):
        return {"web": {"results": [{"title": "架构师", "description": "熟练使用Cursor编程。", "url": "https://www.zhipin.com/job_detail/unit-test.html"}]}}

    def test_query_grid_covers_platforms_and_roles(self):
        plan = build_plan(self.conf)
        self.assertEqual(len(plan), 90)
        self.assertEqual({x.platform for x in plan[:9]}, {"boss", "liepin", "51job"})
        self.assertEqual({x.role for x in plan[:9]}, set(self.conf["roles"]))

    def test_api_key_missing_does_not_call(self):
        fake = FakeAPI([])
        with Store(self.db) as store, self.assertRaises(ValueError):
            discover(store, self.conf, api_key="", plan=build_plan(self.conf), transport=fake)
        self.assertEqual(fake.calls, [])

    def test_page_offset_not_result_offset(self):
        fake = FakeAPI([self.result(), self.result()])
        plan = build_plan(self.conf, ["boss"], ["architect"])[:1]
        with Store(self.db) as store:
            report = discover(store, self.conf, api_key="test", plan=plan, pages=2, transport=fake)
            self.assertEqual(len(store.records()), 1)
            self.assertEqual(store.records()[0].evidence_level, "snippet")
        offsets = [parse_qs(urlsplit(u).query)["offset"][0] for u, _ in fake.calls]
        self.assertEqual(offsets, ["0", "1"])
        self.assertFalse(report["complete_market_coverage"])

    def test_max_requests_marks_budget_skips(self):
        fake = FakeAPI([self.result()])
        with Store(self.db) as store:
            r = discover(store, self.conf, api_key="test", plan=build_plan(self.conf), max_requests=1, transport=fake)
        self.assertEqual(r["requests_made"], 1)
        self.assertEqual(sum(t["status"] == "budget_skipped" for t in r["tasks"]), 89)

    def test_auth_or_rate_limit_stops_provider(self):
        fake = FakeAPI([FetchError("http_429")])
        with Store(self.db) as store:
            r = discover(store, self.conf, api_key="test", plan=build_plan(self.conf), transport=fake)
        self.assertEqual(r["requests_made"], 1)
        self.assertEqual(r["tasks"][1]["status"], "provider_stopped")

    def test_homepage_and_wrong_domain_rejected(self):
        data = {"web": {"results": [{"title": "架构师", "description": "Cursor", "url": "https://www.zhipin.com/"},
                                       {"title": "架构师", "description": "Cursor", "url": "https://evil.example/job/x"}]}}
        fake = FakeAPI([data])
        with Store(self.db) as store:
            r = discover(store, self.conf, api_key="test", plan=build_plan(self.conf)[:1], transport=fake)
            self.assertEqual(store.records(), [])
        self.assertEqual(len(r["rejected_results"]), 2)


class FetchTests(unittest.TestCase):
    def addresses(self, ip):
        return [(2, 1, 6, "", (ip, 443))]

    def test_public_https_allowed(self):
        with patch("socket.getaddrinfo", return_value=self.addresses("8.8.8.8")):
            self.assertEqual(validate_public_url("https://example.com/job?a=1", {"example.com"}), ("example.com", "8.8.8.8", "/job?a=1"))

    def test_private_address_rejected(self):
        for ip in ("127.0.0.1", "10.0.0.1", "169.254.169.254", "192.168.1.1"):
            with self.subTest(ip=ip), patch("socket.getaddrinfo", return_value=self.addresses(ip)), self.assertRaises(FetchError):
                validate_public_url("https://example.com/job", {"example.com"})

    def test_mixed_public_private_dns_rejected(self):
        with patch("socket.getaddrinfo", return_value=self.addresses("8.8.8.8") + self.addresses("127.0.0.1")), self.assertRaises(FetchError):
            validate_public_url("https://example.com/job", {"example.com"})

    def test_unauthorized_domain_scheme_port_rejected(self):
        for u in ("http://example.com/job", "https://evil.com/job", "https://example.com:8443/job", "https://example.com.evil.org/job", "https://u:p@example.com/job"):
            with self.subTest(url=u), self.assertRaises(FetchError):
                validate_public_url(u, {"example.com"})

    def test_robots_denied_no_jd_request(self):
        fake = FakeSite([Response(200, {"content-type": "text/plain"}, b"User-agent: *\nDisallow: /", "https://example.com/robots.txt")])
        fetcher = SiteFetcher({"example.com"}, fake)
        with self.assertRaisesRegex(FetchError, "robots_denied"):
            fetcher.fetch("https://example.com/job")
        self.assertEqual(len(fake.calls), 1)

    def test_robots_unavailable_fail_closed(self):
        fake = FakeSite([Response(404, {}, b"", "https://example.com/robots.txt")])
        fetcher = SiteFetcher({"example.com"}, fake)
        with self.assertRaisesRegex(FetchError, "robots_unavailable"):
            fetcher.fetch("https://example.com/job")
        self.assertEqual(len(fake.calls), 1)

    def test_robots_delay_honored(self):
        fake = FakeSite([Response(200, {}, b"User-agent: *\nAllow: /\nCrawl-delay: 5", ""),
                         Response(200, {"content-type": "text/html"}, "<h1>架构师</h1>".encode(), "")])
        SiteFetcher({"example.com"}, fake).fetch("https://example.com/job")
        self.assertEqual(fake.interval, 5)

    def test_login_challenge_blocked(self):
        fake = FakeSite([Response(200, {}, b"User-agent: *\nAllow: /", ""),
                         Response(200, {"content-type": "text/html"}, "登录后查看完整职位".encode(), "")])
        with self.assertRaisesRegex(FetchError, "login_or_challenge"):
            SiteFetcher({"example.com"}, fake).fetch("https://example.com/job")

    def test_gb18030_decoding(self):
        self.assertEqual(Response(200, {}, "招聘要求".encode("gb18030"), "").text(), "招聘要求")

    def test_redirects_not_followed_and_size_limit(self):
        class Raw:
            status = 302
            def getheaders(self): return [("Location", "https://127.0.0.1/")]
            def read(self, n): return b""
        class Conn:
            def __init__(self, *args): pass
            def request(self, *args, **kw): pass
            def getresponse(self): return Raw()
            def close(self): pass
        with patch("socket.getaddrinfo", return_value=self.addresses("8.8.8.8")), patch("vibe_job_radar.network.PinnedHTTPSConnection", Conn):
            with self.assertRaisesRegex(FetchError, "redirect_not_followed"):
                SafeHTTP({"example.com"}, interval=0).request("https://example.com/job")
        Raw.status = 200
        Raw.read = lambda self, n: b"x" * n
        with patch("socket.getaddrinfo", return_value=self.addresses("8.8.8.8")), patch("vibe_job_radar.network.PinnedHTTPSConnection", Conn):
            with self.assertRaisesRegex(FetchError, "response_too_large"):
                SafeHTTP({"example.com"}, interval=0, max_bytes=10).request("https://example.com/job")


if __name__ == "__main__":
    unittest.main()


class ServiceErrorTests(unittest.TestCase):
    def test_only_bounded_opted_in_error_codes_leave_the_transport(self):
        url='https://team.fixture.test/v1/sync/read'
        client=SafeHTTP({'team.fixture.test'},interval=0)
        allowed={409:{'team_sync_reset_required'}}
        examples=[
            (b'{"error":"team_sync_reset_required"}',409,'team_sync_reset_required'),
            (b'{"error":"secret-response"}',409,'http_409'),
            (b'{"error":"team_sync_reset_required","token":"secret"}',409,'http_409'),
            (b'{"error":["team_sync_reset_required"]}',409,'http_409'),
            (b'not JSON',409,'http_409'),
            (b' '*1024+b'{"error":"team_sync_reset_required"}',409,'http_409'),
            (b'{"error":"team_sync_reset_required"}',500,'http_500'),
        ]
        for body,status,code in examples:
            with self.subTest(status=status,body=body[:60]),patch.object(client,'request',return_value=Response(status,{},body,url)):
                with self.assertRaises(FetchError) as caught:client.json(url,error_codes=allowed)
                self.assertEqual(str(caught.exception),code)
        with patch.object(client,'request',return_value=Response(409,{},examples[0][0],url)):
            with self.assertRaisesRegex(FetchError,'http_409'):client.json(url)
