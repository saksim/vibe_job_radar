"""Bounded passive evidence for artificial native CI waits (Issue111/PR112).

Callbacks only update memory. The watchdog never calls browser/service methods.
"""
from __future__ import annotations
from collections import Counter, deque
from contextlib import contextmanager
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


class Stages:
    def __init__(self):
        self.lock = threading.RLock()
        self.calls = Counter()
        self.pending = {}
        self.history = deque(maxlen=128)
        self.started = time.monotonic()
        self.next_token = 0
        self.dropped = 0
        self.result = {'success': False, 'scope':
            'Ephemeral artificial fixture; fixed method/command names and stacks without locals. Original waits and traffic decisions unchanged.'}

    def backend(self):
        with self.lock:
            if len(self.pending) >= 16:
                self.dropped += 1
                return None
            ident = len(self.pending) + 1
            self.pending[ident] = []
            return ident

    def enter(self, ident, name):
        with self.lock:
            if ident is None or len(self.pending[ident]) >= 32:
                self.dropped += 1
                return None
            name = name if name in LABELS else 'cdp.other'
            self.next_token += 1
            token = self.next_token
            self.pending[ident].append((token, name))
            self.calls[name] += 1
            self._event(ident, name, 'enter')
            return token

    def leave(self, ident, token):
        if token is None:
            return
        with self.lock:
            for i, (current, name) in enumerate(self.pending[ident]):
                if current == token:
                    self.pending[ident].pop(i)
                    self._event(ident, name, 'leave')
                    break

    def _event(self, ident, name, phase):
        self.history.append({'backend': ident, 'method': name, 'phase': phase,
                             'elapsed': round(time.monotonic() - self.started, 3)})

    def snapshot(self):
        with self.lock:
            return {**self.result, 'calls': dict(self.calls),
                    'pending': {str(k): [name for _, name in v] for k, v in self.pending.items()},
                    'history': list(self.history), 'dropped': self.dropped,
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
                return function(self, *args, **kwargs)
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
            incomplete = bool(writer_errors or watcher.is_alive())
            if incomplete:
                stages.result['success'] = False
            try:
                save()
                if not failed and incomplete:
                    raise RuntimeError('Native wait evidence incomplete')
            except OSError:
                if not failed:
                    raise
