"""Frozen report/evidence access must not depend on the mutable jobs database."""
import io
import json
import tempfile
import unittest
import zipfile
from unittest.mock import patch

from vibe_job_radar.evidence_ui import EvidenceService
from vibe_job_radar.report_repository import ReportRepository
from vibe_job_radar.store import SourceReadBusy
from vibe_job_radar.workspace import InputError, Workspace
from test_workbench import capture


class ReportRepositoryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.workspace = Workspace(temporary.name)
        self.workspace.add_job(capture())
        self.source = self.workspace.analyze({})
        self.repository = ReportRepository(self.workspace.root / "reports")
        self.service = EvidenceService(self.workspace)
        row = self.source["requirements"][0]
        self.review = {"run_id": self.source["id"], "requirement_id": row["requirement_id"],
                       "decision": "reject", "reviewer": "Artificial test reviewer",
                       "reason": "Offline repository fixture", "expected_revision": 0}
        self.service.review(self.review)

    def test_broken_live_database_does_not_block_existing_evidence_or_report_generation(self):
        broken = b"artificial broken source database, not user data"
        self.workspace.db.write_bytes(broken)
        with self.assertRaisesRegex(InputError, "岗位数据库"):
            self.workspace.status()
        self.assertEqual(self.repository.read(self.source["id"]), self.source)
        self.assertEqual(self.service.state()["runs"][0]["id"], self.source["id"])
        saved = self.service.review({**self.review, "expected_revision": 1, "decision": "approve"})
        self.assertEqual(saved["revision"], 2)
        with zipfile.ZipFile(io.BytesIO(self.service.export())) as archive:
            self.assertEqual(json.loads(archive.read("revision.json"))["revision"], 2)
        report = self.service.generate({"run_id": self.source["id"], "expected_revision": 2})
        reopened = self.repository.read(report["id"])
        self.assertEqual(reopened, report)
        self.assertTrue(reopened["coverage"])
        self.assertTrue(reopened["matrix"])
        self.assertEqual(self.workspace.db.read_bytes(), broken)
        with self.assertRaisesRegex(InputError, "岗位数据库"):
            self.workspace.status()

    def test_busy_live_database_does_not_block_published_report_or_evidence_reads(self):
        with patch("vibe_job_radar.workspace.Store", side_effect=SourceReadBusy("artificial busy source")) as store:
            with self.assertRaisesRegex(InputError, "岗位数据正在变化"):
                self.workspace.status()
            store.reset_mock()
            self.assertEqual(self.workspace.report(self.source["id"]), self.source)
            self.assertEqual(self.service.state()["revision"], 1)
            self.assertTrue(self.service.export())
            store.assert_not_called()

    def test_unbound_lineage_file_does_not_turn_ordinary_report_into_personal_result(self):
        directory = self.workspace.root / "reports" / self.source["id"]
        (directory / "evidence_revision.json").write_text("{}", encoding="utf-8")
        ordinary = self.repository.read(self.source["id"])
        self.assertNotIn("coverage", ordinary)
        self.assertNotIn("matrix", ordinary)
        for run_id, name in (("../outside", "secret"), (self.source["id"], "../jobs.sqlite"),
                             (self.source["id"], "..\\jobs.sqlite")):
            with self.subTest(run_id=run_id, name=name), self.assertRaises(InputError):
                self.repository.file(run_id, name)

    def test_reopened_personal_result_rejects_changed_lineage_or_coverage_bytes(self):
        report = self.service.generate({"run_id": self.source["id"], "expected_revision": 1})
        for name in ("evidence_revision.json", "job_coverage.csv", "requirement_evidence_matrix.csv"):
            path = self.repository.file(report["id"], name)
            previous = path.read_bytes()
            path.write_bytes(previous + b"\n")
            try:
                with self.subTest(name=name), self.assertRaisesRegex(InputError, "个人报告内容已改变"):
                    self.repository.read(report["id"])
            finally:
                path.write_bytes(previous)
        self.assertEqual(self.repository.read(report["id"]), report)


if __name__ == "__main__":
    unittest.main()
