from __future__ import annotations

import json
import os
import shutil
import tempfile
from collections import defaultdict
from pathlib import Path
from . import config as cfg
from ._version import __version__
from .acquisition_status import snapshot as acquisition_snapshot
from .extract import RuleExtractor, apply_reviews, hard_constraints
from .metrics import catalog_rows
from .models import Requirement
from .report import dashboard, write_requirements_zh
from .research_brief import build_brief, brief_markdown
from .store import Store
from .synthesis import aggregate, descriptions, evidence_matrix, job_coverage, load_candidate
from .utils import atomic_json, atomic_text, digest, json_text, load_json, parse_time, utc_now, write_csv


REQ_FIELDS = list(Requirement.__dataclass_fields__)
SUMMARY_FIELDS = ["scope", "capability", "label", "job_count", "denominator_jobs", "sample_frequency", "employer_count_known", "required_job_count", "preferred_job_count", "bucket", "requirement_ids", "source_urls"]
MATRIX_FIELDS = ["requirement_id", "job_group_id", "title", "capability", "quote", "url", "strength", "status", "weight", "evidence_ids", "potential_evidence_ids", "note"]
COVERAGE_FIELDS = ["job_group_id", "title", "url", "eligible_weight", "user_attested_weight", "evidence_mapping_coverage", "constraint_or_review_rows", "note"]
HARD_FIELDS = ["constraint_id", "job_group_id", "record_id", "roles", "category", "quote", "start", "end", "strength", "url", "evidence_level", "is_synthetic", "note"]


def analyze(db: str | Path, output: str | Path, *, config: dict | None = None,
            role_filter: list[str] | None = None, platform_filter: list[str] | None = None,
            candidate_path: str | Path | None = None, reviews_path: str | Path | None = None,
            demo_mode: bool = False, max_age_days: int = 90, as_of: str | None = None, llm=None) -> dict:
    conf = config or cfg.load_config()
    if max_age_days < 1:
        raise ValueError("max_age_days must be positive")
    if role_filter and set(role_filter) - set(conf["roles"]):
        raise ValueError("unknown role filter")
    if platform_filter and set(platform_filter) - set(conf["platforms"]):
        raise ValueError("unknown platform filter")
    if not Path(db).is_file():
        raise ValueError("database does not exist; ingest/discover first")
    output = Path(output)
    if output.exists() and (not output.is_dir() or any(output.iterdir())):
        raise ValueError("output must be a new or empty directory; use a new run directory to preserve audit history")
    now = parse_time(as_of or utc_now())
    candidate = load_candidate(candidate_path, conf)
    reviews = load_json(reviews_path) if reviews_path else {}
    if not isinstance(reviews, dict):
        raise ValueError("reviews must be an object keyed by requirement_id")
    with Store(db) as store:
        current = store.records()
        historical = store.records(latest_only=False)
        events = store.events()
    audit, chosen = [], []
    full_urls = {j.url for j in current if j.url and j.evidence_level == "full_text" and j.is_synthetic == demo_mode
                 and -1/24 <= (now - parse_time(j.collected_at)).total_seconds()/86400 <= max_age_days
                 and (not j.expires_at or parse_time(j.expires_at) >= now)
                 and cfg.detect_roles(j, conf)
                 and (not role_filter or set(cfg.detect_roles(j, conf)).intersection(role_filter))
                 and (not platform_filter or j.platform in platform_filter)}
    for job in current:
        roles = cfg.detect_roles(job, conf)
        status = "selected"
        age = (now - parse_time(job.collected_at)).total_seconds() / 86400
        if job.is_synthetic != demo_mode:
            status = "synthetic_excluded" if job.is_synthetic else "real_excluded_from_demo"
        elif age < -1 / 24:
            status = "future_collection_time"
        elif age > max_age_days:
            status = "stale_snapshot"
        elif job.expires_at and parse_time(job.expires_at) < now:
            status = "expired"
        elif not roles or (role_filter and not set(roles).intersection(role_filter)):
            status = "role_unmatched"
        elif platform_filter and job.platform not in platform_filter:
            status = "platform_filtered"
        elif job.evidence_level == "snippet" and job.url in full_urls:
            status = "snippet_superseded_by_full_text"
        if status == "selected":
            if role_filter:
                roles = [r for r in roles if r in role_filter]
            chosen.append((job, roles))
        audit.append({"record_id": job.record_id, "title": job.title, "platform": job.platform, "url": job.url,
                      "roles": roles, "status": status, "evidence_level": job.evidence_level, "is_synthetic": job.is_synthetic,
                      "collected_at": job.collected_at, "published_at": job.published_at,
                      "collection_age_days": round(age, 3), "note": "capture recency is NOT confirmation that hiring is still active"})
    groups = defaultdict(list)
    for job, roles in chosen:
        groups[job.fingerprint].append((job, roles))
    extractor = RuleExtractor(conf)
    requirements, constraints, duplicates, errors = [], [], [], []
    full_groups = set()
    for fingerprint, members in sorted(groups.items()):
        members.sort(key=lambda m: m[0].record_id)
        job, roles = members[0]
        gid = "g_" + fingerprint[:24]
        if job.evidence_level == "full_text":
            full_groups.add(gid)
        if len(members) > 1:
            duplicates.append({"job_group_id": gid, "representative_record_id": job.record_id,
                               "source_record_ids": [m[0].record_id for m in members],
                               "source_urls": [m[0].url for m in members],
                               "method": "same normalized employer/title/location/text; offsets apply only to representative"})
        rows = extractor.extract(job, roles, group_id=gid)
        if llm is not None and job.evidence_level == "full_text":
            try:
                proposals = llm.extract(job, roles, group_id=gid)
                indexed = {r.requirement_id: r for r in rows}
                for proposal in proposals:
                    existing = indexed.get(proposal.requirement_id)
                    if existing:
                        if (existing.relation, existing.strength) != (proposal.relation, proposal.strength):
                            existing.review_status = "needs_review"
                            existing.notes += " | rule/LLM label conflict; manual decision required"
                    else:
                        indexed[proposal.requirement_id] = proposal
                rows = list(indexed.values())
            except Exception as exc:
                errors.append({"record_id": job.record_id, "stage": "llm", "error_type": type(exc).__name__, "error": str(exc)})
        for row in rows:
            row.source_record_ids = [m[0].record_id for m in members]
            row.source_urls = sorted({m[0].url for m in members if m[0].url})
            if job.text[row.start:row.end] != row.quote:
                raise RuntimeError("source-span invariant violated")
        requirements.extend(rows)
        constraints.extend(hard_constraints(job, gid, roles))
    unknown_review_ids = apply_reviews(requirements, reviews)
    requirements.sort(key=lambda r: (r.job_group_id, r.start, r.capability))
    summary = aggregate(requirements, conf)
    matrix = evidence_matrix(requirements, candidate, conf, demo_mode=demo_mode)
    coverage = job_coverage(matrix)
    claims, role_claims, gaps = descriptions(summary, requirements, matrix, candidate, conf, demo_mode=demo_mode)
    req_dicts = [r.to_dict() for r in requirements]
    pending = [r for r in requirements if r.review_status == "needs_review"]
    accepted = [r for r in requirements if r.accepted and r.positive]
    known_ids = {r.requirement_id for r in requirements}
    orphan_evidence_ids = sorted({rid for e in candidate.get("evidence", []) for rid in e.get("requirement_ids", []) if rid not in known_ids})
    warnings = ["规则分数不是校准概率；抽取和去重均需人工抽检。", "搜索摘要、过期快照与合成样例不进入真实正文统计。",
                "采集时间不等于职位发布日期或在招确认时间。", "同句多能力分多行；原文偏移仅针对对应正文ID。",
                "岗位资格与限制条款未计入证据映射覆盖率；该比率不是胜任度或录用概率。"]
    if not full_groups:
        warnings.append("没有纳入职位正文；报告仅能展示线索/空结果，不能据此总结真实招聘要求。")
    if unknown_review_ids:
        warnings.append("存在未匹配的review IDs，详见run_manifest。")
    if orphan_evidence_ids:
        warnings.append("存在未匹配的候选人requirement IDs，未据此计分。")
    manifest = {
        "schema_version": 1, "project_version": __version__, "created_at": utc_now(), "as_of": now.isoformat(),
        "mode": "synthetic_demo" if demo_mode else "real_sample", "status": "incomplete" if errors else "completed",
        "complete_market_coverage": False, "market_population_denominator": None,
        "software_acquisition_capabilities": acquisition_snapshot(),
        "rule_engine": extractor.version, "config_sha256": digest(json_text(conf)),
        "snapshot_sha256": digest(json_text([j.to_dict() for j in current])),
        "candidate_sha256": digest(json_text(candidate)), "reviews_sha256": digest(json_text(reviews)),
        "filters": {"roles": role_filter, "platforms": platform_filter, "max_collection_age_days": max_age_days},
        "stats": {"stored_snapshots": len(historical), "current_source_records": len(current), "selected_source_records": len(chosen),
                  "deduplicated_groups": len(groups), "full_text_job_groups": len(full_groups),
                  "vibe_evidence_job_groups": len({r.job_group_id for r in accepted}),
                  "vibe_evidence_source_records": len({ident for r in accepted for ident in (r.source_record_ids or [r.record_id])}),
                  "requirement_rows": len(requirements), "accepted_positive_requirement_rows": len(accepted),
                  "review_queue_rows": len(pending), "hard_constraint_rows": len(constraints),
                  "duplicate_groups": len(duplicates), "analysis_error_rows": len(errors)},
        "unknown_review_ids": unknown_review_ids, "orphan_evidence_requirement_ids": orphan_evidence_ids,
        "warnings": warnings,
    }
    manifest["research_brief"] = build_brief(manifest, summary, requirements, matrix, audit, conf)
    output.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix="." + output.name + "-", dir=output.parent))
    try:
        atomic_text(staging / "jobs.jsonl", "".join(json.dumps(j.to_dict(), ensure_ascii=False) + "\n" for j in current))
        atomic_text(staging / "requirements.jsonl", "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in req_dicts))
        write_csv(staging / "requirements.csv", req_dicts, REQ_FIELDS)
        write_requirements_zh(staging / "requirements_zh.csv", req_dicts, conf)
        write_csv(staging / "capability_summary.csv", summary, SUMMARY_FIELDS)
        write_csv(staging / "requirement_evidence_matrix.csv", matrix, MATRIX_FIELDS)
        write_csv(staging / "job_coverage.csv", coverage, COVERAGE_FIELDS)
        write_csv(staging / "hard_constraints.csv", constraints, HARD_FIELDS)
        write_csv(staging / "review_queue.csv", [r.to_dict() for r in pending], REQ_FIELDS)
        write_csv(staging / "negative_constraints.csv", [r.to_dict() for r in requirements if not r.positive], REQ_FIELDS)
        write_csv(staging / "input_audit.csv", audit, ["record_id", "title", "platform", "url", "roles", "status", "evidence_level", "is_synthetic", "collected_at", "published_at", "collection_age_days", "note"])
        write_csv(staging / "duplicate_groups.csv", duplicates, ["job_group_id", "representative_record_id", "source_record_ids", "source_urls", "method"])
        write_csv(staging / "analysis_errors.csv", errors, ["record_id", "stage", "error_type", "error"])
        write_csv(staging / "audit_events.csv", events, ["event_id", "created_at", "action", "status", "details"])
        write_csv(staging / "metrics_catalog.csv", catalog_rows(), ["metric_id", "label", "unit", "kind", "formula", "measurement_contract"])
        tool_rows = []
        for tool in conf["tools"]:
            matches = [r for r in accepted if tool in r.tools]
            if matches:
                tool_rows.append({"tool": tool, "job_count": len({r.job_group_id for r in matches}),
                                  "requirement_ids": sorted({r.requirement_id for r in matches}),
                                  "note": "ATS vocabulary only; listing tools does not prove competence"})
        write_csv(staging / "ats_keywords.csv", tool_rows, ["tool", "job_count", "requirement_ids", "note"])
        atomic_json(staging / "reviews.template.json", {r.requirement_id: {"decision": "pending", "reviewer": "", "reason": ""} for r in pending})
        atomic_json(staging / "candidate.template.json", {"name": "待填写", "evidence": []})
        atomic_json(staging / "candidate.effective.json", candidate)
        atomic_json(staging / "reviews.effective.json", reviews)
        atomic_json(staging / "effective_config.json", conf)
        atomic_json(staging / "llm_audit.json", llm.audit if llm else [])
        atomic_json(staging / "research_brief.json", manifest["research_brief"])
        atomic_text(staging / "research_brief.md", brief_markdown(manifest["research_brief"]))
        atomic_text(staging / "descriptions.md", claims)
        atomic_text(staging / "role_descriptions.md", role_claims)
        atomic_text(staging / "evidence_gaps.md", gaps)
        atomic_text(staging / "README_OUTPUT.md", "# 输出说明\n\n" + "\n\n".join(warnings) +
                    "\n\n先打开 dashboard.html，再读 requirements_zh.csv、review_queue.csv 与 hard_constraints.csv。\n"
                    "\n同一原文可能对应多项能力；精确原文见 requirements.jsonl，CSV 为防公式注入可能增加前导单引号。\n"
                    "\n输入快照与生成文件SHA256见 run_manifest.json。当前目录为一次运行的不可覆盖产物。\n")
        dashboard(staging / "dashboard.html", manifest, summary, req_dicts, claims, conf)
        manifest["output_files_sha256"] = {p.name: __import__('hashlib').sha256(p.read_bytes()).hexdigest()
                                           for p in sorted(staging.iterdir()) if p.is_file()}
        atomic_json(staging / "run_manifest.json", manifest)
        if output.exists():
            output.rmdir()
        os.replace(staging, output)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return manifest
