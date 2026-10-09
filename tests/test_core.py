import csv
import dataclasses
import json
import math
import random
import tempfile
import unittest
from pathlib import Path

from vibe_job_radar.config import load_config, detect_roles, platform_for_url
from vibe_job_radar.extract import RuleExtractor, apply_reviews, spans
from vibe_job_radar.html_parser import ParseError, parse_job_html
from vibe_job_radar.ingest import iter_items
from vibe_job_radar.llm import OpenAIExtractor
from vibe_job_radar.metrics import format_metric, rate, validate_metric, wape
from vibe_job_radar.models import JobRecord
from vibe_job_radar.pipeline import analyze
from vibe_job_radar.store import Store
from vibe_job_radar.synthesis import aggregate, evidence_matrix, job_coverage, load_candidate
from vibe_job_radar.utils import canonical_url, csv_cell, digest, parse_time

NOW = "2026-09-14T08:07:15+00:00"
CONF = load_config()


def job(text="熟练使用 Cursor 进行 AI 辅助编程，并编写单元测试。", **kw):
    values = dict(title="架构师", text=text, collected_at=NOW, company="测试公司",
                  url="https://www.zhipin.com/job_detail/unit-test.html")
    values.update(kw)
    return JobRecord(**values)


class ModelsTests(unittest.TestCase):
    def test_empty_title_rejected(self):
        with self.assertRaises(ValueError):
            job(title=" ")

    def test_synthetic_consistency(self):
        with self.assertRaises(ValueError):
            job(is_synthetic=True)

    def test_search_cannot_be_fulltext(self):
        with self.assertRaises(ValueError):
            job(source_mode="search_api")

    def test_boolean_string_rejected(self):
        with self.assertRaises(ValueError):
            job(is_synthetic="false")

    def test_timezone_required(self):
        with self.assertRaises(ValueError):
            job(collected_at="2026-09-14T08:00:00")

    def test_tracking_removal_preserves_job_id(self):
        self.assertEqual(canonical_url("https://X.com/job?jobId=7&utm_source=x&foo=2#part"), "https://x.com/job?foo=2&jobId=7")

    def test_credentials_and_javascript_urls_rejected(self):
        for value in ("javascript:alert(1)", "https://user:pass@x.com/job"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                canonical_url(value)

    def test_record_content_integrity(self):
        raw = job().to_dict()
        raw["text"] += "changed"
        with self.assertRaises(ValueError):
            JobRecord.from_dict(raw)

    def test_snapshot_id_does_not_depend_on_capture_time(self):
        a = job()
        b = job(collected_at="2026-09-15T08:00:00+00:00")
        self.assertEqual(a.record_id, b.record_id)

    def test_unknown_companies_not_cross_merged(self):
        a = job(company="")
        b = job(company="", url="https://www.liepin.com/job/other-test.shtml")
        self.assertNotEqual(a.fingerprint, b.fingerprint)

    def test_company_cross_platform_dedup(self):
        a = job()
        b = job(url="https://www.liepin.com/job/other-test.shtml", platform="liepin")
        self.assertEqual(a.fingerprint, b.fingerprint)

    def test_role_not_derived_from_query_metadata(self):
        self.assertEqual(detect_roles(job(title="产品经理", text="熟练使用Cursor构建Demo。"), CONF), [])

    def test_time_series_role_fallback(self):
        self.assertIn("time_series", detect_roles(job(title="算法工程师", text="研究时间序列算法。"), CONF))

    def test_sales_forecast_algorithm_titles_are_time_series(self):
        for title in ("算法专家（供应链）", "算法工程师", "机器学习工程师"):
            with self.subTest(title=title):
                self.assertEqual(detect_roles(job(title=title,
                    text="负责销量预测模型开发，评估季节趋势与未来需求。"), CONF),
                    ["time_series"])

    def test_algorithm_expert_uses_existing_temporal_body_evidence(self):
        for body in ("负责时间序列模型开发。", "负责负荷预测模型研发与验证。"):
            with self.subTest(body=body):
                self.assertEqual(detect_roles(job(title="算法专家", text=body), CONF),
                                 ["time_series"])

    def test_sales_forecast_requires_algorithm_title_and_specific_body_evidence(self):
        cases = [
            ("供应链经理", "使用销量预测报表优化库存与商品布局。"),
            ("商品规划专家", "结合销量预测结果制定经营策略。"),
            ("AI算法工程师", "熟悉InfluxDB时序数据库，开发设备监控。"),
            ("算法专家", "熟悉时序数据库，开发工业数据接口。"),
            ("算法工程师", "负责Transformer架构设计与模型调优。"),
            ("算法专家", "负责金融风控模型与客户分群。"),
            ("算法专家", "建设商品推荐模型提升销量。"),
            ("算法专家", "预测用户点击率和流失风险。"),
        ]
        for title, body in cases:
            with self.subTest(title=title, body=body):
                self.assertEqual(detect_roles(job(title=title, text=body), CONF), [])

    def test_hostname_boundary(self):
        self.assertEqual(platform_for_url("https://zhipin.com.evil.org/job/x", CONF), "unknown")


class ExtractTests(unittest.TestCase):
    def setUp(self):
        self.ex = RuleExtractor(CONF)

    def extract(self, text):
        return self.ex.extract(job(text), ["architect"])

    def test_chinese_adjacent_tool(self):
        self.assertEqual(self.ex.tools_in("熟练使用Cursor编程"), ["Cursor"])

    def test_sql_cursor_not_coding_tool(self):
        self.assertEqual(self.extract("熟悉数据库游标，使用 cursor.execute(query) 读取数据。"), [])

    def test_office_copilot_not_coding(self):
        self.assertEqual(self.extract("熟练使用 Microsoft Copilot 制作 PPT 与 Excel 办公表格。"), [])

    def test_generic_ai_agent_not_vibe(self):
        self.assertEqual(self.extract("负责机器学习、AI推荐算法与Agent研究，熟悉MCP。"), [])

    def test_tool_boundaries(self):
        self.assertEqual(self.ex.tools_in("MyCursorService precodex value"), [])

    def test_negative_is_not_positive(self):
        rows = self.extract("无需使用 Cursor，不要求 Vibe Coding 经验。")
        self.assertTrue(rows)
        self.assertTrue(all(not r.positive for r in rows))
        self.assertEqual(aggregate(rows, CONF), [])

    def test_prohibited_preserved(self):
        rows = self.extract("禁止把客户数据上传到公共 AI 编程工具。")
        self.assertTrue(any(r.capability == "security" for r in rows))
        self.assertTrue(all(r.strength == "prohibited" for r in rows))

    def test_preferred_heading_propagates(self):
        rows = self.extract("加分项\n具有 Cursor 实战经验\n岗位职责\n使用 Codex 编写代码")
        self.assertTrue(any(r.strength == "preferred" and "Cursor" in r.quote for r in rows))
        self.assertTrue(any(r.strength == "expected" and "Codex" in r.quote for r in rows))

    def test_no_punctuation_lines_are_extracted(self):
        rows = self.extract("任职要求\n1. 熟练使用 Cursor\n2. 掌握 Codex")
        self.assertTrue(any("Cursor" in r.quote for r in rows))
        self.assertTrue(any("Codex" in r.quote for r in rows))

    def test_related_generic_requirement_is_separate(self):
        rows = self.extract("熟练使用 Cursor。\n精通分布式架构设计。")
        related = [r for r in rows if "分布式" in r.quote]
        self.assertTrue(related)
        self.assertTrue(all(r.relation == "role_related" and not r.accepted for r in related))

    def test_nearby_context_needs_review(self):
        rows = self.extract("熟练使用 Cursor；编写单元测试。")
        contextual = [r for r in rows if r.capability == "testing_review"]
        self.assertEqual(len(contextual), 1)
        self.assertEqual(contextual[0].relation, "contextual")
        self.assertFalse(contextual[0].accepted)
        apply_reviews(rows, {contextual[0].requirement_id: {"decision": "approve", "reviewer": "tester", "reason": "same requirement"}})
        self.assertTrue(contextual[0].accepted)

    def test_snippet_cannot_be_promoted_by_review(self):
        rows = self.ex.extract(job(evidence_level="snippet"), ["architect"])
        apply_reviews(rows, {r.requirement_id: {"decision": "approve", "reviewer": "x", "reason": "text inspected"} for r in rows})
        self.assertTrue(all(not r.accepted for r in rows))

    def test_welfare_not_job_requirements(self):
        self.assertEqual(self.extract("福利待遇\n公司提供 Cursor 会员。"), [])

    def test_mixed_polarity_needs_review(self):
        rows = self.extract("无需使用 Cursor，但必须掌握 Codex。")
        self.assertTrue(any(r.strength == "not_required" and "Cursor" in r.quote for r in rows))
        self.assertTrue(any(r.strength == "required" and "Codex" in r.quote for r in rows))

    def test_positive_and_security_constraint_split(self):
        rows = self.extract("使用 Cursor 辅助编程，禁止敏感源码外传。")
        self.assertTrue(any(r.capability == "tool_fluency" and r.positive for r in rows))
        self.assertTrue(any(r.capability == "security" and r.strength == "prohibited" for r in rows))

    def test_no_manual_repetition_does_not_negate_tool_requirement(self):
        rows = self.extract("使用 Cursor 辅助编程，无需人工重复编写。")
        self.assertTrue(any(r.capability == "tool_fluency" and r.positive for r in rows))

    def test_llm_code_generation_phrase(self):
        rows = self.extract("能够使用大模型生成代码，并编写单元测试。")
        self.assertTrue(any(r.capability == "ai_coding" for r in rows))

    def test_english_negation(self):
        rows = self.extract("No Vibe Coding experience is required.")
        self.assertTrue(all(not r.positive for r in rows))

    def test_multicapability_without_inventing_quotes(self):
        text = "1. 熟练使用 Cursor 进行 AI 编程，编写单元测试和代码审查。"
        rows = self.extract(text)
        self.assertTrue({"tool_fluency", "testing_review", "ai_coding"}.issubset({r.capability for r in rows}))
        self.assertTrue(all(text[r.start:r.end] == r.quote for r in rows))

    def test_span_invariant_fuzz_200(self):
        rng = random.Random(1234)
        for i in range(200):
            prefix = "".join(rng.choices("甲乙 丙\n；", k=30))
            text = prefix + "\n1. 熟练使用Cursor进行AI编程。\n加分项\n使用Codex审查生成代码"
            for r in self.extract(text):
                self.assertEqual(text[r.start:r.end], r.quote)

    def test_empty_anchor_is_not_fabricated(self):
        self.assertEqual(self.extract("岗位职责\n熟悉架构设计、代码审查、CI/CD与监控。"), [])


class HTMLTests(unittest.TestCase):
    def test_jsonld_ignores_recommendations(self):
        markup = '<script type="application/ld+json">' + json.dumps({"@type": "JobPosting", "title": "架构师", "description": "熟练使用 Cursor 进行 AI 辅助编程，开展单元测试。"}) + '</script><div>推荐岗位Windsurf</div>'
        result = parse_job_html(markup)
        self.assertIn("Cursor", result["text"])
        self.assertNotIn("Windsurf", result["text"])

    def test_multiple_jobs_rejected(self):
        markup = '<script type="application/ld+json">' + json.dumps([{"@type": "JobPosting", "title": "A"}, {"@type": "JobPosting", "title": "B"}]) + '</script>'
        with self.assertRaises(ParseError):
            parse_job_html(markup)

    def test_homepage_rejected(self):
        with self.assertRaises(ParseError):
            parse_job_html("<h1>职位</h1>", source_url="https://www.zhipin.com/")

    def test_no_full_body_fallback(self):
        with self.assertRaises(ParseError):
            parse_job_html("<body>熟练使用Cursor" + "正文" * 30 + "</body>")

    def test_challenge_rejected(self):
        with self.assertRaises(ParseError):
            parse_job_html("<h1>安全验证</h1><div class=job-sec-text>熟练使用Cursor</div>")

    def test_dom_container_and_single_title(self):
        parsed = parse_job_html("<h1>架构师</h1><div class='job-sec-text'>熟练使用 Cursor 进行 AI 辅助编程，要求开展单元测试。</div><div>推荐Codex</div>")
        self.assertNotIn("Codex", parsed["text"])


class MetricTests(unittest.TestCase):
    def metric(self, **kw):
        return {"metric_id": "cycle_time_hours", "baseline": 10, "current": 6, "sample_size": 20,
                "baseline_sample_size": 20, "window": "B", "baseline_window": "A", "comparison_basis": "same tasks", **kw}

    def test_relative_reduction(self):
        self.assertAlmostEqual(validate_metric(self.metric())["relative_reduction_pct"], 40)

    def test_percentage_points_not_relative_percent(self):
        self.assertAlmostEqual(validate_metric(self.metric(metric_id="test_pass_rate", baseline=80, current=90))["percentage_point_change"], 10)

    def test_zero_baseline_not_divided(self):
        self.assertIsNone(validate_metric(self.metric(baseline=0))["relative_reduction_pct"])

    def test_regression_not_written_as_improvement(self):
        self.assertIn("相对上升", format_metric(self.metric(current=15)))

    def test_wape_zero_denominator(self):
        self.assertIsNone(wape([0, 0], [1, 2]))

    def test_wape_can_exceed_100(self):
        self.assertEqual(wape([1], [4]), 300)

    def test_wape_signed_targets(self):
        self.assertEqual(wape([-1, 1], [0, 0]), 100)

    def test_rate_invalid_denominator(self):
        self.assertIsNone(rate(0, 0))
        with self.assertRaises(ValueError):
            rate(2, 1)

    def test_nan_bool_and_bad_sample_rejected(self):
        for kwargs in ({"current": float('nan')}, {"current": True}, {"sample_size": 0}, {"baseline_sample_size": 0}, {"window": ""}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                validate_metric(self.metric(**kwargs))


class StorePipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = self.root / "jobs.sqlite"

    def add(self, *jobs):
        with Store(self.db) as s:
            for j in jobs:
                s.add(j)

    def test_idempotent_snapshot_keeps_observations(self):
        with Store(self.db) as s:
            self.assertTrue(s.add(job()))
            self.assertFalse(s.add(job()))
            self.assertEqual(len(s.records()), 1)
            self.assertEqual(s.conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0], 2)

    def test_backfill_cannot_roll_back_current_version(self):
        newer = job(text="熟练使用Cursor开展AI编程。新版", collected_at=NOW)
        older = job(text="熟练使用Cursor开展AI编程。旧版", collected_at="2026-08-01T00:00:00+00:00")
        self.add(newer, older)
        with Store(self.db) as s:
            self.assertEqual(s.records()[0].text, newer.text)
            self.assertEqual(len(s.records(latest_only=False)), 2)

    def test_synthetic_excluded_in_real_report(self):
        self.add(job(source_mode="synthetic", is_synthetic=True))
        m = analyze(self.db, self.root / "run", as_of=NOW)
        self.assertEqual(m["stats"]["full_text_job_groups"], 0)
        self.assertEqual(m["mode"], "real_sample")

    def test_duplicate_job_frequency_not_inflated(self):
        self.add(job(), job(platform="liepin", url="https://www.liepin.com/job/test.shtml"))
        m = analyze(self.db, self.root / "run", as_of=NOW)
        self.assertEqual(m["stats"]["vibe_evidence_job_groups"], 1)
        self.assertEqual(m["stats"]["duplicate_groups"], 1)

    def test_manifest_hashes_all_payload_files(self):
        import hashlib
        self.add(job())
        out = self.root / "run"
        m = analyze(self.db, out, as_of=NOW)
        for name, expected in m["output_files_sha256"].items():
            self.assertEqual(hashlib.sha256((out/name).read_bytes()).hexdigest(), expected)

    def test_output_not_overwritten(self):
        self.add(job())
        out = self.root / "run"
        analyze(self.db, out, as_of=NOW)
        with self.assertRaises(ValueError):
            analyze(self.db, out, as_of=NOW)

    def test_search_snippet_no_formal_frequency(self):
        self.add(job(evidence_level="snippet", source_mode="search_api"))
        m = analyze(self.db, self.root / "run", as_of=NOW)
        self.assertEqual(m["stats"]["vibe_evidence_job_groups"], 0)
        self.assertGreater(m["stats"]["review_queue_rows"], 0)

    def test_expired_and_stale_are_audited(self):
        self.add(job(expires_at="2026-09-01T00:00:00+00:00"), job(url="https://zhipin.com/job/old", collected_at="2025-01-01T00:00:00+00:00"))
        out = self.root / "run"
        m = analyze(self.db, out, as_of=NOW)
        self.assertEqual(m["stats"]["full_text_job_groups"], 0)
        audit = (out/"input_audit.csv").read_text(encoding="utf-8-sig")
        self.assertIn("expired", audit)
        self.assertIn("stale_snapshot", audit)

    def test_fresh_snippet_not_suppressed_by_stale_fulltext(self):
        self.add(job(collected_at="2025-01-01T00:00:00+00:00"), job(evidence_level="snippet", source_mode="search_api"))
        m = analyze(self.db, self.root / "run", as_of=NOW)
        self.assertGreater(m["stats"]["review_queue_rows"], 0)

    def test_invalid_input_row_does_not_hide_good_row(self):
        p = self.root / "jobs.jsonl"
        p.write_text(json.dumps(job().to_dict()) + "\nBAD_JSON\n", encoding="utf-8")
        items = list(iter_items(p))
        self.assertEqual(len(items), 2)
        self.assertIsInstance(items[0][1], JobRecord)
        self.assertIsInstance(items[1][1], Exception)

    def test_html_xss_escaped_and_csv_formula_safe(self):
        self.add(job(title="架构师<script>alert(1)</script>", text='=HYPERLINK("x") 使用Cursor编程。'))
        out = self.root / "run"
        analyze(self.db, out, as_of=NOW)
        rendered = (out/"dashboard.html").read_text(encoding="utf-8")
        self.assertNotIn("<script>alert(1)</script>", rendered)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", rendered)
        self.assertEqual(csv_cell("=1+1"), "'=1+1")

    def test_no_candidate_no_fake_metrics(self):
        self.add(job())
        out = self.root / "run"
        analyze(self.db, out, as_of=NOW)
        text = (out/"descriptions.md").read_text(encoding="utf-8")
        self.assertIn("尚未提供", text)
        self.assertNotIn("提升80%", text)

    def test_capability_match_is_not_exact_coverage(self):
        r = RuleExtractor(CONF).extract(job(), ["architect"])[0]
        c = {"evidence": [{"evidence_id": "e", "review_status": "approved", "scope": "offline", "capabilities": [r.capability], "requirement_ids": []}]}
        m = evidence_matrix([r], c, CONF, demo_mode=False)
        self.assertEqual(m[0]["status"], "capability_only_not_satisfied")
        self.assertEqual(job_coverage(m)[0]["evidence_mapping_coverage"], 0)

    def test_exact_mapping_is_explicit_user_attestation(self):
        r = RuleExtractor(CONF).extract(job(), ["architect"])[0]
        c = {"evidence": [{"evidence_id": "e", "review_status": "approved", "scope": "offline", "capabilities": [r.capability], "requirement_ids": [r.requirement_id]}]}
        m = evidence_matrix([r], c, CONF, demo_mode=False)
        self.assertEqual(m[0]["status"], "user_attested_exact")

    def test_offline_evidence_cannot_cover_explicit_production(self):
        r = RuleExtractor(CONF).extract(job("使用 Cursor 完成生产系统上线。"), ["architect"])[0]
        c = {"evidence": [{"evidence_id": "e", "review_status": "approved", "scope": "offline", "capabilities": [r.capability], "requirement_ids": [r.requirement_id]}]}
        m = evidence_matrix([r], c, CONF, demo_mode=False)
        self.assertEqual(m[0]["status"], "scope_mismatch_needs_review")
        self.assertEqual(job_coverage(m)[0]["evidence_mapping_coverage"], 0)

    def test_candidate_local_hash_must_match(self):
        evidence = self.root / "evidence.txt"
        evidence.write_text("test")
        p = self.root / "candidate.json"
        p.write_text(json.dumps({"evidence": [{"evidence_id": "e", "review_status": "approved", "scope": "offline", "project": "P", "reviewer": "R", "reviewed_at": NOW, "capabilities": ["ai_coding"], "evidence_ref": "evidence.txt", "sha256": "bad"}]}))
        with self.assertRaises(ValueError):
            load_candidate(p, CONF)


class LLMTests(unittest.TestCase):
    def setUp(self):
        self.ex = OpenAIExtractor(CONF, api_key="unit-test-not-real", model="unit-test-model", consent_send_jd=True)
        self.job = job()
        self.proposal = {"start": 0, "end": len(self.job.text), "quote": self.job.text,
                         "capability": "ai_coding", "relation": "direct", "strength": "required", "context_quote": ""}

    def test_explicit_consent_required(self):
        with self.assertRaises(ValueError):
            OpenAIExtractor(CONF, api_key="test", model="test", consent_send_jd=False)

    def test_exact_source_remains_review_pending(self):
        rows, rejected = self.ex.validate_proposals(self.job, ["architect"], "g", [self.proposal])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].review_status, "needs_review")
        self.assertFalse(rows[0].accepted)
        self.assertEqual(rejected, [])

    def test_hallucinated_quote_rejected(self):
        rows, rejected = self.ex.validate_proposals(self.job, ["architect"], "g", [{**self.proposal, "quote": "必须有10倍效率"}])
        self.assertEqual(rows, [])
        self.assertEqual(len(rejected), 1)

    def test_unknown_capability_rejected(self):
        rows, _ = self.ex.validate_proposals(self.job, ["architect"], "g", [{**self.proposal, "capability": "invented"}])
        self.assertEqual(rows, [])

    def test_boolean_index_rejected(self):
        rows, _ = self.ex.validate_proposals(self.job, ["architect"], "g", [{**self.proposal, "start": False}])
        self.assertEqual(rows, [])

    def test_no_anchor_not_direct(self):
        j = job("熟悉机器学习和架构设计。")
        p = {**self.proposal, "quote": j.text, "start": 0, "end": len(j.text)}
        rows, _ = self.ex.validate_proposals(j, ["architect"], "g", [p])
        self.assertEqual(rows, [])


if __name__ == "__main__":
    unittest.main()
