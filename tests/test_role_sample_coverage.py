"""Synthetic coverage fixtures; original selection and group identities stay authoritative."""
import copy
import csv
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from vibe_job_radar.models import JobRecord, Requirement
from vibe_job_radar.pipeline import analyze
from vibe_job_radar.research_brief import role_sample_counts, brief_markdown
from vibe_job_radar.store import Store


def audit(ident="j1", group="g1", roles=None, status="selected", level="full_text"):
    return {"record_id": ident, "job_group_id": group, "roles": roles or ["architect"],
            "status": status, "evidence_level": level}


def requirement(ident, *, group="g1", roles=None, review="rule_accepted",
                strength="required", relation="direct", level="full_text"):
    return Requirement(requirement_id=ident, record_id="j1", job_group_id=group,
        capability="ai_coding", quote="人工要求", start=0, end=4, relation=relation,
        strength=strength, tools=[], rule_score=.9, extraction_method="fixture",
        review_status=review, roles=roles or ["architect"], evidence_level=level,
        is_synthetic=True, platform="manual", title="人工架构师", company="", url="")


def counts(rows, requirements=(), roles=("domain_algorithm", "time_series", "architect"), **stats):
    return role_sample_counts(rows, list(requirements), list(roles),
        {"current_source_records": len(rows),
         "selected_source_records": sum(a["status"] == "selected" for a in rows), **stats})


class RoleSampleCountsTests(unittest.TestCase):
    def test_source_duplicates_and_capability_rows_never_inflate_job_denominators(self):
        result = counts([audit(), audit("j2")],
                        [requirement("r1"), requirement("r2"), requirement("r3")])["architect"]
        self.assertEqual(result["sample_counts"], {
            "selected_source_records": 2, "full_text_job_groups": 1,
            "vibe_evidence_job_groups": 1, "requirement_rows": 3,
            "review_queue_rows": 0, "rule_accepted_positive_rows": 3,
            "human_approved_positive_rows": 0})
        self.assertEqual(result["sample_status"], "sample_observed")

    def test_pending_negative_related_rejected_and_human_confirmation_stay_separate(self):
        rows = [requirement("rule"), requirement("human", review="approved"),
                requirement("pending", review="needs_review"),
                requirement("negative", strength="prohibited"),
                requirement("ordinary", relation="role_related"),
                requirement("rejected", review="rejected")]
        result = counts([audit()], rows)["architect"]["sample_counts"]
        self.assertEqual(result["requirement_rows"], 6)
        self.assertEqual(result["review_queue_rows"], 1)
        self.assertEqual(result["rule_accepted_positive_rows"], 1)
        self.assertEqual(result["human_approved_positive_rows"], 1)
        self.assertEqual(result["vibe_evidence_job_groups"], 1)

    def test_full_text_without_accepted_ai_differs_from_absent_full_text(self):
        result = counts([audit()], [requirement("negative", strength="not_required")])
        self.assertEqual(result["architect"]["sample_status"], "no_ai_evidence")
        self.assertEqual(result["architect"]["sample_counts"]["full_text_job_groups"], 1)
        self.assertEqual(result["time_series"]["sample_status"], "no_full_text")
        self.assertEqual(result["time_series"]["sample_counts"]["full_text_job_groups"], 0)

    def test_excluded_snapshots_and_snippets_do_not_fill_full_text_coverage(self):
        rows = [audit(level="snippet"), audit("j2", "g2", status="stale_snapshot"),
                audit("j3", "g3", status="synthetic_excluded"),
                audit("j4", "g4", status="role_unmatched")]
        result = counts(rows, [requirement("snippet", level="snippet")])["architect"]
        self.assertEqual(result["sample_counts"]["selected_source_records"], 1)
        self.assertEqual(result["sample_counts"]["full_text_job_groups"], 0)
        self.assertEqual(result["sample_counts"]["vibe_evidence_job_groups"], 0)
        self.assertEqual(result["sample_status"], "no_full_text")

    def test_multirole_job_is_counted_once_in_each_selected_direction(self):
        roles = ["time_series", "architect"]
        result = counts([audit(roles=roles)], [requirement("both", roles=roles)], roles=roles)
        self.assertEqual(set(result), set(roles))
        self.assertEqual([r["sample_counts"]["full_text_job_groups"] for r in result.values()], [1, 1])
        self.assertEqual([r["sample_counts"]["vibe_evidence_job_groups"] for r in result.values()], [1, 1])

    def test_requested_role_never_borrows_other_role_samples(self):
        result = counts([audit()], [requirement("r1")], roles=["time_series"])
        self.assertEqual(set(result), {"time_series"})
        self.assertEqual(result["time_series"]["sample_counts"]["requirement_rows"], 0)
        self.assertEqual(result["time_series"]["sample_counts"]["full_text_job_groups"], 0)

    def test_empty_complete_audit_records_zero(self):
        result = counts([])
        self.assertTrue(all(r["sample_counts"]["full_text_job_groups"] == 0 for r in result.values()))
        self.assertTrue(all(r["sample_status"] == "no_full_text" for r in result.values()))

    def test_legacy_or_incomplete_audit_does_not_turn_unknown_counts_into_zero(self):
        legacy = audit(); del legacy["job_group_id"]
        for result in (counts([legacy]), counts([audit()], current_source_records=2),
                       counts([], selected_source_records=1)):
            self.assertTrue(all(r["sample_counts"] is None for r in result.values()))
            self.assertTrue(all(r["sample_status"] == "not_recorded" for r in result.values()))

    def test_missing_group_on_excluded_record_does_not_hide_valid_counts(self):
        excluded = audit(status="platform_filtered"); del excluded["job_group_id"]
        result = counts([audit(), excluded])["architect"]["sample_counts"]
        self.assertEqual(result["selected_source_records"], 1)
        self.assertEqual(result["full_text_job_groups"], 1)


class RoleSamplePipelineTests(unittest.TestCase):
    fixed = "2026-09-26T10:00:00+00:00"
    body = "要求熟练使用 Cursor 进行 AI 辅助编程，编写单元测试并进行代码审查。"

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.db = self.root / "fixture.sqlite"

    def job(self, ident, **overrides):
        data = dict(title="时间序列算法工程师", text=self.body, company="人工公司",
                    url="https://example.com/jobs/" + ident, source_mode="synthetic",
                    is_synthetic=True, collected_at=self.fixed,
                    rights_note="人工测试材料，不是真实招聘观察。")
        data.update(overrides)
        return JobRecord(**data)

    def report(self, jobs, name="report", **kwargs):
        with Store(self.db) as store:
            for job in jobs:
                store.add(job)
        output = self.root / name
        result = analyze(self.db, output, demo_mode=True, as_of=self.fixed, **kwargs)
        return result, output

    def test_original_group_ids_and_three_export_views_agree(self):
        manifest, output = self.report([self.job("one"), self.job("two")])
        brief = manifest["research_brief"]
        role = next(r for r in brief["roles"] if r["id"] == "time_series")
        self.assertEqual(role["sample_counts"]["selected_source_records"], 2)
        self.assertEqual(role["sample_counts"]["full_text_job_groups"], 1)
        self.assertEqual(role["sample_counts"]["vibe_evidence_job_groups"], 1)
        self.assertEqual(role["sample_counts"]["requirement_rows"], 3)
        self.assertEqual(role["sample_counts"]["human_approved_positive_rows"], 0)
        with (output / "input_audit.csv").open(encoding="utf-8-sig", newline="") as stream:
            audit_rows = list(csv.DictReader(stream))
        self.assertEqual(len({r["job_group_id"] for r in audit_rows}), 1)
        requirements = [json.loads(s) for s in (output / "requirements.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual(audit_rows[0]["job_group_id"], requirements[0]["job_group_id"])
        saved = json.loads((output / "research_brief.json").read_text(encoding="utf-8"))
        self.assertEqual(saved, brief)
        markdown = (output / "research_brief.md").read_text(encoding="utf-8")
        self.assertIn("| 时间序列算法工程师 | 2 | 1 | 1 | 3 | 0 | 3 / 0 |", markdown)
        html = (output / "dashboard.html").read_text(encoding="utf-8")
        self.assertIn('id="role-sample-coverage"', html)
        self.assertIn("<td>时间序列算法工程师</td><td>2</td><td>1</td><td>1</td><td>3</td>", html)
        self.assertIn("合成演示", html)
        for name in ("input_audit.csv", "research_brief.json", "research_brief.md", "dashboard.html"):
            self.assertEqual(hashlib.sha256((output / name).read_bytes()).hexdigest(),
                             manifest["output_files_sha256"][name])

    def test_unknown_employers_do_not_merge_and_filtered_roles_stay_independent(self):
        manifest, _ = self.report([self.job("one", company=""), self.job("two", company="")],
                                  role_filter=["time_series"])
        roles = manifest["research_brief"]["roles"]
        self.assertEqual([r["id"] for r in roles], ["time_series"])
        self.assertEqual(roles[0]["sample_counts"]["full_text_job_groups"], 2)
        self.assertEqual(roles[0]["sample_counts"]["vibe_evidence_job_groups"], 2)

    def test_multirole_and_no_ai_job_preserve_original_global_denominator(self):
        manifest, _ = self.report([
            self.job("both", title="时间序列算法架构师"),
            self.job("domain", title="电力算法工程师", text="负责电网优化和业务约束分析。")])
        samples = {r["id"]: r for r in manifest["research_brief"]["roles"]}
        self.assertEqual(manifest["stats"]["full_text_job_groups"], 2)
        self.assertEqual(manifest["stats"]["vibe_evidence_job_groups"], 1)
        self.assertEqual(sum(r["sample_counts"]["full_text_job_groups"] for r in samples.values()), 3)
        self.assertEqual(samples["domain_algorithm"]["sample_status"], "no_ai_evidence")
        self.assertEqual(samples["domain_algorithm"]["sample_counts"]["vibe_evidence_job_groups"], 0)
        self.assertIn("不能简单相加", manifest["research_brief"]["role_sample_note"])

    def test_new_analysis_keeps_old_report_bytes_and_legacy_markdown_readable(self):
        first, output = self.report([self.job("one")])
        before = {p.name: p.read_bytes() for p in output.iterdir()}
        self.report([self.job("later", title="架构师")], name="next")
        self.assertEqual(before, {p.name: p.read_bytes() for p in output.iterdir()})
        legacy = copy.deepcopy(first["research_brief"])
        legacy.pop("role_sample_note")
        for role in legacy["roles"]:
            for key in ("sample_counts", "sample_status", "sample_note"):
                role.pop(key)
        markdown = brief_markdown(legacy)
        self.assertIn("未记录", markdown)
        self.assertNotIn("| 时间序列算法工程师 | 0", markdown)

    def test_role_labels_cannot_inject_html_or_break_markdown_table(self):
        from vibe_job_radar.config import load_config
        conf = load_config()
        conf["roles"]["time_series"]["label"] = "<script>alert(1)</script>|escaped"
        _, output = self.report([self.job("one")], config=conf)
        for name in ("dashboard.html", "research_brief.md"):
            content = (output / name).read_text(encoding="utf-8")
            self.assertNotIn("<script>alert(1)</script>", content)
            self.assertIn("&lt;script&gt;", content)
        self.assertIn(r"\|escaped", (output / "research_brief.md").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
