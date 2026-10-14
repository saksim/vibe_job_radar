"""Synthetic source and labels only; these checks do not certify real human review."""
import copy
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
import zipfile
from unittest.mock import patch

from vibe_job_radar.evidence_ui import Conflict
from vibe_job_radar.quality_evaluation import evaluate_packet
from vibe_job_radar.quality_ui import QualityService
from vibe_job_radar.workspace import Workspace, InputError
from test_quality_review import fixture_report, span, TEXT


class QualityUITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.workspace = Workspace(Path(self.temp.name) / "workspace")
        self.run = "a" * 32
        self.report = self.workspace.root / "reports" / self.run
        fixture_report(self.report)
        self.original = {p.name: p.read_bytes() for p in self.report.iterdir()}
        self.service = QualityService(self.workspace)

    def open(self):
        return self.service.open({"report_id": self.run})

    def form(self, state, confirmed=False):
        doc = next(d for d in state["corpus"] if d["text"] == TEXT and d["extraction_eligible"])
        row = copy.deepcopy(next(d for d in state["annotations"]["documents"] if d["record_id"] == doc["record_id"]))
        row.update(author="Synthetic test reviewer", requirements=[span(0)], roles=["time_series"],
                   requirements_complete=True, review_state="human_confirmed" if confirmed else "pending")
        return {"report_id": self.run, "expected_revision": state["revision"], "document": row,
                "confirm_human": confirmed}

    def assert_original_unchanged(self):
        self.assertEqual({p.name: p.read_bytes() for p in self.report.iterdir()}, self.original)

    def test_open_is_offline_independent_stable_and_does_not_create_real_labels(self):
        with patch("socket.create_connection", side_effect=AssertionError("offline")):
            first = self.open()
            second = self.open()
        self.assertEqual(first["packet_id"], second["packet_id"])
        self.assertEqual(first["revision"], 0)
        self.assertFalse(any("predicted_roles" in d for d in first["corpus"]))
        self.assertTrue(all(d["requirements"] == [] for d in first["annotations"]["documents"]))
        self.assertIsNone(first["evaluation"]["extraction"]["semantic"]["precision"])
        self.assert_original_unchanged()

    def test_draft_then_explicit_human_confirmation_and_revoke_preserve_denominators(self):
        draft = self.service.save(self.form(self.open()))
        self.assertEqual(draft["evaluation"]["coverage"]["extraction_evaluated_documents"], 0)
        request = self.form(draft, True)
        request["confirm_human"] = False
        with self.assertRaises(InputError):
            self.service.save(request)
        request["confirm_human"] = True
        confirmed = self.service.save(request)
        self.assertEqual(confirmed["evaluation"]["coverage"]["extraction_evaluated_documents"], 1)
        self.assertEqual(confirmed["revision"], 2)
        revoked = self.service.save(self.form(confirmed))
        self.assertEqual(revoked["evaluation"]["coverage"]["extraction_evaluated_documents"], 0)
        self.assert_original_unchanged()

    def test_second_window_cannot_overwrite_a_saved_revision(self):
        stale = self.open()
        fresh = self.service.save(self.form(stale))
        other = QualityService(self.workspace)
        with self.assertRaises(Conflict):
            other.save(self.form(stale))
        self.assertEqual(other.open({"report_id": self.run})["revision"], fresh["revision"])
        self.assert_original_unchanged()

    def test_wrong_span_or_packet_cannot_create_a_revision(self):
        initial = self.open()
        request = self.form(initial)
        request["document"]["requirements"][0]["start"] += 1
        with self.assertRaises(ValueError):
            self.service.save(request)
        imported = copy.deepcopy(initial["annotations"])
        imported["packet_id"] = "0" * 64
        with self.assertRaises(ValueError):
            self.service.import_annotations({"report_id": self.run, "expected_revision": 0,
                                             "annotations": imported})
        with self.assertRaises(ValueError):
            self.service.import_annotations({"report_id": self.run, "expected_revision": 0,
                "annotations_json": '{"packet_id":"a","packet_id":"b"}'})
        self.assertEqual(self.open()["revision"], 0)

    def test_export_round_trips_through_original_cli_evaluator(self):
        saved = self.service.save(self.form(self.open(), True))
        archive = self.service.export({"report_id": self.run})
        output = Path(self.temp.name) / "exported"
        with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
            bundle.extractall(output)
        evaluated = evaluate_packet(output)
        shipped = json.loads((output / "quality-evaluation.json").read_text(encoding="utf-8"))
        self.assertEqual(evaluated["annotations_sha256"], shipped["annotations_sha256"])
        self.assertEqual(evaluated["extraction"], saved["evaluation"]["extraction"])
        imported = json.loads((output / "annotations.json").read_text(encoding="utf-8"))
        request = {"report_id": self.run, "expected_revision": saved["revision"], "annotations": imported}
        with self.assertRaises(InputError):
            self.service.import_annotations(request)
        request["confirm_human"] = True
        self.assertEqual(self.service.import_annotations(request)["revision"], 2)
        self.assert_original_unchanged()

    def test_corrupted_packet_is_rejected_and_input_paths_cannot_escape(self):
        initial = self.open()
        packet = next((self.service.root / self.run).iterdir())
        (packet / "corpus.json").write_text("[]", encoding="utf-8")
        with self.assertRaises(ValueError):
            self.service.save(self.form(initial))
        for bad in ("../outside", "b" * 32, "a" * 31):
            with self.assertRaises(InputError):
                self.service.open({"report_id": bad})
        self.assert_original_unchanged()


if __name__ == "__main__":
    unittest.main()
