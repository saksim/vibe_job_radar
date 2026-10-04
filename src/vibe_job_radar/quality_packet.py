"""Offline, immutable review packets for a pinned report, without re-running rules."""
from __future__ import annotations

import csv
import hashlib
import io
import json
import os
from collections import defaultdict
from pathlib import Path

from .models import JobRecord, RELATIONS, STRENGTHS
from .utils import json_text, utc_now

SOURCE_FILES = ("jobs.jsonl", "requirements.jsonl", "input_audit.csv",
                "duplicate_groups.csv", "effective_config.json")
PACKET_FILES = ("corpus.json", "predictions.json", "labels.json", "source.json",
                "documents.txt", "REVIEW_PROTOCOL.txt")
REVIEW_STATES = {"pending", "assistant_draft", "human_confirmed"}
NEGATIVE = {"not_required", "prohibited"}
PROTOCOL = """独立质量标注包（版本1）

先阅读 documents.txt 中的完整正文和 labels.json 的标签说明，再独立填写
annotations.json。predictions.json 是原报告输出，不能直接复制为标准答案。
可以复制整个检查包后编辑标注；原报告和不可编辑文件都不改写。

每份全文都有一行空白标注。review_state:
pending=未核查；assistant_draft=助手草稿；human_confirmed=填写者声明真人已确认。
author 和 reviewed_at（带时区ISO日期）在草稿/确认时必填。工具不验证人的身份。
助手不得自动填写 human_confirmed。notes 记录疑点和判断依据，不填写个人敏感资料。

roles=null 表示未核查岗位分类，[] 表示已明确不属于任何配置方向。
roles 可包含多个配置方向；只有 source.json 声明的 role_scope 进入分类指标。
岗位标签应依据实际职责独立判断，不仅抄职位名称。原预测来自原 input_audit，
不使用当前规则重新分类。分类范围是同一真实/演示模式、所选平台的完整正文，
包括原未匹配、过期等保存快照；原已知重复组只评估代表记录。
未入选记录没有原去重分组时按保存记录声明范围，不重新猜测合并。
它不是当下在招判断或市场覆盖率。

requirements 中每项独立填写 start/end/quote/capability/relation/strength。
位置是 Python Unicode 码点，start 包含、end 不包含；不是UTF-8字节或UTF-16单元。
必须逐字等于对应全文切片；允许补充原预测中遗漏的句段。一段可有多个不同能力；
同一位置和能力不能重复或填互相冲突的标签。尽量选择一个完整、连续的要求条款，
包含决定强度/否定的上下文，保留条款内部换行；不把相邻无关条款并成一条。

relation: direct=明确AI辅助编程；contextual=上下文明确属于AI辅助工程；
role_related=仅普通岗位能力，不能只因全文出现AI就提升为AI要求。
strength: required=必须；expected=职责/期望；preferred=优先/加分；
not_required=明确不要求；prohibited=明确禁止/限制；unspecified=没有强度信息。
通用模型开发、LLM训练、产品AI功能本身不等于AI辅助写代码。使用原标签目录，
无法归类的内容记入notes；不要发明新标签。此包评估能力要求，不覆盖所有硬条件。

只有真人确认且 requirements_complete=true 的适用全文进入抽取评估。
填写complete前必须逐段检查整篇并补充所有适用能力要求；明确没有要求也需这样确认。
partial、pending、assistant_draft 始终排除。标注可以先确认岗位分类、以后再补全文。
抽取范围限原selected、同模式、完整正文、去重代表记录。未入选和重复非代表正文
不算抽取漏报；因此这个子集的召回率不等于整个采集/筛选流程的端到端召回率。

span_capability检查精确位置+能力；semantic另要求relation和strength一致。
accepted_positive_ai检查原报告已规则接收/人工批准、direct/contextual且非否定的行，
对照标注中的正向AI要求。所有原输出行（含待复核/拒绝）仍进入前两种原输出检查。
原报告可能已有业务人工复核，这不是无条件的“纯规则引擎准确率”。
边界差一个字也算严格FP/FN，不做模糊匹配；分母为0的指标为null而不是满分。

去重只评估dedup_pairs中主动声明的文档对。填写left/right记录ID和same_job，
同一对不可反向重复。仅真人确认且原selected完整正文、原分组可比较的对入分母。
原duplicate_groups与保存的分组是唯一预测来源；没有标过的文档对不推定正确。
少数对的判断不能代表全库去重准确率。未选或摘要对会列为不可比较。

运行quality-evaluate生成新的结果文件，保留每次标注版本和结果，勿覆盖旧结果。
结果含TP/FP/FN、实际分母、排除数量和本机错误明细。没有真人标注就没有真实质量结论。
本包不修改业务review、候选人材料或原报告；不联网。原JD和标注留在本机。
"""


def require(condition, message):
    if not condition:
        raise ValueError(message)


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _object(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, "duplicate JSON object key")
        result[key] = value
    return result


def decode_json(raw):
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8-sig")
    def invalid(_):
        raise ValueError("non-finite JSON number")
    return json.loads(raw, object_pairs_hook=_object, parse_constant=invalid)


def _json_lines(raw):
    return [decode_json(line) for line in raw.decode("utf-8-sig").splitlines() if line.strip()]


def _csv(raw):
    reader = csv.DictReader(io.StringIO(raw.decode("utf-8-sig"), newline=""))
    require(reader.fieldnames is not None and len(reader.fieldnames) == len(set(reader.fieldnames)),
            "missing or duplicate CSV headers")
    rows = list(reader)
    require(all(None not in row and all(v is not None for v in row.values()) for row in rows),
            "invalid CSV row width")
    return rows, reader.fieldnames


def label_list(value, allowed, context):
    require(type(value) is list and all(isinstance(v, str) and v in allowed for v in value),
            context + ": unknown label or invalid list")
    require(len(value) == len(set(value)), context + ": repeated label")
    return value


def validate_span(row, text, capabilities):
    require(type(row) is dict, "requirement must be an object")
    start, end = row.get("start"), row.get("end")
    require(type(start) is int and type(end) is int and 0 <= start < end <= len(text),
            "invalid Unicode span")
    require(isinstance(row.get("quote"), str) and text[start:end] == row["quote"],
            "quote does not match exact source span")
    require(isinstance(row.get("capability"), str) and row["capability"] in capabilities,
            "unknown capability")
    require(isinstance(row.get("relation"), str) and row["relation"] in RELATIONS, "unknown relation")
    require(isinstance(row.get("strength"), str) and row["strength"] in STRENGTHS, "unknown strength")


def anchor(row):
    return row["start"], row["end"], row["capability"]


def positive_ai(row):
    return row["relation"] in {"direct", "contextual"} and row["strength"] not in NEGATIVE


def _catalog(config):
    require(type(config) is dict, "invalid saved configuration")
    result = {}
    for name in ("roles", "capabilities"):
        rows = config.get(name)
        require(type(rows) is dict and bool(rows), "missing saved label catalogue")
        result[name] = {}
        for key, value in rows.items():
            require(isinstance(key, str) and key and type(value) is dict and
                    isinstance(value.get("label"), str), "invalid saved label")
            result[name][key] = {"label": value["label"], "description": value.get("description", "")}
    result["relations"] = sorted(RELATIONS)
    result["strengths"] = sorted(STRENGTHS)
    return result


def _grouping(jobs, audit, duplicate_rows, modern):
    selected = {rid for rid, row in audit.items() if row["status"] == "selected"}
    membership, representatives, saved_ids = {}, {}, {}
    duplicate_ids = set()
    for row in duplicate_rows:
        gid, representative = row.get("job_group_id"), row.get("representative_record_id")
        members = decode_json(row.get("source_record_ids", "null"))
        require(isinstance(gid, str) and gid and gid not in duplicate_ids, "invalid duplicate group ID")
        require(type(members) is list and len(members) > 1 and
                all(isinstance(r, str) and r in selected for r in members), "unknown duplicate member")
        require(len(set(members)) == len(members) and representative in members, "invalid duplicate members")
        require(not set(members).intersection(membership), "overlapping duplicate groups")
        key = "group:" + gid
        duplicate_ids.add(gid)
        for rid in members:
            if modern:
                require(audit[rid].get("job_group_id") == gid, "duplicate group disagrees with audit")
            membership[rid] = key
        representatives[key], saved_ids[key] = representative, gid
    for rid in sorted(selected - set(membership)):
        gid = audit[rid].get("job_group_id") if modern else None
        require(not modern or isinstance(gid, str) and bool(gid), "selected audit is missing group ID")
        key = "group:" + gid if modern else "singleton:" + rid
        require(key not in representatives, "saved duplicate membership is incomplete")
        membership[rid] = key
        representatives[key], saved_ids[key] = rid, gid
    groups = defaultdict(set)
    for rid, key in membership.items():
        groups[key].add(rid)
    for members in groups.values():
        require(len({(jobs[r].evidence_level, jobs[r].is_synthetic) for r in members}) == 1,
                "mixed evidence namespaces in saved group")
    return membership, representatives, saved_ids, groups


def _read_source(report):
    manifest_bytes = (report / "run_manifest.json").read_bytes()
    manifest = decode_json(manifest_bytes)
    require(type(manifest) is dict and type(manifest.get("schema_version")) is int
            and manifest["schema_version"] == 1, "unsupported original report schema")
    require(manifest.get("mode") in {"real_sample", "synthetic_demo"}, "unknown original report mode")
    expected = manifest.get("output_files_sha256")
    require(type(expected) is dict, "original report has no file hashes")
    raw = {}
    for name in SOURCE_FILES:
        require(not (report / name).is_symlink(), "source files must not be symlinks")
        raw[name] = (report / name).read_bytes()
        require(sha256(raw[name]) == expected.get(name), "source hash mismatch: " + name)
    hashes = {name: sha256(data) for name, data in raw.items()}
    hashes["run_manifest.json"] = sha256(manifest_bytes)
    labels = _catalog(decode_json(raw["effective_config.json"]))
    items = [JobRecord.from_dict(j) for j in _json_lines(raw["jobs.jsonl"])]
    jobs = {j.record_id: j for j in items}
    require(len(jobs) == len(items), "duplicate source record")
    audit_rows, headers = _csv(raw["input_audit.csv"])
    audit = {r.get("record_id"): r for r in audit_rows}
    require(len(audit) == len(audit_rows) and set(audit) == set(jobs), "audit/source record mismatch")
    statuses = {"selected", "synthetic_excluded", "real_excluded_from_demo", "future_collection_time",
                "stale_snapshot", "expired", "role_unmatched", "platform_filtered", "snippet_superseded_by_full_text"}
    for rid, row in audit.items():
        require(row.get("status") in statuses, "unknown original selection status")
        row["roles"] = label_list(decode_json(row.get("roles", "null")), labels["roles"], "audit roles")
        require(row.get("evidence_level") == jobs[rid].evidence_level and
                row.get("is_synthetic") == str(jobs[rid].is_synthetic), "audit evidence namespace mismatch")
    duplicates, _ = _csv(raw["duplicate_groups.csv"])
    membership, representatives, saved_ids, groups = _grouping(jobs, audit, duplicates, "job_group_id" in headers)
    filters = manifest.get("filters")
    require(type(filters) is dict, "missing report filters")
    role_scope = label_list(filters.get("roles") or list(labels["roles"]), labels["roles"], "role scope")
    platforms = filters.get("platforms")
    require(platforms is None or type(platforms) is list and all(isinstance(p, str) for p in platforms),
            "invalid platform filter")
    demo = manifest["mode"] == "synthetic_demo"
    corpus = []
    for rid, job in jobs.items():
        key = membership.get(rid)
        full = job.evidence_level == "full_text" and job.is_synthetic == demo
        corpus.append({
            "record_id": rid, "title": job.title, "text": job.text, "company": job.company,
            "location": job.location, "platform": job.platform, "url": job.url,
            "collected_at": job.collected_at, "evidence_level": job.evidence_level,
            "is_synthetic": job.is_synthetic, "selection_status": audit[rid]["status"],
            "group_key": key, "representative_record_id": representatives.get(key),
            "predicted_roles": [r for r in audit[rid]["roles"] if r in role_scope],
            "classification_eligible": (full and (not platforms or job.platform in platforms)
                                        and (key is None or representatives[key] == rid)),
            "extraction_eligible": full and key is not None and representatives[key] == rid,
            "dedup_eligible": full and key is not None,
        })
    predictions, seen_ids, seen_anchors, inferred_ids = [], set(), set(), {}
    original_rows = _json_lines(raw["requirements.jsonl"])
    for row in original_rows:
        require(type(row) is dict, "invalid original prediction")
        rid, ident, gid = row.get("record_id"), row.get("requirement_id"), row.get("job_group_id")
        require(isinstance(rid, str) and rid in membership, "prediction record was not selected")
        key = membership[rid]
        require(representatives[key] == rid, "prediction span is not on its saved representative")
        require(isinstance(ident, str) and ident and ident not in seen_ids, "invalid/repeated prediction ID")
        require(isinstance(gid, str) and bool(gid), "prediction has no group ID")
        require(saved_ids[key] is None or gid == saved_ids[key], "prediction group disagrees with saved group")
        require(gid not in inferred_ids or inferred_ids[gid] == key, "prediction merges distinct saved groups")
        require(not any(k == key and g != gid for g, k in inferred_ids.items()), "conflicting prediction group IDs")
        inferred_ids[gid] = key
        require(row.get("evidence_level") == jobs[rid].evidence_level and
                type(row.get("is_synthetic")) is bool and row["is_synthetic"] == jobs[rid].is_synthetic,
                "prediction evidence namespace mismatch")
        validate_span(row, jobs[rid].text, labels["capabilities"])
        label_list(row.get("roles"), labels["roles"], "prediction roles")
        require(row.get("review_status") in {"rule_accepted", "needs_review", "approved", "rejected"},
                "unknown original review status")
        sources = row.get("source_record_ids", [])
        require(type(sources) is list and all(isinstance(s, str) for s in sources), "invalid source members")
        require(not sources or len(sources) == len(set(sources)) and set(sources) == groups[key],
                "prediction source membership mismatch")
        unique = (rid, *anchor(row))
        require(unique not in seen_anchors, "duplicate/conflicting original span")
        seen_ids.add(ident)
        seen_anchors.add(unique)
        saved = {k: row[k] for k in ("requirement_id", "record_id", "job_group_id", "start", "end",
                                    "quote", "capability", "relation", "strength", "review_status")}
        saved["accepted_positive_ai"] = (row["review_status"] in {"rule_accepted", "approved"} and
                                         jobs[rid].evidence_level == "full_text" and positive_ai(row))
        predictions.append(saved)
    stats = manifest.get("stats")
    require(type(stats) is dict, "missing original statistics")
    observed = {"current_source_records": len(jobs), "selected_source_records": len(membership),
                "deduplicated_groups": len(groups), "duplicate_groups": len(duplicates),
                "full_text_job_groups": sum(jobs[r].evidence_level == "full_text" for r in representatives.values()),
                "requirement_rows": len(predictions),
                "accepted_positive_requirement_rows": sum(r["accepted_positive_ai"] for r in predictions)}
    for name, count in observed.items():
        require(type(stats.get(name)) is int and stats[name] == count, "incomplete original data: " + name)
    source = {k: manifest.get(k) for k in ("schema_version", "project_version", "rule_engine",
                                         "created_at", "as_of", "mode", "status", "filters", "stats")}
    source.update({"report_path": str(report), "files_sha256": hashes, "role_scope": role_scope,
                   "grouping_basis": "saved_audit_and_duplicate_groups" if "job_group_id" in headers
                   else "saved_duplicate_groups_and_remaining_singletons"})
    return corpus, predictions, labels, source


def write_new(path: Path, data: bytes):
    """Never overwrite a previous packet, annotation version or evaluation."""
    with path.open("xb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def create_packet(report: str | Path, output: str | Path):
    report, output = Path(report).resolve(), Path(output).resolve()
    require(output != report and report not in output.parents, "packet must be outside the original report")
    require(not output.exists(), "packet output must be a new directory")
    corpus, predictions, labels, source = _read_source(report)
    values = {"corpus.json": corpus, "predictions.json": predictions,
              "labels.json": labels, "source.json": source}
    contents = {name: (json_text(value) + "\n").encode("utf-8") for name, value in values.items()}
    # Plain text, not HTML/Markdown: source content cannot inject active markup.
    contents["documents.txt"] = "\n\n".join(
        "记录 " + d["record_id"] + "\n标题 " + d["title"] + "\n正文（偏移从正文第一个码点0开始）\n" + d["text"]
        for d in corpus).encode("utf-8")
    contents["REVIEW_PROTOCOL.txt"] = PROTOCOL.encode("utf-8")
    manifest = {"schema_version": 1, "status": "complete", "created_at": utc_now(),
                "files_sha256": {n: sha256(data) for n, data in contents.items()}}
    manifest["packet_id"] = sha256(json_text(manifest).encode("utf-8"))
    annotations = {"schema_version": 1, "packet_id": manifest["packet_id"], "documents": [
        {"record_id": d["record_id"], "review_state": "pending", "author": "", "reviewed_at": "",
         "roles": None, "requirements_complete": False, "requirements": [], "notes": ""} for d in corpus],
        "dedup_pairs": []}
    output.mkdir(parents=True, exist_ok=False)
    # The manifest is last; interrupted writes leave a visibly incomplete packet.
    for name, data in contents.items():
        write_new(output / name, data)
    write_new(output / "annotations.json", (json_text(annotations) + "\n").encode("utf-8"))
    write_new(output / "packet_manifest.json", (json_text(manifest) + "\n").encode("utf-8"))
    return {"packet_id": manifest["packet_id"], "documents": len(corpus), "predictions": len(predictions),
            "extraction_eligible": sum(d["extraction_eligible"] for d in corpus),
            "classification_eligible": sum(d["classification_eligible"] for d in corpus),
            "human_confirmed_documents": 0, "network_calls": 0, "output": str(output)}


def load_packet(packet: str | Path):
    packet = Path(packet)
    manifest = decode_json((packet / "packet_manifest.json").read_bytes())
    require(type(manifest) is dict and set(manifest) ==
            {"schema_version", "status", "created_at", "files_sha256", "packet_id"}, "invalid packet manifest")
    require(type(manifest["schema_version"]) is int and manifest["schema_version"] == 1
            and manifest["status"] == "complete", "incomplete/unsupported packet")
    expected_id = sha256(json_text({k: v for k, v in manifest.items() if k != "packet_id"}).encode("utf-8"))
    require(manifest["packet_id"] == expected_id, "packet identity mismatch")
    require(type(manifest["files_sha256"]) is dict and set(manifest["files_sha256"]) == set(PACKET_FILES),
            "packet immutable file set mismatch")
    values = {}
    for name in PACKET_FILES:
        require(not (packet / name).is_symlink(), "packet immutable files must not be symlinks")
        raw = (packet / name).read_bytes()
        require(sha256(raw) == manifest["files_sha256"][name], "packet file hash mismatch: " + name)
        if name.endswith(".json"):
            values[name[:-5]] = decode_json(raw)
    return manifest, values
