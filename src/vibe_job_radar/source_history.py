"""Bounded local source observations; viewing history never fetches a source.

Only actual catalog attempts are recorded. Cache reads and paging are not new
observations. This metadata is separate from rollback-compatible catalog files.
"""
from contextlib import closing
import copy
import hashlib
import json
import math
import re
import sqlite3

from .public_boards import BOARDS
from .workspace import InputError

RETAINED = 128
MAX_BYTES = 8 * 1024 * 1024
KINDS = {"snapshot", "not_modified", "fetch_error", "structure_error"}
FIELDS = {"source", "adapter", "at", "kind", "code", "revision", "observed_at",
          "total", "change_status", "counts", "shape", "shape_changed"}
COUNT_FIELDS = {"previous_total", "current_total", "added", "updated", "missing", "unchanged"}


def _number(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def _digest(value, *, empty=False):
    return isinstance(value, str) and ((empty and value == "") or re.fullmatch(r"[a-f0-9]{64}", value))


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate field")
        result[key] = value
    return result


def _validate(value):
    if (not isinstance(value, dict) or set(value) != FIELDS
            or value["source"] not in BOARDS or value["kind"] not in KINDS
            or not isinstance(value["adapter"], str) or not re.fullmatch(r"[a-zA-Z0-9_.-]{1,100}", value["adapter"])
            or not _number(value["at"]) or type(value["shape_changed"]) is not bool
            or not _digest(value["revision"], empty=True) or not _digest(value["shape"], empty=True)
            or not isinstance(value["code"], str) or not re.fullmatch(r"[a-z0-9_]{0,80}", value["code"])):
        raise ValueError("invalid source observation")
    success = value["kind"] in {"snapshot", "not_modified"}
    if success:
        if (not value["revision"] or not _number(value["observed_at"])
                or value["observed_at"] > value["at"] or type(value["total"]) is not int
                or not 0 <= value["total"] <= 10000 or value["code"]):
            raise ValueError("invalid successful observation")
    elif (value["observed_at"] is not None or value["total"] is not None
          or value["revision"] or not value["code"]):
        raise ValueError("invalid failure observation")
    counts = value["counts"]
    if value["kind"] == "snapshot":
        if (value["change_status"] not in {"baseline", "compared"}
                or not isinstance(counts, dict) or set(counts) != COUNT_FIELDS):
            raise ValueError("invalid observation changes")
        for key, number in counts.items():
            if key == "previous_total" and value["change_status"] == "baseline" and number is None:
                continue
            if type(number) is not int or not 0 <= number <= 10000:
                raise ValueError("invalid observation count")
        if counts["current_total"] != value["total"]:
            raise ValueError("observation count mismatch")
        if value["change_status"] == "compared":
            if (counts["added"] + counts["updated"] + counts["unchanged"] != value["total"]
                    or counts["missing"] + counts["updated"] + counts["unchanged"] != counts["previous_total"]):
                raise ValueError("observation count mismatch")
        elif counts["previous_total"] is not None or any(counts[key] for key in ("added", "updated", "missing", "unchanged")):
            raise ValueError("baseline is not additions")
    elif counts is not None or value["change_status"] or value["shape_changed"]:
        raise ValueError("non-snapshot changes")
    return value


def shape_fingerprint(payload):
    """Field/type sketch of top level and up to 256 rows, not field values."""
    if not isinstance(payload, dict):
        return ""
    def fields(row):
        return sorted((str(key), type(value).__name__) for key, value in row.items())[:128]
    rows = payload.get("jobs", [])
    shapes = {json.dumps(fields(row), ensure_ascii=True) for row in rows[:256] if isinstance(row, dict)} if isinstance(rows, list) else set()
    value = [fields(payload), sorted(shapes)]
    return hashlib.sha256(json.dumps(value, ensure_ascii=True).encode("utf-8")).hexdigest()


class SourceHistory:
    def __init__(self, root):
        self.root = root
        self.path = root / "observations-v1.sqlite"

    def _safe(self):
        if self.root.is_symlink() or any((self.root / name).is_symlink() for name in
                ("observations-v1.sqlite", "observations-v1.sqlite-journal",
                 "observations-v1.sqlite-wal", "observations-v1.sqlite-shm")):
            raise InputError("来源历史不能使用符号链接。")
        if self.path.exists() and (not self.path.is_file() or self.path.stat().st_size > MAX_BYTES):
            raise InputError("来源历史大小或类型异常；原目录和报告保持不变。")

    @staticmethod
    def _schema(connection, create=False):
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if create and not tables and version == 0:
            connection.execute("CREATE TABLE observations(seq INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT NOT NULL, payload TEXT NOT NULL)")
            connection.execute("CREATE TABLE totals(source TEXT PRIMARY KEY, count INTEGER NOT NULL)")
            connection.execute("PRAGMA user_version=1")
        elif version != 1 or tables != {"observations", "totals", "sqlite_sequence"}:
            raise ValueError("unsupported source history")
        groups = connection.execute("SELECT source,COUNT(*) FROM observations GROUP BY source").fetchall()
        if any(source not in BOARDS or count > RETAINED for source, count in groups):
            raise ValueError("unbounded source history")

    def remember(self, board, kind, at, *, snapshot=None, code="", payload=None):
        self._safe()
        self.root.mkdir(exist_ok=True, mode=0o700)
        existed = self.path.exists()
        success = kind in {"snapshot", "not_modified"}
        change = snapshot.get("catalog_change") if kind == "snapshot" and snapshot else None
        value = dict(source=board.source.key, adapter=board.adapter_version, at=at, kind=kind,
            code=code if re.fullmatch(r"[a-z0-9_]{1,80}", code or "") else "" if success else "public_response_invalid",
            revision=snapshot["revision"] if success else "",
            observed_at=snapshot["observed_at"] if success else None,
            total=len(snapshot["jobs"]) if success else None,
            change_status=change["status"] if change else "",
            counts=copy.deepcopy(change["counts"]) if change else None,
            shape=shape_fingerprint(payload) if kind == "snapshot" else "", shape_changed=False)
        try:
            with closing(sqlite3.connect(self.path.resolve().as_uri() + ("?mode=rw" if existed else "?mode=rwc"), uri=True, timeout=5)) as connection:
                connection.execute("BEGIN IMMEDIATE")
                self._schema(connection, create=not existed)
                if kind == "snapshot":
                    rows = connection.execute("SELECT payload FROM observations WHERE source=? ORDER BY seq DESC", (value["source"],))
                    for row in rows:
                        previous = _validate(json.loads(row[0], object_pairs_hook=_unique))
                        if previous["kind"] == "snapshot" and previous["shape"]:
                            value["shape_changed"] = previous["shape"] != value["shape"]
                            break
                _validate(value)
                connection.execute("INSERT INTO observations(source,payload) VALUES(?,?)",
                                   (value["source"], json.dumps(value, ensure_ascii=True, separators=(",", ":"))))
                connection.execute("INSERT INTO totals(source,count) VALUES(?,1) ON CONFLICT(source) DO UPDATE SET count=count+1", (value["source"],))
                connection.execute("DELETE FROM observations WHERE source=? AND seq NOT IN (SELECT seq FROM observations WHERE source=? ORDER BY seq DESC LIMIT ?)",
                                   (value["source"], value["source"], RETAINED))
                connection.commit()
        except (ValueError, TypeError, KeyError, sqlite3.Error, OSError):
            raise InputError("来源历史无法保存；本次目录与报告仍沿用原结果，请检查工作区。") from None

    def read(self, now):
        self._safe()
        if not _number(now):
            raise InputError("本机时间无效，无法判断来源新鲜度。")
        histories = {key: [] for key in BOARDS}
        totals = {}
        if self.path.exists():
            try:
                with closing(sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)) as connection:
                    connection.execute("BEGIN")
                    self._schema(connection)
                    for source, payload in connection.execute("SELECT source,payload FROM observations ORDER BY seq DESC"):
                        if type(payload) is not str or len(payload) > 4096:
                            raise ValueError("invalid source history size")
                        value = _validate(json.loads(payload, object_pairs_hook=_unique))
                        if value["source"] != source:
                            raise ValueError("mismatched source")
                        histories[source].append(value)
                    totals = dict(connection.execute("SELECT source,count FROM totals"))
                    if (set(totals) != {key for key, rows in histories.items() if rows}
                            or any(key not in BOARDS or type(count) is not int or count < len(histories[key])
                                   for key, count in totals.items())):
                        raise ValueError("invalid history totals")
            except (ValueError, TypeError, KeyError, sqlite3.Error, OSError):
                raise InputError("来源历史损坏或版本不兼容；未清空记录或推断来源正常。") from None
        sources = []
        for key, board in BOARDS.items():
            rows = histories[key]
            confirmed = next((row for row in rows if row["kind"] in {"snapshot", "not_modified"}), None)
            clock_rollback = any(row["at"] > now for row in rows)
            sources.append(dict(source=key, label=board.source.label, history=rows,
                total_observations=totals.get(key, 0), retained=len(rows),
                last_confirmed_at=confirmed["at"] if confirmed else None,
                content_observed_at=confirmed["observed_at"] if confirmed else None,
                stale=confirmed is None or clock_rollback or now - confirmed["at"] >= 600,
                clock_rollback=clock_rollback))
        return dict(schema_version=1, available=True, retained_per_source=RETAINED,
                    shape_sample_limit=256, sources=sources)
