from __future__ import annotations

import csv
import json
from pathlib import Path
from .html_parser import parse_job_html
from .models import JobRecord
from .utils import digest, load_json

ALIASES = {"岗位名称": "title", "职位名称": "title", "岗位": "title", "职位描述": "text", "岗位描述": "text",
           "正文": "text", "JD": "text", "公司": "company", "公司名称": "company", "平台": "platform",
           "链接": "url", "来源链接": "url", "城市": "location", "采集时间": "collected_at"}
SUPPORTED = {".json", ".jsonl", ".csv", ".txt", ".md", ".html", ".htm"}


def boolean(value):
    if type(value) is bool:
        return value
    if isinstance(value, str) and value.lower().strip() in {"true", "1", "false", "0", ""}:
        return value.lower().strip() in {"true", "1"}
    raise ValueError("boolean must be true/false or 1/0")


def coerce(data: dict, source_ref: str, *, csv_mode: bool = False) -> JobRecord:
    if not isinstance(data, dict):
        raise ValueError("each record must be an object")
    fields = {ALIASES.get(k, k): v for k, v in data.items()}
    if csv_mode:
        fields = {k: v for k, v in fields.items() if k in JobRecord.__dataclass_fields__ and v != ""}
    fields.setdefault("source_ref", source_ref)
    if "is_synthetic" in fields:
        fields["is_synthetic"] = boolean(fields["is_synthetic"])
    return JobRecord.from_dict(fields)


def iter_items(path: str | Path):
    """Yield (source reference, raw record OR Exception); row errors do not hide valid rows."""
    path = Path(path)
    if '.radar-sessions' in path.parts:
        yield str(path), ValueError('private browser state is not a job input')
        return
    paths = sorted(p for p in path.rglob("*") if p.is_file() and p.suffix.lower() in SUPPORTED
                   and not p.name.endswith(".meta.json") and ".radar-sessions" not in p.parts) if path.is_dir() else [path]
    if not paths:
        yield str(path), ValueError("no supported input files")
    for file in paths:
        try:
            if file.stat().st_size > 50_000_000:
                raise ValueError("input file exceeds 50 MB; split into smaller imports")
            ext = file.suffix.lower()
            if ext == ".jsonl":
                with file.open(encoding="utf-8-sig") as f:
                    for i, line in enumerate(f, 1):
                        if not line.strip():
                            continue
                        ref = f"{file}:{i}"
                        try:
                            yield ref, coerce(json.loads(line), ref)
                        except (ValueError, TypeError) as exc:
                            yield ref, exc
            elif ext == ".csv":
                with file.open(encoding="utf-8-sig", newline="") as f:
                    for i, row in enumerate(csv.DictReader(f), 2):
                        ref = f"{file}:{i}"
                        try:
                            yield ref, coerce(row, ref, csv_mode=True)
                        except (ValueError, TypeError) as exc:
                            yield ref, exc
            elif ext == ".json":
                raw = load_json(file)
                rows = raw.get("jobs", [raw]) if isinstance(raw, dict) else raw
                if not isinstance(rows, list):
                    raise ValueError("JSON input must be one object, an array, or {jobs: [...]}")
                for i, row in enumerate(rows, 1):
                    ref = f"{file}:{i}"
                    try:
                        yield ref, coerce(row, ref)
                    except (ValueError, TypeError) as exc:
                        yield ref, exc
            elif ext in {".txt", ".md", ".html", ".htm"}:
                text = file.read_text(encoding="utf-8-sig")
                sidecar = Path(str(file) + ".meta.json")
                meta = load_json(sidecar) if sidecar.exists() else {}
                if not isinstance(meta, dict):
                    raise ValueError("input metadata must be a JSON object")
                if not isinstance(meta.get("url", ""), str):
                    raise ValueError("input metadata url must be a string")
                if ext in {".html", ".htm"}:
                    parsed = parse_job_html(text, source_url=meta.get("url", ""))
                    # The HTML is the source of text. Metadata cannot overwrite extracted text.
                    parsed.update({k: v for k, v in meta.items() if k != "text"})
                else:
                    parsed = {"title": file.stem, **meta, "text": text}
                parsed["raw_sha256"] = digest(text)
                yield str(file), coerce(parsed, str(file))
            else:
                raise ValueError("unsupported input extension; use JSONL/JSON/CSV/TXT/MD/HTML")
        except (OSError, ValueError, TypeError, UnicodeError) as exc:
            yield str(file), exc
