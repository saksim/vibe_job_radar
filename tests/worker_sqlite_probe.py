"""Test-only SQLite C-call timing; no SQL, arguments, results or error text."""
from pathlib import Path
import sqlite3
import threading
import time


class WorkerSqliteProbe:
    """Observe future worker threads without replacing any SQLite operation."""

    def __init__(self, worker, clock=time.perf_counter):
        self.worker, self.clock = worker, clock
        self.lock = threading.Lock()
        self.operations = {}
        self.active = []
        self.omitted_depth = self.omitted_calls = 0
        self.previous = None
        self.enabled = False
        self.callback = self._profile

    def start(self):
        if self.enabled:
            raise RuntimeError('probe already started')
        self.previous = threading.getprofile()
        self.enabled = True
        threading.setprofile(self.callback)

    def stop(self):
        self.enabled = False
        if threading.getprofile() is self.callback:
            threading.setprofile(self.previous)

    @staticmethod
    def _operation(function):
        if function is sqlite3.connect:
            return 'connect'
        owner = getattr(function, '__self__', None)
        name = getattr(function, '__name__', '')
        if isinstance(owner, sqlite3.Connection) and name in {
                'execute', 'executemany', 'executescript', 'commit', 'rollback', 'close'}:
            return 'connection.' + name
        if isinstance(owner, sqlite3.Cursor) and name in {'fetchone', 'fetchmany', 'fetchall', 'close'}:
            return 'cursor.' + name
        return None

    def _profile(self, frame, event, function):
        if self.previous is not None:
            self.previous(frame, event, function)
        if (not self.enabled or event not in {'c_call', 'c_return', 'c_exception'}
                or threading.current_thread() is not self.worker()):
            return
        operation = self._operation(function)
        if operation is None:
            return
        now = self.clock()
        with self.lock:
            if event == 'c_call':
                if len(self.active) >= 16 or self.omitted_depth:
                    self.omitted_depth += 1
                    self.omitted_calls += 1
                    return
                facts = {'file': Path(frame.f_code.co_filename).name,
                         'line': frame.f_lineno, 'function': frame.f_code.co_name}
                self.active.append((operation, now, facts))
                stats = self.operations.setdefault(operation, [0, 0, 0, 0.0, 0.0])
                stats[0] += 1
                return
            if self.omitted_depth:
                self.omitted_depth -= 1
                return
            if not self.active:
                return
            label, begin, _ = self.active.pop()
            elapsed = now - begin
            stats = self.operations[label]
            stats[1] += 1
            stats[2] += int(event == 'c_exception')
            stats[3] += elapsed
            stats[4] = max(stats[4], elapsed)

    def snapshot(self):
        with self.lock:
            now = self.clock()
            operations = {
                name: {'calls_started': item[0], 'calls_completed': item[1],
                       'failed_calls': item[2], 'completed_total_ms': round(item[3] * 1000, 3),
                       'completed_max_ms': round(item[4] * 1000, 3)}
                for name, item in self.operations.items()}
            current = [{'operation': name, 'elapsed_ms': round((now - begin) * 1000, 3),
                        'caller': dict(caller)} for name, begin, caller in self.active]
            # Nested calls may overlap; totals are never a wall-time estimate.
            return {'operations': operations, 'current_calls': current,
                    'nested_calls_omitted': self.omitted_calls}