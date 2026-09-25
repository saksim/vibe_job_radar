"""Test-only durable-write timing, including its owned report pool; no paths."""
import os
import threading
import time
from unittest.mock import patch


class WorkerFsyncProbe:
    def __init__(self, worker, clock=time.perf_counter):
        self.worker = worker
        self.clock = clock
        self.original = os.fsync
        self.lock = threading.Lock()
        self.started = self.completed = self.failed = 0
        self.total = self.maximum = 0.0
        self.active_since = {}
        self.replacement = patch.object(os, 'fsync', self.call)

    def start(self):
        self.replacement.start()

    def stop(self):
        self.replacement.stop()

    def call(self, descriptor):
        current, owner = threading.current_thread(), self.worker()
        if current is not owner and not (owner is not None and owner.ident is not None
                and current.name.startswith(f'radar-report-{owner.ident}_')):
            return self.original(descriptor)
        begin = self.clock()
        with self.lock:
            self.started += 1
            self.active_since[current.ident] = begin
        failed = True
        try:
            result = self.original(descriptor)
            failed = False
            return result
        finally:
            elapsed = self.clock() - begin
            with self.lock:
                self.completed += 1
                self.failed += int(failed)
                self.total += elapsed
                self.maximum = max(self.maximum, elapsed)
                self.active_since.pop(current.ident, None)

    def snapshot(self):
        with self.lock:
            # Completed totals can overlap across report writers. The pending
            # duration is the longest current call, never a wall-time sum.
            active = min(self.active_since.values(), default=None)
            return {'calls_started': self.started, 'calls_completed': self.completed,
                    'failed_calls': self.failed, 'completed_total_ms': round(self.total * 1000, 3),
                    'completed_max_ms': round(self.maximum * 1000, 3),
                    'current_call_ms': None if active is None else round((self.clock() - active) * 1000, 3)}
