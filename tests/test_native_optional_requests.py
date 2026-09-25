"""Known marketing traffic is aborted without aborting the job document."""
from dataclasses import replace
import json
import unittest
from unittest.mock import patch

import test_native_acquisition as fixtures
from vibe_job_radar.guided.adapters import builtins
from vibe_job_radar.guided.contracts import CrawlError
from vibe_job_radar.guided.native_policy import NativeRule, contract_for


URL='https://api-wanda.liepin.com/api/com.liepin.cbp.baizhong.op.v2-show-4pc'
TELEMETRY=tuple('https://statistic.liepin.com/statisticPlatform/standard'+kind+'Log.json'
                for kind in ('F','T'))
HOTWORDS='https://api-c.liepin.com/api/com.liepin.searchfront4c.pc-hot-search-word-list'


class OptionalRequestTests(unittest.TestCase):
    setUp=fixtures.NativeControllerTests.setUp
    req=fixtures.NativeControllerTests.req

    def test_hotwords_abort_locally_without_stopping_search_or_reserving_quota(self):
        self.b.contract=contract_for(builtins().get('liepin'))
        for method,kind in [('GET','Fetch'),('GET','XHR'),('OPTIONS','XHR'),
                            ('OPTIONS','Preflight'),('OPTIONS','Other')]:
            with self.subTest(method=method,kind=kind):
                event=self.req(method=method,kind=kind)
                event['request']['url']=HOTWORDS
                with patch.object(self.b.wire,'reserve') as reserve:
                    self.b._paused('session',event)
                reserve.assert_not_called()
                self.b._send.assert_called_with('session','Fetch.failRequest',
                    {'requestId':'fetch-1','errorReason':'BlockedByClient'})
                self.assertIsNone(self.b.error)
                self.assertFalse(self.b._halted)
                self.assertFalse(self.b._requests)
                self.assertFalse(self.b._observations)
                self.assertEqual(self.b.native_counts['business'],0)
        trace=self.b._diagnostics.snapshot()
        self.assertIsNone(trace['first_content_candidate'])
        blocked=[e for e in trace['events'] if e['code']=='native_optional_request_blocked']
        self.assertEqual(len(blocked),5)
        self.assertTrue(all(e['local_block'] and e['impact']=='optional' for e in blocked))

    def test_hotword_abort_does_not_grant_egress_or_expand_other_routes(self):
        contract=contract_for(builtins().get('liepin'))
        for method in ('GET','OPTIONS'):
            with self.assertRaisesRegex(CrawlError,'native_operation_unreviewed'):
                contract.match(HOTWORDS,method,'XHR',authentication=True)
        for url,method,kind in [(HOTWORDS,'POST','XHR'),(HOTWORDS,'HEAD','Fetch'),
                                (HOTWORDS,'GET','Document'),(HOTWORDS,'GET','Script'),
                                (HOTWORDS+'/extra','GET','XHR'),
                                (HOTWORDS.replace('word-list','word-list-v2'),'GET','XHR'),
                                (HOTWORDS.replace('api-c.','other.'),'GET','XHR')]:
            with self.subTest(url=url,method=method,kind=kind):
                self.assertFalse(contract.ignored_request(url,method,kind))
                with self.assertRaises(CrawlError):
                    contract.match(url,method,kind,authentication=True)
                self.b.contract=contract;self.b.error=None;self.b._halted=False
                event=self.req(method=method,kind=kind);event['request']['url']=url
                with patch.object(self.b.wire,'reserve') as reserve:
                    self.b._paused('session',event)
                reserve.assert_not_called()
                self.b._send.assert_called_with('session','Fetch.failRequest',
                    {'requestId':'fetch-1','errorReason':'BlockedByClient'})
                if kind=='Script':
                    # Unknown assets already abort without failing the task.
                    self.assertIsNone(self.b.error)
                    self.assertFalse(self.b._halted)
                else:
                    self.assertIn(self.b.error,{'native_operation_unreviewed','resource_domain_blocked'})
                    self.assertTrue(self.b._halted)

    def test_hotword_optional_handling_cannot_bypass_cancel_or_policy_revocation(self):
        self.b.contract=contract_for(builtins().get('liepin'))
        event=self.req(method='GET');event['request']['url']=HOTWORDS
        self.b.cancelled.set()
        self.b._paused('session',event)
        self.assertEqual(self.b.error,'paused')
        self.b.cancelled.clear();self.b.error=None;self.b._halted=False
        self.b.policy_check=lambda:False
        self.b._paused('session',event)
        self.assertEqual(self.b.error,'native_policy_changed')

    def test_hotword_local_abort_completion_is_not_a_required_network_failure(self):
        self.b.contract=contract_for(builtins().get('liepin'))
        for method in ('GET','OPTIONS'):
            event=self.req(method=method);event['request']['url']=HOTWORDS
            self.b._paused('session',event)
            self.b._received({'sessionId':'session','message':json.dumps({
                'method':'Network.loadingFailed','params':{
                    'requestId':'net-1','errorText':'net::ERR_BLOCKED_BY_CLIENT'}})})
            self.assertIsNone(self.b.error)
            self.assertFalse(self.b._halted)
        self.assertFalse(self.b._observations)

    def test_optional_placement_and_preflight_abort_without_network_or_task_failure(self):
        self.b.contract=contract_for(builtins().get('liepin'))
        for method, kind in [('POST','XHR'),('OPTIONS','Preflight'),('OPTIONS','XHR')]:
            with self.subTest(method=method,kind=kind):
                event=self.req(method=method,kind=kind)
                event['request']['url']=URL
                with patch.object(self.b.wire,'reserve') as reserve:
                    self.b._paused('session',event)
                reserve.assert_not_called()
                self.b._send.assert_called_with('session','Fetch.failRequest',
                    {'requestId':'fetch-1','errorReason':'BlockedByClient'})
                self.assertIsNone(self.b.error)
                self.assertEqual(self.b.native_counts['business'],0)
                self.assertFalse(self.b._requests)
        trace=self.b._diagnostics.snapshot()
        self.assertIsNone(self.b._diagnostics.first_content_candidate)
        self.assertIn('native_optional_request_blocked',{e['code'] for e in trace['events']})
        self.assertTrue(all(e['impact']=='optional' for e in trace['events'] if e['code']=='native_optional_request_blocked'))

    def test_ignored_host_is_not_added_to_network_allowlist(self):
        contract=contract_for(builtins().get('liepin'))
        self.assertNotIn('api-wanda.liepin.com',contract.hosts)
        self.assertNotIn('https://api-wanda.liepin.com',contract.rule_origins)
        with self.assertRaisesRegex(CrawlError,'resource_domain_blocked'):
            contract.match(URL,'POST','XHR',authentication=True)

    def test_statistics_abort_is_local_and_not_a_required_content_failure(self):
        self.b.contract=contract_for(builtins().get('liepin'))
        for url in TELEMETRY:
            for method, kind in [('POST','XHR'),('POST','Fetch'),('OPTIONS','XHR'),
                                 ('OPTIONS','Preflight'),('OPTIONS','Other')]:
                with self.subTest(url=url,method=method,kind=kind):
                    event=self.req(method=method,kind=kind);event['request']['url']=url
                    with patch.object(self.b.wire,'reserve') as reserve:
                        self.b._paused('session',event)
                    reserve.assert_not_called()
                    self.b._send.assert_called_with('session','Fetch.failRequest',
                        {'requestId':'fetch-1','errorReason':'BlockedByClient'})
                    self.assertIsNone(self.b.error)
                    self.assertFalse(self.b._requests)
                    self.assertEqual(self.b.native_counts['business'],0)
            with self.assertRaisesRegex(CrawlError,'resource_domain_blocked'):
                self.b.contract.match(url,'POST','XHR',authentication=True)
        self.assertNotIn('statistic.liepin.com',self.b.contract.hosts)
        self.assertNotIn('https://statistic.liepin.com',self.b.contract.rule_origins)
        trace=self.b._diagnostics.snapshot()
        self.assertIsNone(trace['first_content_candidate'])
        blocked=[e for e in trace['events'] if e['code']=='native_optional_request_blocked']
        self.assertTrue(blocked)
        self.assertTrue(all(e['local_block'] and e['impact']=='optional' for e in blocked))

    def test_same_host_other_statistics_routes_and_get_are_not_ignored(self):
        contract=contract_for(builtins().get('liepin'))
        for endpoint in TELEMETRY:
            for url, method, kind in [(endpoint,'GET','XHR'),(endpoint,'POST','Document'),
                                      (endpoint+'/extra','POST','XHR'),
                                      (endpoint.replace('Log.json','LogX.json'),'POST','XHR'),
                                      (endpoint.replace('statisticPlatform/standard','statisticPlatform/customEvent'),'POST','XHR')]:
                with self.subTest(url=url,method=method,kind=kind):
                    self.assertFalse(contract.ignored_request(url,method,kind))
                    with self.assertRaisesRegex(CrawlError,'resource_domain_blocked'):
                        contract.match(url,method,kind,authentication=True)

    def test_unknown_requests_and_documents_still_stop(self):
        self.b.contract=contract_for(builtins().get('liepin'))
        for url,method,kind in [(URL,'GET','Document'),(URL.replace('v2-show-4pc','submit'),'POST','XHR'),
                                (URL.replace('api-wanda.liepin.com','other.test'),'POST','XHR')]:
            with self.subTest(url=url):
                self.b.error=None;self.b._halted=False
                event=self.req(method=method,kind=kind);event['request']['url']=url
                self.b._paused('session',event)
                self.assertEqual(self.b.error,'resource_domain_blocked')

    def test_cancel_precedes_optional_handling(self):
        self.b.contract=contract_for(builtins().get('liepin'))
        self.b.cancelled.set()
        for url in (URL,*TELEMETRY):
            self.b.error=None;self.b._halted=False
            event=self.req();event['request']['url']=url
            self.b._paused('session',event)
            self.assertEqual(self.b.error,'paused')

    def test_abort_completion_does_not_become_a_network_failure(self):
        self.b.contract=contract_for(builtins().get('liepin'))
        for url in (URL,*TELEMETRY):
            event=self.req();event['request']['url']=url
            self.b._paused('session',event)
            self.b._received({'sessionId':'session','message':json.dumps({
                'method':'Network.loadingFailed','params':{'requestId':'net-1','errorText':'net::ERR_BLOCKED_BY_CLIENT'}})})
            self.assertIsNone(self.b.error)

    def test_ignored_rules_cannot_claim_business_or_authentication_roles(self):
        contract=contract_for(builtins().get('liepin'))
        for rule in [NativeRule('bad_ignore','example.test','/api'),
                     NativeRule('bad_ignore','example.test','/api',role='asset',authentication=True)]:
            with self.assertRaises(ValueError):
                replace(contract,ignored_rules=(rule,))
