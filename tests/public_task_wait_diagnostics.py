"""Test-only observations of a public worker's existing join window.

Real fsync, deadlines and worker cleanup remain owned by their original callers.
Counters exclude calls that began before observation and may overlap in time.
"""
import json
import math
import os
from pathlib import Path
import sys
import threading
import time
from unittest.mock import patch


class WaitFsyncProbe:
    def __init__(self, worker, clock=time.perf_counter):
        self.worker, self.clock = worker, clock
        self.original = os.fsync
        self.lock = threading.Lock()
        self.started = self.completed = self.failed = 0
        self.total = self.maximum = 0.0
        self.active = {}
        self.incomplete = False
        self.replacement = patch.object(os, 'fsync', self.call)

    def __enter__(self):
        self.replacement.start()
        return self

    def __exit__(self, *args):
        self.replacement.stop()

    def _now(self):
        value = self.clock()
        if type(value) not in (float, int) or not math.isfinite(value):
            raise ValueError('diagnostic clock unavailable')
        return value

    def call(self, descriptor):
        token = None
        try:
            current, owner = threading.current_thread(), self.worker()
            owned = current is owner or (owner is not None and owner.ident is not None
                and current.name.startswith(f'radar-report-{owner.ident}_'))
            if owned:
                begin = self._now()
                with self.lock:
                    if len(self.active) < 5:
                        token = object()
                        self.active[token] = begin
                        self.started += 1
                    else:
                        self.incomplete = True
        except Exception:
            self.incomplete = True
        failed = True
        try:
            result = self.original(descriptor)
            failed = False
            return result
        finally:
            if token is not None:
                try:
                    elapsed = max(0.0, self._now() - begin)
                    with self.lock:
                        self.active.pop(token, None)
                        self.completed += 1
                        self.failed += int(failed)
                        self.total += elapsed
                        self.maximum = max(self.maximum, elapsed)
                except Exception:
                    self.incomplete = True

    def snapshot(self):
        if not self.lock.acquire(blocking=False):
            return {'available': False}
        try:
            active = min(self.active.values(), default=None)
            return {'available': True, 'window_only': True,
                    'calls_started': self.started, 'calls_completed': self.completed,
                    'failed_calls': self.failed, 'in_flight_calls': len(self.active),
                    'completed_total_ms': round(self.total * 1000, 3),
                    'completed_max_ms': round(self.maximum * 1000, 3),
                    'oldest_current_ms': None if active is None else round(max(0.0, self._now() - active) * 1000, 3),
                    'incomplete': self.incomplete}
        finally:
            self.lock.release()


def safe_snapshot(probe):
    try:
        return probe.snapshot()
    except Exception:
        return {'available': False}


def wait_diagnostic(tasks, probe, before, elapsed):
    # No task lock, snapshot(), file read, query, descriptor or exception text.
    worker = tasks._thread
    allowed = {'status': {'idle', 'queued', 'running', 'cancelling', 'cancelled',
                          'completed', 'failed', 'interrupted'}, 'phase': {'saving'}}
    state = {}
    for key, values in allowed.items():
        value = tasks._state.get(key)
        state[key] = value if type(value) is str and value in values else 'unknown'
    frames = sys._current_frames()
    def stack(thread):
        frame = frames.get(thread.ident) if thread is not None else None
        rows = []
        while frame is not None and len(rows) < 24:
            rows.append({'file': Path(frame.f_code.co_filename).name,
                         'line': frame.f_lineno, 'function': frame.f_code.co_name})
            frame = frame.f_back
        return rows
    writers = []
    if worker is not None and worker.ident is not None:
        for thread in threading.enumerate():
            if thread.name.startswith(f'radar-report-{worker.ident}_'):
                writers.append(stack(thread))
                if len(writers) == 4:
                    break
    return {'in_memory_state_only': True, 'task': state,
            'wait_elapsed_ms': round(elapsed * 1000, 3), 'worker_stack': stack(worker),
            'report_writers': writers,
            'worker_fsync': {'before_wait': before, 'at_timeout': safe_snapshot(probe)}}


def join_observed(tasks, *, timeout):
    """Return the original alive result; optional evidence never releases a worker."""
    worker = tasks._thread
    probe = WaitFsyncProbe(lambda: worker)
    installed = False
    try:
        try:
            probe.__enter__()
            installed = True
        except Exception:
            pass
        before = safe_snapshot(probe) if installed else {'available': False}
        begin = time.perf_counter()
        worker.join(timeout=timeout)
        alive = worker.is_alive()
        if not alive:
            return False, ''
        try:
            if not installed:
                raise RuntimeError('observation unavailable')
            details = wait_diagnostic(tasks, probe, before, time.perf_counter() - begin)
            text = json.dumps(details, ensure_ascii=False, allow_nan=False)
        except Exception:
            text = '{"capture_status":"unavailable"}'
        return True, '; PUBLIC_TASK_WAIT ' + text
    finally:
        if installed:
            probe.__exit__(None, None, None)
