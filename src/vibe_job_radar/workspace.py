"""Local onboarding service. Reuses the evidence pipeline; never logs in to job sites."""
from __future__ import annotations

import json
import hashlib
import os
import platform
import re
import sqlite3
import sys
import tempfile
import threading
import uuid
from collections import Counter
from contextlib import closing
from itertools import islice
from dataclasses import asdict, replace
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

from .config import detect_roles, load_config, platform_for_url
from .models import JobRecord
from .store import Store

MAX_UPLOAD = 1_000_000
RUN_ID = re.compile(r"[a-f0-9]{32}")


class InputError(ValueError):
    """An actionable error safe to display to the local user."""


def text_field(data: dict, key: str, *, required: bool = False, limit: int = 2000) -> str:
    value = data.get(key, "")
    if not isinstance(value, str) or len(value) > limit:
        raise InputError(f"{key}: 必须为不超过 {limit} 字符的文本。")
    if required and not value.strip():
        raise InputError(f"请填写 {key}。")
    return value


def _redact(value, secret: str):
    if isinstance(value, str):
        return value.replace(secret, "[REDACTED]")
    if isinstance(value, list):
        return [_redact(item, secret) for item in value]
    if isinstance(value, dict):
        return {key: _redact(item, secret) for key, item in value.items()}
    return value


class _SearchStore:
    """Ensure provider-echoed credentials cannot enter persisted observations/events."""
    def __init__(self, store: Store, secret: str):
        self.store, self.secret = store, secret

    def add(self, job: JobRecord) -> bool:
        if any(self.secret in value for value in job.to_dict().values() if isinstance(value, str)):
            raise ValueError("provider echoed a credential; result discarded")
        return self.store.add(job)

    def event(self, action: str, status: str, details: dict) -> None:
        self.store.event(action, status, _redact(details, self.secret))


class Workspace:
    def __init__(self, root: Path | str):
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        for name in ("imports", "reports", "discovery"):
            (self.root / name).mkdir(exist_ok=True, mode=0o700)
        self.db = self.root / "jobs.sqlite"
        self.demo_db = self.root / "demo.sqlite"
        self.config = load_config()
        self._dns_lock = threading.Lock()

    @property
    def dns_resolver(self):
        # Only one resolver/cache/rate budget per workspace object. No file or
        # request is created merely by opening the page.
        with self._dns_lock:
            if not hasattr(self, '_dns_resolver'):
                from .encrypted_dns import PublicResolver
                from .network_settings import read_settings
                self._dns_resolver = PublicResolver(permission=lambda: read_settings(self)['mode'] == 'fake_ip_doh')
            return self._dns_resolver

    def network_policy(self):
        from .network_settings import capture_policy
        return capture_policy(self)

    def network_state(self):
        from .network_settings import state
        return state(self)

    def network_preferences(self, data):
        from .network_settings import save
        return save(self, data)

    def network_proxy_preferences(self, data):
        from .network_settings import save_proxy
        return save_proxy(self, data)

    def doctor(self) -> dict:
        # Probe the actual directory and SQLite, not just os.access(). Never contacts a provider.
        with tempfile.TemporaryDirectory(prefix=".doctor-", dir=self.root) as tmp:
            with closing(sqlite3.connect(str(Path(tmp) / "probe.sqlite"))) as connection:
                connection.execute("CREATE TABLE probe (value INTEGER)")
                connection.execute("INSERT INTO probe VALUES (1)")
                assert connection.execute("SELECT value FROM probe").fetchone()[0] == 1
        return {"python": platform.python_version(), "python_supported": sys.version_info >= (3, 10),
                "sqlite": sqlite3.sqlite_version, "workspace_writable": True,
                "workspace": str(self.root), "network_tested": False,
                "brave_key_configured": bool(os.environ.get("BRAVE_SEARCH_API_KEY", "")),
                "platform_login_supported": False, "llm_enabled": False}

    def _records(self) -> list[JobRecord]:
        if not self.db.is_file():
            return []
        with Store(self.db) as store:
            return [j for j in store.records() if not j.is_synthetic]

    def status(self) -> dict:
        records = self._records()
        counts = Counter(j.evidence_level for j in records)
        runs = []
        for path in (self.root / "reports").iterdir():
            if not RUN_ID.fullmatch(path.name) or path.is_symlink():
                continue
            manifest = path / "run_manifest.json"
            if manifest.is_file():
                try:
                    report = json.loads(manifest.read_text(encoding="utf-8"))
                    runs.append({"id": path.name, "created_at": report["created_at"],
                                 "mode": report["mode"], "stats": report["stats"]})
                except (ValueError, KeyError, OSError):
                    continue
        return {"workspace": str(self.root), "counts": {"records": len(records),
                    "full_text": counts["full_text"], "snippet": counts["snippet"]},
                "roles": {k: v.get("label", k) for k, v in self.config["roles"].items()},
                "platforms": {k: {"label": p.get("label", k), "domains": p["domains"],
                    "manual_import": "available", "automatic_login": "not_implemented",
                    "live_acceptance": "not_verified"} for k, p in self.config["platforms"].items()},
                "brave_key_configured": bool(os.environ.get("BRAVE_SEARCH_API_KEY", "")),
                "records": [{"title": j.title, "platform": j.platform, "url": j.url,
                    "evidence_level": j.evidence_level, "roles": detect_roles(j, self.config)}
                    for j in records[-200:]], "display_limit": 200,
                "runs": sorted(runs, key=lambda x: x["created_at"], reverse=True)[:30],
                "complete_market_coverage": False}

    def _check_source(self, job: JobRecord) -> None:
        if not job.rights_note.strip():
            raise InputError("必须填写来源和允许使用范围；勾选确认不等于获得平台授权。")
        if not job.url and not job.source_ref.strip():
            raise InputError("填写职位来源链接；仅有 App 文本时请填写可追溯的来源说明。")
        if job.url:
            parsed = urlsplit(job.url)
            if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
                raise InputError("来源链接必须为不带用户名/密码的 HTTPS URL。")
            if any(re.search(r"token|cookie|session|password|authorization", key, re.I)
                   for key, _ in parse_qsl(parsed.query)):
                raise InputError("来源链接可能包含登录凭据，请改用不带凭据的职位分享链接。")
            actual = platform_for_url(job.url, self.config)
            if job.platform in self.config["platforms"] and actual != job.platform:
                raise InputError("来源链接与所选平台不一致，请核对平台及职位链接。")

    def add_job(self, data: dict) -> dict:
        allowed = {"title", "text", "company", "location", "platform", "url", "source_ref",
                   "rights_note", "evidence_level", "full_text_confirmed"}
        if set(data) - allowed:
            raise InputError("存在不支持的字段；工作台不接收平台密码或 Cookie。")
        fields = {key: text_field(data, key, required=key in {"title", "text", "rights_note"},
                                 limit=150_000 if key == "text" else 2000)
                  for key in allowed - {"full_text_confirmed", "evidence_level", "platform"}}
        level = data.get("evidence_level", "full_text")
        if level == "full_text" and data.get("full_text_confirmed") is not True:
            raise InputError("请确认粘贴的是完整岗位职责与要求；不完整内容应标记为摘要线索。")
        selected = data.get("platform", "manual")
        if selected not in [*self.config["platforms"], "manual", "unknown"]:
            raise InputError("未知平台。")
        job = JobRecord(**fields, platform=selected, evidence_level=level, source_mode="manual")
        self._check_source(job)
        with Store(self.db) as store:
            inserted = store.add(job)
            store.event("workbench_capture", "ok", {"record_id": job.record_id,
                        "evidence_level": job.evidence_level})
        roles = detect_roles(job, self.config)
        return {"new_snapshots": int(inserted), "record_id": job.record_id, "roles": roles,
                "message": ("已保存。" if inserted else "内容快照已存在，已记录本次观察。") +
                    ("岗位未匹配当前配置；不会纳入默认分析，请检查真实标题或调整岗位配置。" if not roles else "")}

    def import_file(self, data: dict) -> dict:
        from .ingest import iter_items
        suffix = Path(text_field(data, "filename", required=True, limit=200)).suffix.lower()
        if suffix not in {".json", ".jsonl", ".csv"}:
            raise InputError("工作台支持 UTF-8 JSON/JSONL/CSV；TXT/HTML 请使用粘贴表单或原有 CLI。")
        content = text_field(data, "content", required=True, limit=MAX_UPLOAD)
        if len(content.encode("utf-8")) > MAX_UPLOAD:
            raise InputError("导入文件应不超过 1 MB，请分批导入。")
        rights = text_field(data, "rights_note", required=True)
        if data.get("full_text_confirmed") is not True:
            raise InputError("请确认文件有权处理，且 full_text 行确实包含完整职位文本。")
        path = self.root / "imports" / (uuid.uuid4().hex + suffix)
        with path.open("x", encoding="utf-8", newline="") as stream:
            stream.write(content)
        jobs = []
        errors = []
        for index, (generated_ref, item) in enumerate(iter_items(path), 1):
            if index > 1000:
                errors.append("单批最多 1000 条，请拆分文件。")
                break
            if isinstance(item, Exception):
                errors.append(f"第 {index} 项: {type(item).__name__}; 请核对字段、编码和格式。")
                continue
            try:
                if item.is_synthetic:
                    raise InputError("合成样例不能导入真实数据库；请使用演示按钮。")
                # Transport filenames are random; source identity must not be.
                # Include the row index and content hash, never a temporary path.
                source_ref = item.source_ref
                if source_ref == generated_ref:
                    source_ref = "upload:" + hashlib.sha256(content.encode("utf-8")).hexdigest() + f":{index}"
                item = replace(item, rights_note=item.rights_note or rights,
                               source_ref=source_ref, record_id="")
                self._check_source(item)
                jobs.append(item)
            except ValueError as exc:
                errors.append(f"第 {index} 项: {exc}")
        if errors or not jobs:
            # Validation is all-or-nothing: never quietly import only the valid rows.
            path.unlink(missing_ok=True)
            raise InputError("未导入任何行。" + "；".join(errors[:10] or ["文件没有职位记录。"] ))
        with Store(self.db) as store:
            inserted = sum(int(store.add(job)) for job in jobs)
            store.event("workbench_import", "ok", {"file": path.name,
                        "observations": len(jobs), "new_snapshots": inserted})
        return {"observations": len(jobs), "new_snapshots": inserted,
                "message": "校验通过并已导入；原始文件保存在本机 imports 目录。"}

    def filters(self, data: dict) -> tuple[list[str], list[str]]:
        roles = data.get("roles", list(self.config["roles"]))
        platforms = data.get("platforms", ["boss", "liepin", "51job"])
        for name, values in (("roles", roles), ("platforms", platforms)):
            if not isinstance(values, list) or not values or not all(isinstance(x, str) for x in values):
                raise InputError(f"请至少选择一个 {name}。")
            if set(values) - set(self.config[name]):
                raise InputError(f"存在未知 {name}。")
        return list(dict.fromkeys(roles)), list(dict.fromkeys(platforms))

    def plan(self, data: dict) -> dict:
        from .discovery import build_plan
        roles, platforms = self.filters(data)
        tasks = build_plan(self.config, platforms, roles)
        return {"tasks": [asdict(task) for task in tasks], "query_count": len(tasks),
                "network_calls": 0, "complete_market_coverage": False}

    def discover(self, data: dict) -> dict:
        from .discovery import build_plan, discover
        from .network_policy import use_policy
        roles, platforms = self.filters(data)
        budget = data.get("max_requests", 3)
        if type(budget) is not int or not 1 <= budget <= 20:
            raise InputError("请求预算必须为 1～20 的整数。")
        if data.get("consent_search") is not True:
            raise InputError("请确认将检索词发送至 Brave，并按你自己的 API 套餐消耗请求额度。")
        key = text_field(data, "api_key", limit=1000).strip() or os.environ.get("BRAVE_SEARCH_API_KEY", "")
        if not key:
            raise InputError("缺少 Brave Search API Key；可先预览检索计划或手工导入，无需招聘平台密码。")
        with use_policy(self.network_policy()), Store(self.db) as store:
            report = discover(_SearchStore(store, key), self.config, api_key=key, plan=build_plan(self.config, platforms, roles),
                              max_requests=budget, pages=1, count=10)
        # Provider errors must never persist an accidentally echoed credential.
        for task in report["tasks"]:
            task["error"] = _redact(task["error"], key)
        report["rejected_results"] = _redact(report["rejected_results"], key)
        path = self.root / "discovery" / (uuid.uuid4().hex + ".json")
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        states = Counter(task["status"] for task in report["tasks"])
        return {"requests_made": report["requests_made"], "query_count": report["query_count"],
                "task_statuses": dict(states), "leads_stored": sum(t["leads_stored"] for t in report["tasks"]),
                "errors": [t["error"] for t in report["tasks"] if t["status"] == "error"][:10],
                "report_file": str(path), "complete_market_coverage": False,
                "message": "搜索结果仅为摘要线索；请在浏览器中核实并补充有权处理的完整 JD。"}

    def analyze(self, data: dict) -> dict:
        from .pipeline import analyze
        roles, _ = self.filters(data)
        dataset = data.get("dataset", "real")
        if dataset not in {"real", "demo"}:
            raise InputError("dataset 必须是 real 或 demo。")
        demo = dataset == "demo"
        if demo:
            with Store(self.demo_db) as store:
                store.add(JobRecord(title="时间序列算法工程师", company="合成演示公司（非真实招聘）",
                    text="负责时间序列预测。要求熟练使用 Cursor 进行 AI 辅助编程，编写单元测试并进行代码审查。",
                    platform="manual", source_mode="synthetic", is_synthetic=True,
                    source_ref="workbench:synthetic-demo", rights_note="项目内置合成演示，不是市场样本"))
        elif not self.db.is_file():
            raise InputError("还没有真实数据，请先粘贴 JD、导入文件或执行搜索发现。")
        run_id = uuid.uuid4().hex
        analyze(self.demo_db if demo else self.db, self.root / "reports" / run_id,
                           config=self.config, role_filter=roles, demo_mode=demo)
        return self.report(run_id)

    def report_file(self, run_id: str, name: str) -> Path:
        if not RUN_ID.fullmatch(run_id) or not name or Path(name).name != name or "\\" in name:
            raise InputError("无效报告路径。")
        directory = self.root / "reports" / run_id
        root = (self.root / "reports").resolve()
        path = (directory / name).resolve()
        if (directory.is_symlink() or not path.is_relative_to(root / run_id)
                or not (directory / "run_manifest.json").is_file() or not path.is_file()):
            raise InputError("报告不存在或路径不在允许目录内。")
        return path

    def report(self, run_id: str) -> dict:
        manifest_path = self.report_file(run_id, "run_manifest.json")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        stats = manifest["stats"]
        if stats["full_text_job_groups"] == 0:
            message = "没有纳入完整 JD：请补齐正文、核对岗位匹配和采集新鲜度。不能据此归纳市场要求。"
        elif stats["accepted_positive_requirement_rows"] == 0:
            message = "已有正文，但没有已接受的正向 Vibe Coding 要求；请查看原文、否定条款与复核队列。"
        else:
            message = "报告已生成。通用能力并集不等于个人完全满足；没有本人证据的描述仍是待填模板。"
        with self.report_file(run_id, "requirements.jsonl").open(encoding="utf-8") as stream:
            requirements = [json.loads(line) for line in islice(stream, 100) if line.strip()]
        return {"id": run_id, "manifest": manifest, "message": message, "requirements": requirements,
                "descriptions": self.report_file(run_id, "descriptions.md").read_text(encoding="utf-8"),
                "files": sorted(p.name for p in manifest_path.parent.iterdir() if p.is_file() and not p.is_symlink())}
