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
                case.wait('read_retry_wait', timeout=5)
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
            # The first action only prepares the saved retry for this probe.
            # Wait for its real durable writes and task_done before measuring
            # the second, deliberately blocked action with the original 5s.
            # Functional state waits have a separate durable-write allowance.
            queue = case.service._queue
            with queue.all_tasks_done:
                self.assertTrue(queue.all_tasks_done.wait_for(
                    lambda: queue.unfinished_tasks == 0, timeout=10),
                    'first retry diagnostic fixture preparation did not finish')
            first = case.wait('read_retry_wait')
            self.assertEqual(first['read_retry']['used'], 1)
            holder = sqlite3.connect(case.ledger.path)
            holder.execute('BEGIN IMMEDIATE')
            case.now = 1031
            with self.assertRaises(AssertionError) as caught:
                case.wait('read_retry_wait', retry_used=2, timeout=5)
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


    def test_shutdown_read_wait_records_scheduler_insert_before_cleanup(self):
        import test_public_worker as worker_tests
        from unittest.mock import patch
        case = worker_tests.WorkerTests()
        case.setUp()
        release = threading.Event()
        blocked = threading.Event()
        connect = sqlite3.connect
        previous = threading.getprofile()

        def observe_connection(*args, **kwargs):
            connection = connect(*args, **kwargs)
            if threading.current_thread() is case.worker.schedule._thread:
                def authorizer(action, table, column, database, trigger):
                    # Hold an actual schedule INSERT after it has created the
                    # task, while its dispatch lock still excludes the reader.
                    if action == sqlite3.SQLITE_INSERT and table == 'schedule' and case.worker.tasks._thread:
                        blocked.set()
                        if not release.wait(15):
                            return sqlite3.SQLITE_DENY
                    return sqlite3.SQLITE_OK
                connection.set_authorizer(authorizer)
            return connection

        with patch('sqlite3.connect', side_effect=observe_connection):
            try:
                with self.assertRaises(AssertionError) as caught:
                    # This control intentionally times out while SQLite is
                    # blocked; it retains the original five-second observation.
                    case.test_shutdown_during_read_preserves_checkpoint_without_replaying(preparation_timeout=5)
                self.assertTrue(blocked.is_set())
                message = str(caught.exception)
                evidence = json.loads(message.split(' : ', 1)[1])
                pending = evidence['schedule_sqlite']['current_calls']
                self.assertEqual(len(pending), 1)
                self.assertEqual(pending[0]['operation'], 'connection.execute')
                self.assertEqual(pending[0]['caller']['file'], 'public_schedule.py')
                self.assertEqual(pending[0]['caller']['function'], '_write')
                self.assertGreaterEqual(pending[0]['elapsed_ms'], 4000)
                self.assertEqual(evidence['task_sqlite']['current_calls'], [])
                self.assertEqual(evidence['task_sqlite']['operations'], {})
                self.assertTrue(case.worker.tasks._thread.is_alive())
                case.wire.json.assert_not_called()
                for private in (str(case.workspace.root), 'INSERT', 'Architect', 'https://'):
                    self.assertNotIn(private, message)
            finally:
                release.set()
                case.doCleanups()
        self.assertIs(threading.getprofile(), previous)
        self.assertFalse(case.worker.tasks.is_running())
        self.assertFalse(case.worker.schedule.is_running())
        self.assertFalse(case.thread.is_alive())
