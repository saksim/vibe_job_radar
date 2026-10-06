"""Exercise actual native wait code with a deterministic clock; no browser/network."""
import importlib.util
from pathlib import Path
import threading
import unittest

SPEC = importlib.util.spec_from_file_location(
    'native_service_wait_under_test',
    Path(__file__).resolve().parents[1] / 'scripts/native_service_wait.py')
waiter = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(waiter)


class Clock:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now = round(self.now + seconds, 8)


class Service:
    def __init__(self, clock, finish_at=0):
        self.clock = clock
        self.finish_at = finish_at
        self._lock = threading.Lock()
        self.read_at = []
        self.job = {'status': 'completed', 'saved': 1}
        self.on_read = None

    @property
    def _busy(self):
        return self.clock.now < self.finish_at

    def state(self):
        # The short busy check must release its lock before the public read.
        if not self._lock.acquire(blocking=False):
            raise AssertionError('state called while the hint lock was held')
        try:
            self.read_at.append(self.clock.now)
            if self.on_read:
                return self.on_read(self)
            return {'busy': self._busy, 'jobs': [self.job]}
        finally:
            self._lock.release()


class NativeServiceWaitTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.real_time = waiter.time
        waiter.time = self.clock

    def tearDown(self):
        waiter.time = self.real_time

    def test_idle_returns_fresh_public_job_without_sleep(self):
        service = Service(self.clock)
        self.assertIs(waiter.wait_for_guided_job(service), service.job)
        self.assertEqual(service.read_at, [0])
        self.assertEqual(self.clock.sleeps, [])

    def test_busy_reads_periodically_and_wakes_promptly_for_final_state(self):
        service = Service(self.clock, finish_at=4.13)
        self.assertIs(waiter.wait_for_guided_job(service), service.job)
        self.assertEqual(service.read_at, [0, 2, 4, 4.15])
        self.assertLessEqual(self.clock.now - service.finish_at, .05)
        self.assertTrue(all(value == .05 for value in self.clock.sleeps))

    def test_final_result_is_not_a_cached_running_job(self):
        service = Service(self.clock, finish_at=.13)
        def read(s):
            if s._busy:
                return {'busy': True, 'jobs': [{'status': 'running'}]}
            return {'busy': False, 'jobs': [s.job]}
        service.on_read = read
        self.assertIs(waiter.wait_for_guided_job(service), service.job)
        self.assertEqual(service.read_at, [0, .15])

    def test_busy_private_hint_cannot_override_public_completion(self):
        service = Service(self.clock, finish_at=999)
        service.on_read = lambda s: {'busy': False, 'jobs': [s.job]}
        self.assertIs(waiter.wait_for_guided_job(service), service.job)
        self.assertEqual(service.read_at, [0])

    def test_original_timeout_deadline_and_message_are_preserved(self):
        service = Service(self.clock, finish_at=999)
        with self.assertRaisesRegex(TimeoutError, '^native search did not finish$'):
            waiter.wait_for_guided_job(service)
        self.assertEqual(self.clock.now, 45.05)
        self.assertEqual(service.read_at, list(range(0, 45, 2)))

    def test_slow_state_reads_count_against_original_deadline(self):
        service = Service(self.clock, finish_at=999)
        def read(s):
            self.clock.now += 3
            return {'busy': True, 'jobs': []}
        service.on_read = read
        with self.assertRaisesRegex(TimeoutError, '^native search did not finish$'):
            waiter.wait_for_guided_job(service)
        self.assertEqual(service.read_at, list(range(0, 46, 5)))
        self.assertEqual(self.clock.now, 48)
        self.assertLess(len(service.read_at), 12)

    def test_public_state_exception_is_preserved(self):
        service = Service(self.clock, finish_at=999)
        failure = OSError('artificial checkpoint failure')
        def read(s):
            raise failure
        service.on_read = read
        with self.assertRaises(OSError) as caught:
            waiter.wait_for_guided_job(service)
        self.assertIs(caught.exception, failure)
        self.assertEqual(self.clock.sleeps, [])


if __name__ == '__main__':
    unittest.main()
