"""Atomic workspace quotas and durable publisher pacing; never refund attempts."""
from __future__ import annotations

import math
import sqlite3
import time
from contextlib import closing, contextmanager
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from .contracts import CrawlError

# Keep request observations for every accepted publisher window, including rules
# learned after the first request. Limits are not silently truncated to one day.
MAX_PUBLISHER_WINDOW = 366 * 86400


@dataclass(frozen=True)
class Limits:
    page_interval: float = 15
    pages_hour: int = 60
    pages_day: int = 200
    request_interval: float = 0.5
    requests_hour: int = 600
    requests_day: int = 3000
    login_interval: float = 300
    logins_day: int = 3


class RateLimit(CrawlError):
    def __init__(self, wait: float, code: str = 'rate_wait', *, next_allowed_at=None):
        self.wait = max(0.0, wait)
        self.next_allowed_at = next_allowed_at
        super().__init__(code)


class RateLedger:
    def __init__(self, path: Path, limits: Limits | None = None, clock=time.time):
        self.path, self.limits, self.clock = Path(path), limits or Limits(), clock
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self._connection(create=True) as conn:
            conn.execute('BEGIN IMMEDIATE')
            conn.execute('CREATE TABLE IF NOT EXISTS visits (site TEXT, kind TEXT, ts REAL)')
            conn.execute('CREATE INDEX IF NOT EXISTS visits_scope ON visits(site,kind,ts)')
            conn.execute('CREATE TABLE IF NOT EXISTS cooldown (site TEXT PRIMARY KEY, until REAL)')
            conn.execute('CREATE TABLE IF NOT EXISTS publisher_policy ('
                         'site TEXT, origin TEXT, delay REAL NOT NULL, PRIMARY KEY(site,origin))')
            conn.execute('CREATE TABLE IF NOT EXISTS publisher_windows ('
                         'site TEXT, origin TEXT, requests INTEGER, seconds REAL,'
                         'PRIMARY KEY(site,origin,requests,seconds))')
            conn.execute('CREATE TABLE IF NOT EXISTS publisher_visits (site TEXT, origin TEXT, ts REAL)')
            conn.execute('CREATE INDEX IF NOT EXISTS publisher_scope ON publisher_visits(site,origin,ts)')
            conn.execute('CREATE TABLE IF NOT EXISTS clock_seen (site TEXT PRIMARY KEY, ts REAL)')
            conn.commit()

    @contextmanager
    def _connection(self, *, create=False):
        if self.path.is_symlink():
            raise CrawlError('unsafe_workspace')
        try:
            # A removed database during a live task is an error, not fresh quotas.
            with closing(sqlite3.connect(self.path.resolve().as_uri() +
                                         ('?mode=rwc' if create else '?mode=rw'),
                                         uri=True, timeout=10)) as conn:
                yield conn
        except sqlite3.Error as exc:
            raise CrawlError('rate_storage_error') from exc

    def _now(self):
        now = self.clock()
        if isinstance(now, bool) or not isinstance(now, (float, int)) or not math.isfinite(now):
            raise CrawlError('clock_rollback')
        return now

    @staticmethod
    def _origin(value):
        if not isinstance(value, str) or len(value) > 2048:
            raise CrawlError('publisher_policy_invalid')
        try:
            p = urlsplit(value)
            if (p.scheme != 'https' or not p.hostname or p.username or p.password
                    or p.port not in (None, 443) or p.path not in ('', '/') or p.query
                    or p.fragment or any(ord(c) < 33 for c in value)):
                raise ValueError
        except ValueError as exc:
            raise CrawlError('publisher_policy_invalid') from exc
        return 'https://' + p.hostname.lower()

    def set_publisher(self, site: str, origin: str, *, delay=0, requests=None, seconds=None):
        """Merge stricter robots constraints; never discard an existing window.

        Window count and minimum interval are distinct constraints. Origin rules
        affect requests to that origin only, while workspace site quotas remain
        shared across every origin belonging to the adapter.
        """
        origin = self._origin(origin)
        if (isinstance(delay, bool) or not isinstance(delay, (int, float))
                or not math.isfinite(delay) or not 0 <= delay <= MAX_PUBLISHER_WINDOW):
            raise CrawlError('publisher_policy_invalid')
        if (requests is not None or seconds is not None) and (
                type(requests) is not int or not 1 <= requests <= 1_000_000
                or isinstance(seconds, bool) or not isinstance(seconds, (int, float))
                or not math.isfinite(seconds) or not 0 < seconds <= MAX_PUBLISHER_WINDOW):
            raise CrawlError('publisher_policy_invalid')
        with self._connection() as conn:
            conn.execute('BEGIN IMMEDIATE')
            conn.execute('INSERT INTO publisher_policy VALUES (?,?,?) ON CONFLICT(site,origin) '
                         'DO UPDATE SET delay=MAX(delay,excluded.delay)', (site, origin, delay))
            if requests is not None:
                conn.execute('INSERT OR IGNORE INTO publisher_windows VALUES (?,?,?,?)',
                             (site, origin, requests, seconds))
            conn.commit()

    def _budget_candidates(self, conn, site, kind, now):
        p = self.limits
        interval, hourly, daily = {
            'page': (p.page_interval, p.pages_hour, p.pages_day),
            'request': (p.request_interval, p.requests_hour, p.requests_day),
            'login': (p.login_interval, p.logins_day, p.logins_day)}[kind]
        observed = conn.execute('SELECT ts FROM clock_seen WHERE site=?', (site,)).fetchone()
        if observed and observed[0] > now + 1:
            raise RateLimit(observed[0] - now + interval, 'clock_rollback',
                            next_allowed_at=observed[0] + interval)
        candidates = [(now, 'rate_wait')]
        cool = conn.execute('SELECT until FROM cooldown WHERE site=?', (site,)).fetchone()
        if cool:
            candidates.append((cool[0], 'cooldown'))
        rows = [r[0] for r in conn.execute(
            'SELECT ts FROM visits WHERE site=? AND kind=? AND ts>? ORDER BY ts',
            (site, kind, now - 86400))]
        if rows and rows[-1] > now + 1:
            raise RateLimit(rows[-1] - now + interval, 'clock_rollback',
                            next_allowed_at=rows[-1] + interval)
        recent = [t for t in rows if t > now - 3600]
        if len(rows) >= daily:
            candidates.append((rows[-daily] + 86400, 'daily_limit'))
        if len(recent) >= hourly:
            candidates.append((recent[-hourly] + 3600, 'hourly_limit'))
        if rows:
            candidates.append((rows[-1] + interval, 'rate_wait'))
        return candidates

    def login_availability(self, site):
        """Read-only UI advice; reserve() still authorizes every actual attempt."""
        now = self._now()
        with self._connection() as conn:
            conn.execute('BEGIN')
            try:
                due, code = max(self._budget_candidates(conn, site, 'login', now),
                                key=lambda item: item[0])
            except RateLimit as exc:
                due, code = exc.next_allowed_at, exc.code
        blocked = due > now
        return {'available': not blocked, 'reason': code if blocked else '',
                'next_allowed_at': due if blocked else None,
                'wait_seconds': round(max(0, due-now), 1)}

    def reserve(self, site: str, kind: str, *, origin: str | None = None) -> None:
        if kind not in {'page', 'request', 'login'}:
            raise ValueError('unknown budget kind')
        if origin is not None:
            origin = self._origin(origin)
        now = self._now()
        with self._connection() as conn:
            conn.execute('BEGIN IMMEDIATE')
            candidates = self._budget_candidates(conn, site, kind, now)
            if kind == 'request' and origin is not None:
                policy = conn.execute('SELECT delay FROM publisher_policy WHERE site=? AND origin=?',
                                      (site, origin)).fetchone()
                if policy:
                    last = conn.execute('SELECT MAX(ts) FROM publisher_visits WHERE site=? AND origin=?',
                                        (site, origin)).fetchone()[0]
                    if last is not None:
                        candidates.append((last + policy[0], 'publisher_wait'))
                for count, window in conn.execute('SELECT requests,seconds FROM publisher_windows '
                                                  'WHERE site=? AND origin=?', (site, origin)):
                    # At most count observations, not an unbounded list in memory.
                    points = conn.execute('SELECT ts FROM publisher_visits WHERE site=? AND origin=? '
                                          'AND ts>? ORDER BY ts DESC LIMIT ?',
                                          (site, origin, now-window, count)).fetchall()
                    if len(points) == count:
                        candidates.append((points[-1][0] + window, 'publisher_wait'))
            due, code = max(candidates, key=lambda item: item[0])
            if due > now:
                raise RateLimit(due - now, code, next_allowed_at=due)
            # Both budgets are consumed together, after ALL constraints pass.
            conn.execute('INSERT INTO visits VALUES (?,?,?)', (site, kind, now))
            if kind == 'request' and origin is not None:
                conn.execute('INSERT INTO publisher_visits VALUES (?,?,?)', (site, origin, now))
            conn.execute('INSERT INTO clock_seen VALUES (?,?) ON CONFLICT(site) '
                         'DO UPDATE SET ts=MAX(ts,excluded.ts)', (site, now))
            conn.execute('DELETE FROM visits WHERE ts<?', (now - 172800,))
            conn.execute('DELETE FROM publisher_visits WHERE ts<?', (now-MAX_PUBLISHER_WINDOW,))
            conn.commit()

    def cool(self, site: str, seconds: float = 300) -> None:
        if not math.isfinite(seconds):
            seconds = 86400
        self.defer(site, max(300, seconds))

    def defer(self, site: str, seconds: float) -> float:
        """Persist a read backoff in the shared cooldown, without lowering it."""
        if type(seconds) not in (int, float) or not math.isfinite(seconds) or seconds < 0:
            raise CrawlError('read_retry_state_invalid')
        until = self._now() + seconds
        if not math.isfinite(until):
            raise CrawlError('clock_rollback')
        with self._connection() as conn:
            conn.execute('BEGIN IMMEDIATE')
            conn.execute('INSERT INTO cooldown VALUES (?,?) ON CONFLICT(site) '
                         'DO UPDATE SET until=MAX(until,excluded.until)', (site, until))
            until = conn.execute('SELECT until FROM cooldown WHERE site=?', (site,)).fetchone()[0]
            conn.commit()
        return until

    def summary(self, site: str) -> dict:
        now = self._now()
        with self._connection() as conn:
            return {k: {'hour': conn.execute('SELECT COUNT(*) FROM visits WHERE site=? AND kind=? AND ts>?', (site, k, now-3600)).fetchone()[0],
                        'day': conn.execute('SELECT COUNT(*) FROM visits WHERE site=? AND kind=? AND ts>?', (site, k, now-86400)).fetchone()[0]}
                    for k in ('page', 'request', 'login')}
