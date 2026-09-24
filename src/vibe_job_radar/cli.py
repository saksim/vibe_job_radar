from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from dataclasses import asdict
from pathlib import Path
from ._version import __version__
from .config import load_config, platform_for_url
from .discovery import build_plan, discover
from .html_parser import parse_job_html
from .ingest import iter_items
from .llm import OpenAIExtractor
from .models import JobRecord
from .network import FetchError, SiteFetcher
from .pipeline import analyze
from .store import Store
from .utils import atomic_json, digest, json_text, write_csv


def comma_list(value):
    return [part.strip() for part in value.split(",") if part.strip()]


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="vibe-radar", description="Evidence-first recruiting requirements → measurable profile templates")
    p.add_argument("--version", action="version", version=f"vibe-job-radar {__version__}")
    sub = p.add_subparsers(dest="command", required=True)
    def base(name, help):
        s = sub.add_parser(name, help=help)
        s.add_argument("--config", type=Path)
        return s
    s = base("plan", "生成搜索任务清单；不联网、不扣API调用")
    s.add_argument("--platforms", type=comma_list)
    s.add_argument("--roles", type=comma_list)
    s.add_argument("--out", type=Path, required=True)
    s = base("discover", "通过Brave官方搜索API发现职位线索；摘要不是正文")
    s.add_argument("--db", type=Path, required=True)
    s.add_argument("--platforms", type=comma_list)
    s.add_argument("--roles", type=comma_list)
    s.add_argument("--out", type=Path, required=True)
    s.add_argument("--max-requests", type=int, default=20)
    s.add_argument("--pages", type=int, default=1)
    s.add_argument("--count", type=int, default=20)
    s = base("ingest", "导入已获授权的JSONL/JSON/CSV/TXT/MD/HTML；可传目录")
    s.add_argument("input", type=Path)
    s.add_argument("--db", type=Path, required=True)
    s.add_argument("--error-report", type=Path)
    s = base("fetch", "仅抓取明确获准域名的公开职位正文；严格robots门控")
    s.add_argument("--db", type=Path, required=True)
    s.add_argument("--urls", type=Path, help="每行一个URL的TXT，或含url列的CSV；省略时读取数据库摘要线索")
    s.add_argument("--permit-domain", action="append", required=True, help="明确准许的域名；可重复")
    s.add_argument("--rights-note", required=True, help="人工确认的访问授权/允许范围依据；robots不是授权本身")
    s.add_argument("--limit", type=int, default=20)
    s.add_argument("--out", type=Path, required=True)
    s = base("analyze", "正文证据提取、归纳、描述与可观测报告")
    s.add_argument("--db", type=Path, required=True)
    s.add_argument("--out", type=Path, required=True)
    s.add_argument("--roles", type=comma_list)
    s.add_argument("--platforms", type=comma_list)
    s.add_argument("--candidate", type=Path)
    s.add_argument("--reviews", type=Path)
    s.add_argument("--demo-mode", action="store_true")
    s.add_argument("--max-age-days", type=int, default=90)
    s.add_argument("--as-of", help="带时区ISO时间；用于可复现验收")
    s.add_argument("--llm-model", help="可选OpenAI结构化抽取模型；用户选择可用模型，不默认调用")
    s.add_argument("--consent-send-jd", action="store_true")
    s = sub.add_parser('public-worker',help='独立执行工作区已确认的公开查询计划和待办；无网页服务器/浏览器')
    s.add_argument('--workspace',type=Path,required=True)
    return p


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == 'public-worker':
            from .public_worker import run_cli
            return run_cli(args.workspace)
        conf = load_config(args.config)
        if args.command == "plan":
            tasks = build_plan(conf, args.platforms, args.roles)
            atomic_json(args.out, {"tasks": [asdict(t) for t in tasks], "query_count": len(tasks),
                                   "network_calls": 0, "complete_market_coverage": False})
            print(json_text({"query_count": len(tasks), "out": str(args.out), "network_calls": 0}))
        elif args.command == "ingest":
            inserted = observed = 0
            errors = []
            with Store(args.db) as store:
                for ref, item in iter_items(args.input):
                    if isinstance(item, Exception):
                        detail = {"source_ref": ref, "error_type": type(item).__name__, "error": str(item)}
                        errors.append(detail)
                        store.event("ingest", "error", detail)
                    else:
                        inserted += int(store.add(item))
                        observed += 1
                store.event("ingest_summary", "partial" if errors else "ok", {"inserted": inserted, "observed": observed, "errors": len(errors)})
            error_path = args.error_report or args.db.with_suffix(".ingest_errors.json")
            atomic_json(error_path, errors)
            print(json_text({"new_snapshots": inserted, "observations": observed, "errors": len(errors), "error_report": str(error_path)}))
            return 2 if errors else 0
        elif args.command == "discover":
            with Store(args.db) as store:
                report = discover(store, conf, api_key=os.environ.get("BRAVE_SEARCH_API_KEY", ""),
                                  plan=build_plan(conf, args.platforms, args.roles), max_requests=args.max_requests,
                                  pages=args.pages, count=args.count)
            atomic_json(args.out, report)
            print(json_text({"requests_made": report["requests_made"], "query_count": report["query_count"], "out": str(args.out)}))
            return 2 if any(t["status"] in {"error", "provider_stopped"} for t in report["tasks"]) else 0
        elif args.command == "fetch":
            if args.limit < 1 or not args.rights_note.strip():
                raise ValueError("positive limit and nonempty rights-note required")
            fetcher = SiteFetcher(set(args.permit_domain))
            outcomes = []
            with Store(args.db) as store:
                if args.urls:
                    if args.urls.suffix.lower() == ".csv":
                        with args.urls.open(encoding="utf-8-sig", newline="") as f:
                            urls = [r["url"] for r in csv.DictReader(f)]
                    else:
                        urls = [u.strip() for u in args.urls.read_text(encoding="utf-8-sig").splitlines() if u.strip() and not u.startswith("#")]
                else:
                    urls = [r.url for r in store.records() if r.evidence_level == "snippet" and r.url and not r.is_synthetic]
                for index, url in enumerate(dict.fromkeys(urls)):
                    if index >= args.limit:
                        outcomes.append({"url": url, "status": "budget_skipped", "error": "fetch limit"})
                        continue
                    try:
                        response = fetcher.fetch(url)
                        markup = response.text()
                        item = parse_job_html(markup, source_url=url)
                        record = JobRecord(**item, url=url, platform=platform_for_url(url, conf),
                                           source_mode="public_fetch", rights_note=args.rights_note,
                                           source_ref=url, raw_sha256=digest(markup))
                        store.add(record)
                        result = {"url": url, "status": "ok", "record_id": record.record_id, "parser": record.parser}
                    except (FetchError, ValueError, TypeError) as exc:
                        result = {"url": url, "status": "blocked_or_error", "error": str(exc)}
                    outcomes.append(result)
                    store.event("fetch", result["status"], result)
            atomic_json(args.out, {"outcomes": outcomes, "complete_market_coverage": False})
            print(json_text({"out": str(args.out), "ok": sum(x["status"] == "ok" for x in outcomes), "total": len(outcomes)}))
            return 2 if any(x["status"] == "blocked_or_error" for x in outcomes) else 0
        elif args.command == "analyze":
            llm = OpenAIExtractor(conf, api_key=os.environ.get("OPENAI_API_KEY", ""), model=args.llm_model,
                                  consent_send_jd=args.consent_send_jd) if args.llm_model else None
            report = analyze(args.db, args.out, config=conf, role_filter=args.roles, platform_filter=args.platforms,
                             candidate_path=args.candidate, reviews_path=args.reviews, demo_mode=args.demo_mode,
                             max_age_days=args.max_age_days, as_of=args.as_of, llm=llm)
            print(json_text({"status": report["status"], "mode": report["mode"], "stats": report["stats"], "out": str(args.out)}))
            return 2 if report["status"] == "incomplete" else 0
        return 0
    except (OSError, ValueError, TypeError, FetchError, json.JSONDecodeError) as exc:
        print(f"ERROR [{type(exc).__name__}]: {exc}", file=sys.stderr)
        return 2
