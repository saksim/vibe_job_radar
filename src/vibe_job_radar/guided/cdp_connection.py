"""Bounded, owner-thread CDP commands for an application-owned browser.

Runtime evaluation does not require subscribing to all console events. This
connection never enables that subscription, modifies console methods or changes
the browser's automation identity. HTTP permission remains in NativeBackend.
"""
from __future__ import annotations

from collections import defaultdict, deque
import json
import threading
import time

from .contracts import CrawlError


class CDPSession:
    def __init__(self, connection, ident=None):
        self.connection, self.ident = connection, ident
        self.callbacks = defaultdict(list)
        self.detached = False

    def on(self, method, callback):
        if sum(map(len, self.callbacks.values())) >= 128:
            raise CrawlError('native_observation_limit')
        self.callbacks[method].append(callback)

    def send(self, method, params=None, *, timeout=30):
        if self.detached:
            raise CrawlError('browser_closed')
        return self.connection.call(self.ident, method, params or {}, timeout=timeout)

    def detach(self):
        if self.ident is not None and not self.detached:
            self.connection.root.send('Target.detachFromTarget', {'sessionId': self.ident})
            self.detached = True
            self.callbacks.clear()
            self.connection.sessions.pop(self.ident, None)


class CDPConnection:
    MAX_MESSAGE = 8_000_000
    MAX_DEFERRED_EVENTS = 512
    MAX_DEFERRED_BYTES = 8_000_000
    FORBIDDEN = frozenset({'Runtime.enable', 'Runtime.disable', 'Runtime.discardConsoleEntries',
                           'Page.setBypassCSP', 'Security.setIgnoreCertificateErrors'})

    def __init__(self, transport):
        # The transport owns inherited pipes, not a discoverable TCP endpoint.
        # It neither accepts external clients nor logs protocol payloads.
        self.socket = transport
        self.owner = threading.get_ident()
        self.sequence = 0
        self.responses, self.pending = {}, set()
        self._received_sequence = 0
        self._response_order = {}
        self._events = deque()
        self._event_bytes = 0
        self._dispatching = False
        self._pump_callbacks = []
        self.sessions = {}
        self.closed = False
        self.root = self.session(None)
        self.root.on('Target.detachedFromTarget', self._retire)

    def _retire(self, event):
        session = self.sessions.pop(event.get('sessionId'), None)
        if session is not None:
            session.detached = True
            session.callbacks.clear()

    def session(self, ident):
        if ident not in self.sessions:
            if len(self.sessions) >= 64:
                raise CrawlError('native_observation_limit')
            self.sessions[ident] = CDPSession(self, ident)
        return self.sessions[ident]

    def _owner(self):
        if threading.get_ident() != self.owner:
            raise CrawlError('native_protocol_error')
        if self.closed:
            raise CrawlError('browser_closed')

    def call(self, session, method, params, *, timeout=30):
        self._owner()
        if method in self.FORBIDDEN:
            raise CrawlError('native_protocol_error')
        if len(self.pending) >= 32:
            raise CrawlError('native_observation_limit')
        self.sequence += 1
        ident = self.sequence
        command = {'id': ident, 'method': method, 'params': params}
        if session is not None:
            command['sessionId'] = session
        encoded = json.dumps(command, ensure_ascii=False)
        if len(encoded.encode('utf-8')) > self.MAX_MESSAGE:
            raise CrawlError('native_observation_limit')
        self.pending.add(ident)
        try:
            self.socket.send(encoded)
            deadline = time.monotonic() + max(.01, min(timeout, 90))
            # A callback may need a command acknowledgment to finish. Events
            # received during that wait must not recursively enter callbacks.
            # Drain events that PRECEDED this reply. A nested acknowledgment
            # can also receive later events; those belong to subsequent pumping,
            # not an ever-growing completion condition for an answered command.
            # Navigation/retirement before the reply still reaches its caller.
            while (ident not in self.responses or (not self._dispatching and self._events
                    and self._events[0][2] < self._response_order[ident])):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise CrawlError('native_protocol_error')
                self.pump(min(.05, remaining))
            response = self.responses.pop(ident)
            if 'error' in response:
                # Raw CDP errors may contain expressions, URL or form values.
                raise CrawlError('native_protocol_error')
            return response.get('result', {})
        except CrawlError:
            raise
        except Exception:
            self.closed = True
            raise CrawlError('browser_closed') from None
        finally:
            self.pending.discard(ident)
            self.responses.pop(ident, None)
            self._response_order.pop(ident, None)

    def pump(self, timeout=.05):
        self._owner()
        result = self._pump(timeout)
        # Cooperative work may issue one command, but must never sleep for a
        # future deadline. Nested acknowledgments defer events as callbacks do.
        # Retirements/navigation already queued take precedence over due work.
        if not self._dispatching and not self._events:
            self._dispatching = True
            try:
                for callback in tuple(self._pump_callbacks):
                    callback()
            finally:
                self._dispatching = False
        return result

    def _pump(self, timeout):
        if self._events and not self._dispatching:
            message, size, _ = self._events.popleft()
            self._event_bytes -= size
            self._dispatch(message)
            return True
        try:
            raw = self.socket.recv(timeout=max(0, timeout))
        except TimeoutError:
            return False
        except Exception:
            self.closed = True
            raise CrawlError('browser_closed') from None
        if not isinstance(raw, str):
            raise CrawlError('native_observation_limit')
        size = len(raw.encode('utf-8'))
        if size > self.MAX_MESSAGE:
            raise CrawlError('native_observation_limit')
        try:
            message = json.loads(raw)
        except (ValueError, RecursionError):
            raise CrawlError('native_protocol_error') from None
        if not isinstance(message, dict):
            raise CrawlError('native_protocol_error')
        self._received_sequence += 1
        if 'id' in message:
            if message['id'] not in self.pending:
                raise CrawlError('native_protocol_error')
            self.responses[message['id']] = message
            self._response_order[message['id']] = self._received_sequence
            return True
        session = self.sessions.get(message.get('sessionId'))
        if session is None or session.detached or not session.callbacks.get(message.get('method')):
            return True
        if self._dispatching:
            if (len(self._events) >= self.MAX_DEFERRED_EVENTS
                    or self._event_bytes + size > self.MAX_DEFERRED_BYTES):
                raise CrawlError('native_observation_limit')
            self._events.append((message, size, self._received_sequence))
            self._event_bytes += size
        else:
            self._dispatch(message)
        return True

    def add_pump_callback(self, callback):
        self._owner()
        if callback not in self._pump_callbacks:
            if len(self._pump_callbacks) >= 8:
                raise CrawlError('native_observation_limit')
            self._pump_callbacks.append(callback)

    def remove_pump_callback(self, callback):
        self._owner()
        if callback in self._pump_callbacks:
            self._pump_callbacks.remove(callback)

    def _dispatch(self, message):
        session = self.sessions.get(message.get('sessionId'))
        if session is None or session.detached:
            return
        self._dispatching = True
        try:
            for callback in tuple(session.callbacks.get(message.get('method'), ())):
                callback(message.get('params', {}))
        finally:
            self._dispatching = False

    def wait(self, seconds):
        deadline = time.monotonic() + max(0, seconds)
        while time.monotonic() < deadline:
            self.pump(min(.05, deadline - time.monotonic()))

    def close(self):
        try:
            self.socket.close()
        finally:
            self.closed = True
            for session in self.sessions.values():
                session.detached = True
                session.callbacks.clear()
            self.sessions.clear()
            self.responses.clear()
            self._response_order.clear()
            self.pending.clear()
            self._events.clear()
            self._event_bytes = 0
            self._pump_callbacks.clear()
