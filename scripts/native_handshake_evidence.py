"""Fixed, bounded timing facts for the owned CI proxy handshake only."""
import threading
import time

LIMIT = 128
COMMANDS = {"Fetch.enable", "Fetch.continueRequest", "Fetch.continueWithAuth"}
EVENTS = {"Fetch.authRequired", "Fetch.requestPaused", "Network.loadingFailed"}
ALLOWED = ({method + "." + phase for method in COMMANDS for phase in ("sent", "acknowledged")}
           | {method + ".event" for method in EVENTS}
           | {"guard." + operation + "." + phase for operation in ("accept", "dispatch", "handle", "open")
              for phase in ("started", "returned", "raised")}
           | {"guard.reply." + status + "." + phase for status in ("400", "403", "407", "502", "503", "other")
              for phase in ("started", "returned", "raised")})


class HandshakeTrace:
    def __init__(self):
        self._start = time.monotonic()
        self._lock = threading.Lock()
        self._rows = []
        self._complete = True
        self._overflow = False

    def record(self, name):
        try:
            if not isinstance(name, str) or name not in ALLOWED:
                return
            if not self._lock.acquire(False):
                self._complete = False
                return
            try:
                if len(self._rows) >= LIMIT:
                    self._overflow = True
                    self._complete = False
                    return
                self._rows.append({"event": name,
                                   "elapsed_ms": max(0, round((time.monotonic() - self._start) * 1000, 3))})
            finally:
                self._lock.release()
        except Exception:
            self._complete = False

    def snapshot(self):
        if not self._lock.acquire(False):
            return {"available": False, "complete": False, "overflow": False, "events": None}
        try:
            return {"available": True, "complete": self._complete, "overflow": self._overflow,
                    "events": [dict(row) for row in self._rows]}
        finally:
            self._lock.release()


def handshake_facts(backend):
    try:
        trace = getattr(backend, "_handshake_trace", None)
        if isinstance(trace, HandshakeTrace):
            return trace.snapshot()
    except Exception:
        pass
    return {"available": False, "complete": False, "overflow": False, "events": None}


def record_handshake(backend, name):
    try:
        trace = getattr(backend, "_handshake_trace", None)
        if isinstance(trace, HandshakeTrace):
            trace.record(name)
    except Exception:
        pass


def observed_send(backend, operation, session, method, params=None, callback=None):
    """Keep original command/callback semantics and record fixed handshake names."""
    key = method if isinstance(method, str) and method in COMMANDS else "other"
    record_handshake(backend, key + ".sent")
    def acknowledged(result):
        record_handshake(backend, key + ".acknowledged")
        if callback:
            callback(result)
    return operation(session, method, params, acknowledged)


def observed_message(backend, message):
    """Observe an already decoded message without retaining parameters or IDs."""
    try:
        method = message.get("method")
        if isinstance(method, str) and method in EVENTS:
            record_handshake(backend, method + ".event")
    except Exception:
        pass
