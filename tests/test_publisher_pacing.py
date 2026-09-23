"""Publisher constraints integrated with real SQLite, bridge and task recovery.

Only controlled fixtures are used. This is not real recruitment-site or VPN
certification. No relaxed target checks or production clock overrides are added.
"""
from __future__ import annotations
from contextlib import closing
import concurrent.futures
import dataclasses
import json
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from test_guided import FakeBackend, fixture_adapter
import test_guided as guided_fixtures
import test_guided_merge_review as browser_fixtures
from vibe_job_radar.guided.contracts import CrawlError
from vibe_job_radar.guided.rate import Limits, RateLedger, RateLimit
from vibe_job_radar.guided.transport import PinnedTransport, WireResponse
from vibe_job_radar.guided.service import GuidedService
from vibe_job_radar.guided.adapters import Registry

ORIGIN = 'https://jobs.fixture.test'


class PublisherLedgerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)/'rates.sqlite'
        self.now = 100000.0
        self.limits = Limits(request_interval=0, page_interval=0)
        self.ledger = self.new_ledger()

    def new_ledger(self):
        return RateLedger(self.path, self.limits, clock=lambda: self.now)

    def reserve(self, ledger=None, origin=ORIGIN):
        (ledger or self.ledger).reserve('fixture', 'request', origin=origin)

    def test_robots_observation_counts_towards_newly_learned_delay(self):
        self.reserve()
        self.ledger.set_publisher('fixture', ORIGIN, delay=90)
        with self.assertRaises(RateLimit) as error: self.reserve()
        self.assertEqual(error.exception.code, 'publisher_wait')
        self.assertEqual(error.exception.next_allowed_at, self.now+90)
        self.assertEqual(self.ledger.summary('fixture')['request']['day'], 1)
        self.now += 90; self.reserve()

    def test_rule_and_observations_survive_new_instance(self):
        self.ledger.set_publisher('fixture', ORIGIN, delay=60)
        self.reserve()
        with self.assertRaises(RateLimit): self.reserve(self.new_ledger())
        self.now += 60; self.reserve(self.new_ledger())

    def test_stricter_rule_is_not_overwritten_by_faster_one(self):
        self.ledger.set_publisher('fixture', ORIGIN, delay=120)
        self.ledger.set_publisher('fixture', ORIGIN, delay=10)
        self.reserve(); self.now += 119
        with self.assertRaises(RateLimit): self.reserve()

    def test_exact_request_window_not_only_average_interval(self):
        self.ledger.set_publisher('fixture', ORIGIN, requests=2, seconds=60)
        self.reserve(); self.now += 1; self.reserve()
        self.now += 30
        with self.assertRaises(RateLimit) as error: self.reserve()
        self.assertEqual(error.exception.wait, 29)
        self.now += 29; self.reserve()

    def test_multiple_windows_remain_independent(self):
        self.ledger.set_publisher('fixture', ORIGIN, requests=2, seconds=60)
        self.ledger.set_publisher('fixture', ORIGIN, requests=1, seconds=10)
        self.reserve(); self.now += 10; self.reserve(); self.now += 10
        with self.assertRaises(RateLimit) as error: self.reserve()
        self.assertEqual(error.exception.wait, 40)

    def test_origin_rule_does_not_throttle_unrelated_resource_origin(self):
        self.ledger.set_publisher('fixture', ORIGIN, delay=500)
        self.reserve(); self.reserve(origin='https://cdn.fixture.test')
        self.assertEqual(self.ledger.summary('fixture')['request']['day'], 2)

    def test_workspace_limit_and_publisher_reservation_are_atomic(self):
        ledger = RateLedger(self.path, dataclasses.replace(self.limits, requests_hour=1), clock=lambda: self.now)
        ledger.set_publisher('fixture', ORIGIN, delay=10)
        self.reserve(ledger); self.now += 15
        with self.assertRaises(RateLimit) as error: self.reserve(ledger)
        self.assertEqual(error.exception.code, 'hourly_limit')
        with closing(sqlite3.connect(self.path)) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM publisher_visits').fetchone()[0], 1)
        self.assertEqual(ledger.summary('fixture')['request']['day'], 1)

    def test_next_time_is_maximum_of_cooldown_and_publisher(self):
        self.ledger.set_publisher('fixture', ORIGIN, delay=1000)
        self.reserve(); self.ledger.cool('fixture', 300)
        with self.assertRaises(RateLimit) as error: self.reserve()
        self.assertEqual(error.exception.next_allowed_at, self.now+1000)

    def test_window_longer_than_two_days_does_not_lose_history(self):
        self.ledger.set_publisher('fixture', ORIGIN, requests=1, seconds=400000)
        self.reserve(); self.now += 200000
        self.ledger.reserve('other', 'request')  # maintenance/pruning path
        with self.assertRaises(RateLimit) as error: self.reserve()
        self.assertEqual(error.exception.wait, 200000)

    def test_invalid_rules_fail_without_partial_write(self):
        for kw in ({'delay': float('nan')}, {'delay': -1}, {'delay': True},
                   {'requests': 0, 'seconds': 30}, {'requests': 2, 'seconds': 0},
                   {'requests': 1}, {'seconds': 3}):
            with self.subTest(kw=kw), self.assertRaises(CrawlError):
                self.ledger.set_publisher('fixture', ORIGIN, **kw)
        with closing(sqlite3.connect(self.path)) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM publisher_policy').fetchone()[0], 0)

    def test_invalid_origin_fails_without_credentials_in_error(self):
        for origin in ('http://x', 'https://user:SECRET@x', 'https://x/path', 'https://[bad'):
            with self.subTest(origin=origin), self.assertRaises(CrawlError) as error:
                self.ledger.set_publisher('fixture', origin, delay=10)
            self.assertNotIn('SECRET', str(error.exception))

    def test_concurrent_workers_cannot_both_take_last_slot(self):
        self.ledger.set_publisher('fixture', ORIGIN, requests=1, seconds=60)
        barrier = threading.Barrier(8)
        def take(_):
            ledger = self.new_ledger(); barrier.wait(timeout=5)
            try: self.reserve(ledger); return True
            except RateLimit: return False
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            self.assertEqual(sum(pool.map(take, range(8))), 1)

    def test_deleted_database_does_not_reset_active_ledger(self):
        self.path.unlink()
        with self.assertRaises(CrawlError) as error: self.reserve()
        self.assertEqual(error.exception.code, 'rate_storage_error')
        self.assertFalse(self.path.exists())

    def test_corrupted_database_fails_closed(self):
        self.path.write_bytes(b'not a database')
        with self.assertRaises(CrawlError) as error: self.reserve()
        self.assertEqual(error.exception.code, 'rate_storage_error')

    def test_clock_rollback_across_kind_is_detected(self):
        self.reserve(); self.now -= 5
        with self.assertRaises(RateLimit) as error: self.ledger.reserve('fixture', 'page')
        self.assertEqual(error.exception.code, 'clock_rollback')


class PublisherBridgeTests(unittest.TestCase):
    setUp = PublisherLedgerTests.setUp
    new_ledger = PublisherLedgerTests.new_ledger
    reserve = PublisherLedgerTests.reserve
    def wire(self, **kw):
        return PinnedTransport(fixture_adapter(), self.ledger, threading.Event(), **kw)

    def test_allowed_slower_robots_is_installed_not_rejected(self):
        wire = self.wire(max_inline_wait=0)
        with patch.object(wire, 'fetch', return_value=WireResponse(200, {}, b'User-agent: *\nAllow: /\nCrawl-delay: 90\nRequest-rate: 2/180')):
            wire.ensure_robots(ORIGIN+'/job/1')
        self.reserve()
        with self.assertRaises(RateLimit): wire.reserve('request', origin=ORIGIN)

    def test_long_wait_does_not_create_connection_or_consume_quota(self):
        self.ledger.set_publisher('fixture', ORIGIN, delay=90); self.reserve()
        with patch('vibe_job_radar.guided.transport.validate_public_url', return_value=('jobs.fixture.test', '8.8.8.8', '/job/1')), patch('vibe_job_radar.guided.transport.PinnedHTTPSConnection') as conn:
            with self.assertRaises(RateLimit) as error: self.wire(max_inline_wait=0).fetch(ORIGIN+'/job/1')
        self.assertEqual(error.exception.next_allowed_at, self.now+90)
        conn.assert_not_called()
        self.assertEqual(self.ledger.summary('fixture')['request']['day'], 1)

    def test_short_wait_is_cancellable(self):
        self.ledger.set_publisher('fixture', ORIGIN, delay=20); self.reserve()
        wire = self.wire()
        def cancel(_): wire.cancelled.set(); return True
        with patch.object(wire.cancelled, 'wait', side_effect=cancel):
            with self.assertRaises(CrawlError) as error: wire.reserve('request', origin=ORIGIN)
        self.assertEqual(error.exception.code, 'paused')

    def test_429_expires_in_same_session_but_does_not_replay_request(self):
        wire = self.wire(max_inline_wait=0)
        with patch('vibe_job_radar.guided.transport.validate_public_url', return_value=('jobs.fixture.test', '8.8.8.8', '/job/1')), patch('vibe_job_radar.guided.transport.PinnedHTTPSConnection') as conn:
            response = conn.return_value.getresponse.return_value
            response.status = 429; response.getheaders.return_value = [('Retry-After', '600')]
            with self.assertRaises(CrawlError): wire.fetch(ORIGIN+'/job/1')
            self.assertEqual(conn.call_count, 1)
            with self.assertRaises(RateLimit): wire.fetch(ORIGIN+'/job/1')
            self.assertEqual(conn.call_count, 1)
            self.now += 600; response.status = 200; response.read.return_value = b'ok'
            self.assertEqual(wire.fetch(ORIGIN+'/job/1').body, b'ok')
            self.assertEqual(conn.call_count, 2)


class PublisherBrowserPropagationTests(unittest.TestCase):
    def test_bridge_due_time_survives_route_and_snapshot(self):
        browser = browser_fixtures.BrowserReviewTests().backend()
        route = Mock(); route.request.url = ORIGIN+'/job/1'
        route.request.resource_type = 'document'; route.request.method = 'GET'
        due = RateLimit(90, 'publisher_wait', next_allowed_at=123456)
        browser.wire.fetch.side_effect = due
        browser._route(route)
        with self.assertRaises(RateLimit) as error: browser.snapshot()
        self.assertIs(error.exception, due)


class PublisherTaskRecoveryTests(unittest.TestCase):
    setUp = guided_fixtures.ServiceTests.setUp
    create = guided_fixtures.ServiceTests.create
    wait = guided_fixtures.ServiceTests.wait
    def job(self, ident):
        # Poll through the public, lock-protected snapshot. The private _load()
        # helper is for the owning worker / callers already holding _lock.
        # Opening its JSON while the worker os.replace()s it races on Windows.
        return next(item for item in self.service.state()['jobs'] if item['id'] == ident)
    def test_partial_report_due_selection_and_automatic_resume(self):
        ident = self.create(max_pages=2)
        rows = self.job(ident)['cards']; selected = [c['id'] for c in rows]
        backend = self.service._backends[ident]; original = backend.open
        now = [100000.0]; self.service.ledger.clock = lambda: now[0]
        blocked = [True]
        def fetch(url, **kwargs):
            if '/job/2' in url and blocked[0]:
                blocked[0] = False
                raise RateLimit(90, 'publisher_wait', next_allowed_at=now[0]+90)
            return original(url, **kwargs)
        backend.open = fetch
        self.service.action({'id': ident, 'action': 'collect', 'selected': selected}); self.wait()
        pending = self.job(ident)
        self.assertEqual(pending['status'], 'waiting_rate')
        self.assertEqual(pending['selection'], selected); self.assertTrue(pending['report_id'])
        self.assertEqual(pending['cards'][0]['status'], 'ok')
        self.assertTrue(pending['auto_resume']); self.assertEqual(pending['next_allowed_at'], now[0]+90)
        now[0] += 90
        deadline = time.monotonic()+5
        while self.job(ident)['status'] != 'completed' and time.monotonic() < deadline: time.sleep(.02)
        finished = self.job(ident)
        self.assertEqual(finished['status'], 'completed', finished)
        self.assertEqual(sum('/job/1' in url for url in backend.opens), 1)
        self.assertTrue(all(c['status']=='ok' for c in self.job(ident)['cards']))

    def test_restart_retains_due_time_but_does_not_restore_credentials(self):
        ident = self.create()
        selected = [self.job(ident)['cards'][0]['id']]
        due = self.service.ledger.clock() + 90
        def deferred(*args, **kwargs):
            raise RateLimit(90, 'publisher_wait', next_allowed_at=due)
        self.service._backends[ident].open = deferred
        # Produce a real deferred checkpoint on its owning worker. The old
        # fixture wrote an already-expired deadline while that owner was alive;
        # its valid automatic resume could erase the deadline before inspection.
        self.service.action({'id':ident,'action':'collect','selected':selected})
        self.wait()
        self.assertEqual(self.job(ident)['status'], 'waiting_rate')
        self.assertEqual(self.job(ident)['next_allowed_at'], due)
        self.service.close()
        other = GuidedService(self.workspace, registry=Registry([fixture_adapter()]), backend_factory=FakeBackend)
        try:
            job = other.state()['jobs'][0]
            self.assertEqual(job['next_allowed_at'], due)
            self.assertEqual(job['selection'], selected)
            self.assertFalse(job['automatic_resume_available']); self.assertFalse(job['browser_open'])
            other._resume_due(); self.assertFalse(other._busy)
        finally: other.close()
