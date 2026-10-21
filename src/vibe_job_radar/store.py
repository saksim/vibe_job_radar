from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
import sqlite3
from pathlib import Path
from .models import JobRecord
from .utils import json_text, utc_now, parse_time


class SourceReadBusy(sqlite3.OperationalError):
    """A stable source image could not be captured without changing it."""


def _digest(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.digest()


class Store:
    """Single-writer local store; exact snapshots and source observations are retained."""
    def __init__(self, path: str | Path, *, read_only: bool = False):
        self.path = Path(path)
        self._read_snapshot = None
        if read_only:
            # SQLite mode=ro can still create WAL coordination files. Open only
            # a private stable image; never tell SQLite a live source is immutable.
            self.conn = self._connect_read_snapshot()
            try:
                self.conn.execute("PRAGMA query_only=ON")
                self._validate_read_schema()
            except BaseException:
                self.__exit__()
                raise
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, timeout=30)
        try:
            self.conn.execute("PRAGMA foreign_keys=ON")
            self.conn.execute("PRAGMA journal_mode=WAL")
            version = self.conn.execute("PRAGMA user_version").fetchone()[0]
            if version not in {0, 1}:
                raise ValueError(f"unsupported database schema: {version}")
            if version == 0:
                # Publish a new schema and its version in one durable commit,
                # rather than flushing every CREATE separately. Recheck after
                # taking the writer lock in case another initializer ran first.
                self.conn.execute("BEGIN IMMEDIATE")
                version = self.conn.execute("PRAGMA user_version").fetchone()[0]
                if version not in {0, 1}:
                    raise ValueError(f"unsupported database schema: {version}")
            for statement in (
                '''CREATE TABLE IF NOT EXISTS records(
                    record_id TEXT PRIMARY KEY, identity_key TEXT NOT NULL, fingerprint TEXT NOT NULL,
                    body TEXT NOT NULL, first_seen TEXT NOT NULL, last_seen TEXT NOT NULL)''',
                'CREATE INDEX IF NOT EXISTS records_identity ON records(identity_key)',
                '''CREATE TABLE IF NOT EXISTS observations(
                    id INTEGER PRIMARY KEY, record_id TEXT NOT NULL REFERENCES records(record_id),
                    collected_at TEXT NOT NULL, source_ref TEXT NOT NULL, source_mode TEXT NOT NULL)''',
                '''CREATE TABLE IF NOT EXISTS events(
                    id INTEGER PRIMARY KEY, created_at TEXT NOT NULL, action TEXT NOT NULL, status TEXT NOT NULL, details TEXT NOT NULL)''',
            ):
                self.conn.execute(statement)
            # A current WAL database still takes no initialization writer lock
            # or version write merely to read beside an existing writer.
            if version == 0:
                self.conn.execute("PRAGMA user_version=1")
            self.conn.commit()
        except BaseException:
            # __enter__ was never reached: close also rolls back a failed or
            # interrupted initialization and releases Windows file handles.
            self.conn.close()
            raise

    def _connect_read_snapshot(self):
        source = self.path.resolve()
        wal = source.with_name(source.name + "-wal")
        journal = source.with_name(source.name + "-journal")
        for _ in range(3):
            temporary = tempfile.TemporaryDirectory(prefix="vjr-source-read-")
            target = Path(temporary.name) / source.name
            try:
                # A rollback journal requires coordinated recovery. Do not
                # copy possibly uncommitted database pages without that lock.
                if journal.exists():
                    temporary.cleanup()
                    continue
                files = [source] + ([wal] if wal.exists() else [])
                for original in files:
                    shutil.copyfile(original, target.with_name(original.name))
                # Compare contents, not just timestamps: Windows may delay
                # mtime updates while a WAL writer keeps its handle open.
                stable = all(_digest(original) == _digest(target.with_name(original.name))
                             for original in files)
                if not stable or journal.exists() or wal.exists() != (wal in files):
                    temporary.cleanup()
                    continue
                # The WAL is part of the database. SQLite reconstructs its
                # coordination index privately and ignores uncommitted frames.
                connection = sqlite3.connect(target, timeout=30)
                self._read_snapshot = temporary
                return connection
            except FileNotFoundError:
                temporary.cleanup()
                continue
            except BaseException:
                temporary.cleanup()
                raise
        raise SourceReadBusy("source database changed during read; retry when writes settle")

    def _validate_read_schema(self):
        version = self.conn.execute("PRAGMA user_version").fetchone()[0]
        if version not in {0, 1}:
            raise ValueError(f"unsupported database schema: {version}")
        for table, expected in (
            ("records", [("record_id", "TEXT", 0, 1), ("identity_key", "TEXT", 1, 0),
                         ("fingerprint", "TEXT", 1, 0), ("body", "TEXT", 1, 0),
                         ("first_seen", "TEXT", 1, 0), ("last_seen", "TEXT", 1, 0)]),
            ("observations", [("id", "INTEGER", 0, 1), ("record_id", "TEXT", 1, 0),
                              ("collected_at", "TEXT", 1, 0), ("source_ref", "TEXT", 1, 0),
                              ("source_mode", "TEXT", 1, 0)]),
            ("events", [("id", "INTEGER", 0, 1), ("created_at", "TEXT", 1, 0),
                        ("action", "TEXT", 1, 0), ("status", "TEXT", 1, 0), ("details", "TEXT", 1, 0)]),
        ):
            columns = [(row[1], row[2].upper(), row[3], row[5]) for row in
                       self.conn.execute("PRAGMA table_info(" + table + ")")]
            if columns != expected:
                raise ValueError("source database is incomplete or incompatible; no tables were rebuilt")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        try:
            self.conn.close()
        finally:
            if self._read_snapshot is not None:
                self._read_snapshot.cleanup()
                self._read_snapshot = None

    def add(self, job: JobRecord) -> bool:
        with self.conn:
            return self._add(job)

    def _add(self, job: JobRecord) -> bool:
        """Write within the caller's transaction; preserve every observation."""
        exists = self.conn.execute("SELECT body FROM records WHERE record_id=?", (job.record_id,)).fetchone()
        body = job.to_dict()
        if exists:
            old = json.loads(exists[0])
            if parse_time(old["collected_at"]) > parse_time(job.collected_at):
                body = old
        self.conn.execute('''INSERT INTO records VALUES(?,?,?,?,?,?)
            ON CONFLICT(record_id) DO UPDATE SET last_seen=excluded.last_seen,body=excluded.body''',
            (job.record_id, job.identity, job.fingerprint, json_text(body), utc_now(), utc_now()))
        self.conn.execute("INSERT INTO observations(record_id,collected_at,source_ref,source_mode) VALUES(?,?,?,?)",
                          (job.record_id, job.collected_at, job.source_ref, job.source_mode))
        return not bool(exists)

    def add_batch(self, jobs: list[JobRecord], *, action: str, details: dict) -> int:
        """Commit a validated batch and its audit event together, or neither."""
        with self.conn:
            self.conn.execute("BEGIN IMMEDIATE")
            inserted = sum(int(self._add(job)) for job in jobs)
            self._event(action, "ok", {**details, "observations": len(jobs), "new_snapshots": inserted})
        return inserted

    def records(self, *, latest_only: bool = True) -> list[JobRecord]:
        rows = self.conn.execute("SELECT identity_key,body FROM records ORDER BY last_seen,record_id").fetchall()
        if latest_only:
            # Latest captured snapshot wins; a late import of an old snapshot cannot roll it back.
            chosen = {}
            for identity, body in rows:
                parsed = json.loads(body)
                if identity not in chosen or parse_time(parsed["collected_at"]) >= parse_time(json.loads(chosen[identity])["collected_at"]):
                    chosen[identity] = body
            values = chosen.values()
        else:
            values = (body for _, body in rows)
        return [JobRecord.from_dict(json.loads(body)) for body in values]

    def event(self, action: str, status: str, details: dict) -> None:
        with self.conn:
            self._event(action, status, details)

    def _event(self, action: str, status: str, details: dict) -> None:
        self.conn.execute("INSERT INTO events(created_at,action,status,details) VALUES(?,?,?,?)",
                          (utc_now(), action, status, json_text(details)))

    def events(self) -> list[dict]:
        return [{"event_id": r[0], "created_at": r[1], "action": r[2], "status": r[3], "details": json.loads(r[4])}
                for r in self.conn.execute("SELECT * FROM events ORDER BY id")]
