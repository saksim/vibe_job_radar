"""Real SQLite blocking/error controls for bounded worker-only observation."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest

from worker_sqlite_probe import WorkerSqliteProbe


class WorkerSqliteProbeTests(unittest.TestCase):
    def test_pending_real_lock_and_durable_result_are_both_observed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'private-database.sqlite'
            with closing(sqlite3.connect(path)) as holder:
                holder.execute('CREATE TABLE private_table (value TEXT)')
                holder.commit()
                holder.execute('BEGIN IMMEDIATE')
                errors = []
                clock = [10.0]
                def work():
                    try:
                        with closing(sqlite3.connect(path, timeout=5)) as connection:
                            connection.execute('BEGIN IMMEDIATE')
                            connection.execute('INSERT INTO private_table VALUES (?)', ('PRIVATE_VALUE',))
                            connection.commit()
                    except Exception as error:
                        errors.append(error)
                worker = threading.Thread(target=work)
                probe = WorkerSqliteProbe(lambda: worker, clock=lambda: clock[0])
                probe.start()
                try:
                    worker.start()
                    deadline = time.monotonic() + 3
                    pending = probe.snapshot()
                    while time.monotonic() < deadline:
                        pending = probe.snapshot()
                        if any(call['operation'] == 'connection.execute' for call in pending['current_calls']):
                            break
                        time.sleep(.005)
                    clock[0] = 12.5
                    pending = probe.snapshot()
                    self.assertEqual(len(pending['current_calls']), 1)
                    current = pending['current_calls'][0]
                    self.assertEqual(current['operation'], 'connection.execute')
                    self.assertEqual(current['elapsed_ms'], 2500)
                    self.assertEqual(current['caller']['function'], 'work')
                    self.assertEqual(pending['operations']['connection.execute']['calls_completed'], 0)
                finally:
                    holder.rollback()
                    worker.join(6)
                    probe.stop()
                self.assertFalse(worker.is_alive())
                self.assertEqual(errors, [])
                self.assertEqual(holder.execute('SELECT value FROM private_table').fetchall(), [('PRIVATE_VALUE',)])
                result = probe.snapshot()
                self.assertEqual(result['operations']['connection.execute']['calls_started'], 2)
                self.assertEqual(result['operations']['connection.commit']['calls_completed'], 1)
                self.assertEqual(result['operations']['connection.execute']['completed_max_ms'], 2500)
                self.assertEqual(result['current_calls'], [])
                for secret in (str(path), 'private_table', 'PRIVATE_VALUE', 'BEGIN IMMEDIATE'):
                    self.assertNotIn(secret, json.dumps([pending, result]))

    def test_sqlite_failure_propagates_and_other_threads_are_excluded(self):
        errors = []
        def work():
            with closing(sqlite3.connect(':memory:')) as connection:
                try:
                    connection.execute('SELECT * FROM PRIVATE_MISSING_TABLE')
                except sqlite3.OperationalError as error:
                    errors.append(error)
        worker = threading.Thread(target=work)
        probe = WorkerSqliteProbe(lambda: worker)
        original_connect = sqlite3.connect
        probe.start()
        try:
            other = threading.Thread(target=work)
            other.start(); other.join(5)
            self.assertFalse(other.is_alive())
            self.assertEqual(probe.snapshot()['operations'], {})
            worker.start(); worker.join(5)
            self.assertFalse(worker.is_alive())
        finally:
            probe.stop()
        self.assertIs(sqlite3.connect, original_connect)
        self.assertEqual(len(errors), 2)
        self.assertTrue(all(isinstance(error, sqlite3.OperationalError) for error in errors))
        self.assertIn('PRIVATE_MISSING_TABLE', str(errors[1]))
        result = probe.snapshot()
        self.assertEqual(result['operations']['connection.execute']['failed_calls'], 1)
        self.assertEqual(result['operations']['connection.execute']['calls_completed'], 1)
        self.assertEqual(result['operations']['connection.close']['calls_completed'], 1)
        self.assertNotIn('PRIVATE_', json.dumps(result))
        self.assertEqual(result['current_calls'], [])

    def test_existing_profiler_and_actual_cursor_returns_are_preserved(self):
        previous = threading.getprofile()
        observed = []
        results = []
        def profiler(frame, event, function):
            if event == 'c_call' and function is sqlite3.connect:
                observed.append(event)
        def work():
            with closing(sqlite3.connect(':memory:')) as connection:
                results.append(connection.execute('SELECT 17').fetchone())
                results.append(connection.execute('SELECT 19').fetchall())
        worker = threading.Thread(target=work)
        threading.setprofile(profiler)
        probe = WorkerSqliteProbe(lambda: worker)
        try:
            probe.start()
            worker.start(); worker.join(5)
            self.assertFalse(worker.is_alive())
            probe.stop()
            self.assertIs(threading.getprofile(), profiler)
        finally:
            probe.stop()
            threading.setprofile(previous)
        self.assertEqual(observed, ['c_call'])
        self.assertEqual(results, [(17,), [(19,)]])
        result = probe.snapshot()
        self.assertEqual(result['operations']['cursor.fetchone']['calls_completed'], 1)
        self.assertEqual(result['operations']['cursor.fetchall']['calls_completed'], 1)
        self.assertEqual(result['nested_calls_omitted'], 0)


    def test_read_retry_timeout_records_pending_sqlite_before_cleanup(self):
        import test_read_retry as retry_tests
        case = retry_tests.ReadRetryWorkerTests()
        case.setUp()
        holder = sqlite3.connect(case.ledger.path)
        try:
            holder.execute('BEGIN IMMEDIATE')
            case.failures = 1
            case.create()
            with self.assertRaises(AssertionError) as caught:
                case.wait('read_retry_wait')
            message = str(caught.exception)
            self.assertIn('after 5s: ', message)
            evidence = json.loads(message.split('after 5s: ', 1)[1])
            self.assertTrue(evidence['worker_alive'])
            pending = evidence['worker_sqlite']['at_timeout']['current_calls']
            self.assertEqual(len(pending), 1)
            self.assertEqual(pending[0]['operation'], 'connection.execute')
            self.assertEqual(pending[0]['caller']['file'], 'rate.py')
            self.assertEqual(pending[0]['caller']['function'], 'reserve')
            self.assertGreaterEqual(pending[0]['elapsed_ms'], 4000)
            self.assertEqual(evidence['worker_sqlite']['at_timeout']['nested_calls_omitted'], 0)
            self.assertIn('worker_fsync', evidence)
            self.assertNotIn(str(case.ledger.path), message)
        finally:
            holder.rollback()
            holder.close()
            case.doCleanups()

    def test_second_retry_wait_retains_pending_sqlite_and_original_budget(self):
        import test_read_retry as retry_tests
        case = retry_tests.ReadRetryWorkerTests()
        case.setUp()
        holder = None
        try:
            case.failures = 9
            case.create()
            first = case.wait('read_retry_wait')
            self.assertEqual(first['read_retry']['used'], 1)
            holder = sqlite3.connect(case.ledger.path)
            holder.execute('BEGIN IMMEDIATE')
            case.now = 1031
            with self.assertRaises(AssertionError) as caught:
                case.wait('read_retry_wait', retry_used=2)
            message = str(caught.exception)
            self.assertIn('after 5s: ', message)
            evidence = json.loads(message.split('after 5s: ', 1)[1])
            self.assertEqual(evidence['expected_read_retry_used'], 2)
            self.assertEqual(evidence['observed_read_retry_used'], 1)
            self.assertTrue(evidence['worker_alive'])
            pending = evidence['worker_sqlite']['at_timeout']['current_calls']
            self.assertEqual(len(pending), 1)
            self.assertEqual(pending[0]['operation'], 'connection.execute')
            self.assertEqual(pending[0]['caller']['file'], 'rate.py')
            self.assertEqual(pending[0]['caller']['function'], 'reserve')
            self.assertGreaterEqual(pending[0]['elapsed_ms'], 4000)
            self.assertIn('worker_fsync', evidence)
            self.assertNotIn(str(case.ledger.path), message)
        finally:
            if holder is not None:
                holder.rollback()
                holder.close()
            case.doCleanups()
