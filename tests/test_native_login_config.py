"""The publisher's login UI configuration is a read, not an account action or job result."""
import unittest
from unittest.mock import patch

import test_native_acquisition as fixtures
from vibe_job_radar.guided.adapters import builtins
from vibe_job_radar.guided.contracts import CrawlError
from vibe_job_radar.guided.native_policy import contract_for


URL='https://api-c.liepin.com/api/com.liepin.pupa.get-pc-login-scan-config'
ORIGIN='https://www.liepin.com'


class LoginConfigTests(unittest.TestCase):
    setUp=fixtures.NativeControllerTests.setUp
    req=fixtures.NativeControllerTests.req

    def event(self,method='POST',kind='XHR'):
        event=self.req(method=method,kind=kind)
        event['request'].update(url=URL,postData='',headers={'Origin':ORIGIN,'X-Client-Type':'web'})
        if method=='OPTIONS':event['request']['headers'].update({
            'Access-Control-Request-Method':'POST','Access-Control-Request-Headers':'x-client-type'})
        return event

    def test_configuration_read_is_available_before_auth_without_granting_account_actions(self):
        contract=contract_for(builtins().get('liepin'))
        for method,kind in [('POST','XHR'),('POST','Fetch'),('OPTIONS','Preflight'),('OPTIONS','XHR')]:
            with self.subTest(method=method,kind=kind):
                rule=contract.match(URL,method,kind,authentication=False)
                self.assertEqual(rule.role,'login')
                self.assertFalse(rule.authentication)
                self.assertFalse(contract.ignored_request(URL,method,kind))
                rule.validate_headers(method,self.event(method,kind)['request']['headers'])
        password='https://api-passport.liepin.com/api/com.liepin.passport.account.account-pwd-login'
        with self.assertRaisesRegex(CrawlError,'native_operation_unreviewed'):
            contract.match(password,'POST','XHR',authentication=False)
        self.assertTrue(contract.match(password,'POST','XHR',authentication=True).authentication)

    def test_real_browser_request_is_counted_and_checked_but_response_never_becomes_job_data(self):
        self.b.contract=contract_for(builtins().get('liepin'))
        for method,kind in [('POST','XHR'),('OPTIONS','Preflight')]:
            event=self.event(method,kind)
            self.b.wire.install_robots('https://api-c.liepin.com',200,'text/plain',b'User-agent: *\nAllow: /\n')
            with patch.object(self.b.wire.ledger,'reserve',wraps=self.b.wire.ledger.reserve) as reserve:
                self.b._paused('session',event)
            self.assertIsNone(self.b.error)
            reserve.assert_called_once_with('fixture','request',origin='https://api-c.liepin.com')
            self.b._send.assert_called_with('session','Fetch.continueRequest',{'requestId':'fetch-1'})
            response={**event,'responseStatusCode':200,'responseHeaders':[{'name':'Content-Type','value':'application/json'}]}
            self.b._paused('session',response)
            self.b._send.reset_mock()
            self.b._finished('session',{'requestId':'net-1'})
            self.b._send.assert_not_called()  # No Network.getResponseBody or stored login/config payload.
            self.assertFalse(self.b._observations)
            self.assertFalse(self.b._requests)
        self.assertEqual(self.b.native_counts['login'],2)  # Request roles, not password submissions.
        self.assertEqual(self.b.native_counts['business'],0)
        self.assertFalse(self.b.auth_mode)
        self.assertEqual(self.b.wire.ledger.summary('fixture')['request']['day'],2)
        self.assertEqual(self.b.wire.ledger.summary('fixture')['login']['day'],0)

    def test_other_methods_document_paths_and_hosts_stay_unreviewed(self):
        contract=contract_for(builtins().get('liepin'))
        for url,method,kind in [(URL,'GET','XHR'),(URL,'POST','Document'),(URL,'POST','Script'),
                                (URL+'/extra','POST','XHR'),(URL.replace('scan-config','scan-code'),'POST','XHR'),
                                (URL.replace('api-c.','api-passport.'),'POST','XHR')]:
            with self.subTest(url=url,method=method,kind=kind),self.assertRaises(CrawlError):
                contract.match(url,method,kind,authentication=True)

    def test_preflight_keeps_exact_origin_post_and_header_contract(self):
        self.b.contract=contract_for(builtins().get('liepin'))
        for headers in ({'Origin':'https://other.invalid'}, {'Access-Control-Request-Method':'GET'},
                        {'Access-Control-Request-Headers':'authorization'}):
            with self.subTest(headers=headers):
                self.b.error=None;self.b._halted=False
                event=self.event('OPTIONS','XHR');event['request']['headers'].update(headers)
                with patch.object(self.b.wire.ledger,'reserve') as reserve:
                    self.b._paused('session',event)
                reserve.assert_not_called()
                self.assertEqual(self.b.error,'native_operation_unreviewed')

    def test_publisher_refusal_is_required_failure_and_never_replaced_with_config(self):
        self.b.contract=contract_for(builtins().get('liepin'))
        event=self.event('OPTIONS','XHR')
        with patch.object(self.b.wire,'ensure_robots'),patch.object(self.b.wire,'reserve'):
            self.b._paused('session',event)
        self.assertIsNone(self.b.error)
        self.b._paused('session',{**event,'responseStatusCode':403,'responseHeaders':[]})
        self.assertEqual(self.b.error,'http_403');self.assertTrue(self.b._halted)
        self.assertFalse(self.b._observations)
        self.b._send.assert_called_with('session','Fetch.failRequest',{'requestId':'fetch-1','errorReason':'BlockedByClient'})
