"""Artificial labels test metric denominators; none are real human adjudications."""
import contextlib
import copy
import io
import json
from pathlib import Path
import shutil
import socket
import tempfile
import unittest
from unittest.mock import patch

from vibe_job_radar.cli import main
from vibe_job_radar.config import load_config
from vibe_job_radar.models import JobRecord
from vibe_job_radar.pipeline import analyze
from vibe_job_radar.quality_evaluation import evaluate_packet, write_evaluation
from vibe_job_radar.quality_packet import create_packet, load_packet, sha256
from vibe_job_radar.store import Store
from vibe_job_radar.utils import atomic_json, atomic_text, json_text, write_csv

FIXED = "2026-09-26T10:00:00+00:00"
TEXT = "😀必须使用 AI 辅助编程。\n不要求使用 Cursor。\n需要编写单元测试。"
QUOTES = ("必须使用 AI 辅助编程。", "不要求使用 Cursor。", "需要编写单元测试。")


def span(index, *, text=TEXT, capability=None, relation="direct", strength="required"):
    quote = QUOTES[index]
    return {"start": text.index(quote), "end": text.index(quote) + len(quote), "quote": quote,
            "capability": capability or ("ai_coding", "tool_fluency", "testing_review")[index],
            "relation": relation, "strength": strength}


def refresh_hashes(report):
    manifest = json.loads((report / "run_manifest.json").read_text(encoding="utf-8"))
    manifest["output_files_sha256"] = {n: sha256((report / n).read_bytes())
                                       for n in manifest["output_files_sha256"]}
    atomic_json(report / "run_manifest.json", manifest)


def fixture_report(root):
    root.mkdir()
    def job(key, **kwargs):
        values = dict(title="人工架构师", text=TEXT, company="人工夹具公司", platform="manual",
                      url="https://example.com/" + key, is_synthetic=True,
                      source_mode="synthetic", collected_at=FIXED)
        values.update(kwargs)
        return JobRecord(**values)
    twins = sorted([job("one"), job("two")], key=lambda j: j.record_id)
    jobs = [*twins, job("other", title="人工算法工程师", text="负责模型开发。"),
            job("unmatched", title="人工文员"), job("snippet", evidence_level="snippet"),
            job("real", is_synthetic=False, source_mode="manual"),
            job("stale", collected_at="2020-01-01T00:00:00+00:00", title="过期人工架构师")]
    audit = []
    for i, j in enumerate(jobs):
        selected = i < 3 or i == 4
        roles = ["time_series", "domain_algorithm"] if i == 2 else ["architect"] if i != 3 else []
        audit.append({"record_id": j.record_id, "roles": roles,
                      "status": "selected" if selected else {3: "role_unmatched", 5: "real_excluded_from_demo", 6: "stale_snapshot"}[i],
                      "evidence_level": j.evidence_level, "is_synthetic": j.is_synthetic,
                      "job_group_id": ("g_alpha" if i < 2 else "g_beta" if i == 2 else "g_snippet") if selected else ""})
    predictions = []
    for i in (0, 1):
        predictions.append({**span(i, strength="required" if i == 0 else "preferred"),
                            "requirement_id": "r_" + str(i), "record_id": jobs[0].record_id,
                            "job_group_id": "g_alpha", "roles": ["architect"],
                            "review_status": "rule_accepted", "is_synthetic": True, "evidence_level": "full_text",
                            "source_record_ids": [j.record_id for j in twins]})
    atomic_text(root / "jobs.jsonl", "".join(json_text(j.to_dict()).replace("\n", " ") + "\n" for j in jobs))
    atomic_text(root / "requirements.jsonl", "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in predictions))
    write_csv(root / "input_audit.csv", audit, list(audit[0]))
    duplicates = [{"job_group_id": "g_alpha", "representative_record_id": jobs[0].record_id,
                   "source_record_ids": [j.record_id for j in twins]}]
    write_csv(root / "duplicate_groups.csv", duplicates, list(duplicates[0]))
    atomic_json(root / "effective_config.json", load_config())
    atomic_json(root / "run_manifest.json", {
        "schema_version": 1, "mode": "synthetic_demo", "status": "completed", "created_at": FIXED,
        "as_of": FIXED, "filters": {"roles": None, "platforms": None},
        "stats": {"current_source_records": 7, "selected_source_records": 4, "deduplicated_groups": 3,
                  "full_text_job_groups": 2, "duplicate_groups": 1, "requirement_rows": 2,
                  "accepted_positive_requirement_rows": 2},
        "output_files_sha256": {n: "" for n in ("jobs.jsonl", "requirements.jsonl", "input_audit.csv",
                                               "duplicate_groups.csv", "effective_config.json")}})
    refresh_hashes(root)
    return [j.record_id for j in jobs]


class QualityReviewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.shared = tempfile.TemporaryDirectory()
        cls.shared_root = Path(cls.shared.name)
        cls.source = cls.shared_root / "original"
        cls.ids = fixture_report(cls.source)
        cls.base_packet = cls.shared_root / "packet"
        cls.initial = create_packet(cls.source, cls.base_packet)

    @classmethod
    def tearDownClass(cls):
        cls.shared.cleanup()

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.packet = self.base_packet
        self.value = json.loads((self.packet / "annotations.json").read_text(encoding="utf-8"))

    def document(self, index=0, state="human_confirmed", complete=True, requirements=None, roles=None):
        row = next(d for d in self.value["documents"] if d["record_id"] == self.ids[index])
        row.update(review_state=state, author="Artificial test reviewer", reviewed_at=FIXED,
                   requirements_complete=complete, requirements=requirements or [], roles=roles)
        return row

    def result(self):
        path = self.root / "annotations.json"
        path.write_text(json_text(self.value), encoding="utf-8")
        return evaluate_packet(self.packet, path)

    def pair(self, left, right, same, state="human_confirmed"):
        row = {"left": self.ids[left], "right": self.ids[right], "same_job": same, "review_state": state,
               "author": "Artificial test reviewer", "reviewed_at": FIXED, "notes": ""}
        self.value["dedup_pairs"].append(row)
        return row

    def mutable_report(self):
        return Path(shutil.copytree(self.source, self.root / "report"))

    def test_empty_packet_keeps_all_records_blank_and_metrics_unknown(self):
        self.assertEqual(self.initial["documents"], 7)
        self.assertEqual(self.initial["predictions"], 2)
        self.assertEqual(self.initial["extraction_eligible"], 2)
        self.assertEqual(self.initial["classification_eligible"], 4)
        self.assertTrue(all(d["roles"] is None and d["requirements"] == [] for d in self.value["documents"]))
        r = self.result()
        self.assertEqual(r["coverage"]["extraction_evaluated_documents"], 0)
        self.assertEqual(r["coverage"]["review_states"]["human_confirmed"], 0)
        for kind in ("span_capability", "semantic", "accepted_positive_ai"):
            self.assertIsNone(r["extraction"][kind]["precision"])
            self.assertIsNone(r["extraction"][kind]["recall"])
            self.assertIsNone(r["extraction"][kind]["f1"])
        self.assertIsNone(r["classification"]["exact_match"]["accuracy"])
        self.assertIsNone(r["deduplication"]["accuracy"])

    def test_assistant_complete_draft_never_becomes_human_gold(self):
        self.document(state="assistant_draft", requirements=[span(0)], roles=["architect"])
        r = self.result()
        self.assertEqual(r["coverage"]["review_states"]["assistant_draft"], 1)
        self.assertEqual(r["coverage"]["human_complete_documents"], 0)
        self.assertEqual(r["classification"]["reviewed_documents"], 0)
        self.assertIsNone(r["extraction"]["semantic"]["recall"])

    def test_partial_human_document_can_classify_but_cannot_hide_unchecked_misses(self):
        self.document(complete=False, requirements=[span(0)], roles=["architect"])
        r = self.result()
        self.assertEqual(r["coverage"]["human_partial_documents"], 1)
        self.assertEqual(r["coverage"]["extraction_evaluated_documents"], 0)
        self.assertEqual(r["classification"]["exact_match"]["accuracy"], 1)
        self.assertIsNone(r["extraction"]["semantic"]["recall"])

    def test_independent_missed_requirement_and_wrong_negation_have_explicit_denominators(self):
        self.document(requirements=[span(0), span(1, strength="not_required"), span(2)])
        r = self.result()["extraction"]
        self.assertEqual([r["span_capability"][k] for k in ("tp", "fp", "fn")], [2, 0, 1])
        self.assertEqual([r["semantic"][k] for k in ("tp", "fp", "fn")], [1, 1, 2])
        self.assertEqual([r["accepted_positive_ai"][k] for k in ("tp", "fp", "fn")], [1, 1, 1])
        self.assertEqual(r["accepted_positive_ai"]["precision"], .5)
        self.assertEqual(r["accepted_positive_ai"]["recall"], .5)
        self.assertEqual(r["negation"]["aligned_negative_gold"], 1)
        self.assertEqual(r["negation"]["accuracy"], 0)
        self.assertEqual(r["span_capability"]["false_negatives"][0]["capability"], "testing_review")

    def test_explicit_empty_gold_counts_false_positives_without_inventing_recall(self):
        self.document()
        r = self.result()["extraction"]["semantic"]
        self.assertEqual([r[k] for k in ("tp", "fp", "fn", "predicted", "gold")], [0, 2, 0, 2, 0])
        self.assertEqual(r["precision"], 0)
        self.assertIsNone(r["recall"])

    def test_completed_zero_prediction_zero_gold_document_still_has_no_rate_denominator(self):
        self.document(2)
        r = self.result()
        self.assertEqual(r["coverage"]["extraction_evaluated_documents"], 1)
        self.assertIsNone(r["extraction"]["semantic"]["f1"])
        self.assertIsNone(r["extraction"]["negation"]["accuracy"])

    def test_strict_unicode_boundaries_preserve_non_bmp_codepoints(self):
        row = span(0)
        self.assertEqual(row["start"], 1)
        row.update(start=0, quote="😀" + row["quote"])
        self.document(requirements=[row])
        r = self.result()["extraction"]["span_capability"]
        self.assertEqual([r[k] for k in ("tp", "fp", "fn")], [0, 2, 1])

    def test_duplicate_non_representative_cannot_double_extraction_denominator(self):
        self.document(0, requirements=[span(0)])
        self.document(1, requirements=[span(0)])
        r = self.result()
        self.assertEqual(r["coverage"]["human_complete_documents"], 2)
        self.assertEqual(r["coverage"]["extraction_evaluated_documents"], 1)
        self.assertEqual(r["coverage"]["human_complete_but_extraction_ineligible"], 1)
        self.assertEqual(r["extraction"]["span_capability"]["predicted"], 2)

    def test_unmatched_fulltext_can_expose_classification_miss_without_fake_extraction_fn(self):
        self.document(3, requirements=[span(0)], roles=["architect"])
        r = self.result()
        self.assertEqual(r["extraction"]["span_capability"]["fn"], 0)
        self.assertEqual(r["coverage"]["extraction_evaluated_documents"], 0)
        self.assertEqual(r["classification"]["per_role"]["architect"]["fn"], 1)

    def test_role_multilabel_counts_and_empty_labels_are_independent_of_requirement_review(self):
        self.document(2, complete=False, roles=["time_series", "architect"])
        self.document(0, complete=False, roles=[])
        r = self.result()["classification"]
        self.assertEqual(r["reviewed_documents"], 2)
        self.assertEqual([r["micro"][k] for k in ("tp", "fp", "fn")], [1, 2, 1])
        self.assertEqual(r["exact_match"]["accuracy"], 0)
        self.assertEqual(r["per_role"]["domain_algorithm"]["fp"], 1)

    def test_namespace_and_snippets_are_explicitly_ineligible(self):
        for i in (4, 5):
            self.document(i, requirements=[span(0)], roles=["architect"])
        r = self.result()
        self.assertEqual(r["coverage"]["human_complete_but_extraction_ineligible"], 2)
        self.assertEqual(r["classification"]["reviewed_documents"], 0)

    def test_dedup_declared_comparable_pairs_and_unassessable_pairs_stay_separate(self):
        self.pair(0, 1, True)
        self.pair(0, 2, True)
        self.pair(0, 4, False)
        self.pair(0, 3, False)
        r = self.result()["deduplication"]
        self.assertEqual(r["comparable_human_pairs"], 2)
        self.assertEqual(len(r["unassessable_pairs"]), 2)
        self.assertEqual([r["scores"][k] for k in ("tp", "fp", "fn")], [1, 0, 1])
        self.assertEqual(r["accuracy"], .5)

    def test_false_merge_and_true_negative_do_not_inflate_pair_recall(self):
        self.pair(0, 1, False)
        self.pair(0, 2, False)
        r = self.result()["deduplication"]
        self.assertEqual(r["scores"]["fp"], 1)
        self.assertIsNone(r["scores"]["recall"])
        self.assertEqual(r["accuracy"], .5)

    def test_draft_pairs_never_enter_scores(self):
        self.pair(0, 1, True, "assistant_draft")
        self.pair(0, 2, True, "pending")
        r = self.result()["deduplication"]
        self.assertEqual(r["comparable_human_pairs"], 0)
        self.assertIsNone(r["scores"]["precision"])

    def test_bad_document_span_labels_and_confirmation_are_rejected(self):
        original = copy.deepcopy(self.value)
        mutations = [
            lambda d: d.update(requirements_complete="true"),
            lambda d: d.update(author=""),
            lambda d: d.update(reviewed_at="2026-09-26T10:00:00"),
            lambda d: d.update(review_state="approved"),
            lambda d: d.update(roles=["invented"]),
            lambda d: d.update(roles=["architect", "architect"]),
            lambda d: d.update(extra="unknown"),
            lambda d: d["requirements"][0].update(start=True),
            lambda d: d["requirements"][0].update(end=99999),
            lambda d: d["requirements"][0].update(quote="wrong"),
            lambda d: d["requirements"][0].update(capability="invented"),
            lambda d: d["requirements"][0].update(relation=[]),
            lambda d: d["requirements"][0].update(strength="mandatory"),
            lambda d: d["requirements"][0].update(extra=True),
            lambda d: d["requirements"].append(copy.deepcopy(d["requirements"][0])),
        ]
        for mutate in mutations:
            with self.subTest(mutation=mutations.index(mutate)):
                self.value = copy.deepcopy(original)
                row = self.document(requirements=[span(0)])
                mutate(row)
                with self.assertRaises(ValueError):
                    self.result()

    def test_missing_unknown_duplicate_record_and_wrong_packet_are_rejected(self):
        original = copy.deepcopy(self.value)
        mutations = [
            lambda a: a["documents"].pop(),
            lambda a: a["documents"].append(copy.deepcopy(a["documents"][0])),
            lambda a: a["documents"][0].update(record_id="unknown"),
            lambda a: a.update(packet_id="other"),
            lambda a: a.update(schema_version=True),
            lambda a: a.update(extra=True),
        ]
        for mutate in mutations:
            with self.subTest(mutation=mutations.index(mutate)):
                self.value = copy.deepcopy(original)
                mutate(self.value)
                with self.assertRaises(ValueError):
                    self.result()

    def test_bad_pairs_and_reversed_duplicate_are_rejected_even_if_drafts(self):
        original = copy.deepcopy(self.value)
        mutations = [
            lambda p: p.update(left="unknown"),
            lambda p: p.update(right=p["left"]),
            lambda p: p.update(same_job=1),
            lambda p: p.update(same_job=None),
            lambda p: p.update(extra=True),
        ]
        for mutate in mutations:
            with self.subTest(mutation=mutations.index(mutate)):
                self.value = copy.deepcopy(original)
                mutate(self.pair(0, 1, True))
                with self.assertRaises(ValueError):
                    self.result()
        self.value = copy.deepcopy(original)
        self.pair(0, 1, True)
        self.pair(1, 0, True, "assistant_draft")
        with self.assertRaisesRegex(ValueError, "reversed"):
            self.result()

    def test_known_duplicate_sources_do_not_inflate_classification_denominator(self):
        self.document(0, complete=False, roles=["architect"])
        self.document(1, complete=False, roles=["architect"])
        r = self.result()
        self.assertEqual(r["classification"]["reviewed_documents"], 1)
        self.assertEqual(r["classification"]["micro"]["tp"], 1)
        self.assertEqual(r["coverage"]["roles_declared_but_ineligible"], 1)

    def test_negative_misses_spurious_predictions_and_wrong_negative_kind_are_separate(self):
        report = self.mutable_report()
        rows = [json.loads(line) for line in (report / "requirements.jsonl").read_text(encoding="utf-8").splitlines()]
        for row in rows:
            row["strength"] = "not_required"
        atomic_text(report / "requirements.jsonl", "".join(json.dumps(row) + "\n" for row in rows))
        manifest = json.loads((report / "run_manifest.json").read_text(encoding="utf-8"))
        manifest["stats"]["accepted_positive_requirement_rows"] = 0
        atomic_json(report / "run_manifest.json", manifest)
        refresh_hashes(report)
        self.packet = self.root / "negative"
        create_packet(report, self.packet)
        self.value = json.loads((self.packet / "annotations.json").read_text(encoding="utf-8"))
        self.document(requirements=[span(0), span(1, strength="prohibited"), span(2, strength="not_required")])
        r = self.result()["extraction"]["negation"]
        self.assertEqual(r["aligned_negative_gold"], 1)
        self.assertEqual(r["accuracy"], 0)
        self.assertEqual(r["missed_negative_gold"], 1)
        self.assertEqual(r["spurious_negative_predictions"], 1)
        self.assertEqual(r["wrong_negative_kind"], 1)

    def test_saved_pending_and_rejected_predictions_do_not_enter_accepted_positive_metric(self):
        report = self.mutable_report()
        rows = [json.loads(line) for line in (report / "requirements.jsonl").read_text(encoding="utf-8").splitlines()]
        for row, status in zip(rows, ("needs_review", "rejected")):
            row["review_status"] = status
        atomic_text(report / "requirements.jsonl", "".join(json.dumps(row) + "\n" for row in rows))
        manifest = json.loads((report / "run_manifest.json").read_text(encoding="utf-8"))
        manifest["stats"]["accepted_positive_requirement_rows"] = 0
        atomic_json(report / "run_manifest.json", manifest)
        refresh_hashes(report)
        self.packet = self.root / "pending"
        create_packet(report, self.packet)
        self.value = json.loads((self.packet / "annotations.json").read_text(encoding="utf-8"))
        self.document(requirements=[span(0), span(1, strength="preferred")])
        r = self.result()["extraction"]
        self.assertEqual(r["semantic"]["tp"], 2)
        self.assertEqual(r["accepted_positive_ai"]["predicted"], 0)
        self.assertEqual(r["accepted_positive_ai"]["fn"], 2)

    def test_original_hash_mismatch_is_rejected_before_any_output_directory(self):
        report = self.mutable_report()
        (report / "requirements.jsonl").write_text("", encoding="utf-8")
        target = self.root / "bad"
        with self.assertRaisesRegex(ValueError, "source hash mismatch"):
            create_packet(report, target)
        self.assertFalse(target.exists())

    def test_packet_tampering_and_identity_changes_fail_closed(self):
        packet = Path(shutil.copytree(self.packet, self.root / "copy"))
        path = packet / "predictions.json"
        original = path.read_bytes()
        path.write_text("[]", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "hash mismatch"):
            evaluate_packet(packet)
        path.write_bytes(original)
        path = packet / "packet_manifest.json"
        manifest = json.loads(path.read_text(encoding="utf-8"))
        manifest["packet_id"] = "changed"
        path.write_text(json_text(manifest), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "identity mismatch"):
            load_packet(packet)

    def test_missing_duplicate_membership_and_counts_are_rejected_not_regrouped(self):
        report = self.mutable_report()
        (report / "duplicate_groups.csv").write_text("job_group_id,representative_record_id,source_record_ids\n", encoding="utf-8")
        refresh_hashes(report)
        with self.assertRaisesRegex(ValueError, "membership is incomplete"):
            create_packet(report, self.root / "bad")
        self.assertFalse((self.root / "bad").exists())

    def test_legacy_groups_use_saved_pairs_and_singletons_without_current_rules(self):
        import csv
        report = self.mutable_report()
        with (report / "input_audit.csv").open(encoding="utf-8-sig", newline="") as stream:
            rows = list(csv.DictReader(stream))
        fields = [k for k in rows[0] if k != "job_group_id"]
        write_csv(report / "input_audit.csv", rows, fields)
        refresh_hashes(report)
        target = self.root / "legacy"
        before = {p.name: p.read_bytes() for p in report.iterdir()}
        with patch("vibe_job_radar.config.detect_roles", side_effect=AssertionError("must not reclassify")), \
             patch.object(JobRecord, "fingerprint", property(lambda _: (_ for _ in ()).throw(AssertionError("must not regroup")))):
            result = create_packet(report, target)
        self.assertEqual(result["extraction_eligible"], 2)
        _, data = load_packet(target)
        self.assertEqual(data["source"]["grouping_basis"], "saved_duplicate_groups_and_remaining_singletons")
        self.assertEqual(before, {p.name: p.read_bytes() for p in report.iterdir()})

    def test_output_overwrite_and_writing_inside_original_report_are_refused(self):
        with self.assertRaisesRegex(ValueError, "new directory"):
            create_packet(self.source, self.packet)
        with self.assertRaisesRegex(ValueError, "outside"):
            create_packet(self.source, self.source / "nested")
        with self.assertRaisesRegex(ValueError, "outside"):
            write_evaluation(self.packet, self.source / "quality.json")
        out = self.root / "score.json"
        write_evaluation(self.packet, out)
        before = out.read_bytes()
        with self.assertRaisesRegex(ValueError, "new file"):
            write_evaluation(self.packet, out)
        self.assertEqual(out.read_bytes(), before)

    def test_interrupted_packet_write_has_no_complete_manifest_and_preserves_partial_files(self):
        from vibe_job_radar import quality_packet
        target = self.root / "interrupted"
        real = quality_packet.write_new
        calls = []
        def writing(path, raw):
            calls.append(path)
            if len(calls) == 3:
                raise OSError("synthetic write failure")
            return real(path, raw)
        with patch.object(quality_packet, "write_new", side_effect=writing):
            with self.assertRaises(OSError):
                create_packet(self.source, target)
        self.assertTrue((target / "corpus.json").is_file())
        self.assertFalse((target / "packet_manifest.json").exists())
        with self.assertRaises(FileNotFoundError):
            load_packet(target)

    def test_duplicate_json_keys_in_annotations_are_not_silently_accepted(self):
        path = self.root / "bad.json"
        path.write_text('{"schema_version":1,"schema_version":1}', encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "duplicate JSON"):
            evaluate_packet(self.packet, path)

    def test_actual_pipeline_and_cli_remain_offline_immutable_and_filter_scoped(self):
        db, report, packet = self.root / "jobs.sqlite", self.root / "real-pipeline", self.root / "pack"
        jobs = [
            JobRecord(title="时间序列算法架构师", text=TEXT, company="人工", source_mode="synthetic",
                      is_synthetic=True, collected_at=FIXED),
            JobRecord(title="未匹配职位", text=TEXT, company="人工", source_mode="synthetic",
                      is_synthetic=True, collected_at=FIXED),
        ]
        with Store(db) as store:
            for j in jobs:
                store.add(j)
        with patch.object(socket.socket, "connect", side_effect=AssertionError("offline only")):
            analyze(db, report, demo_mode=True, as_of=FIXED, role_filter=["time_series"])
            before = {p.name: sha256(p.read_bytes()) for p in report.iterdir()}
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(["quality-pack", "--report", str(report), "--out", str(packet)]), 0)
                self.assertEqual(main(["quality-evaluate", "--packet", str(packet),
                                       "--out", str(self.root / "score.json")]), 0)
            _, data = load_packet(packet)
            self.assertEqual(data["source"]["role_scope"], ["time_series"])
            predicted_roles = [d["predicted_roles"] for d in data["corpus"]]
            self.assertIn(["time_series"], predicted_roles)
            self.assertNotIn("architect", {r for rows in predicted_roles for r in rows})
            self.assertEqual(before, {p.name: sha256(p.read_bytes()) for p in report.iterdir()})
        self.assertEqual(json.loads((self.root / "score.json").read_text(encoding="utf-8"))["coverage"]["review_states"]["human_confirmed"], 0)


if __name__ == "__main__":
    unittest.main()
