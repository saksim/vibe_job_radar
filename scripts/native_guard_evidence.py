"""Passive fixed counters for each owned guard in developer CI probes.

The interval starts after backend initialization. No request data, addresses,
headers or exception text are retained. Recording never waits for its lock.
Incomplete/unavailable evidence must not be interpreted as zero activity.
"""
from __future__ import annotations

import threading

LIMIT = 100_000
OPERATIONS = ('accept', 'dispatch', 'handle', 'reply', 'open')
REPLIES = ('400', '403', '407', '502', '503', 'other')
OUTCOMES = ('started', 'returned', 'raised')


def unavailable(interval=None):
    return {'available': False, 'interval': interval, 'complete': False,
            'overflow': False, 'operations': None, 'replies': None}


class GuardEvidence:
    def __init__(self):
        self._lock = threading.Lock()
        self._incomplete = False
        self._overflow = False
        self._operations = {name: dict.fromkeys(OUTCOMES, 0) for name in OPERATIONS}
        self._replies = {name: dict.fromkeys(OUTCOMES, 0) for name in REPLIES}

    def _note(self, operation, outcome, reply):
        if not self._lock.acquire(False):
            self._incomplete = True
            return
        try:
            rows = [self._operations[operation]]
            if reply is not None:
                rows.append(self._replies[reply])
            for row in rows:
                if row[outcome] >= LIMIT:
                    self._overflow = self._incomplete = True
                else:
                    row[outcome] += 1
        finally:
            self._lock.release()

    def _safe_note(self, operation, outcome, reply):
        try:
            self._note(operation, outcome, reply)
        except Exception:
            self._incomplete = True

    def observed(self, original, operation):
        def call(*args, **kwargs):
            reply = None
            if operation == 'reply':
                status = args[1] if len(args) > 1 else kwargs.get('status')
                reply = str(status) if type(status) is int and status in {400, 403, 407, 502, 503} else 'other'
            self._safe_note(operation, 'started', reply)
            try:
                value = original(*args, **kwargs)
            except BaseException:
                self._safe_note(operation, 'raised', reply)
                raise
            self._safe_note(operation, 'returned', reply)
            return value
        return call

    def snapshot(self):
        if not self._lock.acquire(False):
            return unavailable('after_backend_init')
        try:
            return {'available': True, 'interval': 'after_backend_init',
                    'complete': not self._incomplete, 'overflow': self._overflow,
                    'operations': {name: dict(row) for name, row in self._operations.items()},
                    'replies': {name: dict(row) for name, row in self._replies.items()}}
        finally:
            self._lock.release()


def install_guard_observer(guard):
    """Wrap only this guard and its server; never patch a production class."""
    changed = []
    try:
        probe = GuardEvidence()
        bindings = [(guard.server, 'get_request', 'accept'),
                    (guard.server, 'process_request', 'dispatch'),
                    (guard, '_handle', 'handle'),
                    (guard, '_reply', 'reply'),
                    (guard, '_open', 'open')]
        # Resolve everything before changing any method.
        originals = [(owner, name, operation, getattr(owner, name))
                     for owner, name, operation in bindings]
        if not all(callable(original) for _, _, _, original in originals):
            return None
        for owner, name, operation, original in originals:
            existed = name in owner.__dict__
            previous = owner.__dict__.get(name)
            setattr(owner, name, probe.observed(original, operation))
            changed.append((owner, name, existed, previous))
        return probe
    except Exception:
        # Even partial installation must preserve the original behavior.
        for owner, name, existed, previous in reversed(changed):
            try:
                if existed:
                    setattr(owner, name, previous)
                else:
                    delattr(owner, name)
            except Exception:
                pass
        return None


def guard_transport_facts(backend):
    try:
        probe = getattr(backend, '_guard_evidence', None)
        return probe.snapshot() if isinstance(probe, GuardEvidence) else unavailable()
    except Exception:
        return unavailable()
