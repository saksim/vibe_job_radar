"""Test-only, nonblocking owner wait facts; never read task files or secrets."""
from contextlib import closing, contextmanager
import json
from pathlib import Path
import sqlite3
import sys
import threading
import time
import traceback


def wait_diagnostic(tasks, probe=None, before=None, *, sqlite_probe=None, sqlite_before=None):
    # snapshot()/busy() acquire the same lock that a stalled writer can hold.
    # These are best-effort observations, not an atomic task-state assertion.
    worker = tasks._thread
    allowed = {'status': {'idle', 'queued', 'running', 'cancelling', 'cancelled',
                          'completed', 'failed', 'interrupted'},
               'phase': {'saving'}}
    state = {key: tasks._state.get(key) if isinstance(tasks._state.get(key), str)
             and tasks._state.get(key) in values else 'unknown'
             for key, values in allowed.items()}
    frames = sys._current_frames()
    def stack(thread):
        frame = frames.get(thread.ident) if thread is not None else None
        rows = []
        if frame is not None:
            for frame, number in traceback.walk_stack(frame):
                rows.append({'file': Path(frame.f_code.co_filename).name,
                             'line': number, 'function': frame.f_code.co_name})
                if len(rows) == 24:
                    break
        return rows
    result = {'worker_alive': bool(worker and worker.is_alive()), 'task': state,
              'worker_stack': stack(worker), 'report_writers': []}
    if worker is not None and worker.ident is not None:
        for thread in threading.enumerate():
            if thread.name.startswith(f'radar-report-{worker.ident}_'):
                result['report_writers'].append(stack(thread))
                if len(result['report_writers']) == 4:
                    break
    if probe is not None:
        result['worker_fsync'] = {'before_wait': before, 'at_timeout': probe.snapshot()}
    if sqlite_probe is not None:
        result['worker_sqlite'] = {'before_wait': sqlite_before, 'at_timeout': sqlite_probe.snapshot()}
    return result


def require_child_ready(event, tasks, probe, emit, *, sqlite_probe=None, timeout=5):
    """Emit before teardown; lock diagnostics keep their five-second default."""
    before = probe.snapshot()
    sqlite_before = sqlite_probe.snapshot() if sqlite_probe is not None else None
    ready = event.wait(timeout)
    if not ready:
        emit(json.dumps({'event': 'owner_wait_failed',
                         'diagnostic': wait_diagnostic(tasks, probe, before,
                             sqlite_probe=sqlite_probe, sqlite_before=sqlite_before)}))
    assert ready, f'child fixture did not enter transport after {timeout:g}s'

@contextmanager
def hold_reader_through_worker_commit(path, worker, *, seconds=6.5):
    """Hold a real reader until seconds after the worker enters ledger COMMIT."""
    ready, committed, release = threading.Event(), threading.Event(), threading.Event()
    deadline = []
    errors = []
    previous = threading.getprofile()
    enabled = True

    def reader():
        try:
            with closing(sqlite3.connect(path)) as holder:
                holder.execute('BEGIN')
                holder.execute('SELECT COUNT(*) FROM visits').fetchone()
                ready.set()
                # No countdown runs during task construction or worker startup.
                while not committed.is_set() and not release.wait(.02):
                    pass
                if committed.is_set():
                    release.wait(max(0.0, deadline[0] - time.monotonic()))
                holder.rollback()
        except Exception as error:
            errors.append(type(error).__name__)
        finally:
            ready.set()

    def profile(frame, event, function):
        if previous is not None:
            previous(frame, event, function)
        if (enabled and event == 'c_call' and not committed.is_set()
                and threading.current_thread() is worker()
                and getattr(function, '__name__', '') == 'commit'
                and isinstance(getattr(function, '__self__', None), sqlite3.Connection)
                and Path(frame.f_code.co_filename).name == 'rate.py'):
            deadline.append(time.monotonic() + seconds)
            committed.set()

    holder_thread = threading.Thread(target=reader, daemon=True)
    holder_thread.start()
    try:
        assert ready.wait(30) and not errors, 'reader control did not become ready'
        threading.setprofile(profile)
        yield
        assert committed.is_set(), 'control did not observe a ledger commit'
    finally:
        enabled = False
        if threading.getprofile() is profile:
            threading.setprofile(previous)
        release.set()
        holder_thread.join(30)
        assert not holder_thread.is_alive(), 'reader control did not stop'
        assert not errors, 'reader control failed: ' + ','.join(errors)
