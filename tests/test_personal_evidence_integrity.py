"""Artificial local evidence; publication failures must not resemble completed work."""
import hashlib
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from vibe_job_radar import evidence_ui
from vibe_job_radar.evidence_ui import EvidenceService
from vibe_job_radar.workspace import InputError, Workspace
from test_workbench import capture


class PersonalEvidenceIntegrityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.workspace = Workspace(temporary.name)
        self.workspace.add_job(capture())
        self.source = self.workspace.analyze({})["id"]
        self.service = EvidenceService(self.workspace)
        row = self.service.catalogue({"run_id": self.source})["rows"][0]
        self.service.review({"run_id": self.source, "requirement_id": row["requirement_id"],
                             "decision": "reject", "reviewer": "Artificial test reviewer",
                             "reason": "Artificial storage fixture", "expected_revision": 0})
        self.source_files = {p.name: p.read_bytes() for p in
                             (self.workspace.root / "reports" / self.source).iterdir()}

    def generate(self):
        return self.service.generate({"run_id": self.source, "expected_revision": 1})

    def assert_source_only(self):
        self.assertEqual({r["id"] for r in self.workspace.status()["runs"]}, {self.source})
        self.assertEqual({p.name: p.read_bytes() for p in
                          (self.workspace.root / "reports" / self.source).iterdir()}, self.source_files)

    def test_report_stays_private_until_lineage_and_hashes_are_complete(self):
        original = evidence_ui.analyze
        observations = []
        def observe(*args, **kwargs):
            result = original(*args, **kwargs)
            observations.append({r["id"] for r in self.workspace.status()["runs"]})
            return result
        with patch.object(evidence_ui, "analyze", side_effect=observe):
            report = self.generate()
        self.assertEqual(observations, [{self.source}])
        lineage = json.loads(self.workspace.report_file(report["id"], "evidence_revision.json").read_text(encoding="utf-8"))
        self.assertEqual((lineage["source_run_id"], lineage["revision"]), (self.source, 1))
        self.assertEqual(len(self.workspace.status()["runs"]), 2)
        for name, digest in report["manifest"]["output_files_sha256"].items():
            self.assertEqual(hashlib.sha256(self.workspace.report_file(report["id"], name).read_bytes()).hexdigest(), digest)

    def test_lineage_write_failure_cannot_leave_a_completed_report(self):
        original = evidence_ui.atomic_json
        def fail(path, value):
            if Path(path).name == "evidence_revision.json":
                raise OSError("artificial lineage write failure")
            return original(path, value)
        with patch.object(evidence_ui, "atomic_json", side_effect=fail):
            with self.assertRaisesRegex(OSError, "artificial lineage"):
                self.generate()
        self.assert_source_only()
        self.assertEqual(list((self.workspace.root / "reports").glob(".evidence-*")), [])

    def test_final_manifest_failure_cannot_leave_a_completed_report(self):
        original = evidence_ui.atomic_json
        def fail(path, value):
            if Path(path).name == "run_manifest.json":
                raise OSError("artificial final manifest failure")
            return original(path, value)
        with patch.object(evidence_ui, "atomic_json", side_effect=fail):
            with self.assertRaisesRegex(OSError, "artificial final manifest"):
                self.generate()
        self.assert_source_only()

    def test_truncated_history_is_rejected_without_reinitializing_it(self):
        self.service.db.write_bytes(b"")
        with self.assertRaises(InputError):
            EvidenceService(self.workspace)
        self.assertEqual(self.service.db.read_bytes(), b"")

    def test_missing_history_table_is_rejected_without_repairing_it(self):
        with closing(sqlite3.connect(self.service.db)) as connection, connection:
            connection.execute("DROP TABLE revisions")
        previous = self.service.db.read_bytes()
        with self.assertRaises(InputError):
            EvidenceService(self.workspace)
        self.assertEqual(self.service.db.read_bytes(), previous)

    def test_existing_service_does_not_recreate_a_removed_database(self):
        self.service.db.unlink()
        with self.assertRaises(InputError):
            self.service.state()
        self.assertFalse(self.service.db.exists())

    def test_valid_legacy_history_reopens_without_rewriting_original_bytes(self):
        with closing(sqlite3.connect(self.service.db)) as connection, connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 0)
        previous = self.service.db.read_bytes()
        reopened = EvidenceService(self.workspace).state()
        self.assertEqual(reopened["revision"], 1)
        self.assertEqual(len(reopened["reviews"]), 1)
        self.assertEqual(self.service.db.read_bytes(), previous)

    def test_restart_with_missing_artifact_free_history_does_not_create_revision_zero(self):
        self.assertEqual(list(self.service.artifacts.iterdir()), [])
        self.service.db.unlink()
        with self.assertRaises(InputError):
            EvidenceService(self.workspace)
        self.assertFalse(self.service.db.exists())

    def test_sqlite_maintenance_statistics_preserve_valid_history(self):
        with closing(sqlite3.connect(self.service.db)) as connection, connection:
            connection.execute("ANALYZE")
            self.assertIsNotNone(connection.execute("SELECT name FROM sqlite_master WHERE name='sqlite_stat1'").fetchone())
        previous = self.service.db.read_bytes()
        reopened = EvidenceService(self.workspace).state()
        self.assertEqual(reopened["revision"], 1)
        self.assertEqual(len(reopened["reviews"]), 1)
        self.assertEqual(self.service.db.read_bytes(), previous)

    def test_input_cleanup_failure_does_not_publish_a_completed_report(self):
        original = tempfile.TemporaryDirectory.cleanup
        def fail_input_cleanup(temporary):
            original(temporary)
            if Path(temporary.name).name.startswith(".evidence-input-"):
                raise OSError("artificial input cleanup failure")
        with patch.object(tempfile.TemporaryDirectory, "cleanup", fail_input_cleanup):
            with self.assertRaisesRegex(OSError, "artificial input cleanup"):
                self.generate()
        self.assert_source_only()
        self.assertEqual(list((self.workspace.root / "reports").glob(".evidence-*")), [])


if __name__ == "__main__":
    unittest.main()
