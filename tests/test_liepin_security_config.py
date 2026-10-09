"""The publisher's fixed security configuration is a required read, never a job."""
import json
import unittest
from unittest.mock import patch
from types import SimpleNamespace

import test_native_acquisition as fixtures
from vibe_job_radar.guided.adapters import builtins
from vibe_job_radar.guided.contracts import CrawlError, PageSnapshot
from vibe_job_radar.guided.native_policy import contract_for

HOST = 'dalisi4api.tongdao.cn'
ORIGIN = 'https://' + HOST
URL = ORIGIN + '/static/cfg/v2.json'


class LiepinSecurityConfigTests(unittest.TestCase):
    def setUp(self):
        fixtures.NativeControllerTests.setUp(self)
        self.b.adapter = builtins().get('liepin')
        self.b.contract = contract_for(self.b.adapter)
        self.b.page = SimpleNamespace(url=self.b.adapter.search_base)
        self.b.wire.adapter = self.b.adapter
        self.b.wire.install_robots(ORIGIN, 404, 'text/html', b'Not Found')

    def request(self, url=URL, method='GET', kind='XHR'):
        event = fixtures.NativeControllerTests.req(self, method=method, kind=kind)
        event['request']['url'] = url
        event['request'].pop('postData')
        return event

    def response(self, status=200):
        event = self.request()
        event.update(responseStatusCode=status,
                     responseHeaders=[{'name':'content-type', 'value':'application/json'}])
        return event

    def test_exact_get_is_required_accounted_and_not_an_ignored_request(self):
        contract = self.b.contract
        for kind in ('XHR', 'Fetch'):
            with self.subTest(kind=kind):
                rule = contract.match(URL, 'GET', kind)
                self.assertEqual(rule.key, 'liepin_security_config')
                self.assertEqual(rule.role, 'business')
                self.assertFalse(rule.authentication)
                self.assertFalse(contract.ignored_request(URL, 'GET', kind))
        self.assertIn(ORIGIN, contract.rule_origins)
        self.b._paused('session', self.request())
        self.assertIsNone(self.b.error)
        self.b._send.assert_called_with('session', 'Fetch.continueRequest',
                                       {'requestId':'fetch-1'})
        self.assertEqual(self.b.native_counts['business'], 1)
        summary = self.b.wire.ledger.summary('liepin')
        self.assertEqual(summary['request']['day'], 1)
        self.assertEqual(summary['page']['day'], 0)
        self.assertEqual(summary['login']['day'], 0)

    def test_methods_resources_and_nearby_paths_do_not_gain_permission(self):
        cases = [(URL, method, 'XHR') for method in ('POST','HEAD','OPTIONS','PUT')]
        cases += [(URL, 'GET', kind) for kind in ('Document','Script','Image','Preflight')]
        cases += [(URL + '/extra', 'GET', 'XHR'),
                  (URL.replace('v2.json','v3.json'), 'GET', 'XHR'),
                  (ORIGIN + '/api/config', 'GET', 'XHR')]
        for url, method, kind in cases:
            with self.subTest(url=url, method=method, kind=kind):
                with self.assertRaises(CrawlError):
                    self.b.contract.match(url, method, kind, authentication=True)
                self.assertFalse(self.b.contract.ignored_request(url, method, kind))

    def test_unreviewed_method_stops_before_quota_or_network(self):
        for method in ('POST', 'OPTIONS'):
            with self.subTest(method=method):
                self.b.error = None
                self.b._halted = False
                with patch.object(self.b.wire, 'reserve') as reserve:
                    self.b._paused('session', self.request(method=method))
                reserve.assert_not_called()
                self.assertTrue(self.b._halted)
                self.b._send.assert_called_with('session', 'Fetch.failRequest',
                    {'requestId':'fetch-1', 'errorReason':'BlockedByClient'})

    def test_exact_https_host_and_port_are_required(self):
        for url in (URL.replace('https:', 'http:'),
                    URL.replace(HOST, HOST + '.other.test'),
                    URL.replace(HOST, 'other.tongdao.cn'),
                    URL.replace(HOST, HOST + ':444'),
                    URL.replace(HOST, 'user:pass@' + HOST)):
            with self.subTest(url=url), self.assertRaises(CrawlError):
                self.b.contract.match(url, 'GET', 'XHR')

    def test_robots_denial_stops_configuration_before_request_accounting(self):
        self.b.wire.install_robots(ORIGIN, 200, 'text/plain',
                                  b'User-agent: *\nDisallow: /static/\n')
        with patch.object(self.b.wire, 'reserve') as reserve:
            self.b._paused('session', self.request())
        reserve.assert_not_called()
        self.assertEqual(self.b.error, 'robots_denied')
        self.assertTrue(self.b._halted)

    def test_publisher_403_remains_fatal_and_cannot_supply_a_job(self):
        self.b._paused('session', self.request())
        self.b._paused('session', self.response(403))
        self.assertEqual(self.b.error, 'http_403')
        self.assertTrue(self.b._halted)
        self.assertFalse(self.b.observations())

    def test_native_json_is_not_rewritten_or_used_as_search_result(self):
        self.b._paused('session', self.request())
        self.b._paused('session', self.response())
        self.b._send.assert_called_with('session', 'Fetch.continueResponse',
                                       {'requestId':'fetch-1'})
        self.b._finished('session', {'requestId':'net-1'})
        self.b._send.assert_called_with('session', 'Network.getResponseBody',
                                       {'requestId':'net-1'}, self.b._send.call_args.args[3])
        callback = self.b._send.call_args.args[3]
        payload = {'flag':1, 'data':{'jobCardList':[{'title':'synthetic non-job'}]}}
        callback({'body':json.dumps(payload)})
        observations = self.b.observations()
        self.assertEqual(len(observations), 1)
        self.assertEqual(observations[0].operation, 'liepin_security_config')
        self.assertEqual(observations[0].payload, payload)
        self.assertFalse(self.b.adapter.native_ready(observations))
        page = PageSnapshot(self.b.adapter.search_base, '<p>synthetic</p>',
                            business=observations, business_required=True)
        with self.assertRaisesRegex(CrawlError, 'page_not_ready'):
            self.b.adapter.cards(page)

    def test_cancel_and_policy_revocation_still_precede_the_allowed_read(self):
        self.b.cancelled.set()
        self.b._paused('session', self.request())
        self.assertEqual(self.b.error, 'paused')
        self.b.cancelled.clear()
        self.b.error = None
        self.b._halted = False
        self.b.policy_check = lambda:False
        self.b._paused('session', self.request())
        self.assertEqual(self.b.error, 'native_policy_changed')
