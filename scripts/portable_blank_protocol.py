"""Bounded, payload-free observations of the frozen check's first data document.

The verifier consumes DEBUG=pw:protocol from its own child, never saves raw logs,
and never sends an extra browser command. These observations cannot qualify a
failed health check or establish why Chromium or the SDK stopped progressing.
"""
from __future__ import annotations

import json
import threading
import time

from vibe_job_radar.guided.browser_health import OFFLINE_CHECK_URL

LINE_LIMIT = 65536
EVENT_LIMIT = 64
_EVENTS = {
    'Page.frameStartedLoading': 'frame_started',
    'Page.frameStoppedLoading': 'frame_stopped',
    'Page.domContentEventFired': 'dom_event',
    'Page.loadEventFired': 'load_event',
    'Runtime.executionContextsCleared': 'contexts_cleared',
    'Inspector.targetCrashed': 'target_crashed',
}


def bounded_lines(stream):
    """Discard an entire overlong line, including its final fragment."""
    while True:
        line = stream.readline(LINE_LIMIT + 1)
        if not line:
            return
        if len(line) > LINE_LIMIT:
            while line and not line.endswith(b'\n'):
                line = stream.readline(LINE_LIMIT + 1)
            yield None
        else:
            yield line


class BlankNavigationTrace:
    def __init__(self):
        self._lock = threading.Lock()
        self._navigation = None
        self._loader = None
        self._started = None
        self._events = []
        self._dropped = 0
        self._malformed = 0
        self._overflow = 0
        self._reader_failed = False
        self._eof = False

    def feed(self, line):
        # URL and opaque protocol IDs are used only for correlation. No value
        # supplied by the child is copied into exported event names.
        with self._lock:
            if line is None:
                self._overflow = min(self._overflow + 1, 999999)
                return
            if b'pw:protocol ' not in line:
                return
            try:
                tail = line.split(b'pw:protocol ', 1)[1]
                direction, payload = tail.split(b' {', 1)
                if direction not in (b'SEND \xe2\x96\xba', b'\xe2\x97\x80 RECV'):
                    return
                message = json.JSONDecoder().raw_decode(('{' + payload.decode('utf8')).strip())[0]
                if not isinstance(message, dict):
                    raise ValueError('protocol object required')
                self._observe(direction.startswith(b'SEND'), message)
            except Exception:
                # Optional diagnostics must not end the pipe reader or hide
                # the real application's entry URL, result or failure.
                self._malformed = min(self._malformed + 1, 999999)

    def _record(self, name, loader=None):
        if len(self._events) >= EVENT_LIMIT:
            self._dropped = min(self._dropped + 1, 999999)
            return
        if not isinstance(loader, str) or len(loader) > 256:
            loader = None
        elapsed = max(0, round((time.perf_counter_ns() - self._started) / 1_000_000))
        self._events.append((name, elapsed, loader))

    def _observe(self, sent, message):
        method, session = message.get('method'), message.get('sessionId')
        params = message.get('params', {})
        if sent and method == 'Page.navigate' and params.get('url') == OFFLINE_CHECK_URL:
            if self._navigation is not None:
                self._record('additional_inline_navigation')
                return  # Never replace the first attempt with a later one.
            frame = params.get('frameId')
            if (not isinstance(session, str) or not 0 < len(session) <= 256
                    or not isinstance(frame, str) or not 0 < len(frame) <= 256
                    or type(message.get('id')) is not int):
                raise ValueError('navigation correlation absent')
            self._navigation = (session, frame, message['id'])
            self._started = time.perf_counter_ns()
            self._record('navigate_sent')
            return
        if self._navigation is None:
            return
        selected_session, frame, command = self._navigation
        if sent:
            if method in ('Target.disposeBrowserContext', 'Browser.close'):
                self._record('cleanup_command')
            return
        if session != selected_session:
            return
        if message.get('id') == command:
            result = message.get('result', {})
            loader = result.get('loaderId')
            if isinstance(loader, str) and len(loader) <= 256:
                self._loader = loader
            self._record('navigate_error' if 'error' in message or result.get('errorText') else 'navigate_returned', loader)
        elif method == 'Page.frameNavigated':
            value = params.get('frame', {})
            if value.get('id') == frame:
                self._record('inline_committed' if value.get('url') == OFFLINE_CHECK_URL else 'other_document_committed', value.get('loaderId'))
        elif method == 'Page.lifecycleEvent' and params.get('frameId') == frame:
            name = {'init': 'lifecycle_init', 'DOMContentLoaded': 'lifecycle_dom', 'load': 'lifecycle_load'}.get(params.get('name'))
            if name:
                self._record(name, params.get('loaderId'))
        elif method == 'Runtime.executionContextCreated':
            aux = params.get('context', {}).get('auxData', {})
            if aux.get('frameId') == frame:
                self._record('main_context_created' if aux.get('isDefault') is True else 'isolated_context_created')
        elif method in _EVENTS and ('frameId' not in params or params['frameId'] == frame):
            self._record(_EVENTS[method])

    def finish(self, *, reader_failed=False):
        with self._lock:
            self._eof = not reader_failed
            self._reader_failed = reader_failed

    def snapshot(self):
        with self._lock:
            events = [{'event': name, 'received_ms': elapsed,
                       'loader_relation': ('unknown' if not isinstance(loader, str) or self._loader is None else
                                           'navigation' if loader == self._loader else 'other')}
                      for name, elapsed, loader in self._events]
            return {'schema_version': 1, 'source': 'owned_child_playwright_protocol',
                    'navigation_observed': self._navigation is not None, 'events': events,
                    'events_dropped': self._dropped, 'malformed_lines': self._malformed,
                    'overlong_lines': self._overflow, 'reader_failed': self._reader_failed,
                    'stream_ended': self._eof, 'historical_cause_confirmed': False,
                    'scope': 'First fixed inline navigation only. Receive times include pipe scheduling; '
                             'protocol observations never change the original health result or timeout.'}
