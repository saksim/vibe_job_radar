"""Offline acceptance: synthetic fixtures exercise real code, never live job platforms."""
from __future__ import annotations

import http.client
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from vibe_job_radar.workbench import LocalServer, MAX_BODY
from vibe_job_radar.workspace import InputError, Workspace


def capture(**overrides):
    # Test fixture only; deliberately uses the real-input namespace to test that path.
    data = {"title": "时间序列算法工程师", "company": "TEST FIXTURE NOT MARKET DATA",
            "text": "负责时间序列预测。要求熟练使用 Cursor 进行 AI 辅助编程，必须编写单元测试并进行代码审查。",
            "platform": "boss", "url": "https://www.zhipin.com/job_detail/test-fixture.html",
            "rights_note": "Offline test fixture authored for automated testing only",
            "evidence_level": "full_text", "full_text_confirmed": True}
    data.update(overrides)
    return data


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="radar test ")
        self.root = Path(self.tmp.name) / "workspace"
        self.workspace = Workspace(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def test_doctor_and_empty_status_are_offline(self):
        with patch("socket.create_connection", side_effect=AssertionError("network forbidden")):
            report = self.workspace.doctor()
            self.assertTrue(report["workspace_writable"])
            self.assertFalse(report["network_tested"])
            self.assertEqual(self.workspace.status()["counts"]["records"], 0)
        self.assertFalse(self.workspace.db.exists())

    def test_capture_deduplication_and_restart(self):
        first = self.workspace.add_job(capture())
        second = self.workspace.add_job(capture())
        self.assertEqual(first["new_snapshots"], 1)
        self.assertEqual(second["new_snapshots"], 0)
        self.assertIn("time_series", first["roles"])
        self.assertEqual(Workspace(self.root).status()["counts"]["full_text"], 1)

    def test_require_completeness_confirmation(self):
        with self.assertRaises(InputError):
            self.workspace.add_job(capture(full_text_confirmed=False))
        self.assertEqual(self.workspace.status()["counts"]["records"], 0)

    def test_allow_honest_snippet_without_full_text_confirmation(self):
        self.workspace.add_job(capture(evidence_level="snippet", full_text_confirmed=False))
        self.assertEqual(self.workspace.status()["counts"]["snippet"], 1)

    def test_rights_and_provenance_are_required(self):
        for invalid in ({"rights_note": ""}, {"url": "", "source_ref": ""}):
            with self.subTest(invalid=invalid), self.assertRaises(InputError):
                self.workspace.add_job(capture(**invalid))

    def test_mismatched_platform_is_rejected(self):
        with self.assertRaises(InputError):
            self.workspace.add_job(capture(platform="liepin"))

    def test_never_accept_login_credentials_as_fields(self):
        for key in ("password", "cookie", "authorization", "is_synthetic"):
            with self.subTest(key=key), self.assertRaises(InputError):
                self.workspace.add_job(capture(**{key: "secret"}))

    def test_session_tokens_in_source_url_are_rejected(self):
        with self.assertRaises(ValueError):
            self.workspace.add_job(capture(url="https://www.zhipin.com/job_detail/a.html?token=secret"))

    def test_unmatched_role_is_explained(self):
        result = self.workspace.add_job(capture(title="收银员", text="负责收银工作。"))
        self.assertIn("未匹配", result["message"])

    def test_invalid_import_does_not_partially_write(self):
        good = {k: v for k, v in capture().items() if k != "full_text_confirmed"}
        with self.assertRaises(InputError):
            self.workspace.import_file({"filename": "jobs.jsonl", "content": json.dumps(good) + '\n{"title": "missing text"}',
                                        "rights_note": "test", "full_text_confirmed": True})
        self.assertEqual(self.workspace.status()["counts"]["records"], 0)
        self.assertEqual(list((self.root / "imports").iterdir()), [])

    def test_csv_import_chinese_columns_and_filename_sandbox(self):
        result = self.workspace.import_file({"filename": "../../outside.csv",
            "content": "职位名称,平台,来源链接,职位描述\n时间序列算法工程师,boss,https://www.zhipin.com/job_detail/csv.html,要求使用 Cursor 进行AI辅助编程\n",
            "rights_note": "test fixture", "full_text_confirmed": True})
        self.assertEqual(result["new_snapshots"], 1)
        self.assertFalse((self.root.parent / "outside.csv").exists())
        self.assertEqual(len(list((self.root / "imports").iterdir())), 1)

    def test_synthetic_import_into_real_db_is_rejected(self):
        row = {k: v for k, v in capture().items() if k != "full_text_confirmed"}
        row.update(source_mode="synthetic", is_synthetic=True)
        with self.assertRaises(InputError):
            self.workspace.import_file({"filename": "jobs.json", "content": json.dumps(row),
                                        "rights_note": "test", "full_text_confirmed": True})

    def test_empty_import_and_unsupported_file_are_rejected(self):
        for name, content in (("empty.json", "[]"), ("jobs.exe", "not executable input")):
            with self.subTest(name=name), self.assertRaises(InputError):
                self.workspace.import_file({"filename": name, "content": content,
                                            "rights_note": "test", "full_text_confirmed": True})

    def test_missing_real_data_has_actionable_error(self):
        with self.assertRaisesRegex(InputError, "真实数据"):
            self.workspace.analyze({})

    def test_real_input_pipeline_and_fresh_output_directories(self):
        self.workspace.add_job(capture())
        with patch("socket.create_connection", side_effect=AssertionError("network forbidden")):
            first = self.workspace.analyze({"roles": ["time_series"]})
            second = self.workspace.analyze({"roles": ["time_series"]})
        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual(first["manifest"]["mode"], "real_sample")
        self.assertEqual(first["manifest"]["stats"]["full_text_job_groups"], 1)
        self.assertGreater(first["manifest"]["stats"]["accepted_positive_requirement_rows"], 0)
        self.assertTrue(first["requirements"])
        self.assertIn("requirements_zh.csv", first["files"])
        self.assertIn("待填", first["descriptions"])
        self.assertEqual(len(Workspace(self.root).status()["runs"]), 2)

    def test_demo_never_populates_real_database(self):
        report = self.workspace.analyze({"dataset": "demo"})
        self.assertEqual(report["manifest"]["mode"], "synthetic_demo")
        self.assertGreater(report["manifest"]["stats"]["requirement_rows"], 0)
        self.assertEqual(self.workspace.status()["counts"]["records"], 0)
        self.assertFalse(self.workspace.db.exists())

    def test_snippet_only_report_does_not_claim_full_evidence(self):
        self.workspace.add_job(capture(evidence_level="snippet", full_text_confirmed=False))
        report = self.workspace.analyze({})
        self.assertEqual(report["manifest"]["stats"]["full_text_job_groups"], 0)
        self.assertIn("没有纳入完整", report["message"])

    def test_no_matching_roles_does_not_look_like_successful_research(self):
        self.workspace.add_job(capture(title="收银员", text="负责收银。"))
        self.assertIn("没有纳入完整", self.workspace.analyze({})["message"])

    def test_plan_is_offline_and_does_not_create_records(self):
        with patch("socket.create_connection", side_effect=AssertionError("network forbidden")):
            plan = self.workspace.plan({"roles": ["time_series"], "platforms": ["boss"]})
        self.assertGreater(plan["query_count"], 0)
        self.assertEqual(plan["network_calls"], 0)
        self.assertFalse(self.workspace.db.exists())

    def test_invalid_filters_and_budgets(self):
        for data in ({"roles": []}, {"roles": "architect"}, {"platforms": ["invalid"]}):
            with self.subTest(data=data), self.assertRaises(InputError):
                self.workspace.plan(data)
        for budget in (True, 0, 21, "3"):
            with self.subTest(budget=budget), self.assertRaises(InputError):
                self.workspace.discover({"max_requests": budget, "consent_search": True})

    def test_missing_key_and_missing_consent_are_explicit(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(InputError, "Key"):
                self.workspace.discover({"consent_search": True})
            with self.assertRaisesRegex(InputError, "确认"):
                self.workspace.discover({"api_key": "not-real"})

    def test_search_adapter_with_mocked_provider_keeps_snippet(self):
        response = {"web": {"results": [{"title": "时间序列算法工程师", "description": "要求熟练使用 Cursor",
                    "url": "https://www.zhipin.com/job_detail/mock-api.html"}]},
                    "query": {"more_results_available": False}}
        with patch("vibe_job_radar.discovery.SafeHTTP") as transport:
            transport.return_value.json.return_value = response
            result = self.workspace.discover({"roles": ["time_series"], "platforms": ["boss"],
                "max_requests": 1, "consent_search": True, "api_key": "TEST-SECRET-NOT-A-REAL-KEY"})
        self.assertEqual(result["requests_made"], 1)
        self.assertEqual(self.workspace.status()["counts"]["snippet"], 1)
        self.assertEqual(self.workspace.status()["counts"]["full_text"], 0)
        self.assertNotIn("TEST-SECRET-NOT-A-REAL-KEY", json.dumps(result))
        for path in (self.root / "discovery").glob("*.json"):
            self.assertNotIn("TEST-SECRET-NOT-A-REAL-KEY", path.read_text(encoding="utf-8"))

    def test_provider_echoed_key_is_redacted_from_errors_and_events(self):
        from vibe_job_radar.store import Store
        secret = 'TEST-SECRET-"NOT-REAL'
        with patch("vibe_job_radar.discovery.SafeHTTP") as transport:
            transport.return_value.json.side_effect = ValueError("provider echoed: " + secret)
            result = self.workspace.discover({"roles": ["time_series"], "platforms": ["boss"],
                "max_requests": 1, "consent_search": True, "api_key": secret})
        self.assertNotIn(secret, str(result))
        with Store(self.workspace.db) as store:
            self.assertNotIn(secret, str(store.events()))
        for path in (self.root / "discovery").glob("*.json"):
            self.assertNotIn(secret, str(json.loads(path.read_text(encoding="utf-8"))))

    def test_status_never_returns_secret_values(self):
        with patch.dict(os.environ, {"BRAVE_SEARCH_API_KEY": "TEST-SECRET-XYZ"}):
            self.assertTrue(self.workspace.status()["brave_key_configured"])
            self.assertNotIn("TEST-SECRET-XYZ", json.dumps(self.workspace.status()))
            self.assertNotIn("TEST-SECRET-XYZ", json.dumps(self.workspace.doctor()))

    def test_report_path_traversal(self):
        for run, name in (("../outside", "secret"), ("a" * 32, "../secret"), ("a" * 32, "..\\secret")):
            with self.subTest(run=run, name=name), self.assertRaises(InputError):
                self.workspace.report_file(run, name)

    def test_symlink_download_cannot_escape_report(self):
        report = self.workspace.analyze({"dataset": "demo"})
        outside = self.root / "secret.txt"
        outside.write_text("secret", encoding="utf-8")
        link = self.root / "reports" / report["id"] / "leak.txt"
        try:
            link.symlink_to(outside)
        except (OSError, NotImplementedError):
            self.skipTest("symlink privileges unavailable on this OS")
        with self.assertRaises(InputError):
            self.workspace.report_file(report["id"], "leak.txt")

    def test_source_launcher_doctor_handles_spaces(self):
        root = Path(__file__).resolve().parents[1]
        result = subprocess.run([sys.executable, str(root / "scripts" / "start_workbench.py"),
                                 "--doctor", "--workspace", str(self.root / "spaces are fine")],
                                capture_output=True, text=True, encoding="utf-8", timeout=30,
                                env={**os.environ, "PYTHONIOENCODING": "utf-8"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(json.loads(result.stdout)["workspace_writable"])


class HTTPTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.server = LocalServer(Workspace(self.tmp.name))
        self.worker = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        self.worker.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.worker.join(timeout=5)
        self.tmp.cleanup()

    def call(self, path, data=None, *, authorized=True, headers=None, raw=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=30)
        request_headers = {"X-Radar-Token": self.server.token} if authorized else {}
        if data is not None or raw is not None:
            request_headers["Content-Type"] = "application/json"
        request_headers.update(headers or {})
        payload = raw if raw is not None else (json.dumps(data).encode() if data is not None else None)
        connection.request("POST" if payload is not None else "GET", path, body=payload, headers=request_headers)
        response = connection.getresponse()
        result = (response.status, dict(response.getheaders()), response.read())
        connection.close()
        return result

    def test_static_shell_is_public_but_data_is_not(self):
        self.assertEqual(self.call("/", authorized=False)[0], 200)
        self.assertEqual(self.call("/api/status", authorized=False)[0], 403)
        with patch.object(self.server.guided,'state',side_effect=AssertionError('status must not load private task state')):
            code, _, body = self.call("/api/status")
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(body)['acquisition_sites'],self.server.guided.registry.describe())
        self.assertEqual(json.loads(body)['counts']['records'],0)
        self.assertIn("/app.js", self.call("/")[2].decode())
        self.assertNotIn("innerHTML", self.call("/app.js")[2].decode())

    def test_host_and_origin_defend_against_cross_site_requests(self):
        self.assertEqual(self.call("/api/status", headers={"Host": "evil.example"})[0], 403)
        self.assertEqual(self.call("/api/job", capture(), headers={"Origin": "https://evil.example"})[0], 403)
        self.assertEqual(self.call("/api/status", headers={"Origin": self.server.origin})[0], 200)

    def test_request_size_and_object_validation(self):
        self.assertEqual(self.call("/api/job", raw=b"[]")[0], 400)
        self.assertEqual(self.call("/api/job", raw=b"{broken")[0], 400)
        self.assertEqual(self.call("/api/job", raw=b"{}", headers={"Content-Length": str(MAX_BODY + 1)})[0], 413)
        self.assertEqual(self.call("/api/job", raw=b"{}", headers={"Content-Type": "text/plain"})[0], 415)

    def test_mutations_are_serialized(self):
        self.server.mutation_lock.acquire()
        try:
            self.assertEqual(self.call("/api/job", capture())[0], 409)
        finally:
            self.server.mutation_lock.release()

    def test_http_end_to_end_real_capture_to_download(self):
        self.assertEqual(self.call("/api/job", capture())[0], 200)
        code, _, payload = self.call("/api/analyze", {"roles": ["time_series"]})
        self.assertEqual(code, 200, payload)
        report = json.loads(payload)
        self.assertGreater(report["manifest"]["stats"]["requirement_rows"], 0)
        url = f'/api/download/{report["id"]}/requirements_zh.csv'
        self.assertEqual(self.call(url, authorized=False)[0], 403)
        code, headers, data = self.call(url)
        self.assertEqual(code, 200)
        self.assertTrue(data)
        self.assertEqual(headers["Content-Type"], "application/octet-stream")
        self.assertIn("attachment", headers["Content-Disposition"])
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(self.call(f'/api/download/{report["id"]}/..%2f..%2fjobs.sqlite')[0], 400)


if __name__ == "__main__":
    unittest.main()
