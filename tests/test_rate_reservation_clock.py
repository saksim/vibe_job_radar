"""Quota decisions use the clock after a real SQLite writer wait."""
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

from vibe_job_radar.guided.rate import Limits, RateLedger, RateLimit


class ReservationClockTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'rates.sqlite'
        self.now = 1_000_000.0
        self.ledger = RateLedger(self.path, Limits(request_interval=0), clock=lambda: self.now)
        self.real_connect = sqlite3.connect

    def queued_reservation(self, after_wait, *, kind='request', writer_update=None):
        attempted = threading.Event()
        errors = []

        def connect(*args, **kwargs):
            connection = self.real_connect(*args, **kwargs)
            # SQLite runs this callback before trying to acquire the write
            # reservation, while the other actual connection owns it.
            connection.set_trace_callback(
                lambda sql: attempted.set() if sql == 'BEGIN IMMEDIATE' else None)
            return connection

        def reserve():
            try:
                self.ledger.reserve('fixture', kind)
            except BaseException as error:
                errors.append(error)

        with closing(self.real_connect(self.path)) as writer:
            writer.execute('BEGIN IMMEDIATE')
            with patch('vibe_job_radar.guided.rate.sqlite3.connect', side_effect=connect):
                worker = threading.Thread(target=reserve)
                worker.start()
                try:
                    self.assertTrue(attempted.wait(5), 'reservation did not reach SQLite')
                    self.assertTrue(writer.in_transaction)
                    self.now = after_wait
                    if writer_update:
                        writer_update(writer)
                    writer.commit()
                finally:
                    writer.rollback()
                    worker.join(5)
        self.assertFalse(worker.is_alive(), 'reservation did not finish after lock release')
        return errors

    def rows(self):
        with closing(self.real_connect(self.path)) as connection:
            return connection.execute('SELECT kind,ts FROM visits ORDER BY ts').fetchall()

    def test_queued_request_accepts_a_later_clock_committed_by_the_lock_owner(self):
        later = self.now + 10
        errors = self.queued_reservation(later, writer_update=lambda writer: writer.execute(
            'INSERT INTO clock_seen VALUES (?,?)', ('fixture', later)))
        self.assertEqual(errors, [])
        self.assertEqual(self.rows(), [('request', later)])

    def test_cooldown_that_expires_during_writer_wait_does_not_refuse_request(self):
        self.ledger.defer('fixture', 5)
        later = self.now + 10
        self.assertEqual(self.queued_reservation(later), [])
        self.assertEqual(self.rows(), [('request', later)])

    def test_queued_page_records_acquisition_time_and_preserves_full_next_interval(self):
        later = self.now + 10
        self.assertEqual(self.queued_reservation(later, kind='page'), [])
        with self.assertRaises(RateLimit) as caught:
            RateLedger(self.path, self.ledger.limits, clock=lambda: self.now).reserve('fixture', 'page')
        self.assertEqual(caught.exception.wait, self.ledger.limits.page_interval)
        self.assertEqual(self.rows(), [('page', later)])

    def test_real_clock_rollback_during_writer_wait_still_refuses_without_spending(self):
        self.ledger.reserve('fixture', 'request')
        original = self.rows()
        errors = self.queued_reservation(self.now - 10)
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], RateLimit)
        self.assertEqual(errors[0].code, 'clock_rollback')
        self.assertEqual(self.rows(), original)


if __name__ == '__main__':
    unittest.main()