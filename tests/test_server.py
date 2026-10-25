"""Remote session boundaries and the real offline import/report path."""
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from vibe_job_radar.application import WorkbenchApplication
from vibe_job_radar.server import ServerWorkbench, OwnerSessions, validate_origin, read_password
from vibe_job_radar.workspace import Workspace

PASSWORD = "test-only-owner-password-123456789"
ORIGIN = "https://radar.example.test"


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="radar-server-test-")
        self.app = WorkbenchApplication(Workspace(Path(self.tmp.name) / "workspace"), public_client=None)
        self.now = 100.0
        self.sessions = OwnerSessions(PASSWORD, clock=lambda: self.now)
        self.server = ServerWorkbench(self.app, origin=ORIGIN, password=PASSWORD, sessions=self.sessions)
        self.cookie = ""
        self.csrf = ""

    def tearDown(self):
        self.app.close()
        self.tmp.cleanup()

    def request(self, path="/", method="GET", data=None, *, origin=ORIGIN, host="radar.example.test",
                cookie=None, token=None, raw=None, length=None, **extra):
        payload = raw if raw is not None else json.dumps(data or {}).encode()
        env = {"REQUEST_METHOD": method, "PATH_INFO": path, "HTTP_HOST": host,
               "wsgi.input": io.BytesIO(payload), "CONTENT_LENGTH": str(len(payload) if length is None else length),
               "CONTENT_TYPE": "application/json", "HTTP_COOKIE": self.cookie if cookie is None else cookie,
               "HTTP_X_RADAR_TOKEN": self.csrf if token is None else token}
        if origin is not None:
            env["HTTP_ORIGIN"] = origin
        env.update(extra)
        status, headers = [], []
        body = b"".join(self.server(env, lambda s, h: (status.append(int(s.split()[0])), headers.extend(h))))
        return status[0], dict(headers), body

    def login(self):
        status, headers, body = self.request("/api/session/login", "POST", {"password": PASSWORD}, cookie="")
        self.assertEqual(status, 200, body)
        self.cookie = headers["Set-Cookie"].split(";")[0]
        key = self.cookie.split("=", 1)[1]
        self.csrf = self.sessions.get(key).csrf
        return headers

    def test_private_routes_require_session_even_with_old_desktop_token(self):
        for path in ("/api/status", "/api/bootstrap", "/evidence-draft.js", "/api/evidence/export", "/api/public/history/export",
                     "/api/download/missing/report", "/app.js", "/server-session.js"):
            with self.subTest(path=path):
                self.assertEqual(self.request(path, token="old-desktop-token")[0], 401)
        self.assertEqual(self.request("/")[0], 303)
        self.assertEqual(self.request("/login")[0], 200)
        self.assertEqual(self.request("/healthz", host="127.0.0.1:8080", origin=None)[2], b'{"ok": true}')

    def test_login_origin_host_and_request_format_fail_closed(self):
        self.assertEqual(self.request("/api/session/login", "POST", {"password": PASSWORD}, origin=None)[0], 403)
        self.assertEqual(self.request("/api/session/login", "POST", {"password": PASSWORD}, origin="https://evil.test")[0], 403)
        self.assertEqual(self.request("/api/session/login", "POST", {"password": PASSWORD}, host="evil.test")[0], 403)
        self.assertEqual(self.request("/api/session/login", "POST", raw=b"[]")[0], 400)
        self.assertEqual(self.request("/api/session/login", "POST", raw=b"{}", length=5000)[0], 413)
        self.assertEqual(self.request("/api/session/login", "POST", {"password": PASSWORD},
                                      HTTP_SEC_FETCH_SITE="cross-site")[0], 403)

    def test_cookie_csrf_and_security_headers(self):
        headers = self.login()
        for flag in ("__Host-radar-session=", "HttpOnly", "SameSite=Strict", "Secure", "Path=/"):
            self.assertIn(flag, headers["Set-Cookie"])
        self.assertEqual(self.request("/api/status", token="")[0], 403)
        self.assertEqual(self.request("/api/status", token="incorrect")[0], 403)
        status, headers, body = self.request("/api/status")
        self.assertEqual(status, 200, body)
        self.assertEqual(json.loads(body)["runtime"]["mode"], "server")
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(headers["X-Frame-Options"], "DENY")
        self.assertNotIn(PASSWORD.encode(), body)
        self.assertEqual(self.request("/api/status", cookie=self.cookie+"; "+self.cookie)[0], 401)

    def test_bootstrap_retains_server_capabilities_without_opening_jobs(self):
        self.login()
        self.assertEqual(self.request("/api/bootstrap", token="incorrect")[0], 403)
        with patch.object(self.app.workspace, "status", side_effect=AssertionError("operational query")):
            status, _, body = self.request("/api/bootstrap")
        self.assertEqual(status, 200, body)
        value = json.loads(body)
        self.assertEqual(value["runtime"]["mode"], "server")
        self.assertFalse(value["runtime"]["desktop_browser"])
        self.assertEqual(value["acquisition_sites"], [])
        self.assertTrue(value["roles"])
        self.assertTrue(value["platforms"])
        self.assertNotIn("runs", value)
        self.assertNotIn(PASSWORD.encode(), body)

    def test_expiry_logout_and_rotation_revoke(self):
        self.login()
        old = self.cookie
        self.login()
        # Login with an existing cookie rotates it.
        status, headers, _ = self.request("/api/session/login", "POST", {"password": PASSWORD})
        self.assertEqual(status, 200)
        self.assertEqual(self.request("/api/status")[0], 401)
        self.cookie = headers["Set-Cookie"].split(";")[0]
        self.csrf = self.sessions.get(self.cookie.split("=",1)[1]).csrf
        self.assertEqual(self.request("/api/session/logout", "POST")[0], 200)
        self.assertEqual(self.request("/api/status")[0], 401)
        self.cookie = old
        self.now += 8 * 60 * 60 + 1
        self.assertEqual(self.request("/api/status")[0], 401)

    def test_rate_limit_is_bounded_without_forwarded_ip_trust(self):
        for _ in range(5):
            self.assertEqual(self.request("/api/session/login", "POST", {"password": "wrong"})[0], 401)
        self.assertEqual(self.request("/api/session/login", "POST", {"password": PASSWORD},
                                      HTTP_X_FORWARDED_FOR="1.2.3.4")[0], 429)
        self.now += 61
        self.login()

    def test_desktop_and_host_mutations_are_rejected_before_dispatch(self):
        self.login()
        with patch.object(self.server.http, "post", side_effect=AssertionError("must not dispatch")):
            for path in ("/api/guided/create", "/api/releases/install", "/api/windows/startup/enable",
                         "/api/collection/handoff_start", "/api/team/protect",
                         "/api/network/proxy-auth", "/api/network/wpad/discover"):
                with self.subTest(path=path):
                    status, _, body = self.request(path, "POST", {"consent": True})
                    self.assertEqual(status, 409, body)
        self.assertEqual(self.request("/guided")[0], 409)

    def test_html_bootstrap_is_authenticated_and_precedes_page_scripts(self):
        self.login()
        for path in ("/", "/advanced", "/quality"):
            status, _, body = self.request(path)
            self.assertEqual(status, 200)
            self.assertLess(body.index(b"/server-session.js"), body.index(b"</head>"))
            for script in (b"/network-settings.js", b"/release-updates.js", b"/windows-startup.js"):
                self.assertNotIn(script, body)
        status, _, body = self.request("/server-session.js")
        self.assertEqual(status, 200)
        self.assertIn(self.csrf.encode(), body)
        self.assertNotIn(self.cookie.encode(), body)
        self.assertNotIn(PASSWORD.encode(), body)

    def test_body_limits_strict_json_and_same_origin_mutations(self):
        self.login()
        self.assertEqual(self.request("/api/job", "POST", raw=b"{}", length=2_000_001)[0], 413)
        self.assertEqual(self.request("/api/job", "POST", raw=b"{}", length=10)[0], 400)
        self.assertEqual(self.request("/api/job", "POST", origin=None)[0], 403)
        self.assertEqual(self.request("/api/job", "POST", CONTENT_TYPE="text/plain")[0], 415)
        self.assertEqual(self.request("/api/job", "POST", HTTP_TRANSFER_ENCODING="chunked")[0], 415)
        self.assertEqual(self.request("/api/quality/open", "POST", raw=b'{"report_id":"a","report_id":"b"}')[0], 400)

    def test_uploaded_material_analysis_and_download_use_original_business_path(self):
        self.login()
        row = {"title": "时间序列算法工程师", "company": "OFFLINE SERVER FIXTURE",
               "text": "负责时间序列预测。要求熟练使用 Cursor 进行 AI 辅助编程，必须编写单元测试并进行代码审查。",
               "platform": "boss", "url": "https://www.zhipin.com/job_detail/server-fixture.html",
               "evidence_level": "full_text"}
        status, _, body = self.request("/api/import", "POST", {
            "filename":"jobs.jsonl", "content":json.dumps(row),
            "rights_note":"Offline authored fixture only", "full_text_confirmed":True})
        self.assertEqual(status, 200, body)
        self.assertEqual(json.loads(body)["new_snapshots"], 1)
        status, _, body = self.request("/api/analyze", "POST", {"dataset":"real"})
        self.assertEqual(status, 200, body)
        report = json.loads(body)
        status, _, saved = self.request("/api/report/" + report["id"])
        self.assertEqual(status, 200, saved)
        artifact = report["files"][0]
        status, headers, downloaded = self.request("/api/download/" + report["id"] + "/" + artifact)
        self.assertEqual(status, 200, downloaded[:200])
        self.assertIn("attachment;", headers["Content-Disposition"])
        self.assertGreater(len(downloaded), 0)
        self.assertEqual(self.request("/api/download/" + report["id"] + "/" + artifact, cookie="")[0], 401)


class ConfigurationTests(unittest.TestCase):
    def test_remote_http_and_ambiguous_origins_rejected(self):
        for value in ("http://example.test", "https://example.test/", "https://u:p@example.test",
                      "https://example.test?q=1", "https://example.test#x", "https://example.test:0",
                      "https://example.test:99999", "https://example.test\n"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_origin(value)
        self.assertEqual(validate_origin(ORIGIN), ORIGIN)
        self.assertEqual(validate_origin("http://localhost:8080"), "http://localhost:8080")

    def test_secret_file_must_be_regular_and_bounded(self):
        with tempfile.TemporaryDirectory() as root:
            secret = Path(root) / "password"
            secret.write_text(PASSWORD + "\n", encoding="utf-8")
            self.assertEqual(read_password(secret), PASSWORD)
            secret.write_text("short", encoding="utf-8")
            with self.assertRaises(ValueError): read_password(secret)
            secret.write_text("x" * 1025, encoding="utf-8")
            with self.assertRaises(ValueError): read_password(secret)
            with self.assertRaises(ValueError): read_password(Path(root))


if __name__ == "__main__":
    unittest.main()
