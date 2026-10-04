"""Readable research decisions derived only from the existing analysis results.

No ranking of candidates, new extraction, provider requests or inferred achievements.
The machine-readable source reports remain authoritative for every quoted span.
"""
from __future__ import annotations

from collections import Counter

from .models import Requirement
from .utils import parse_time

EXCLUSIONS = {
    "role_unmatched": "未匹配本次目标岗位",
    "platform_filtered": "不在本次来源范围",
    "stale_snapshot": "采集快照超过本次新鲜度范围",
    "expired": "已记录为过期岗位",
    "synthetic_excluded": "排除合成示例",
    "real_excluded_from_demo": "演示不使用真实输入",
    "future_collection_time": "采集时间异常",
    "snippet_superseded_by_full_text": "摘要已有完整正文替代",
}


def role_sample_counts(audit: list[dict], requirements: list[Requirement],
                       roles: list[str], stats: dict) -> dict[str, dict]:
    """Count the selected snapshots and original groups, never infer missing ones."""
    selected = [a for a in audit if a["status"] == "selected"]
    recorded = (len(audit) == stats["current_source_records"]
                and len(selected) == stats["selected_source_records"]
                and all(isinstance(a.get("job_group_id"), str) and a["job_group_id"]
                        for a in selected))
    result = {}
    for key in roles:
        if not recorded:
            result[key] = {"sample_counts": None, "sample_status": "not_recorded",
                           "sample_note": "本报告未记录方向样本计数，不能将缺失当作0。"}
            continue
        records = [a for a in selected if key in a["roles"]]
        full_groups = {a["job_group_id"] for a in records if a["evidence_level"] == "full_text"}
        rows = [r for r in requirements if key in r.roles]
        positive = [r for r in rows if r.accepted and r.positive
                    and r.job_group_id in full_groups]
        counts = {
            "selected_source_records": len(records),
            "full_text_job_groups": len(full_groups),
            "vibe_evidence_job_groups": len({r.job_group_id for r in positive}),
            "requirement_rows": len(rows),
            "review_queue_rows": sum(r.review_status == "needs_review" for r in rows),
            "rule_accepted_positive_rows": sum(r.review_status == "rule_accepted" for r in positive),
            "human_approved_positive_rows": sum(r.review_status == "approved" for r in positive),
        }
        if not full_groups:
            status = "no_full_text"
            note = "本方向尚无纳入的完整正文，暂不能形成岗位要求结论。"
        elif not positive:
            status = "no_ai_evidence"
            note = "已有完整正文，尚无已接收正向AI编程证据；请复核原文与待复核项。"
        else:
            status = "sample_observed"
            note = "仅反映本次样本；规则接收不等于人工确认，也不证明样本充分。"
        result[key] = {"sample_counts": counts, "sample_status": status, "sample_note": note}
    return result


def build_brief(manifest: dict, summary: list[dict], requirements: list[Requirement],
                matrix: list[dict], audit: list[dict], config: dict) -> dict:
    """Build a bounded, deterministic first-read view without changing eligibility."""
    stats = manifest["stats"]
    accepted = [r for r in requirements if r.accepted and r.positive]
    by_id = {r.requirement_id: r for r in accepted}
    missing = {r["requirement_id"] for r in matrix
               if r["requirement_id"] in by_id and r["status"] != "user_attested_exact"}
    roles = manifest["filters"]["roles"] or list(config["roles"])
    selected = [a for a in audit if a["status"] == "selected"]
    role_samples = role_sample_counts(audit, requirements, roles, stats)
    excluded = Counter(a["status"] for a in audit if a["status"] != "selected")
    if not stats["full_text_job_groups"]:
        state, conclusion = "no_full_text", "本批没有可用于研究的目标岗位完整正文，暂不能给出能力结论。"
        next_step = "先核对来源、岗位匹配和正文完整性，再获取目标岗位；不要用固定案例替代。"
    elif not accepted:
        state, conclusion = "no_ai_evidence", "本批已有目标岗位正文，但尚未提取到已接收的正向 AI 编程要求。"
        next_step = "复核原文、普通岗位能力和待复核项；这不表示招聘方没有相关要求，也不表示市场没有需求。"
    else:
        state, conclusion = "research_ready", "已形成本批目标岗位的 AI 编程要求清单；下一步用本人实际工作逐条举证。"
        next_step = "先复核招聘原文，再补本人项目、负责范围和真实指标；不要把要求或模板当作个人经历。"
    if manifest["status"] != "completed":
        state = "analysis_incomplete"
        conclusion = "本批分析有未完成步骤；以下仅为已经处理部分，不应当作完整结论。"
        next_step = "先检查 analysis_errors.csv，再复核已有结果；保留本次报告，不覆盖历史。"

    def capability(row: dict) -> dict:
        ids = [rid for rid in row["requirement_ids"] if rid in by_id]
        examples = []
        for rid in ids[:3]:
            r = by_id[rid]
            examples.append({"requirement_id": rid, "title": r.title, "url": r.url,
                             "quote_preview": r.quote[:600], "quote_is_excerpt": len(r.quote) > 600,
                             "review_status": r.review_status})
        return {"id": row["capability"], "label": row["label"],
                "job_count": row["job_count"], "denominator_jobs": row["denominator_jobs"],
                "bucket": row["bucket"], "requirement_count": len(ids),
                "evidence_to_check": sum(rid in missing for rid in ids),
                "examples": examples, "example_count": len(examples),
                "suggested_measurements": list(config["capabilities"][row["capability"]].get("metric_ids", []))}

    overall = [capability(r) for r in summary if r["scope"] == "all"]
    # One observed job is not evidence of a common cross-job requirement.
    common = [c for c in overall if c["denominator_jobs"] >= 2
              and c["bucket"] in {"intersection", "common"}]
    dates = sorted((a["collected_at"] for a in selected), key=parse_time)
    return {
        "schema_version": 1, "mode": manifest["mode"], "status": state,
        "conclusion": conclusion, "next_step": next_step,
        "scope_note": "仅本批纳入的目标岗位样本。分母是有已接收正向AI编程证据的去重职位；不是全市场比例或录用概率。",
        "review_note": "规则接收不等于人工验真。摘要、否定条款、普通岗位能力和待复核项不进入正向AI编程能力统计。",
        "counts": {k: stats[k] for k in ("current_source_records", "selected_source_records",
                   "full_text_job_groups", "vibe_evidence_job_groups", "requirement_rows",
                   "review_queue_rows", "hard_constraint_rows")},
        "source_labels": sorted({config["platforms"].get(a["platform"], {}).get("label", a["platform"])
                                 for a in selected}),
        "observed_from": dates[0] if dates else "", "observed_to": dates[-1] if dates else "",
        "exclusions": [{"reason": key, "label": EXCLUSIONS.get(key, key), "count": value}
                       for key, value in sorted(excluded.items())],
        "capabilities": overall, "common_capability_ids": [c["id"] for c in common],
        "role_sample_note": "完整正文数包含未提取到AI编程证据的岗位。一个岗位可属于多个方向，各方向数量不能简单相加。",
        "roles": [{"id": key, "label": config["roles"][key]["label"], **role_samples[key],
                   "capabilities": [capability(r) for r in summary if r["scope"] == key]}
                  for key in roles],
        "evidence_to_check": len(missing),
        "evidence_note": "待补证是本报告没有对应的本人已确认精确映射，不等于本人不具备该能力。",
        "draft_note": "先填写项目、本人负责范围、验证阶段、同口径基线/当前值、样本量和证据；没有观测值就保持待填。",
        "files": {"requirements": "requirements_zh.csv", "roles": "role_descriptions.md",
                  "gaps": "evidence_gaps.md", "constraints": "hard_constraints.csv"},
    }


def brief_markdown(brief: dict) -> str:
    """Plain first-read guide. Escape input text so JD content cannot inject markup."""
    def text(value) -> str:
        import html
        import re
        return re.sub(r"([\\`*_{}\[\]()#+.!|>~-])", r"\\\1", html.escape(str(value), quote=False)).replace("\n", " ")

    lines = ["# 本批岗位研究结论", "",
             "> " + ("合成演示，不是招聘市场事实或本人经历。" if brief["mode"] == "synthetic_demo"
                      else "真实输入样本，不是全市场调研，也不确认岗位仍在招聘。"),
             "", brief["conclusion"], "", brief["scope_note"], "", "## 1. 本次拿到了什么", "",
             f"输入 {brief['counts']['current_source_records']} 条，纳入完整正文 {brief['counts']['full_text_job_groups']} 个去重岗位；"
             f"有已接收正向 AI 编程证据 {brief['counts']['vibe_evidence_job_groups']} 个。",
             "来源：" + text("、".join(brief["source_labels"]))]
    for excluded in brief["exclusions"]:
        lines.append(f"- {text(excluded['label'])}：{excluded['count']} 条。")
    lines += ["", "### 各方向样本覆盖", "",
              brief.get("role_sample_note", "旧报告没有记录方向样本计数。"), "",
              "| 方向 | 纳入来源记录 | 完整正文岗位（去重） | 含AI证据岗位 | 要求行 | 待复核行 | 正向AI要求：规则接收 / 人工确认 |",
              "|---|---:|---:|---:|---:|---:|---:|"]
    sample_notes = []
    for role in brief["roles"]:
        counts = role.get("sample_counts")
        if counts is None:
            lines.append("| " + text(role["label"]) + " | 未记录 | 未记录 | 未记录 | 未记录 | 未记录 | 未记录 |")
        else:
            values = [counts[k] for k in ("selected_source_records", "full_text_job_groups",
                      "vibe_evidence_job_groups", "requirement_rows", "review_queue_rows")]
            lines.append("| " + text(role["label"]) + " | " + " | ".join(str(v) for v in values) +
                         f" | {counts['rule_accepted_positive_rows']} / {counts['human_approved_positive_rows']} |")
        sample_notes.append(text(role["label"]) + "：" +
                            text(role.get("sample_note", "本报告未记录方向样本计数，不能将缺失当作0。")))
    lines += [""] + sample_notes
    lines += ["", "## 2. 样本要求与分岗位补充", "", brief["review_note"]]
    if not brief["common_capability_ids"]:
        lines += ["", "本批没有足够证据归纳跨岗位共同项；单个岗位要求不能直接作为通用底座。"]
    for role in brief["roles"]:
        lines += ["", "### " + text(role["label"])]
        if not role["capabilities"]:
            lines.append("没有该方向的已接收正向 AI 编程证据，不借用其他岗位要求。")
        for cap in role["capabilities"]:
            lines.append(f"- {text(cap['label'])}：{cap['job_count']}/{cap['denominator_jobs']} 个样本岗位；"
                         f"{cap['evidence_to_check']} 条要求待补证或确认。")
            for example in cap["examples"]:
                lines.append("  - 原文" + ("节选" if example["quote_is_excerpt"] else "") + "：" + text(example["quote_preview"]) +
                             "；要求ID：" + text(example["requirement_id"]) + "；来源：" + text(example["url"]))
    lines += ["", "## 3. 我的证据下一步", "", brief["evidence_note"], "", brief["draft_note"],
              "", brief["next_step"], "", "先在本机工作台打开这份报告并进入“用我的证据继续”，复核原文、填写本人经历，再生成个人报告。",
              "学历、年限和禁止项另见 hard_constraints.csv；逐条完整原文见 requirements_zh.csv。"]
    return "\n".join(lines) + "\n"
