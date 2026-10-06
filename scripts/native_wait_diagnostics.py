"""Bounded passive evidence for artificial native CI waits (Issue111/PR112).

Callbacks only update memory. The watchdog never calls browser/service methods.
"""
from __future__ import annotations
from collections import Counter, deque
from contextlib import ExitStack, contextmanager
import faulthandler
from functools import wraps
import json
import os
import sys
import threading
import time

METHODS = ('initialize', '_configure_context', '_new_page', '_load_robots',
           'open', 'snapshot', 'close')
COMMANDS = frozenset({
    'Network.enable', 'Network.setUserAgentOverride', 'Network.setCacheDisabled',
    'Fetch.enable', 'Fetch.continueRequest', 'Fetch.continueResponse',
    'Fetch.continueWithAuth', 'Fetch.failRequest', 'Fetch.getResponseBody',
    'Fetch.fulfillRequest', 'Runtime.runIfWaitingForDebugger',
    'Target.setAutoAttach', 'Network.getResponseBody', 'Page.stopLoading',
})
LABELS = frozenset(METHODS) | {'cdp.' + name for name in COMMANDS} | {'cdp.other'}


def require_ci():
    if ('--controlled' not in sys.argv or os.environ.get('CI') != 'true'
            or os.environ.get('GITHUB_ACTIONS') != 'true'):
        raise SystemExit('Native wait diagnostics require an explicitly enabled ephemeral GitHub runner.')


def error_chain(error):
    names = []; seen = set()
    while error is not None and id(error) not in seen and len(names) < 8:
        seen.add(id(error)); names.append(type(error).__name__[:80])
        error = error.__cause__ or error.__context__
    return names


class CallTimings:
    """Bounded elapsed times; nested/parallel totals are not wall time."""

    def __init__(self, labels, *, capacity=64, clock=time.perf_counter):
        self.labels = frozenset(labels) | {'other'}
        self.capacity, self.clock = capacity, clock
        self.lock = threading.RLock()
        self.started = clock()
        self.next_token = self.dropped = 0
        self.active, self.completed = {}, {}

    def enter(self, label):
        with self.lock:
            if len(self.active) >= self.capacity:
                self.dropped += 1
                return None
            label = label if label in self.labels else 'other'
            self.next_token += 1
            self.active[self.next_token] = (label, self.clock())
            return self.next_token

    def leave(self, token):
        if token is None:
            return
        with self.lock:
            call = self.active.pop(token, None)
            if call is None:
                return
            label, began = call
            elapsed = self.clock() - began
            stats = self.completed.setdefault(label, [0, 0.0, 0.0])
            stats[0] += 1
            stats[1] += elapsed
            stats[2] = max(stats[2], elapsed)

    def observe(self, label, function):
        @wraps(function)
        def timed(*args, **kwargs):
            token = self.enter(label)
            try:
                return function(*args, **kwargs)
            finally:
                self.leave(token)
        return timed

    def snapshot(self):
        with self.lock:
            now = self.clock()
            return {
                'scope': 'Elapsed calls including failures; nested and concurrent totals overlap.',
                'completed': {label: {'count': stats[0],
                    'total_ms': round(stats[1] * 1000, 3),
                    'max_ms': round(stats[2] * 1000, 3)}
                    for label, stats in self.completed.items()},
                'pending': [{'operation': label,
                    'started_ms': round((began - self.started) * 1000, 3),
                    'elapsed_ms': round((now - began) * 1000, 3)}
                    for label, began in self.active.values()],
                'dropped': self.dropped,
            }


IO_LABELS = frozenset({
    'sqlite.connect', 'rate.initialize', 'rate.reserve', 'rate.publisher',
    'rate.login_availability', 'service.state', 'service.checkpoint',
    'service.report', 'file.fsync', 'file.replace',
})


@contextmanager
def observe_io(stages):
    """CI probe only: same calls/arguments/results/errors; no payload capture."""
    import sqlite3
    from unittest.mock import patch
    from vibe_job_radar.guided.rate import RateLedger
    from vibe_job_radar.guided.service import GuidedService

    targets = (
        (sqlite3, 'connect', 'sqlite.connect'),
        (RateLedger, '__init__', 'rate.initialize'),
        (RateLedger, 'reserve', 'rate.reserve'),
        (RateLedger, 'set_publisher', 'rate.publisher'),
        (RateLedger, 'login_availability', 'rate.login_availability'),
        (GuidedService, 'state', 'service.state'),
        (GuidedService, '_save', 'service.checkpoint'),
        (GuidedService, '_finalize_report', 'service.report'),
        (os, 'fsync', 'file.fsync'),
        (os, 'replace', 'file.replace'),
    )
    with ExitStack() as stack:
        for owner, attribute, label in targets:
            stack.enter_context(patch.object(owner, attribute,
                stages.io_timings.observe(label, getattr(owner, attribute))))
        yield


class Stages:
    def __init__(self):
        self.lock = threading.RLock()
        self.calls = Counter()
        self.timings = CallTimings(LABELS, capacity=512)
        self.io_timings = CallTimings(IO_LABELS)
        self.pending = {}
        self.history = deque(maxlen=128)
        self.started = time.monotonic()
        self.next_token = 0
        self.next_backend = 0
        self.closed_backends = 0
        self.after_close_calls = 0
        self.retiring = set()
        self.dropped = 0
        self.result = {'success': False, 'scope':
            'Ephemeral artificial fixture; fixed method/command names and stacks without locals. Original waits and traffic decisions unchanged.'}

    def backend(self):
        with self.lock:
            if len(self.pending) >= 16:
                self.dropped += 1
                return None
            self.next_backend += 1
            ident = self.next_backend
            self.pending[ident] = []
            return ident

    def enter(self, ident, name):
        with self.lock:
            if ident not in self.pending:
                if type(ident) is int and 0 < ident <= self.next_backend:
                    # A late call on a closed instance still reaches the real
                    # backend. It cannot alias another instance's diagnostic slot.
                    self.after_close_calls += 1
                else:
                    self.dropped += 1
                return None
            if len(self.pending[ident]) >= 32:
                self.dropped += 1
                return None
            name = name if name in LABELS else 'cdp.other'
            self.next_token += 1
            token = self.next_token
            self.pending[ident].append((token, name, self.timings.enter(name)))
            self.calls[name] += 1
            self._event(ident, name, 'enter')
            return token

    def leave(self, ident, token):
        if token is None:
            return
        with self.lock:
            for i, (current, name, timing_token) in enumerate(self.pending.get(ident, [])):
                if current == token:
                    self.pending[ident].pop(i)
                    self.timings.leave(timing_token)
                    self._event(ident, name, 'leave')
                    break
            self._retire_if_finished(ident)

    def closed(self, ident):
        """Release a successful close only after its outer calls also finish."""
        with self.lock:
            if ident in self.pending:
                self.retiring.add(ident)
                self._retire_if_finished(ident)

    def _retire_if_finished(self, ident):
        if ident in self.retiring and not self.pending[ident]:
            del self.pending[ident]
            self.retiring.remove(ident)
            self.closed_backends += 1

    def _event(self, ident, name, phase):
        self.history.append({'backend': ident, 'method': name, 'phase': phase,
                             'elapsed': round(time.monotonic() - self.started, 3)})

    def snapshot(self):
        with self.lock:
            return {**self.result, 'calls': dict(self.calls),
                    'pending': {str(k): [name for _, name, _ in v] for k, v in self.pending.items()},
                    'history': list(self.history), 'dropped': self.dropped,
                    'timings': self.timings.snapshot(),
                    'io_timings': self.io_timings.snapshot(),
                    'allocated_backends': self.next_backend,
                    'closed_backends': self.closed_backends,
                    'after_close_calls': self.after_close_calls,
                    'elapsed': round(time.monotonic() - self.started, 3)}


def observed_backend(base, stages):
    class Backend(base):
        def __init__(self, *args, **kwargs):
            self._wait_probe_id = stages.backend()
            token = stages.enter(self._wait_probe_id, 'initialize')
            try:
                super().__init__(*args, **kwargs)
            finally:
                stages.leave(self._wait_probe_id, token)

        def _send(self, session, method, params=None, callback=None):
            name = method if method in COMMANDS else 'other'
            token = stages.enter(self._wait_probe_id, 'cdp.' + name)
            try:
                return super()._send(session, method, params, callback)
            finally:
                stages.leave(self._wait_probe_id, token)

    def observe(name, function):
        @wraps(function)
        def recorded(self, *args, **kwargs):
            token = stages.enter(self._wait_probe_id, name)
            try:
                value = function(self, *args, **kwargs)
                if name == 'close':
                    stages.closed(self._wait_probe_id)
                return value
            finally:
                stages.leave(self._wait_probe_id, token)
        return recorded

    for name in METHODS[1:]:
        setattr(Backend, name, observe(name, getattr(base, name)))
    return Backend


@contextmanager
def capture(stages, out, prefix, *, interval=5, stack_interval=35):
    """Retain pre-cleanup evidence without file writes inside browser callbacks."""
    out.mkdir(parents=True, exist_ok=True)
    progress = out / (prefix + '-progress.json')
    checkpoints = out / (prefix + '-checkpoints.jsonl')
    stop = threading.Event()
    writer_errors = []

    def save():
        progress.write_text(json.dumps(stages.snapshot(), indent=2), encoding='utf-8')

    def watch():
        next_stack=time.monotonic()+stack_interval
        try:
            with checkpoints.open('w', encoding='utf-8') as stream:
                for _ in range(36):
                    if stop.wait(interval):
                        return
                    stream.write(json.dumps(stages.snapshot()) + '\n')
                    stream.flush()
                    if time.monotonic()>=next_stack:
                        # Synchronous dumping from this Python thread holds the
                        # interpreter lock while traversing other Python frames.
                        # The C watchdog can race active frame/metadata updates.
                        faulthandler.dump_traceback(file=stacks,all_threads=True)
                        next_stack=time.monotonic()+stack_interval
        except (OSError,RuntimeError) as error:
            writer_errors.extend(error_chain(error))

    save()
    watcher = threading.Thread(target=watch, name='native-wait-evidence', daemon=True)
    failed = False
    with (out / (prefix + '-threads.log')).open('w', encoding='utf-8') as stacks:
        watcher.start()
        try:
            yield
            stages.result['success'] = True
        except BaseException as error:
            failed = True
            stages.result['error_types'] = error_chain(error)
            try:
                faulthandler.dump_traceback(file=stacks, all_threads=True)
            except OSError:
                pass
            raise
        finally:
            stop.set(); watcher.join(timeout=2)
            stages.result['watchdog_stopped'] = not watcher.is_alive()
            stages.result['writer_error_types'] = writer_errors[:8]
            final = stages.snapshot()
            # A returned acceptance call does not prove its worker has finished
            # closing. Preserve the original service deadline and report this
            # unfinished cleanup at the same boundary, without waiting again.
            incomplete = bool(writer_errors or watcher.is_alive() or final['dropped']
                              or final['pending']
                              or final['timings']['dropped']
                              or final['io_timings']['dropped']
                              or final['io_timings']['pending']
                              or final['allocated_backends'] != final['closed_backends'])
            if incomplete:
                stages.result['success'] = False
                if not failed and (final['pending']
                                   or final['io_timings']['pending']
                                   or final['allocated_backends'] != final['closed_backends']):
                    try:
                        faulthandler.dump_traceback(file=stacks, all_threads=True)
                    except (OSError, RuntimeError) as error:
                        writer_errors.extend(error_chain(error))
                        stages.result['writer_error_types'] = writer_errors[:8]
            try:
                save()
                if not failed and incomplete:
                    raise RuntimeError('Native wait evidence incomplete')
            except OSError:
                if not failed:
                    raise
