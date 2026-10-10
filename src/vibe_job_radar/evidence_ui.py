"""Versioned, local-only evidence editing; the existing pipeline remains authoritative."""
from __future__ import annotations

import base64
import copy
import csv
import hashlib
import io
import json
import os
import re
import sqlite3
import shutil
import tempfile
import uuid
import zipfile
from contextlib import closing
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

from .config import validate
from .extract import RuleExtractor
from .url_safety import credential_query_key
from .metrics import catalog_rows, format_metric, validate_metric
from .models import JobRecord
from .pipeline import analyze
from .store import Store
from .synthesis import load_candidate
from .utils import atomic_json, canonical_url, json_text, utc_now
from .workspace import InputError, Workspace, text_field

HEX = re.compile(r"[a-f0-9]{64}")
EID = re.compile(r"e_[a-f0-9]{32}")


class Conflict(InputError):
    """The browser edited an old revision; never silently overwrite newer work."""


def number(data, name, default, maximum):
    value = data.get(name, default)
    if type(value) is not int or not 0 <= value <= maximum:
        raise InputError(f"{name} 必须为 0～{maximum} 的整数。")
    return value


class EvidenceService:
    def __init__(self, workspace: Workspace):
        self.workspace = workspace
        self.root = workspace.root / "evidence"
        self.root.mkdir(exist_ok=True, mode=0o700)
        self.artifacts = self.root / "artifacts"
        self.artifacts.mkdir(exist_ok=True, mode=0o700)
        if self.root.is_symlink() or self.artifacts.is_symlink():
            raise InputError("证据目录不能使用符号链接。")
        self.db = self.root / "history.sqlite"
        with closing(self.connect()) as c:
            c.executescript("""
            CREATE TABLE IF NOT EXISTS revisions(revision INTEGER PRIMARY KEY, body TEXT NOT NULL,
                                                 action TEXT NOT NULL, created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS artifacts(id TEXT PRIMARY KEY, name TEXT NOT NULL,
                                                size INTEGER NOT NULL, created_at TEXT NOT NULL);
            """)
            c.commit()

    def connect(self):
        return sqlite3.connect(self.db, timeout=30)

    @staticmethod
    def _current(c):
        row = c.execute("SELECT revision,body FROM revisions ORDER BY revision DESC LIMIT 1").fetchone()
        return (row[0], json.loads(row[1])) if row else (0, {"name": "本人", "evidence": [], "reviews": {}})

    def state(self, data=None):
        with closing(self.connect()) as c:
            revision, state = self._current(c)
            artifacts = [{"id": r[0], "name": r[1], "size": r[2]} for r in c.execute("SELECT id,name,size FROM artifacts ORDER BY created_at DESC")]
            history = [{"revision": r[0], "action": r[1], "created_at": r[2]} for r in c.execute("SELECT revision,action,created_at FROM revisions ORDER BY revision DESC LIMIT 100")]
        runs = [r for r in self.workspace.status()["runs"] if r["mode"] == "real_sample"]
        return {"revision": revision, **state, "artifacts": artifacts, "history": history,
                "runs": runs, "metrics": catalog_rows(),
                "capabilities": {k: v["label"] for k, v in self.workspace.config["capabilities"].items()}}

    def _write(self, expected, action, mutate):
        if type(expected) is not int or expected < 0:
            raise InputError("缺少有效的 expected_revision。")
        with closing(self.connect()) as c:
            c.execute("BEGIN IMMEDIATE")
            revision, state = self._current(c)
            if revision != expected:
                raise Conflict("其他窗口已修改证据，请刷新并核对后重新保存。")
            mutate(state)
            c.execute("INSERT INTO revisions VALUES(?,?,?,?)", (revision + 1, json_text(state), action, utc_now()))
            c.commit()
        return self.state()

    def _artifact_path(self, artifact_id):
        if not isinstance(artifact_id, str) or not HEX.fullmatch(artifact_id):
            raise InputError("附件 ID 无效。")
        with closing(self.connect()) as c:
            if not c.execute("SELECT 1 FROM artifacts WHERE id=?", (artifact_id,)).fetchone():
                raise InputError("附件不存在。")
        path = self.artifacts / artifact_id
        if path.is_symlink() or not path.is_file() or path.resolve().parent != self.artifacts.resolve():
            raise InputError("附件路径无效。")
        if hashlib.sha256(path.read_bytes()).hexdigest() != artifact_id:
            raise InputError("附件哈希不一致，拒绝使用被修改的证据。")
        return path

    def upload(self, data):
        name = text_field(data, "name", required=True, limit=200)
        content = text_field(data, "content_base64", required=True, limit=1_400_000)
        if data.get("rights_confirmed") is not True:
            raise InputError("请确认你有权使用该附件，且不含不必要的个人信息或密钥。")
        try:
            raw = base64.b64decode(content, validate=True)
        except ValueError as exc:
            raise InputError("附件编码错误。") from exc
        if not 0 < len(raw) <= 1_000_000:
            raise InputError("单个附件必须在 1 字节～1 MB 之间。")
        ident = hashlib.sha256(raw).hexdigest()
        with closing(self.connect()) as c:
            total, count = c.execute("SELECT COALESCE(SUM(size),0),COUNT(*) FROM artifacts").fetchone()
            exists = c.execute("SELECT 1 FROM artifacts WHERE id=?", (ident,)).fetchone()
            if not exists and (total + len(raw) > 50_000_000 or count >= 100):
                raise InputError("此工作区附件总量上限为 50 MB / 100 个。请使用新的工作区。")
            path = self.artifacts / ident
            if path.is_symlink():
                raise InputError("附件路径不能使用符号链接。")
            if not path.exists():
                with path.open("xb") as stream:
                    stream.write(raw)
            if hashlib.sha256(path.read_bytes()).hexdigest() != ident:
                raise InputError("附件哈希不一致。")
            c.execute("INSERT OR IGNORE INTO artifacts VALUES(?,?,?,?)", (ident, Path(name.replace("\\", "/")).name, len(raw), utc_now()))
            c.commit()
        return {"artifact_id": ident, "sha256": ident, "size": len(raw)}

    def _source(self, run_id):
        manifest = json.loads(self.workspace.report_file(run_id, "run_manifest.json").read_text(encoding="utf-8"))
        if manifest["mode"] != "real_sample":
            raise InputError("个人证据只能映射真实输入报告，不能映射合成演示。")
        contents = {}
        for name in ("requirements.jsonl", "jobs.jsonl", "effective_config.json"):
            raw = self.workspace.report_file(run_id, name).read_bytes()
            if hashlib.sha256(raw).hexdigest() != manifest["output_files_sha256"].get(name):
                raise InputError("来源报告内容已改变，请重新分析后选择新报告。")
            contents[name] = raw.decode("utf-8")
        rows = [json.loads(line) for line in contents["requirements.jsonl"].splitlines() if line.strip()]
        jobs = [JobRecord.from_dict(json.loads(line)) for line in contents["jobs.jsonl"].splitlines() if line.strip()]
        config = json.loads(contents["effective_config.json"])
        validate(config)
        return manifest, rows, jobs, config

    def _source_jobs(self, data, fields):
        if not isinstance(data, dict) or set(data) - fields:
            raise InputError("原文查询含有不支持的字段。")
        run_id = text_field(data, "run_id", required=True, limit=32)
        _, _, jobs, _ = self._source(run_id)
        source = {job.record_id: job for job in jobs}
        if len(source) != len(jobs):
            raise InputError("来源报告存在重复岗位标识，无法唯一定位原文。")
        return run_id, source

    @staticmethod
    def _source_metadata(job):
        # Bound the list independently of body size; full JD is fetched only
        # when the user explicitly opens one report-scoped record.
        return {"record_id": job.record_id, "title": job.title[:500],
                "title_is_excerpt": len(job.title) > 500,
                "company": job.company[:300], "platform": job.platform[:100],
                "url": job.url[:2048], "evidence_level": job.evidence_level,
                "collected_at": job.collected_at, "published_at": job.published_at[:100]}

    def sources(self, data):
        run_id, source = self._source_jobs(data, {"run_id", "page"})
        page = number(data, "page", 0, 100000)
        jobs = list(source.values())
        if page and page * 20 >= len(jobs):
            raise InputError("原文页码超出本报告范围。")
        return {"run_id": run_id, "page": page, "page_size": 20,
                "total": len(jobs),
                "rows": [self._source_metadata(job) for job in jobs[page*20:page*20+20]]}

    def source(self, data):
        run_id, source = self._source_jobs(data, {"run_id", "record_id"})
        record_id = text_field(data, "record_id", required=True, limit=26)
        if not re.fullmatch(r"j_[a-f0-9]{24}", record_id) or record_id not in source:
            raise InputError("所选岗位不属于这份报告。")
        job = source[record_id]
        return {"run_id": run_id, "record": {**self._source_metadata(job), "text": job.text}}

    @staticmethod
    def _reviews_for_run(state, run_id):
        """Read report-scoped keys plus legacy rid-only entries without rewriting history."""
        selected = {}
        prefix = run_id + ":"
        for key, review in state["reviews"].items():
            if review.get("source_run_id") == run_id and ":" not in key:
                selected[key] = review
        for key, review in state["reviews"].items():
            if key.startswith(prefix) and review.get("source_run_id") == run_id:
                selected[key[len(prefix):]] = review
        return selected

    def catalogue(self, data):
        run_id = text_field(data, "run_id", required=True, limit=32)
        _, rows, jobs, source_config = self._source(run_id)
        query = text_field(data, "query", limit=200).casefold()
        page = number(data, "page", 0, 100000)
        state = self.state()
        source = {j.record_id: j for j in jobs}
        reviews = self._reviews_for_run(state, run_id)
        if query:
            rows = [r for r in rows if query in (r["quote"] + r["title"] + r["capability"]).casefold()]
        total = len(rows)
        for row in rows[page * 50:page * 50 + 50]:
            job = source[row["record_id"]]
            if job.text[row["start"]:row["end"]] != row["quote"]:
                raise InputError("原文偏移校验失败。")
            row["source_text"] = job.text
            row["saved_review"] = reviews.get(row["requirement_id"])
        return {"rows": rows[page * 50:page * 50 + 50], "page": page, "total": total,
                "revision": state["revision"], "page_size": 50,
                "capabilities": {k: v["label"] for k, v in source_config["capabilities"].items()}}

    def review(self, data):
        run_id = text_field(data, "run_id", required=True, limit=32)
        rid = text_field(data, "requirement_id", required=True, limit=100)
        _, rows, _, _ = self._source(run_id)
        if rid not in {r["requirement_id"] for r in rows}:
            raise InputError("所选要求不属于这个报告。")
        decision = data.get("decision")
        if decision not in ("approve", "reject", "pending"):
            raise InputError("复核决定无效。")
        record = {"decision": decision, "reviewer": text_field(data, "reviewer", required=True),
                  "reason": text_field(data, "reason", required=True), "reviewed_at": utc_now(), "source_run_id": run_id}
        def mutate(state):
            # Legacy records have source_run_id but use only rid as the key.
            # Migrate only this report's entry; another report must not be removed.
            if state["reviews"].get(rid, {}).get("source_run_id") == run_id:
                del state["reviews"][rid]
            state["reviews"][run_id + ":" + rid] = record
        return self._write(data.get("expected_revision"), f"review:{run_id}:{rid}:{decision}", mutate)

    def metric(self, data):
        allowed = {"metric_id", "baseline", "current", "sample_size", "baseline_sample_size", "window", "baseline_window", "comparison_basis"}
        if not isinstance(data, dict) or set(data) - allowed:
            raise InputError("指标含有不支持的字段。")
        for key in ("window", "comparison_basis"):
            text_field(data, key, required=True)
        text_field(data, "baseline_window")
        try:
            result = validate_metric(data)
        except (ValueError, TypeError, KeyError) as exc:
            raise InputError("指标无效：检查非负数值、样本量、窗口、基线和百分比范围。") from exc
        return {"metric": result, "description": format_metric(result)}

    def _candidate(self, state, directory):
        candidate = {"name": state["name"], "evidence": []}
        for item in state["evidence"]:
            e = copy.deepcopy(item)
            if e.get("artifact_id"):
                path = self._artifact_path(e["artifact_id"])
                e["evidence_ref"] = os.path.relpath(path, directory).replace(os.sep, "/")
                e["sha256"] = e["artifact_id"]
            else:
                e["evidence_ref"] = e.get("external_url", "")
            candidate["evidence"].append(e)
        return candidate

    def save(self, data):
        allowed = {"expected_revision", "name", "evidence_id", "project", "contribution", "scope", "run_id",
                   "artifact_id", "external_url", "capabilities", "requirement_ids", "metrics", "review_status", "reviewer", "attested"}
        if set(data) - allowed:
            raise InputError("不接受本地路径或未知证据字段；请上传附件或填写 HTTPS 引用。")
        eid = data.get("evidence_id") or "e_" + uuid.uuid4().hex
        if not isinstance(eid, str) or not EID.fullmatch(eid):
            raise InputError("证据 ID 无效。")
        run_id = text_field(data, "run_id", required=True, limit=32)
        _, rows, _, source_config = self._source(run_id)
        known = {r["requirement_id"]: r for r in rows}
        ids = data.get("requirement_ids", [])
        caps = data.get("capabilities", [])
        for values, allowed_ids in ((ids, known), (caps, source_config["capabilities"])):
            if not isinstance(values, list) or len(values) > 1000 or not all(isinstance(x, str) and x in allowed_ids for x in values):
                raise InputError("要求或能力映射无效，请从界面重新选择。")
        if not caps or any(known[r]["capability"] not in caps for r in ids):
            raise InputError("请勾选证据真实支持的能力，且须包含所映射要求的能力。")
        metrics = data.get("metrics", [])
        if not isinstance(metrics, list) or len(metrics) > 20:
            raise InputError("每条证据最多 20 项指标。")
        for m in metrics:
            self.metric(m)
        status = data.get("review_status", "draft")
        scope = data.get("scope")
        if status not in ("draft", "approved") or scope not in ("offline", "shadow", "production"):
            raise InputError("证据状态或验证阶段无效。")
        if status == "approved" and data.get("attested") is not True:
            raise InputError("请显式确认本人经历、指标与逐条映射；确认不是第三方认证。")
        artifact = text_field(data, "artifact_id", limit=64)
        external = text_field(data, "external_url")
        if artifact and external:
            raise InputError("请选择附件或外部引用之一。")
        if artifact:
            self._artifact_path(artifact)
        if external:
            external = canonical_url(external)
            if urlsplit(external).scheme != "https" or any(credential_query_key(k) for k, _ in parse_qsl(urlsplit(external).query)):
                raise InputError("外部引用必须为不带登录凭据的 HTTPS URL。")
        if status == "approved" and not (artifact or external):
            raise InputError("确认的证据必须有附件或外部引用。")
        e = {"evidence_id": eid, "project": text_field(data, "project", required=True),
             "contribution": text_field(data, "contribution", required=True, limit=4000),
             "scope": scope, "source_run_id": run_id, "artifact_id": artifact, "external_url": external,
             "capabilities": sorted(set(caps)), "requirement_ids": sorted(set(ids)), "metrics": metrics,
             "review_status": status, "reviewer": text_field(data, "reviewer", required=status == "approved"),
             "reviewed_at": utc_now() if status == "approved" else ""}
        name = text_field(data, "name", required=True)
        def mutate(state):
            if data.get("evidence_id") and not any(x["evidence_id"] == eid for x in state["evidence"]):
                raise InputError("正在编辑的证据不存在，请刷新。")
            entries = [x for x in state["evidence"] if x["evidence_id"] != eid] + [e]
            if len(entries) > 200:
                raise InputError("此工作区最多保存 200 条个人证据。")
            state.update(name=name, evidence=entries)
            with tempfile.TemporaryDirectory(dir=self.root) as tmp:
                path = Path(tmp) / "candidate.json"
                # Validate this entry against its own source snapshot; other reports
                # may legitimately have different capability contracts.
                atomic_json(path, self._candidate({"name": name, "evidence": [e]}, path.parent))
                load_candidate(path, source_config)
        return self._write(data.get("expected_revision"), f"evidence:{eid}:{status}", mutate)

    def remove(self, data):
        eid = text_field(data, "evidence_id", required=True)
        def mutate(state):
            if not any(e["evidence_id"] == eid for e in state["evidence"]):
                raise InputError("证据不存在。")
            state["evidence"] = [e for e in state["evidence"] if e["evidence_id"] != eid]
        return self._write(data.get("expected_revision"), "remove:" + eid, mutate)

    def generate(self, data):
        state = self.state()
        if type(data.get("expected_revision")) is not int or data["expected_revision"] != state["revision"]:
            raise Conflict("证据版本已变化，请刷新后生成报告。")
        run_id = text_field(data, "run_id", required=True, limit=32)
        manifest, frozen_rows, jobs, config = self._source(run_id)
        if manifest.get("rule_engine") != RuleExtractor.version:
            raise InputError("提取规则版本与来源报告不同，请先重新分析并显式复核、映射新报告。")
        all_evidence_count = len(state["evidence"])
        # Only explicitly mapped source-report evidence participates. Other
        # reports remain editable/exportable but do not contaminate this one.
        state["evidence"] = [e for e in state["evidence"] if e["source_run_id"] == run_id]
        state["reviews"] = self._reviews_for_run(state, run_id)
        ident = uuid.uuid4().hex
        output = self.workspace.root / "reports" / ident
        with tempfile.TemporaryDirectory(prefix=".evidence-input-", dir=output.parent) as tmp:
            tmp = Path(tmp)
            with Store(tmp / "snapshot.sqlite") as store:
                for job in jobs:
                    store.add(job)
            candidate = self._candidate(state, tmp)
            atomic_json(tmp / "candidate.json", candidate)
            atomic_json(tmp / "reviews.json", state["reviews"])
            analyze(tmp / "snapshot.sqlite", output, config=config, candidate_path=tmp / "candidate.json",
                    reviews_path=tmp / "reviews.json", role_filter=manifest["filters"]["roles"],
                    platform_filter=manifest["filters"]["platforms"], as_of=manifest["as_of"],
                    max_age_days=manifest["filters"]["max_collection_age_days"])
        # Version labels alone are not enough (e.g. an unversioned change or
        # a prior LLM proposal). Reject semantic drift rather than silently
        # reusing review/mapping IDs for a different extracted requirement set.
        fields = ("requirement_id", "record_id", "job_group_id", "capability", "quote", "start", "end",
                  "relation", "strength", "tools", "roles", "evidence_level", "is_synthetic")
        def semantics(rows):
            return sorted((json_text({k: row[k] for k in fields}) for row in rows))
        regenerated = [json.loads(line) for line in (output / "requirements.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
        if semantics(regenerated) != semantics(frozen_rows):
            shutil.rmtree(output)
            raise InputError("提取结果与已冻结的来源要求不一致；请重新分析，并显式复核和映射，旧报告未被修改。")
        # Include the UI revision in the same auditable report, without rewriting the source report.
        atomic_json(output / "evidence_revision.json", {"revision": state["revision"], "source_run_id": run_id,
                    "source_as_of": manifest["as_of"], "generated_at": utc_now(),
                    "source_evidence_count": len(state["evidence"]),
                    "excluded_other_report_evidence_count": all_evidence_count - len(state["evidence"]),
                    "note": "基于所选历史招聘快照和本次个人证据生成，不表示重新核实职位仍在招聘。"})
        result = json.loads((output / "run_manifest.json").read_text(encoding="utf-8"))
        result["output_files_sha256"]["evidence_revision.json"] = hashlib.sha256((output / "evidence_revision.json").read_bytes()).hexdigest()
        atomic_json(output / "run_manifest.json", result)
        report = self.workspace.report(ident)
        with self.workspace.report_file(ident, "job_coverage.csv").open(encoding="utf-8-sig", newline="") as stream:
            report["coverage"] = list(csv.DictReader(stream))
        with self.workspace.report_file(ident, "requirement_evidence_matrix.csv").open(encoding="utf-8-sig", newline="") as stream:
            report["matrix"] = list(csv.DictReader(stream))[:200]
        return report

    def export(self):
        state = self.state()
        candidate = {"name": state["name"], "evidence": copy.deepcopy(state["evidence"])}
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
            included = set()
            for e in candidate["evidence"]:
                if e.get("artifact_id"):
                    aid = e["artifact_id"]
                    e["evidence_ref"], e["sha256"] = "artifacts/" + aid, aid
                    if aid not in included:
                        archive.write(self._artifact_path(aid), e["evidence_ref"])
                        included.add(aid)
                else:
                    e["evidence_ref"] = e.get("external_url", "")
            archive.writestr("candidate.json", json_text(candidate))
            run_ids = {r.get("source_run_id", "") for r in state["reviews"].values()}
            run_ids.update(e.get("source_run_id", "") for e in candidate["evidence"])
            run_ids = sorted(r for r in run_ids if re.fullmatch(r"[a-f0-9]{32}", r))
            by_report = {r: self._reviews_for_run(state, r) for r in run_ids}
            archive.writestr("reviews.by_report.json", json_text(by_report))
            # A root CLI file is unambiguous only for at most one source report.
            if len(run_ids) <= 1:
                archive.writestr("reviews.json", json_text(by_report[run_ids[0]] if run_ids else {}))
            for run_id in run_ids:
                scoped = {"name": candidate["name"], "evidence": copy.deepcopy([
                    e for e in candidate["evidence"] if e.get("source_run_id") == run_id])}
                for e in scoped["evidence"]:
                    if e.get("artifact_id"):
                        e["evidence_ref"] = "../../artifacts/" + e["artifact_id"]
                archive.writestr(f"reports/{run_id}/candidate.json", json_text(scoped))
                archive.writestr(f"reports/{run_id}/reviews.json", json_text(by_report[run_id]))
            archive.writestr("revision.json", json_text({"revision": state["revision"], "history": state["history"]}))
            archive.writestr("README.txt", "个人证据导出；包含个人数据，请妥善保管。哈希只校验文件一致性，不证明经历真实性。\n"
                            "多报告时分别使用 reports/<来源报告ID>/candidate.json 与 reviews.json；根目录不合并冲突决定。\n"
                            "reviews.by_report.json 按来源报告保存复核；使用原 CLI 时应匹配对应来源数据与配置。\n")
        return output.getvalue()
