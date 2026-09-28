"""Keep native requests paused while pacing, without blocking CDP events.

Only prepared accounting metadata is retained. Chromium owns all headers and
bodies; a due request is continued once, never reconstructed or replayed.
"""
from collections import deque
from dataclasses import dataclass, field
import json
import time

from .contracts import CrawlError
from .diagnostic_trace import notify, observe
from .rate import RateLimit


@dataclass(repr=False)
class PausedRequest:
    session: str
    request_id: str
    key: tuple
    origin: str
    record: dict = field(repr=False)
    business: bool
    authentication: bool = False
    size: int = 0
    due: float = 0
    deadline: float | None = None


class NativeRequestPacer:
    MAX_REQUESTS = 128
    MAX_BYTES = 1_000_000

    def __init__(self, backend):
        self.backend = backend
        self.queue = deque()
        self.bytes = 0
        self.connection = None

    def _check(self):
        b = self.backend
        if b.cancelled.is_set():
            raise CrawlError('paused')
        if not getattr(b, 'policy_check', lambda: True)():
            raise CrawlError('native_policy_changed')
        if b._halted:
            raise b.wait_error or CrawlError(b.error or 'site_stopped')

    def _reserve(self, item):
        b = self.backend
        self._check()
        if item.authentication and not b.auth_mode:
            raise CrawlError('native_operation_unreviewed')
        if item.record['role'] not in {'asset', 'robots'}:
            b.wire.ensure_robots(item.record['url'])
        if item.deadline is None:
            # FIFO waiting is not a new attempt. Start the original inline wait
            # budget when this request first reaches its quota reservation.
            item.deadline = time.monotonic() + b.wire.max_inline_wait
        try:
            b.wire.reserve_request_nowait(origin=item.origin)
        except RateLimit as exc:
            if exc.code not in {'rate_wait', 'publisher_wait'} or exc.wait > item.deadline - time.monotonic():
                raise
            item.due = time.monotonic() + exc.wait
            b.wire.progress(exc.code, round(exc.wait, 1))
            return False
        self._check()
        return True

    def submit(self, item):
        b = self.backend
        occupied = set(b._requests) | {i.key for i in self.queue}
        if (len(occupied) >= self.MAX_REQUESTS and item.key not in occupied
                or any(i.key == item.key for i in self.queue)):
            raise CrawlError('native_observation_limit')
        b._admit_request(item)
        if not self.queue and self._reserve(item):
            b._continue_request(item)
            return
        item.size = len(json.dumps([item.session, item.request_id, item.key,
            item.origin, item.record], ensure_ascii=False).encode('utf-8'))
        if len(self.queue) >= self.MAX_REQUESTS or self.bytes + item.size > self.MAX_BYTES:
            raise CrawlError('native_observation_limit')
        if self.connection is None:
            self.connection = b.browser.connection
            self.connection.add_pump_callback(self.pump)
        self.queue.append(item)
        self.bytes += item.size

    def pump(self):
        if not self.queue or self.backend._closing:
            return
        item = self.queue[0]
        b = self.backend
        try:
            self._check()
            if item.session not in b._sessions:
                self._pop()
                return
            if item.record['epoch'] != b._epoch:
                self._pop()
                b._requests.pop(item.key, None)
                b._hops.pop(item.key, None)
                b.native_counts['blocked'] += 1
                b._send(item.session, 'Fetch.failRequest',
                        {'requestId': item.request_id, 'errorReason': 'Aborted'})
                return
            if time.monotonic() < item.due or not self._reserve(item):
                return
            self._pop()  # Nested acknowledgment cannot continue this twice.
            b._continue_request(item)
        except Exception as exc:
            if self.queue and self.queue[0] is item:
                self._pop()
            code = getattr(exc, 'code', 'native_protocol_error')
            with observe(getattr(b, '_diagnostics', None), 'route', actor='browser',
                         url=item.record['url'], impact='required_by_backend'):
                notify(getattr(b, '_diagnostics', None), 'mark', code=code)
            b.native_counts['blocked'] += 1
            b._fatal(code, exc)
            try:
                b._send(item.session, 'Fetch.failRequest',
                        {'requestId': item.request_id, 'errorReason': 'BlockedByClient'})
            except Exception:
                b._fatal('native_protocol_error')

    def _pop(self):
        item = self.queue.popleft()
        self.bytes -= item.size

    def retire(self, session, network_id=None):
        self.queue = deque(i for i in self.queue if not
            (i.session == session and (network_id is None or i.key[1] == network_id)))
        self.bytes = sum(i.size for i in self.queue)

    def close(self):
        if self.connection is not None and not self.connection.closed:
            self.connection.remove_pump_callback(self.pump)
        self.connection = None
        self.queue.clear()
        self.bytes = 0
