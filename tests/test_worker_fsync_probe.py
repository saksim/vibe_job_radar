"""Deterministic diagnostic checks; the probe never replaces durable writes."""
import json
import os
import tempfile
import threading
import unittest
from unittest.mock import patch

from worker_fsync_probe import WorkerFsyncProbe


class WorkerFsyncProbeTests(unittest.TestCase):
    def test_pending_and_completed_durations_are_distinct_without_descriptor_or_path(self):
        entered = threading.Event()
        release = threading.Event()
        clock = [10.0]
        sentinel = object()
        results = []
        def held(descriptor):
            self.assertIs(descriptor, sentinel)
            entered.set()
            if not release.wait(5):
                raise AssertionError('test release missing')
            return 'same_return'
        worker = threading.Thread(target=lambda: results.append(os.fsync(sentinel)))
        with patch.object(os, 'fsync', held):
            probe = WorkerFsyncProbe(lambda: worker, clock=lambda: clock[0])
            probe.start()
            try:
                worker.start()
                self.assertTrue(entered.wait(5))
                clock[0] = 12.5
                pending = probe.snapshot()
                self.assertEqual(pending, {'calls_started': 1, 'calls_completed': 0, 'failed_calls': 0,
                                          'completed_total_ms': 0, 'completed_max_ms': 0, 'current_call_ms': 2500})
                release.set()
                worker.join(5)
                self.assertFalse(worker.is_alive())
                self.assertEqual(results, ['same_return'])
                self.assertEqual(probe.snapshot(), {'calls_started': 1, 'calls_completed': 1, 'failed_calls': 0,
                                                   'completed_total_ms': 2500, 'completed_max_ms': 2500, 'current_call_ms': None})
                self.assertNotIn('descriptor', json.dumps(pending))
                self.assertNotIn('path', json.dumps(pending))
            finally:
                release.set()
                worker.join(5)
                probe.stop()
            self.assertIs(os.fsync, held)

    def test_failure_propagates_unchanged_without_retaining_exception_text(self):
        clock = iter([1.0, 1.025])
        error = OSError('PRIVATE_PATH_OR_CONTENT')
        with patch.object(os, 'fsync', side_effect=error) as original:
            selected = threading.current_thread()
            probe = WorkerFsyncProbe(lambda: selected, clock=lambda: next(clock))
            probe.start()
            try:
                with self.assertRaises(OSError) as caught:
                    os.fsync(123)
                self.assertIs(caught.exception, error)
                original.assert_called_once_with(123)
                evidence = probe.snapshot()
                self.assertEqual(evidence['failed_calls'], 1)
                self.assertEqual(evidence['completed_total_ms'], 25)
                self.assertIsNone(evidence['current_call_ms'])
                self.assertNotIn('PRIVATE_', json.dumps(evidence))
            finally:
                probe.stop()

    def test_original_real_write_runs_and_other_thread_is_not_counted(self):
        selected = threading.current_thread()
        original = os.fsync
        probe = WorkerFsyncProbe(lambda: selected)
        probe.start()
        try:
            with tempfile.TemporaryFile() as file:
                file.write(b'artificial persistence check')
                file.flush()
                self.assertIsNone(os.fsync(file.fileno()))
                errors = []
                def other():
                    try:
                        os.fsync(file.fileno())
                    except Exception as exc:
                        errors.append(type(exc).__name__)
                thread = threading.Thread(target=other)
                thread.start()
                thread.join(5)
                self.assertFalse(thread.is_alive())
                self.assertEqual(errors, [])
                self.assertEqual(probe.snapshot()['calls_completed'], 1)
                file.seek(0)
                self.assertEqual(file.read(), b'artificial persistence check')
        finally:
            probe.stop()
        self.assertIs(os.fsync, original)
