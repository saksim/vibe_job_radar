"""A preview never spends quota or replaces the final reservation."""
import json
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock
from vibe_job_radar.guided.rate import RateLedger, RateLimit
from vibe_job_radar.guided.service import GuidedService
from vibe_job_radar.workspace import Workspace

class LoginAvailabilityTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.now=100000.0
        self.path=Path(self.tmp.name)/'rates.sqlite'
        self.ledger=RateLedger(self.path,clock=lambda:self.now)

    def dump(self):
        with closing(sqlite3.connect(self.path)) as conn:
            return list(conn.iterdump())

    def assert_refusal_matches(self, status):
        before=self.dump()
        with self.assertRaises(RateLimit) as caught:self.ledger.reserve('liepin','login')
        self.assertEqual(caught.exception.code,status['reason'])
        self.assertEqual(caught.exception.next_allowed_at,status['next_allowed_at'])
        self.assertEqual(self.dump(),before)

    def test_repeated_preview_never_creates_attempt_or_clock_record(self):
        before=self.dump()
        for _ in range(5):self.assertTrue(self.ledger.login_availability('liepin')['available'])
        self.assertEqual(self.dump(),before)
        self.ledger.reserve('liepin','login')
        status=self.ledger.login_availability('liepin')
        self.assertFalse(status['available']);self.assertEqual(status['reason'],'rate_wait')
        self.assertEqual(status['next_allowed_at'],100300)
        self.assert_refusal_matches(status)

    def test_rolling_day_survives_restart_and_expires_at_exact_boundary(self):
        for stamp in (100000,100301,100602):
            self.now=stamp;self.ledger.reserve('liepin','login')
        self.now=100903
        self.ledger=RateLedger(self.path,clock=lambda:self.now)
        status=self.ledger.login_availability('liepin')
        self.assertEqual(status['reason'],'daily_limit')
        self.assertEqual(status['next_allowed_at'],186400)
        self.assert_refusal_matches(status)
        self.now=186400
        self.assertTrue(self.ledger.login_availability('liepin')['available'])
        self.ledger.reserve('liepin','login')
        self.assertEqual(self.ledger.summary('liepin')['login']['day'],3)

    def test_shared_cooldown_keeps_its_longer_deadline(self):
        self.ledger.reserve('liepin','login');self.ledger.cool('liepin',900)
        status=self.ledger.login_availability('liepin')
        self.assertEqual(status['reason'],'cooldown')
        self.assertEqual(status['next_allowed_at'],100900)
        self.assert_refusal_matches(status)

    def test_clock_rollback_remains_blocked_without_rewriting_clock(self):
        self.ledger.reserve('liepin','login');self.now-=2
        before=self.dump();status=self.ledger.login_availability('liepin')
        self.assertEqual(status['reason'],'clock_rollback')
        self.assertEqual(self.dump(),before);self.assert_refusal_matches(status)

    def test_an_old_available_preview_does_not_authorize_a_later_request(self):
        self.assertTrue(self.ledger.login_availability('liepin')['available'])
        other=RateLedger(self.path,clock=lambda:self.now);other.reserve('liepin','login')
        self.assert_refusal_matches(self.ledger.login_availability('liepin'))

    def test_state_shows_budget_before_password_input_without_mutating_task(self):
        service=GuidedService(Workspace(Path(self.tmp.name)/'workspace'),ledger=self.ledger)
        self.addCleanup(service.close);service._submit=Mock()
        ident=service.create({'platform':'liepin','keyword':'synthetic quota preview','consent':True,'rights_note':'synthetic quota fixture','max_pages':1,'max_jobs':1})['id']
        for stamp in (100000,100301,100602):
            self.now=stamp;self.ledger.reserve('liepin','login')
        self.now=100903
        before_task=service._path(ident).read_bytes();before_ledger=self.dump()
        state=service.state();job=state['jobs'][0]
        self.assertFalse(job['browser_open']);self.assertFalse(job['login_availability']['available'])
        self.assertEqual(job['login_availability']['next_allowed_at'],186400)
        self.assertEqual(service._path(ident).read_bytes(),before_task)
        self.assertEqual(self.dump(),before_ledger)
        self.assertNotIn('login_availability',json.loads(before_task))
        self.assertEqual(service._submit.call_count,1)
