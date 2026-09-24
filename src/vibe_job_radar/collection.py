"""Budgeted, restartable acquisition. No login bypass and no claim of market completeness."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import tempfile
import uuid
from dataclasses import asdict
from contextlib import contextmanager
from functools import wraps
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .config import load_config, platform_for_url, validate
from .discovery import BRAVE_ENDPOINT, build_plan
from .html_parser import parse_job_html, plain_text
from .models import JobRecord
from .network import FetchError, SafeHTTP, SiteFetcher
from .network_policy import current_policy, use_policy
from .store import Store
from .utils import atomic_json, canonical_url, domain_matches, parse_time, utc_now
from .workspace import InputError, Workspace, text_field
from .url_safety import credential_query_key
from .public_category import MODE as CATEGORY_MODE

ID = re.compile(r"[a-f0-9]{32}")
TERMINAL = {"completed", "needs_attention", "empty"}


def integer(data, key, default, low, high):
    value = data.get(key, default)
    if type(value) is not int or not low <= value <= high:
        raise InputError(f"{key} 必须为 {low}～{high} 的整数。")
    return value


def safe_url(value):
    if not isinstance(value, str) or len(value) > 2048:
        raise InputError("URL 必须为不超过 2048 字符的文本。")
    value = canonical_url(value)
    p = urlsplit(value)
    if not value or p.scheme != "https" or p.port not in (None, 443):
        raise InputError("只支持不带登录凭据的 HTTPS 标准端口链接。")
    if any(credential_query_key(key) for key, _ in parse_qsl(p.query)):
        raise InputError("URL 包含疑似凭据参数，请使用不带凭据的链接。")
    return value


@contextmanager
def writer_lock(root):
    """OS-released advisory lock across UI/CLI processes; no stale PID guessing."""
    path = root / ".writer.lock"
    if path.is_symlink():
        raise InputError("任务锁不能使用符号链接。")
    with path.open("a+b") as stream:
        stream.seek(0, 2)
        if stream.tell() == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise InputError("另一个采集进程正在执行，请勿并发使用此工作区。") from exc
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def serialized(method):
    @wraps(method)
    def wrapped(self, *args, **kwargs):
        with writer_lock(self.root):
            return method(self, *args, **kwargs)
    return wrapped


class Collector:
    def __init__(self, workspace: Workspace):
        self.workspace = workspace
        self.root = workspace.root / "collections"
        self.root.mkdir(exist_ok=True, mode=0o700)
        if self.root.is_symlink():
            raise InputError("采集目录不能是符号链接。")
        self.platform_config = workspace.root / "platforms.override.json"
        if self.platform_config.is_symlink():
            raise InputError("平台配置不能使用符号链接。")
        if self.platform_config.exists():
            workspace.config = load_config(self.platform_config)
        self.clients = {}
        self._recover()

    def _client(self, key, factory, *, site=False):
        if key not in self.clients:
            self.clients[key] = factory()
        client = self.clients[key]
        # A new explicit step uses the current workspace preference, while the
        # same transport retains publisher pacing and prior host refusals.
        transport = client.transport if site else client
        policy = current_policy()
        transport.network_policy = policy
        transport.resolver = policy.resolver
        return client

    @serialized
    def _recover(self):
        # A saved in-flight request has an uncertain outcome after process restart.
        # Its reserved budget is not refunded and it is never retried automatically.
        for path in self.root.glob("*.json"):
            if ID.fullmatch(path.stem) and not path.is_symlink():
                state = json.loads(path.read_text(encoding="utf-8"))
                if state.get("in_flight"):
                    flight = state.pop("in_flight")
                    state[flight["queue"]][flight["index"]]["status"] = "interrupted_uncertain"
                    state["status"] = "paused"
                    if flight["queue"] == "feed_outcomes":
                        state["feed_done"] = True
                    state["warnings"].append("上次请求中断，结果未知；已计入预算，不自动重试。")
                    self._save(state)

    def _path(self, ident):
        if not isinstance(ident, str) or not ID.fullmatch(ident):
            raise InputError("采集任务 ID 无效。")
        path = self.root / (ident + ".json")
        if path.is_symlink():
            raise InputError("采集文件不能是符号链接。")
        return path

    def _load(self, ident):
        path = self._path(ident)
        if not path.is_file():
            raise InputError("采集任务不存在。")
        return json.loads(path.read_text(encoding="utf-8"))

    def _save(self, state):
        state["updated_at"] = utc_now()
        atomic_json(self._path(state["id"]), state)

    def _view(self, state):
        summary = {}
        for platform in state["platforms"]:
            tasks = [t for t in state["tasks"] if t["platform"] == platform]
            details = [d for d in state["details"] if d["platform"] == platform]
            summary[platform] = {"queries_attempted": sum(t["attempts"] for t in tasks),
                "detail_outcomes": {k: sum(d["status"] == k for d in details) for k in sorted({d["status"] for d in details})},
                "feed_records": sum(f.get("platforms", {}).get(platform, 0) for f in state["feed_outcomes"]),
                "platform_connector_certified": False}
        from .collection_recovery import explain
        return {**state, **explain(state), "platform_summary": summary,
                "note": "执行完成不等于全市场完整；公开抓取受许可/robots/页面结构限制，授权数据源由用户提供契约。"}

    def list(self, data=None):
        runs = []
        for path in self.root.glob("*.json"):
            if ID.fullmatch(path.stem) and not path.is_symlink():
                s = self._load(path.stem)
                runs.append({k: s[k] for k in ("id", "mode", "status", "updated_at", "search_requests", "detail_attempts", "feed_requests", "report_id")})
        return {"runs": sorted(runs, key=lambda s: s["updated_at"], reverse=True)[:100]}

    def status(self, data):
        return self._view(self._load(data.get("id")))

    @serialized
    def start(self, data):
        roles, platforms = self.workspace.filters({**data, "platforms": data.get("platforms", list(self.workspace.config["platforms"]))})
        mode = data.get("mode", "search")
        if mode not in {"search", "urls", "feed", CATEGORY_MODE}:
            raise InputError("未知采集模式。")
        rights = text_field(data, "rights_note", required=True)
        permits = data.get("permit_platforms", [])
        if not isinstance(permits, list) or not all(isinstance(x, str) and x in platforms for x in permits):
            raise InputError("正文访问许可必须是所选平台的子集。")
        if data.get("consent") is not True:
            raise InputError("请确认本次采集、保存范围及请求预算。")
        if mode == "search" and data.get("search_storage_rights") is not True:
            raise InputError("请确认搜索 API 套餐允许保存搜索结果。")
        if mode == CATEGORY_MODE:
            if set(roles) != {'architect'} or set(platforms) != {'liepin'}:
                raise InputError("公开分类目前仅支持猎聘架构师；请只选择该岗位和平台。")
            if 'liepin' not in permits:
                raise InputError("请先确认本次猎聘分类页和正文访问许可。")
        tasks = [dict(asdict(t), offset=0, attempts=0) for t in build_plan(self.workspace.config, platforms, roles)] if mode == "search" else []
        state = {"id": uuid.uuid4().hex, "schema_version": 1, "mode": mode, "status": "paused", "roles": roles,
            "platforms": platforms, "permit_platforms": permits, "rights_note": rights, "tasks": tasks, "details": [],
            "pages": integer(data, "pages", 1, 1, 10), "search_budget": integer(data, "search_budget", 24, 1, 300),
            "detail_budget": integer(data, "detail_budget", 20, 0, 300), "search_requests": 0,
            "detail_attempts": 0, "feed_requests": 0, "feed_budget": integer(data, "feed_budget", 5, 1, 20),
            "fresh_hours": integer(data, "fresh_hours", 24, 1, 720), "task_cursor": 0,
            "phase": "search" if mode == "search" else ("feed" if mode == "feed" else "detail"),
            "feed_done": False, "feed_cursor": "", "seen_cursors": [], "blocked_hosts": [], "warnings": [],
            "feed_outcomes": [], "report_id": "", "complete_market_coverage": False, "created_at": utc_now()}
        if mode == CATEGORY_MODE:
            from .public_category import MAX_DETAILS, new_outcome
            state.update(phase='category', category_attempts=0, category_outcomes=[new_outcome()],
                         detail_budget=integer(data, 'detail_budget', MAX_DETAILS, 1, MAX_DETAILS))
        if mode == "urls":
            from .public_job_links import prepare_public_job_link, public_detail_parser
            urls = text_field(data, "urls", required=True, limit=100000).splitlines()
            if len(urls) > 300:
                raise InputError("每批最多 300 个链接。")
            for url in urls:
                if url.strip():
                    prepared, normalization = prepare_public_job_link(url.strip())
                    self._enqueue(state, prepared)
                    row = next((r for r in state['details'] if r['url'] == prepared), None)
                    if row is not None:
                        parser = public_detail_parser(prepared)
                        if parser:
                            row['detail_parser'] = parser
                        if normalization:
                            # Keep all removed field names regardless of paste
                            # order, without another request. Parser selection
                            # applies equally to a clean link entered alone.
                            prior = row.get('link_normalization', {}).get('removed_parameters', [])
                            row['link_normalization'] = {**normalization,
                                'removed_parameters': sorted(set(prior) | set(normalization['removed_parameters']))}
            if not state["details"]:
                raise InputError("没有与所选平台匹配的职位链接。")
        if mode == "feed":
            state["endpoint"] = safe_url(text_field(data, "endpoint", required=True))
            state["contract_ref"] = safe_url(text_field(data, "contract_ref", required=True))
            if "cursor" in dict(parse_qsl(urlsplit(state["endpoint"]).query)):
                raise InputError("数据源 URL 不能预填 cursor；游标由流水线管理。")
        self._save(state)
        return self._view(state)

    @serialized
    def register(self, data):
        key = text_field(data, "key", required=True, limit=32)
        label = text_field(data, "label", required=True, limit=100)
        domains = [d.strip().lower() for d in text_field(data, "domains", required=True, limit=2000).split(",") if d.strip()]
        if not re.fullmatch(r"[a-z][a-z0-9_]{1,31}", key) or key in self.workspace.config["platforms"]:
            raise InputError("平台标识必须是新的小写英文/数字/下划线名称，不能覆盖已有平台。")
        if not domains or len(domains) > 10:
            raise InputError("请提供 1～10 个域名，用英文逗号分隔。")
        existing = [d for p in self.workspace.config["platforms"].values() for d in p["domains"]]
        if any(domain_matches(a, b) or domain_matches(b, a) for a in domains for b in existing):
            raise InputError("域名与已有平台重叠，不能改变已有来源归属。")
        config = json.loads(json.dumps(self.workspace.config))
        config["platforms"][key] = {"label": label, "domains": list(dict.fromkeys(domains))}
        validate(config)
        atomic_json(self.platform_config, {"platforms": config["platforms"]})
        self.workspace.config = config
        return {"key": key, "label": label, "domains": domains,
                "message": "平台已配置；这不代表取得访问授权或通过实站认证。"}

    def _enqueue(self, state, url):
        platform = platform_for_url(url, self.workspace.config)
        if platform not in state["platforms"] or urlsplit(url).path in {"", "/"}:
            return False
        if any(d["url"] == url for d in state["details"]):
            return False
        if len(state["details"]) >= 3000:
            return False
        state["details"].append({"url": url, "platform": platform, "status": "pending", "record_id": ""})
        return True

    def _search(self, state, key):
        if not key:
            raise InputError("搜索阶段需要 Brave Key；暂停不消耗请求。")
        tasks = state["tasks"]
        for _ in tasks:
            index = state["task_cursor"] % len(tasks)
            state["task_cursor"] += 1
            task = tasks[index]
            if task["status"] in {"planned", "next_page"}:
                break
        else:
            state["phase"] = "detail"
            return
        if state["search_requests"] >= state["search_budget"]:
            for task in tasks:
                if task["status"] in {"planned", "next_page"}:
                    task["status"] = "budget_skipped"
            state["phase"] = "detail"
            return
        state["search_requests"] += 1
        task["attempts"] += 1
        state["in_flight"] = {"queue": "tasks", "index": index}
        self._save(state)
        client = self._client("search:" + state["id"], lambda: SafeHTTP({"api.search.brave.com"}, interval=1.1))
        try:
            payload = client.json(BRAVE_ENDPOINT + "?" + urlencode({"q": task["query"], "count": 20,
                "offset": task["offset"], "search_lang": "zh-hans", "country": "cn"}), headers={"X-Subscription-Token": key})
            results = (payload.get("web") or {}).get("results") or []
            if not isinstance(results, list) or len(results) > 20:
                raise ValueError("invalid results")
            with Store(self.workspace.db) as store:
                for item in results:
                    try:
                        if key in json.dumps(item, ensure_ascii=False):
                            raise ValueError("credential echo")
                        url = safe_url(item["url"])
                        if platform_for_url(url, self.workspace.config) != task["platform"] or urlsplit(url).path in {"", "/"}:
                            raise ValueError("domain mismatch")
                        record = JobRecord(title=plain_text(item.get("title") or ""), text=plain_text(item.get("description") or ""),
                            url=url, platform=task["platform"], source_mode="search_api", evidence_level="snippet",
                            rights_note=state["rights_note"], source_ref=f'collection:{state["id"]}:{task["query_id"]}:{task["offset"]}')
                        store.add(record)
                        task["leads_stored"] += 1
                        self._enqueue(state, url)
                    except (ValueError, KeyError, TypeError):
                        task["results_rejected"] = task.get("results_rejected", 0) + 1
            task["offset"] += 1
            task["status"] = "exhausted" if not results or (payload.get("query") or {}).get("more_results_available") is False else (
                "page_limit" if task["offset"] >= state["pages"] else "next_page")
        except (FetchError, ValueError, TypeError, AttributeError) as exc:
            task["status"] = "error"
            task["error"] = exc.code if isinstance(exc, FetchError) else "invalid_provider_response"
            if task["error"] in {"http_401", "http_403", "http_429", "host_circuit_open"}:
                for t in tasks:
                    if t["status"] in {"planned", "next_page"}:
                        t["status"] = "provider_stopped"
                state["phase"] = "detail"
        finally:
            state.pop("in_flight", None)

    def _detail(self, state):
        row = next((d for d in state["details"] if d["status"] == "pending"), None)
        if row is None:
            state["phase"] = "report"
            return
        if row["platform"] not in state["permit_platforms"]:
            row["status"] = "permission_required"
            return
        host = urlsplit(row["url"]).hostname
        if host in state["blocked_hosts"]:
            row["status"] = "host_stopped"
            return
        now = parse_time(utc_now())
        from .public_job_links import public_detail_cache_matches
        with Store(self.workspace.db) as store:
            prior = next((r for r in store.records() if r.url == row["url"] and r.evidence_level == "full_text" and not r.is_synthetic
                and public_detail_cache_matches(row, r)
                and (not row.get('category_title') or ' '.join(r.title.split()) == row['category_title'])
                and 0 <= (now - parse_time(r.collected_at)).total_seconds() <= state["fresh_hours"] * 3600
                and (not r.expires_at or parse_time(r.expires_at) > now)), None)
        if prior:
            row.update(status="fresh_reused", record_id=prior.record_id)
            return
        if state["detail_attempts"] >= state["detail_budget"]:
            row["status"] = "budget_skipped"
            return
        state["detail_attempts"] += 1
        state["in_flight"] = {"queue": "details", "index": state["details"].index(row)}
        self._save(state)
        domains = set(self.workspace.config["platforms"][row["platform"]]["domains"])
        client = self._client((state["id"], row["platform"]), lambda: SiteFetcher(domains), site=True)
        try:
            response = client.fetch(row["url"])
            markup = response.text()
            final_url = safe_url(response.url or row["url"])
            if row.get('detail_parser') or row.get('link_normalization'):
                from .public_job_links import parse_prepared_job
                parsed = parse_prepared_job(row, final_url, markup)
            else:
                parsed = parse_job_html(markup, source_url=final_url)
            if row.get('category_title') and ' '.join(parsed['title'].split()) != row['category_title']:
                raise FetchError('category_job_title_changed')
            record = JobRecord(**parsed, url=final_url, platform=row["platform"], source_mode="public_fetch",
                rights_note=state["rights_note"], source_ref=f'collection:{state["id"]}', raw_sha256=hashlib.sha256(markup.encode()).hexdigest())
            with Store(self.workspace.db) as store:
                store.add(record)
            row.update(status="ok", record_id=record.record_id, final_url=record.url)
        except (FetchError, ValueError, TypeError) as exc:
            row["status"] = exc.code if isinstance(exc, FetchError) else "parse_error"
            if row["status"] in {"http_401", "http_403", "http_429", "login_or_challenge", "host_circuit_open", "redirect_login_required", "redirect_verification_required", "manual_required"}:
                state["blocked_hosts"].append(host)
        finally:
            diagnostic = getattr(client, "last_diagnostic", None)
            if isinstance(diagnostic, dict):
                row["fetch_diagnostic"] = diagnostic
            state.pop("in_flight", None)

    def _category(self, state):
        from .public_category import URL, parse_category
        from .public_job_links import public_detail_parser
        row = state['category_outcomes'][0]
        if row['status'] != 'pending':
            state['phase'] = 'detail'
            return
        state['category_attempts'] += 1
        row['status'] = 'requesting'
        row['capture_started_at'] = utc_now()
        state['in_flight'] = {'queue': 'category_outcomes', 'index': 0}
        self._save(state)
        client = self._client((state['id'], 'liepin'), lambda: SiteFetcher({'liepin.com'}), site=True)
        try:
            response = client.fetch(URL)
            candidates = parse_category(response.url or URL, response.text())
            selected = [c for c in candidates if c['status'] != 'duplicate'][:state['detail_budget']]
            row.update(status='ok', candidates=candidates, card_count=len(candidates),
                       selected_positions=[c['position'] for c in selected],
                       raw_sha256=hashlib.sha256(response.body).hexdigest(),
                       final_url=URL, selection_rule='first_unique_cards_in_publisher_order')
            for candidate in selected:
                detail = dict(url=candidate['url'], platform='liepin', record_id='',
                              category_position=candidate['position'], category_title=candidate['title'],
                              status='pending' if candidate['status'] == 'available' else candidate['status'])
                if detail['status'] == 'pending':
                    detail['detail_parser'] = public_detail_parser(detail['url'])
                state['details'].append(detail)
        except (FetchError, ValueError, TypeError) as exc:
            row['status'] = exc.code if isinstance(exc, FetchError) else 'category_structure_changed'
        finally:
            diagnostic = getattr(client, 'last_diagnostic', None)
            if isinstance(diagnostic, dict):
                row['fetch_diagnostic'] = diagnostic
            state.pop('in_flight', None)
            row['capture_finished_at'] = utc_now()
            state['phase'] = 'detail'

    def category_next_preview(self, data):
        from .public_category_next import preview
        return preview(self, data)

    @serialized
    def category_next_start(self, data):
        from .public_category_next import start
        return start(self, data)

    def _feed(self, state, key):
        # Explicit publisher contract: GET endpoint?cursor=opaque -> {jobs:[JobRecord], next_cursor:str|null}.
        # This is not an undocumented BOSS/Liepin API and not labeled as one.
        if state["feed_done"] or state["feed_requests"] >= state["feed_budget"]:
            if not state["feed_done"]:
                state["warnings"].append("授权数据源页数预算用尽，剩余数据未获取。")
            state["phase"] = "report"
            return
        index = len(state["feed_outcomes"])
        row = {"status": "requesting"}
        state["feed_outcomes"].append(row)
        state["in_flight"] = {"queue": "feed_outcomes", "index": index}
        state["feed_requests"] += 1
        self._save(state)
        p = urlsplit(state["endpoint"])
        params = parse_qsl(p.query) + ([("cursor", state["feed_cursor"])] if state["feed_cursor"] else [])
        url = urlunsplit((p.scheme, p.netloc, p.path, urlencode(params), ""))
        client = self._client("feed:" + state["id"], lambda: SafeHTTP({p.hostname}, interval=1.1))
        try:
            payload = client.json(url, headers={"Authorization": "Bearer " + key} if key else {})
            rows = payload.get("jobs")
            cursor = payload.get("next_cursor") or ""
            if not isinstance(rows, list) or len(rows) > 100 or not isinstance(cursor, str) or len(cursor) > 1000:
                raise ValueError("invalid feed contract")
            # Never persist a cursor that echoes a credential.
            if key and key in cursor:
                raise ValueError("credential echo")
            if cursor and cursor in state["seen_cursors"]:
                raise ValueError("repeated cursor")
            records = []
            for raw in rows:
                if not isinstance(raw, dict) or (key and key in json.dumps(raw, ensure_ascii=False)):
                    raise ValueError("invalid record")
                value = dict(raw)
                value.setdefault("evidence_level", "snippet")
                value.update(source_mode="authorized_feed", is_synthetic=False, collected_at=utc_now(),
                    source_ref="feed:" + state["endpoint"], rights_note=state["rights_note"], record_id="")
                if raw.get("is_synthetic") or raw.get("source_mode") == "synthetic" or not raw.get("url"):
                    raise ValueError("synthetic or missing URL")
                value["url"] = safe_url(value["url"])
                value["platform"] = platform_for_url(value["url"], self.workspace.config)
                if value["platform"] not in state["platforms"]:
                    raise ValueError("platform not selected")
                records.append(JobRecord.from_dict(value))
            with Store(self.workspace.db) as store:
                for record in records:
                    store.add(record)
            row.update(status="ok", records=len(records), full_text=sum(r.evidence_level == "full_text" for r in records),
                       platforms={p: sum(r.platform == p for r in records) for p in state["platforms"]})
            state["feed_done"] = not cursor
            state["seen_cursors"].append(cursor)
            state["feed_cursor"] = cursor
        except (FetchError, ValueError, TypeError, KeyError) as exc:
            row["status"] = exc.code if isinstance(exc, FetchError) else "invalid_feed_contract"
            state["feed_done"] = True
        finally:
            state.pop("in_flight", None)

    @serialized
    def step(self, data):
        state = self._load(data.get("id"))
        if state["status"] in TERMINAL:
            return self._view(state)
        if state.get("in_flight"):
            raise InputError("此任务有未结束的请求，请勿并发执行。")
        key = text_field(data, "api_key", limit=1000).strip()
        if state["mode"] == "search":
            key = key or os.environ.get("BRAVE_SEARCH_API_KEY", "")
        if state["phase"] in {"search", "detail", "feed", "category"}:
            # Read/validate consent before reserving any attempt or saving an
            # in-flight marker. This also covers the CLI, outside HTTP handlers.
            policy = self.workspace.network_policy()
            with use_policy(policy):
                state["status"] = "running"
                if state["phase"] == "search":
                    self._search(state, key)
                elif state["phase"] == "detail":
                    self._detail(state)
                elif state["phase"] == "category":
                    self._category(state)
                else:
                    self._feed(state, key)
        else:
            if self.workspace.db.is_file():
                from .pipeline import analyze
                ident = uuid.uuid4().hex
                if state["mode"] in {"urls", CATEGORY_MODE}:
                    ids = {d["record_id"] for d in state["details"] if d["status"] in {"ok", "fresh_reused"}}
                    # Empty Store creation must not generate a misleading report;
                    # unrelated historical jobs are not results of this URL batch.
                    if ids:
                        with Store(self.workspace.db) as store:
                            records = [j for j in store.records(latest_only=False) if j.record_id in ids]
                        with tempfile.TemporaryDirectory(prefix=".url-batch-", dir=self.root) as tmp:
                            db = Path(tmp)/"batch.sqlite"
                            with Store(db) as batch:
                                for job in records:
                                    batch.add(job)
                            analyze(db, self.workspace.root / "reports" / ident, config=self.workspace.config,
                                    role_filter=state["roles"], platform_filter=state["platforms"])
                        state["report_id"] = ident
                else:
                    analyze(self.workspace.db, self.workspace.root / "reports" / ident, config=self.workspace.config,
                            role_filter=state["roles"], platform_filter=state["platforms"])
                    state["report_id"] = ident
            problems = any(d["status"] not in {"ok", "fresh_reused"} for d in state["details"]) or any(
                t["status"] in {"error", "provider_stopped", "budget_skipped", "interrupted_uncertain"} for t in state["tasks"]) or any(
                f["status"] != "ok" for f in state["feed_outcomes"]) or any(
                c['status'] != 'ok' for c in state.get('category_outcomes', [])) or bool(state["warnings"])
            obtained = any(d["status"] in {"ok", "fresh_reused"} for d in state["details"]) or any(f.get("records", 0) for f in state["feed_outcomes"])
            state["status"] = "needs_attention" if problems else ("completed" if obtained else "empty")
            if state["report_id"]:
                directory = self.workspace.root / "reports" / state["report_id"]
                atomic_json(directory / "collection_manifest.json", self._view(state))
                manifest = json.loads((directory / "run_manifest.json").read_text(encoding="utf-8"))
                manifest["output_files_sha256"]["collection_manifest.json"] = hashlib.sha256((directory / "collection_manifest.json").read_bytes()).hexdigest()
                atomic_json(directory / "run_manifest.json", manifest)
        self._save(state)
        return self._view(state)


def main(argv=None):
    p = argparse.ArgumentParser(description="继续既有采集任务；配置和新建任务请使用工作台。")
    p.add_argument("--workspace", type=Path, default=Path.home() / ".vibe-job-radar")
    p.add_argument("--run-id", required=True)
    args = p.parse_args(argv)
    service = Collector(Workspace(args.workspace))
    mode = service.status({"id": args.run_id})["mode"]
    key_name = {"search": "BRAVE_SEARCH_API_KEY", "feed": "RADAR_FEED_TOKEN"}.get(mode)
    key = os.environ.get(key_name, "") if key_name else ""
    while True:
        state = service.step({"id": args.run_id, "api_key": key})
        print(json.dumps({k: state[k] for k in ("id", "status", "phase", "search_requests", "detail_attempts", "feed_requests")}), flush=True)
        if state["status"] in TERMINAL:
            return 0 if state["status"] == "completed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
