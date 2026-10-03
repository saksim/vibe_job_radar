from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from .models import JobRecord
from .utils import json_text, utc_now, parse_time


class Store:
    """Single-writer local store; exact snapshots and source observations are retained."""
    def __init__(self, path: str | Path):
        self.path = Path(path)
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

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.conn.close()

    def add(self, job: JobRecord) -> bool:
        exists = self.conn.execute("SELECT body FROM records WHERE record_id=?", (job.record_id,)).fetchone()
        body = job.to_dict()
        if exists:
            old = json.loads(exists[0])
            if parse_time(old["collected_at"]) > parse_time(job.collected_at):
                body = old
        with self.conn:
            self.conn.execute('''INSERT INTO records VALUES(?,?,?,?,?,?)
                ON CONFLICT(record_id) DO UPDATE SET last_seen=excluded.last_seen,body=excluded.body''',
                (job.record_id, job.identity, job.fingerprint, json_text(body), utc_now(), utc_now()))
            self.conn.execute("INSERT INTO observations(record_id,collected_at,source_ref,source_mode) VALUES(?,?,?,?)",
                              (job.record_id, job.collected_at, job.source_ref, job.source_mode))
        return not bool(exists)

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
            self.conn.execute("INSERT INTO events(created_at,action,status,details) VALUES(?,?,?,?)",
                              (utc_now(), action, status, json_text(details)))

    def events(self) -> list[dict]:
        return [{"event_id": r[0], "created_at": r[1], "action": r[2], "status": r[3], "details": json.loads(r[4])}
                for r in self.conn.execute("SELECT * FROM events ORDER BY id")]
