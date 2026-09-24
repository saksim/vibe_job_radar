"""Test-only timing of the original worker's durable writes; no file metadata."""
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
        self.active_since = None
        self.replacement = patch.object(os, 'fsync', self.call)

    def start(self):
        self.replacement.start()

    def stop(self):
        self.replacement.stop()

    def call(self, descriptor):
        if threading.current_thread() is not self.worker():
            return self.original(descriptor)
        begin = self.clock()
        with self.lock:
            self.started += 1
            self.active_since = begin
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
                self.active_since = None

    def snapshot(self):
        with self.lock:
            active = self.active_since
            return {'calls_started': self.started, 'calls_completed': self.completed,
                    'failed_calls': self.failed, 'completed_total_ms': round(self.total * 1000, 3),
                    'completed_max_ms': round(self.maximum * 1000, 3),
                    'current_call_ms': None if active is None else round((self.clock() - active) * 1000, 3)}
