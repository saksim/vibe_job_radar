"""Test-only, nonblocking owner wait facts; never read task files or secrets."""
import json
from pathlib import Path
import sys
import threading
import traceback


def wait_diagnostic(tasks, probe=None, before=None):
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
    return result


def require_child_ready(event, tasks, probe, emit):
    """Emit before teardown; preserve the existing five-second assertion."""
    before = probe.snapshot()
    ready = event.wait(5)
    if not ready:
        emit(json.dumps({'event': 'owner_wait_failed',
                         'diagnostic': wait_diagnostic(tasks, probe, before)}))
    assert ready, 'child fixture did not enter transport after 5s'
