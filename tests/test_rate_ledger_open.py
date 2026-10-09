"""Real temporary SQLite files: constructor reads versus required writes."""
from contextlib import closing
import hashlib
from pathlib import Path
import queue
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from vibe_job_radar.guided.contracts import CrawlError
from vibe_job_radar.guided.rate import RateLedger, Limits, RateLimit


class LedgerOpenTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)/'rates.sqlite'
        self.now = 1_000_000.0
        self.limits = Limits(request_interval=0)
        self.real_connect = sqlite3.connect

    def ledger(self):
        return RateLedger(self.path, self.limits, clock=lambda: self.now)

    def trace(self, statements):
        def connect(*args, **kwargs):
            conn = self.real_connect(*args, **kwargs)
            conn.set_trace_callback(statements.append)
            return conn
        return patch('sqlite3.connect', side_effect=connect)

    def snapshot(self):
        with closing(self.real_connect(self.path)) as conn:
            return list(conn.iterdump()), conn.execute('PRAGMA user_version').fetchone()[0]

    def test_existing_complete_open_only_reads_and_preserves_database_bytes(self):
        first = self.ledger()
        first.reserve('liepin', 'request')
        first.cool('liepin', 600)
        before = self.snapshot()
        digest = hashlib.sha256(self.path.read_bytes()).hexdigest()
        statements = []
        with self.trace(statements):
            for _ in range(3): self.ledger()
        self.assertEqual(len(statements), 3)
        self.assertTrue(all(sql.startswith('SELECT ') for sql in statements))
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(hashlib.sha256(self.path.read_bytes()).hexdigest(), digest)

    def test_complete_open_finishes_while_another_connection_holds_writer_reservation(self):
        self.ledger().reserve('liepin', 'request')
        done = threading.Event(); errors = []
        def open_again():
            try: self.ledger()
            except BaseException as exc: errors.append(exc)
            finally: done.set()
        with closing(self.real_connect(self.path)) as writer:
            writer.execute('BEGIN IMMEDIATE')
            thread = threading.Thread(target=open_again)
            thread.start()
            try:
                self.assertTrue(done.wait(5), 'Read-only open waited for the unrelated writer')
                self.assertTrue(writer.in_transaction)
                self.assertEqual(self.ledger().summary('liepin')['request']['day'], 1)
            finally:
                writer.rollback(); thread.join(5)
        self.assertFalse(thread.is_alive()); self.assertEqual(errors, [])

    def test_complete_open_works_with_a_real_other_process_holding_the_write_lock(self):
        self.ledger().reserve('liepin', 'request')
        script = (
            "import sqlite3,sys; c=sqlite3.connect(sys.argv[1]); "
            "c.execute('BEGIN IMMEDIATE'); print('READY',flush=True); "
            "sys.stdin.readline(); c.rollback(); c.close()")
        child = subprocess.Popen([sys.executable, '-c', script, str(self.path)],
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, text=True)
        ready = queue.Queue()
        reader = threading.Thread(target=lambda: ready.put(child.stdout.readline()))
        reader.start()
        try:
            self.assertEqual(ready.get(timeout=5).strip(), 'READY')
            reader.join(5)
            self.assertIsNone(child.poll())
            # The constructor has its original 10s SQLite timeout. It must
            # succeed while the child still owns the write reservation.
            current = self.ledger()
            self.assertIsNone(child.poll())
            self.assertEqual(current.summary('liepin')['request']['day'], 1)
        finally:
            try: child.communicate(input='RELEASE\n', timeout=5)
            except subprocess.TimeoutExpired:
                child.kill(); child.communicate(timeout=5)
            reader.join(5)
        self.assertEqual(child.returncode, 0)
        self.assertFalse(reader.is_alive())

    def test_fresh_creation_keeps_one_atomic_writer_transaction(self):
        statements = []
        with self.trace(statements): self.ledger()
        self.assertEqual(statements.count('BEGIN IMMEDIATE'), 1)
        self.assertEqual(statements.count('COMMIT'), 1)
        self.assertEqual(sum(sql.startswith('CREATE ') for sql in statements), 8)
        self.assertEqual(self.snapshot()[1], 0)  # No new durable version marker.
        self.assertEqual(self.ledger().summary('liepin')['request']['day'], 0)

    def test_legacy_schema_is_extended_without_changing_visits_or_cooldown(self):
        with closing(self.real_connect(self.path)) as conn:
            conn.execute('CREATE TABLE visits (site TEXT, kind TEXT, ts REAL)')
            conn.execute('CREATE TABLE cooldown (site TEXT PRIMARY KEY, until REAL)')
            conn.execute('INSERT INTO visits VALUES (?,?,?)', ('liepin', 'login', self.now))
            conn.execute('INSERT INTO cooldown VALUES (?,?)', ('liepin', self.now+900))
            conn.commit()
        statements = []
        with self.trace(statements): ledger = self.ledger()
        self.assertEqual(statements.count('BEGIN IMMEDIATE'), 1)
        self.assertEqual(statements.count('COMMIT'), 1)
        self.assertEqual(ledger.summary('liepin')['login']['day'], 1)
        with self.assertRaises(RateLimit) as caught: ledger.reserve('liepin', 'request')
        self.assertEqual(caught.exception.code, 'cooldown')
        self.assertEqual(caught.exception.wait, 900)
        statements = []
        with self.trace(statements): self.ledger()
        self.assertTrue(all(sql.startswith('SELECT ') for sql in statements))

    def test_missing_index_is_restored_and_no_readiness_result_is_cached(self):
        self.ledger().reserve('liepin', 'request')
        with closing(self.real_connect(self.path)) as conn:
            conn.execute('DROP INDEX visits_scope'); conn.commit()
        statements = []
        with self.trace(statements): ledger = self.ledger()
        self.assertEqual(statements.count('BEGIN IMMEDIATE'), 1)
        with closing(self.real_connect(self.path)) as conn:
            self.assertIsNotNone(conn.execute(
                "SELECT sql FROM sqlite_master WHERE name='visits_scope'").fetchone())
        self.assertEqual(ledger.summary('liepin')['request']['day'], 1)

    def test_equivalent_but_unrecognized_schema_keeps_original_initialization_path(self):
        self.ledger()
        with closing(self.real_connect(self.path)) as conn:
            conn.execute('DROP TABLE clock_seen')
            conn.execute('CREATE TABLE clock_seen(site TEXT PRIMARY KEY, ts REAL)')
            conn.commit()
        before = self.snapshot(); statements = []
        with self.trace(statements): self.ledger()
        self.assertEqual(statements.count('BEGIN IMMEDIATE'), 1)
        self.assertEqual(statements.count('COMMIT'), 1)
        self.assertEqual(self.snapshot(), before)

    def test_failed_initialization_rolls_back_all_prior_ddl_and_closes_connection(self):
        class BrokenConnection(sqlite3.Connection):
            def execute(conn, sql, *args, **kwargs):
                if sql.startswith('CREATE TABLE IF NOT EXISTS publisher_policy'):
                    raise sqlite3.OperationalError('Artificial initialization failure')
                return super().execute(sql, *args, **kwargs)
        def connect(*args, **kwargs):
            return self.real_connect(*args, factory=BrokenConnection, **kwargs)
        with patch('sqlite3.connect', side_effect=connect), self.assertRaisesRegex(CrawlError, 'rate_storage_error'):
            self.ledger()
        with closing(self.real_connect(self.path)) as conn:
            self.assertEqual(conn.execute('SELECT name FROM sqlite_master').fetchall(), [])
        self.ledger().reserve('liepin', 'request')
        self.assertEqual(self.ledger().summary('liepin')['request']['day'], 1)

    def test_two_fresh_initializers_and_reservations_share_one_daily_budget(self):
        barrier = threading.Barrier(3); errors = []
        limits = Limits(request_interval=0, requests_hour=2, requests_day=2)
        def reserve():
            try:
                barrier.wait(timeout=5)
                RateLedger(self.path, limits, clock=lambda: self.now).reserve('liepin', 'request')
            except BaseException as exc: errors.append(exc)
        workers = [threading.Thread(target=reserve) for _ in range(2)]
        for worker in workers: worker.start()
        try: barrier.wait(timeout=5)
        finally:
            for worker in workers: worker.join(10)
        self.assertTrue(all(not worker.is_alive() for worker in workers))
        self.assertEqual(errors, [])
        ledger = RateLedger(self.path, limits, clock=lambda: self.now)
        self.assertEqual(ledger.summary('liepin')['request']['day'], 2)
        with self.assertRaises(RateLimit) as caught: ledger.reserve('liepin', 'request')
        self.assertEqual(caught.exception.code, 'daily_limit')

    def test_actual_mutations_keep_write_transactions_and_durable_restart_effects(self):
        ledger = self.ledger()
        mutations = [
            lambda: ledger.reserve('liepin', 'request'),
            lambda: ledger.set_publisher('liepin', 'https://www.liepin.com', delay=61, requests=1, seconds=120),
            lambda: ledger.defer('liepin', 30),
            lambda: ledger.cool('liepin', 600),
        ]
        for mutate in mutations:
            with self.subTest(operation=mutate):
                statements = []
                with self.trace(statements): mutate()
                self.assertEqual(statements.count('BEGIN IMMEDIATE'), 1)
                self.assertEqual(statements.count('COMMIT'), 1)
        reopened = self.ledger()
        self.assertEqual(reopened.summary('liepin')['request']['day'], 1)
        with self.assertRaises(RateLimit) as caught: reopened.reserve('liepin', 'login')
        self.assertEqual(caught.exception.code, 'cooldown')
        self.assertEqual(caught.exception.wait, 600)

    def test_publisher_history_and_clock_rollback_are_not_refreshed_by_reopening(self):
        origin = 'https://www.liepin.com'
        ledger = self.ledger()
        ledger.reserve('liepin', 'request', origin=origin)
        ledger.set_publisher('liepin', origin, delay=61, requests=1, seconds=120)
        with self.assertRaises(RateLimit) as caught: self.ledger().reserve('liepin', 'request', origin=origin)
        self.assertEqual(caught.exception.code, 'publisher_wait')
        self.assertEqual(caught.exception.wait, 120)
        before = self.snapshot(); self.now -= 2
        with self.assertRaises(RateLimit) as caught: self.ledger().reserve('liepin', 'request')
        self.assertEqual(caught.exception.code, 'clock_rollback')
        self.assertEqual(self.snapshot(), before)

    def test_missing_database_during_active_use_is_not_recreated(self):
        ledger = self.ledger()
        self.path.unlink()
        with self.assertRaisesRegex(CrawlError, 'rate_storage_error'):
            ledger.reserve('liepin', 'request')
        self.assertFalse(self.path.exists())

    def test_corrupt_database_stays_failed_without_replacement(self):
        raw = b'Artificial invalid database'
        self.path.write_bytes(raw)
        with self.assertRaisesRegex(CrawlError, 'rate_storage_error'): self.ledger()
        self.assertEqual(self.path.read_bytes(), raw)

    def test_symlink_refusal_still_precedes_any_sqlite_open(self):
        with patch.object(Path, 'is_symlink', return_value=True), \
             patch('sqlite3.connect') as connect, \
             self.assertRaisesRegex(CrawlError, 'unsafe_workspace'):
            self.ledger()
        connect.assert_not_called()


    def test_repeated_or_weaker_publisher_rules_preserve_database_bytes(self):
        ledger = self.ledger(); origin = 'https://jobs.fixture.test'
        ledger.set_publisher('fixture', origin, delay=7, requests=2, seconds=30)
        ledger.reserve('fixture', 'request', origin=origin)
        before = self.snapshot(); digest = hashlib.sha256(self.path.read_bytes()).hexdigest()
        statements = []
        with self.trace(statements):
            for delay in (7, 0, 3):
                ledger.set_publisher('fixture', origin.upper().replace('HTTPS:', 'https:')+'/',
                                     delay=delay, requests=2, seconds=30)
                ledger.set_publisher('fixture', origin, delay=delay)
        self.assertTrue(all(sql.startswith('SELECT ') for sql in statements))
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(hashlib.sha256(self.path.read_bytes()).hexdigest(), digest)
        with self.assertRaises(RateLimit) as caught:
            self.ledger().reserve('fixture', 'request', origin=origin)
        self.assertEqual(caught.exception.wait, 7)

    def test_cached_bridge_and_native_rules_finish_while_another_process_holds_writer(self):
        from test_guided import fixture_adapter
        from vibe_job_radar.guided.native_browser import NativeControl
        from vibe_job_radar.guided.transport import PinnedTransport, WireResponse
        ledger = self.ledger(); origin = 'https://jobs.fixture.test'
        body = b'User-agent: *\nAllow: /\nCrawl-delay: 7\nRequest-rate: 2/30\n'
        native = NativeControl(fixture_adapter(), ledger, threading.Event())
        native.install_robots(origin, 200, 'text/plain', body)
        bridge = PinnedTransport(fixture_adapter(), ledger, threading.Event())
        with patch.object(bridge, 'fetch', return_value=WireResponse(200, {'content-type':'text/plain'}, body)):
            bridge.ensure_robots(origin+'/job/1')
        script = (
            "import sqlite3,sys; c=sqlite3.connect(sys.argv[1]); "
            "c.execute('BEGIN IMMEDIATE'); print('READY',flush=True); "
            "sys.stdin.readline(); c.rollback(); c.close()")
        child = subprocess.Popen([sys.executable, '-c', script, str(self.path)],
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, text=True)
        ready = queue.Queue()
        reader = threading.Thread(target=lambda: ready.put(child.stdout.readline()))
        reader.start()
        try:
            self.assertEqual(ready.get(timeout=5).strip(), 'READY')
            reader.join(5)
            # Real original 10s SQLite timeout: unchanged rules must not need
            # the held writer reservation, through either production transport.
            with patch.object(bridge, 'fetch', side_effect=AssertionError('cached robots')):
                bridge.ensure_robots(origin+'/job/2')
            native.install_robots(origin, 200, 'text/plain', body)
            self.assertIsNone(child.poll())
        finally:
            try: child.communicate(input='RELEASE\n', timeout=5)
            except subprocess.TimeoutExpired:
                child.kill(); child.communicate(timeout=5)
            reader.join(5)
        self.assertEqual(child.returncode, 0)
        self.assertFalse(reader.is_alive())

    def test_stricter_new_window_and_new_scope_still_commit_and_survive_restart(self):
        ledger = self.ledger(); origin = 'https://jobs.fixture.test'
        ledger.set_publisher('fixture', origin, delay=7, requests=2, seconds=30)
        updates = [
            ('fixture', origin, dict(delay=9, requests=2, seconds=30)),
            ('fixture', origin, dict(delay=1, requests=1, seconds=60)),
            ('another', origin, dict(delay=1, requests=1, seconds=60)),
            ('fixture', 'https://other.fixture.test', dict(delay=1, requests=1, seconds=60)),
        ]
        for site, target, rule in updates:
            statements = []
            with self.trace(statements): ledger.set_publisher(site, target, **rule)
            self.assertEqual(statements.count('BEGIN IMMEDIATE'), 1)
            self.assertEqual(statements.count('COMMIT'), 1)
        with closing(self.real_connect(self.path)) as conn:
            self.assertEqual(conn.execute(
                'SELECT delay FROM publisher_policy WHERE site=? AND origin=?',
                ('fixture', origin)).fetchone(), (9,))
            self.assertEqual(conn.execute(
                'SELECT requests,seconds FROM publisher_windows WHERE site=? AND origin=? ORDER BY requests',
                ('fixture', origin)).fetchall(), [(1, 60), (2, 30)])
        reopened = self.ledger(); reopened.reserve('fixture', 'request', origin=origin)
        with self.assertRaises(RateLimit) as caught:
            reopened.reserve('fixture', 'request', origin=origin)
        self.assertEqual(caught.exception.wait, 60)

    def test_failed_publisher_window_write_rolls_back_stricter_delay(self):
        ledger = self.ledger(); origin = 'https://jobs.fixture.test'
        ledger.set_publisher('fixture', origin, delay=7, requests=2, seconds=30)
        before = self.snapshot()
        class BrokenConnection(sqlite3.Connection):
            def execute(conn, sql, *args, **kwargs):
                if sql.startswith('INSERT OR IGNORE INTO publisher_windows'):
                    raise sqlite3.OperationalError('Artificial window write failure')
                return super().execute(sql, *args, **kwargs)
        def connect(*args, **kwargs):
            return self.real_connect(*args, factory=BrokenConnection, **kwargs)
        with patch('sqlite3.connect', side_effect=connect), self.assertRaisesRegex(CrawlError, 'rate_storage_error'):
            ledger.set_publisher('fixture', origin, delay=120, requests=1, seconds=300)
        self.assertEqual(self.snapshot(), before)
        self.ledger().set_publisher('fixture', origin, delay=120, requests=1, seconds=300)

    def test_concurrent_publisher_merges_retain_both_windows_and_strictest_delay(self):
        ledger = self.ledger(); origin = 'https://jobs.fixture.test'
        ledger.set_publisher('fixture', origin, delay=7)
        barrier = threading.Barrier(3); errors = []
        def install(delay, count, seconds):
            try:
                barrier.wait(timeout=5)
                ledger.set_publisher('fixture', origin, delay=delay, requests=count, seconds=seconds)
            except BaseException as exc: errors.append(exc)
        workers = [threading.Thread(target=install, args=args) for args in ((9, 2, 30), (12, 1, 60))]
        for worker in workers: worker.start()
        try: barrier.wait(timeout=5)
        finally:
            for worker in workers: worker.join(15)
        self.assertTrue(all(not worker.is_alive() for worker in workers)); self.assertEqual(errors, [])
        with closing(self.real_connect(self.path)) as conn:
            self.assertEqual(conn.execute('SELECT delay FROM publisher_policy').fetchall(), [(12,)])
            self.assertEqual(conn.execute('SELECT requests,seconds FROM publisher_windows ORDER BY requests').fetchall(),
                             [(1, 60), (2, 30)])
        reopened = self.ledger(); reopened.reserve('fixture', 'request', origin=origin)
        with self.assertRaises(RateLimit) as caught: reopened.reserve('fixture', 'request', origin=origin)
        self.assertEqual(caught.exception.wait, 60)

    def test_noop_during_pending_stricter_commit_does_not_cache_request_authorization(self):
        ledger = self.ledger(); origin = 'https://jobs.fixture.test'
        ledger.set_publisher('fixture', origin, delay=7)
        ledger.reserve('fixture', 'request', origin=origin)
        done = threading.Event(); errors = []
        def install():
            try: ledger.set_publisher('fixture', origin, delay=7)
            except BaseException as exc: errors.append(exc)
            finally: done.set()
        with closing(self.real_connect(self.path)) as writer:
            writer.execute('BEGIN IMMEDIATE')
            writer.execute('UPDATE publisher_policy SET delay=120 WHERE site=? AND origin=?', ('fixture', origin))
            worker = threading.Thread(target=install); worker.start()
            try:
                self.assertTrue(done.wait(5), 'Sufficient committed rule should not wait for the pending stricter writer')
                self.assertTrue(writer.in_transaction)
            finally:
                writer.commit(); worker.join(15)
        self.assertFalse(worker.is_alive()); self.assertEqual(errors, [])
        self.now += 8
        with self.assertRaises(RateLimit) as caught:
            ledger.reserve('fixture', 'request', origin=origin)
        self.assertEqual(caught.exception.wait, 112)

    def test_repeated_publisher_install_does_not_cache_missing_or_corrupt_storage(self):
        origin = 'https://jobs.fixture.test'
        for damaged in ('missing', 'corrupt'):
            self.path = Path(self.temp.name)/(damaged+'.sqlite')
            ledger = self.ledger()
            ledger.set_publisher('fixture', origin, delay=7, requests=2, seconds=30)
            if damaged == 'missing': self.path.unlink()
            else: self.path.write_bytes(b'Artificial invalid database')
            with self.subTest(damaged=damaged), self.assertRaisesRegex(CrawlError, 'rate_storage_error'):
                ledger.set_publisher('fixture', origin, delay=7, requests=2, seconds=30)
            if damaged == 'missing': self.assertFalse(self.path.exists())
            else: self.assertEqual(self.path.read_bytes(), b'Artificial invalid database')


if __name__ == '__main__':
    unittest.main()
