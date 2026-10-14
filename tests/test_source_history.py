"""Synthetic catalogs verify history; no real-source quality or availability claim."""
from contextlib import closing
import json
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from vibe_job_radar.local_public import LocalPublicDataClient
from vibe_job_radar.network import FetchError, JSONRepresentation
from vibe_job_radar.public_boards import ANTHROPIC, CLOUDFLARE, CURSOR
from vibe_job_radar.public_contract import ContractError
from vibe_job_radar.source_history import SourceHistory, RETAINED
from vibe_job_radar.workspace import Workspace, InputError
from test_local_public import payload, query
from test_public_sources import cloudflare_payload
from test_public_ashby import cursor_payload
from test_public_revalidation import FixtureTransport
import test_workbench as http_fixtures


class SourceHistoryTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.workspace = Workspace(tmp.name)
        self.now = [1700000000]
        self.wire = FixtureTransport()
        self.client = LocalPublicDataClient(self.workspace, transport=self.wire, clock=lambda:self.now[0])
        self.history = SourceHistory(self.client.root)

    def source(self, board=ANTHROPIC):
        return next(row for row in self.client.history()["sources"] if row["source"] == board.source.key)

    def fetch(self, **options):
        return self.client.search(query(**options), consent=True)

    def test_view_is_offline_lazy_and_cache_and_paging_do_not_add_observations(self):
        with patch("socket.create_connection", side_effect=AssertionError("offline")):
            self.assertEqual(self.source()["total_observations"], 0)
        self.assertFalse(self.client.root.exists())
        first = self.fetch(limit=1)
        self.fetch(query="PRIVATE QUERY SHOULD NOT BE IN HISTORY")
        self.fetch(limit=1, cursor=first["response"]["next_cursor"])
        self.assertEqual(len(self.wire.calls), 1)
        row = self.source()
        self.assertEqual(row["total_observations"], 1)
        self.assertEqual(row["history"][0]["change_status"], "baseline")
        serialized = json.dumps(self.client.history())
        for secret in ("PRIVATE QUERY", "ARTIFICIAL", "80000", "https://", "Cursor for"):
            self.assertNotIn(secret, serialized)

    def test_refresh_304_restart_and_shape_change_have_separate_times_and_counts(self):
        self.fetch()
        original_cache = self.client.path.read_bytes()
        self.now[0] += 601
        self.wire.answer = JSONRepresentation(304, None, '"fixture-v1"')
        self.fetch()
        row = self.source()
        self.assertEqual(row["history"][0]["kind"], "not_modified")
        self.assertEqual(row["last_confirmed_at"], self.now[0])
        self.assertEqual(row["content_observed_at"], 1700000000)
        self.assertEqual(self.client.path.read_bytes(), original_cache)
        self.now[0] += 601
        changed = payload(5)
        changed["jobs"][0]["content"] += "<p>New synthetic duty.</p>"
        changed["new_metadata_field"] = "never persisted"
        self.wire.answer = JSONRepresentation(200, changed, '"fixture-v2"')
        self.fetch()
        restarted = LocalPublicDataClient(self.workspace, transport=self.wire, clock=lambda:self.now[0])
        latest = restarted.history()["sources"][0]["history"][0]
        self.assertEqual(latest["counts"]["updated"], 1)
        self.assertTrue(latest["shape_changed"])
        self.assertEqual(latest["total"], 5)
        self.assertEqual(self.source()["total_observations"], 3)
        self.assertNotIn("never persisted", json.dumps(restarted.history()))

    def test_failures_remain_visible_without_advancing_valid_catalog(self):
        self.fetch()
        original = self.client.path.read_bytes()
        self.now[0] += 601
        self.wire.answer = FetchError("http_503")
        stale = self.fetch()
        self.assertTrue(stale["stale"])
        self.assertEqual(self.source()["history"][0]["kind"], "fetch_error")
        self.now[0] += 601
        self.wire.answer = JSONRepresentation(200, {"jobs": []}, None)
        with self.assertRaises(ContractError):
            self.fetch()
        row = self.source()
        self.assertEqual(row["history"][0]["kind"], "structure_error")
        self.assertEqual(row["last_confirmed_at"], 1700000000)
        self.assertEqual(self.client.path.read_bytes(), original)
        self.assertTrue(row["stale"])

    def test_sources_are_isolated_and_retention_reports_total_without_deleting_cache(self):
        first = self.fetch()
        value = json.loads(self.client.path.read_text(encoding="utf-8"))
        for i in range(RETAINED + 1):
            self.history.remember(ANTHROPIC, "not_modified", self.now[0] + i + 1, snapshot=value)
        self.now[0] += RETAINED + 2
        row = self.source()
        self.assertEqual(row["retained"], RETAINED)
        self.assertEqual(row["total_observations"], RETAINED + 2)
        self.assertEqual(self.source(CLOUDFLARE)["retained"], 0)
        self.assertEqual(self.source(CURSOR)["retained"], 0)
        self.assertEqual(json.loads(self.client.path.read_text(encoding="utf-8")), value)
        self.assertFalse(row["stale"])
        for board, content in ((CLOUDFLARE, cloudflare_payload()), (CURSOR, cursor_payload())):
            self.now[0] += 31
            self.wire.answer = JSONRepresentation(200, content, None)
            self.fetch(source_scope=(board.source.key,))
            self.assertEqual(self.source(board)["retained"], 1)
            self.assertEqual(self.source(board)["history"][0]["adapter"], board.adapter_version)
        self.assertEqual(self.source()["retained"], RETAINED)

    def test_history_failure_is_explicit_but_does_not_remove_original_acquisition(self):
        self.client.root.mkdir()
        self.history.path.write_bytes(b"broken optional history")
        result = self.fetch()
        self.assertEqual(result["returned_jobs"], 3)
        self.assertIn("来源历史无法保存", result["history_warning"])
        with self.assertRaises(InputError):
            self.client.history()
        self.assertEqual(self.history.path.read_bytes(), b"broken optional history")
        self.assertTrue(self.client.path.exists())

    def test_truncated_history_cannot_restart_with_a_healthy_zero_total(self):
        self.fetch()
        self.history.path.write_bytes(b"")
        self.now[0] += 601
        result = self.fetch()
        self.assertIn("来源历史无法保存", result["history_warning"])
        self.assertEqual(result["returned_jobs"], 3)
        with self.assertRaises(InputError):
            self.client.history()
        self.assertEqual(self.history.path.read_bytes(), b"")

    def test_clock_rollback_and_metadata_corruption_cannot_claim_freshness(self):
        self.fetch()
        self.now[0] -= 1
        self.assertTrue(self.source()["clock_rollback"])
        self.assertTrue(self.source()["stale"])
        with closing(sqlite3.connect(self.history.path)) as connection, connection:
            raw = connection.execute("SELECT payload FROM observations LIMIT 1").fetchone()[0]
            value = json.loads(raw); value["counts"]["current_total"] += 1
            connection.execute("UPDATE observations SET payload=?", (json.dumps(value),))
        with self.assertRaises(InputError):
            self.client.history()


class SourceHistoryHTTPTests(unittest.TestCase):
    setUp = http_fixtures.HTTPTests.setUp
    tearDown = http_fixtures.HTTPTests.tearDown
    call = http_fixtures.HTTPTests.call

    def test_history_routes_are_authenticated_and_export_matches_offline_view(self):
        self.assertEqual(self.call("/api/public/history", authorized=False)[0], 403)
        self.assertEqual(self.call("/api/public/history/export", authorized=False)[0], 403)
        with patch("vibe_job_radar.network.SafeHTTP.request", side_effect=AssertionError("no fetch")):
            status, _, raw = self.call("/api/public/history")
            self.assertEqual(status, 200)
            value = json.loads(raw)
            status, headers, raw = self.call("/api/public/history/export")
            self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw), value)
        self.assertEqual(len(value["sources"]), 3)
        self.assertIn("attachment", headers["Content-Disposition"])
        self.assertFalse((self.server.workspace.root / "local_public").exists())


if __name__ == "__main__":
    unittest.main()
