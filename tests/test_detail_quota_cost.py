"""Artificial detail work checks quota accounting; no recruiting traffic."""
import copy
from contextlib import closing
import json
import sqlite3
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import test_detail_attempt_history as base
from test_guided import detail
from vibe_job_radar.guided import attempt_history, quota_cost
from vibe_job_radar.guided.contracts import CrawlError
from vibe_job_radar.guided.native_browser import NativeControl
from vibe_job_radar.guided.rate import Limits, RateLedger, RateLimit
from vibe_job_radar.guided.transport import PinnedTransport
from vibe_job_radar.workspace import InputError
from vibe_job_radar.utils import atomic_json


class DetailQuotaCostTests(unittest.TestCase):
    setUp = base.DetailAttemptHistoryTests.setUp

    def wire(self, cls=PinnedTransport, **limits):
        settings=Limits(page_interval=0,request_interval=0,**limits)
        ledger=RateLedger(self.workspace.root/'quota-cost-fixture.sqlite',settings)
        return cls(self.adapter,ledger,threading.Event(),max_inline_wait=0)

    def collect(self,*results,wire=None,returned=None):
        case=self
        class Backend:
            def __init__(self):
                self.wire=wire if wire is not None else case.wire()
                self.results=iter(results);self.opens=[]
            def open(self,url):
                self.opens.append(url)
                saved=case.service._load(case.state['id'])
                case.assertEqual(saved[quota_cost.KEY]['attempts'][-1]['status'],'unfinished')
                charges,error=next(self.results)
                self.wire.reserve('page')
                for _ in range(charges):self.wire.reserve('request',origin='https://example.org')
                if error is not None:raise error
                return detail(url)
        backend=Backend()
        self.service._collect(self.state,backend,self.adapter,returned_detail=returned)
        return backend

    def rows(self):
        return self.service._load(self.state['id'])[quota_cost.KEY]['attempts']

    def test_bridge_and_native_count_successful_reservations_only(self):
        for cls in (PinnedTransport,NativeControl):
            with self.subTest(transport=cls.__name__):
                wire=self.wire(cls,requests_hour=1,requests_day=1)
                # Use a separate actual ledger for each transport.
                wire.ledger=RateLedger(self.workspace.root/(cls.__name__+'.sqlite'),wire.ledger.limits)
                reserve=(lambda:wire.reserve_request_nowait(origin='https://example.org')) if cls is NativeControl else (
                    lambda:wire.reserve('request',origin='https://example.org'))
                reserve()
                with self.assertRaises(RateLimit):reserve()
                self.assertEqual(wire.quota_reservations,{'request':1,'page':0})
                self.assertEqual(wire.quota_uncertain,0)
                with closing(sqlite3.connect(wire.ledger.path)) as db:
                    self.assertEqual(db.execute("SELECT count(*) FROM visits WHERE kind='request'").fetchone()[0],1)

    def test_failed_retry_costs_reach_reports_and_offline_ledger_without_rewriting_old_report(self):
        self.collect((2,CrawlError('jd_incomplete')),(3,None))
        path=self.workspace.root/'reports'/self.state['report_id']/'guided_acquisition.json'
        first=path.read_bytes()
        self.collect((4,None))
        self.assertEqual([r['request'] for r in self.rows()],[2,3,4])
        self.assertEqual([r['page'] for r in self.rows()],[1,1,1])
        self.assertEqual(path.read_bytes(),first)
        latest=json.loads((self.workspace.root/'reports'/self.state['report_id']/'guided_acquisition.json').read_bytes())
        self.assertEqual(latest[quota_cost.KEY]['attempts'],self.rows())
        from vibe_job_radar.live_acceptance import AcceptanceLedger
        from vibe_job_radar.guided.adapters import builtins
        self.state.update(platform='liepin',backend='native',
                          search_url=builtins().get('liepin').search_url('时间序列'))
        self.service._save(self.state)
        clock=['2026-01-01T00:00:00Z']
        ledger=AcceptanceLedger(self.workspace.root,clock=lambda:clock[0])
        plan=ledger.create({'keyword':'时间序列','roles':['time_series'],'search_url':'','backend':'native',
            'selection_rule':'ARTIFICIAL OFFLINE QUOTA CHECK','timezone':'UTC','source_revision':'a'*40,
            'source_sha256':'b'*64,'browser':'msedge','browser_version':'fixture',
            'planner_os':'ARTIFICIAL TEST','network_mode':'system','consent':True})
        clock[0]='2099-01-01T00:00:00Z'  # Artificial reader clock, never site/date evidence.
        result=ledger.capture(plan['id'])
        cost=result[quota_cost.KEY]
        self.assertEqual(cost['request_quota_charged'],9)
        self.assertEqual(cost['page_quota_charged'],3)
        self.assertEqual(cost['measured_attempts'],3)
        self.assertEqual(cost['attempts_without_measured_cost'],0)
        self.assertEqual(result['certification'],'not_live_verified')
        self.assertIn('search_login_and_http_attempts_not_recorded',result['gaps'])
        self.assertEqual(ledger.capture(plan['id'])[quota_cost.KEY],cost)

    def test_missing_replaced_or_reset_counter_is_unknown_not_zero(self):
        for defect in ('missing','replaced','reset'):
            with self.subTest(defect=defect):
                state=copy.deepcopy(self.state);wire=self.wire()
                wire.quota_reservations={'request':5,'page':2}
                backend=SimpleNamespace(wire=wire)
                if defect=='missing':backend.wire=SimpleNamespace()
                attempt=attempt_history.begin(state,state['selection'][0])
                before=quota_cost.begin(state,attempt,backend)
                if defect=='replaced':backend.wire=self.wire()
                if defect=='reset':wire.quota_reservations={'request':0,'page':0}
                attempt_history.finish(attempt,attempt_history.monotonic(),error=CrawlError('robots_denied'))
                quota_cost.finish(state,attempt,before,backend)
                row=state[quota_cost.KEY]['attempts'][-1]
                self.assertEqual(row['status'],'unavailable')
                self.assertIsNone(row['request']);self.assertIsNone(row['page'])

    def test_uncertain_storage_outcome_cannot_be_reported_as_free_request(self):
        wire=self.wire();reserve=wire.ledger.reserve
        def ambiguous(site,kind,**kwargs):
            reserve(site,kind,**kwargs)
            if kind=='request':raise CrawlError('rate_storage_error')
        with patch.object(wire.ledger,'reserve',side_effect=ambiguous),self.assertRaisesRegex(CrawlError,'rate_storage_error'):
            self.collect((1,None),wire=wire)
        self.assertEqual(self.rows()[0]['status'],'unavailable')
        self.assertIsNone(self.rows()[0]['request'])
        with closing(sqlite3.connect(wire.ledger.path)) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM visits WHERE kind='request'").fetchone()[0],1)

    def test_interruption_keeps_unknown_cost_after_new_attempt_succeeds(self):
        with self.assertRaises(KeyboardInterrupt):self.collect((2,KeyboardInterrupt()))
        old=copy.deepcopy(self.rows()[0])
        self.assertEqual(old['status'],'unfinished');self.assertIsNone(old['request'])
        self.state=self.service._load(self.state['id'])
        self.collect((1,None),(1,None))
        self.assertEqual(self.rows()[0],old)
        self.assertEqual([r['request'] for r in self.rows()],[None,1,1])

    def test_legacy_history_stays_partial(self):
        self.state.pop(quota_cost.KEY);self.service._save(self.state)
        self.collect((1,None),(1,None))
        cost=quota_cost.snapshot(self.service._load(self.state['id']))
        self.assertEqual(cost['origin'],'legacy_partial')
        summary=quota_cost.summarize({self.state['id']:cost},
                    {self.state['id']:self.state[attempt_history.KEY]})
        self.assertEqual(summary['tasks_with_unknown_prior_history'],1)
        self.assertEqual(summary['request_quota_charged'],2)

    def test_corruption_and_discarded_completed_cost_are_rejected(self):
        self.collect((1,None),(2,None))
        before=quota_cost.snapshot(self.state);bad=copy.deepcopy(before)
        bad['attempts'][0]['request']+=1
        with self.assertRaisesRegex(CrawlError,'quota_cost_invalid'):quota_cost.extends(before,bad)
        bad=copy.deepcopy(before);bad['attempts'].pop()
        with self.assertRaisesRegex(CrawlError,'quota_cost_invalid'):quota_cost.extends(before,bad)
        broken=copy.deepcopy(self.state);broken[quota_cost.KEY]['attempts'][0]['request']=-1
        atomic_json(self.service._path(self.state['id']),broken)
        with self.assertRaises(InputError):self.service._load(self.state['id'])

    def test_login_returned_body_has_zero_new_processing_quota(self):
        returned=(self.state['selection'][0],detail(self.state['cards'][0]['url']))
        backend=self.collect((2,None),returned=returned)
        self.assertEqual(len(backend.opens),1)
        self.assertEqual([r['request'] for r in self.rows()],[0,2])
        self.assertEqual([r['page'] for r in self.rows()],[0,1])


if __name__=='__main__':
    unittest.main()
