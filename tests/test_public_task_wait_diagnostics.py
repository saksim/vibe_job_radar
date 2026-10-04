"""Bounded failure observations preserve the original public-worker join and I/O."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import public_task_wait_diagnostics as diagnostics
import test_public_task_lifecycle as lifecycle


class PublicWaitDiagnosticTests(unittest.TestCase):
    def test_real_fsync_is_called_once_and_not_replaced_by_a_counter(self):
        with tempfile.TemporaryFile() as file, patch.object(os, 'fsync', wraps=os.fsync) as original:
            file.write(b'artificial persistence probe');file.flush()
            probe = diagnostics.WaitFsyncProbe(threading.current_thread)
            with probe:
                self.assertIsNone(os.fsync(file.fileno()))
            original.assert_called_once_with(file.fileno())
            row = probe.snapshot()
            self.assertEqual((row['calls_started'], row['calls_completed'], row['failed_calls'], row['in_flight_calls']), (1, 1, 0, 0))
            self.assertFalse(row['incomplete'])
            self.assertGreaterEqual(row['completed_max_ms'], 0)

    def test_unrelated_thread_calls_are_forwarded_without_observation(self):
        marker = object()
        with patch.object(os, 'fsync', return_value=marker) as original:
            probe = diagnostics.WaitFsyncProbe(lambda: None)
            with probe:
                self.assertIs(os.fsync(123), marker)
            original.assert_called_once_with(123)
            self.assertEqual(probe.snapshot()['calls_started'], 0)

    def test_inflight_duration_and_original_failure_are_kept_separate(self):
        entered, release = threading.Event(), threading.Event()
        now = [1.0];error = OSError('PRIVATE fixture error');caught = []
        def original(_):
            entered.set()
            if not release.wait(5):raise AssertionError('fixture not released')
            raise error
        def work():
            try:os.fsync(123)
            except OSError as exc:caught.append(exc)
        worker = threading.Thread(target=work)
        with patch.object(os, 'fsync', side_effect=original) as observed:
            probe = diagnostics.WaitFsyncProbe(lambda: worker, clock=lambda: now[0])
            with probe:
                worker.start()
                try:
                    self.assertTrue(entered.wait(5));now[0] = 2.0
                    row = probe.snapshot()
                    self.assertEqual((row['calls_completed'], row['in_flight_calls'], row['oldest_current_ms']), (0, 1, 1000.0))
                    self.assertEqual(row['completed_total_ms'], 0)
                finally:
                    now[0] = 3.0;release.set();worker.join(5)
            self.assertFalse(worker.is_alive());observed.assert_called_once_with(123)
            self.assertEqual(caught, [error]);row = probe.snapshot()
            self.assertEqual((row['calls_completed'], row['failed_calls'], row['completed_total_ms'], row['oldest_current_ms']), (1, 1, 2000.0, None))
            self.assertNotIn('PRIVATE', json.dumps(row))

    def test_only_owned_report_pool_is_observed_and_frames_are_bounded(self):
        owner = threading.current_thread();barrier = threading.Barrier(5);release = threading.Event()
        prefix = f'radar-report-{owner.ident}_'
        def original(_):
            if threading.current_thread().name.startswith(prefix):
                barrier.wait(5)
                if not release.wait(5):raise AssertionError('fixture not released')
        task = SimpleNamespace(_thread=owner, _state={'status':'running', 'phase':'saving'})
        with patch.object(os, 'fsync', side_effect=original):
            probe = diagnostics.WaitFsyncProbe(lambda: owner)
            with probe, ThreadPoolExecutor(max_workers=4, thread_name_prefix=prefix[:-1]) as pool:
                futures = [pool.submit(os.fsync, 123) for _ in range(4)]
                try:
                    barrier.wait(5)
                    foreign = threading.Thread(target=lambda: os.fsync(456), name='unrelated')
                    foreign.start();foreign.join(5);self.assertFalse(foreign.is_alive())
                    row = diagnostics.wait_diagnostic(task, probe, {}, 1.0)
                    self.assertEqual(row['worker_fsync']['at_timeout']['calls_started'], 4)
                    self.assertEqual(len(row['report_writers']), 4)
                    for stack in [row['worker_stack'], *row['report_writers']]:
                        self.assertLessEqual(len(stack), 24)
                        self.assertTrue(all(set(frame)=={'file','line','function'} and '/' not in frame['file'] and '\\' not in frame['file'] for frame in stack))
                finally:release.set()
                for future in futures:future.result()

    def test_excess_active_calls_are_bounded_and_explicitly_incomplete(self):
        owner = threading.current_thread();barrier = threading.Barrier(7);release = threading.Event()
        def original(_):
            barrier.wait(5)
            if not release.wait(5):raise AssertionError('fixture not released')
        with patch.object(os, 'fsync', side_effect=original) as observed:
            probe = diagnostics.WaitFsyncProbe(lambda: owner)
            workers = [threading.Thread(target=lambda: os.fsync(123), name=f'radar-report-{owner.ident}_{i}') for i in range(6)]
            with probe:
                for worker in workers:worker.start()
                try:
                    barrier.wait(5);row = probe.snapshot()
                    self.assertEqual(row['in_flight_calls'], 5);self.assertTrue(row['incomplete'])
                finally:
                    release.set()
                    for worker in workers:worker.join(5)
            self.assertEqual(observed.call_count, 6)
            self.assertTrue(all(not worker.is_alive() for worker in workers))

    def test_snapshot_never_waits_for_the_probe_lock(self):
        probe = diagnostics.WaitFsyncProbe(lambda: None)
        with probe.lock:
            self.assertEqual(probe.snapshot(), {'available':False})

    def test_actual_original_wait_preserves_timeout_and_filters_private_state(self):
        worker = Mock();worker.ident = None;worker.is_alive.return_value = True
        task = SimpleNamespace(_thread=worker, _state={'status':'running','phase':{'PRIVATE':'BODY'},'query':'PRIVATE','message':'PRIVATE','source_url':'https://private.invalid'}, _lock=Mock(), state=Mock(side_effect=AssertionError('must not read task')), snapshot=Mock(side_effect=AssertionError('must not lock task')))
        case = lifecycle.PublicLifecycleTests('test_restart_is_offline_and_requires_confirmation_of_saved_query')
        with self.assertRaises(AssertionError) as caught:case.wait(task)
        worker.join.assert_called_once_with(timeout=10)
        text = str(caught.exception);self.assertIn('PUBLIC_TASK_WAIT ', text)
        row = json.loads(text.split('PUBLIC_TASK_WAIT ', 1)[1])
        self.assertEqual(row['task'], {'status':'running','phase':'unknown'})
        self.assertTrue(row['in_memory_state_only']);self.assertIn('at_timeout', row['worker_fsync'])
        self.assertNotIn('PRIVATE', text);self.assertNotIn('private.invalid', text)
        task.state.assert_not_called();task.snapshot.assert_not_called();task._lock.acquire.assert_not_called()
        class ChangingState(dict):
            reads = 0
            def get(self, key, default=None):
                if key == 'status':
                    self.reads += 1
                    return 'running' if self.reads <= 2 else 'PRIVATE_RACE'
                return super().get(key, default)
        task._state = ChangingState(status='running', phase='saving')
        row = diagnostics.wait_diagnostic(task, diagnostics.WaitFsyncProbe(lambda: worker), {}, 0)
        self.assertEqual(task._state.reads, 1)
        self.assertEqual(row['task']['status'], 'running')
        self.assertNotIn('PRIVATE_RACE', json.dumps(row))

    def test_optional_observation_errors_preserve_io_and_failed_wait(self):
        with patch.object(os, 'fsync', return_value=7) as original:
            probe = diagnostics.WaitFsyncProbe(threading.current_thread, clock=Mock(side_effect=RuntimeError('PRIVATE')))
            with probe:self.assertEqual(os.fsync(123), 7)
            original.assert_called_once_with(123);self.assertTrue(probe.incomplete)
        worker = Mock();worker.ident = None;worker.is_alive.return_value = True
        with patch.object(diagnostics, 'wait_diagnostic', side_effect=ValueError('PRIVATE')):
            alive, message = diagnostics.join_observed(SimpleNamespace(_thread=worker), timeout=10)
        self.assertTrue(alive);self.assertIn('unavailable', message);self.assertNotIn('PRIVATE', message)
        worker.join.assert_called_once_with(timeout=10)
        worker.reset_mock()
        with patch.object(diagnostics.WaitFsyncProbe, '__enter__', side_effect=RuntimeError('PRIVATE')):
            alive, message = diagnostics.join_observed(SimpleNamespace(_thread=worker), timeout=10)
        self.assertTrue(alive);self.assertIn('capture_status', message)
        self.assertIn('unavailable', message);self.assertNotIn('at_timeout', message)
        worker.join.assert_called_once_with(timeout=10)

    def test_successful_wait_keeps_original_completion_and_no_failure_packet(self):
        worker = Mock();worker.is_alive.return_value = False
        with patch.object(diagnostics, 'wait_diagnostic', side_effect=AssertionError('must not observe success')) as capture:
            alive, message = diagnostics.join_observed(SimpleNamespace(_thread=worker), timeout=10)
        self.assertFalse(alive);self.assertEqual(message, '')
        worker.join.assert_called_once_with(timeout=10);capture.assert_not_called()


if __name__ == '__main__':
    unittest.main()
