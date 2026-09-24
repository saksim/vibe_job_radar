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
TELEMETRY='https://statistic.liepin.com/statisticPlatform/standardFLog.json'


class OptionalRequestTests(unittest.TestCase):
    setUp=fixtures.NativeControllerTests.setUp
    req=fixtures.NativeControllerTests.req

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
        for method, kind in [('POST','XHR'),('OPTIONS','XHR'),('OPTIONS','Preflight')]:
            event=self.req(method=method,kind=kind);event['request']['url']=TELEMETRY
            with patch.object(self.b.wire,'reserve') as reserve:
                self.b._paused('session',event)
            reserve.assert_not_called()
            self.assertIsNone(self.b.error)
        self.assertNotIn('statistic.liepin.com',self.b.contract.hosts)
        trace=self.b._diagnostics.snapshot()
        self.assertIsNone(trace['first_content_candidate'])
        self.assertTrue(all(e['local_block'] for e in trace['events'] if e['code']=='native_optional_request_blocked'))

    def test_same_host_other_statistics_routes_and_get_are_not_ignored(self):
        contract=contract_for(builtins().get('liepin'))
        for url, method in [(TELEMETRY,'GET'),(TELEMETRY+'/extra','POST'),
                            (TELEMETRY.replace('standardFLog.json','account'),'POST')]:
            self.assertFalse(contract.ignored_request(url,method,'XHR'))

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
        event=self.req();event['request']['url']=URL
        self.b._paused('session',event)
        self.assertEqual(self.b.error,'paused')

    def test_abort_completion_does_not_become_a_network_failure(self):
        self.b.contract=contract_for(builtins().get('liepin'))
        event=self.req();event['request']['url']=URL
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
