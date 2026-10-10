"""A verified missing dependency stays offline while its publisher handles failure."""
from dataclasses import replace
import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import test_native_acquisition as fixtures
from vibe_job_radar.guided.adapters import builtins
from vibe_job_radar.guided.contracts import CrawlError, PageSnapshot
from vibe_job_radar.guided.native_policy import contract_for, NativeRule
from vibe_job_radar.network import FetchError

HOST = 'dalisi4api.tongdao.cn'
ORIGIN = 'https://' + HOST
URL = ORIGIN + '/static/cfg/v2.json'
NX = 'encrypted_dns_name_not_found'


class ConfigDNSTests(unittest.TestCase):
    def setUp(self):
        fixtures.NativeControllerTests.setUp(self)
        self.b.adapter = builtins().get('liepin')
        self.b.contract = contract_for(self.b.adapter)
        self.b.wire.adapter = self.b.adapter
        for origin in self.b.contract.rule_origins:
            if origin != ORIGIN:
                self.b.wire.install_robots(origin, 404, 'text/html', b'Not Found')
        self.b.tunnel.resolve_host = Mock(side_effect=FetchError(NX))
        self.b.page = Mock(url=self.b.adapter.search_base)
        self.scratch = Mock()
        self.scratch.goto.side_effect = CrawlError(NX)
        self.b._new_page = Mock(return_value=self.scratch)

    def request(self, *, url=URL, method='GET', kind='Fetch'):
        e = fixtures.NativeControllerTests.req(self, method=method, kind=kind)
        e['request'] = {'url':url, 'method':method}
        return e

    def resolved(self):
        self.b.tunnel.resolve_host.side_effect = None
        self.b.tunnel.resolve_host.return_value = ('93.184.216.34',)
        self.scratch.goto.side_effect = None
        self.scratch.goto.return_value = SimpleNamespace(status=404,
            header_value=lambda _: 'text/html', body=lambda: b'Not Found')

    def test_missing_config_does_not_preempt_the_main_document(self):
        self.b._settle = Mock()
        self.b.snapshot = Mock(return_value='observed main document')
        self.assertEqual(self.b.open(self.b.adapter.search_base), 'observed main document')
        self.b.page.goto.assert_called_once()
        self.b.tunnel.resolve_host.assert_called_once_with(HOST)
        self.b._new_page.assert_not_called()
        self.assertNotIn(ORIGIN, self.b.wire.rules)
        self.assertIsNone(self.b.error)
        self.assertFalse(self.b._halted)

    def test_failed_dependency_reaches_fetch_without_egress_or_fake_content(self):
        self.b._load_robots()
        for direct in (False, True):
            for kind in ('Fetch', 'XHR'):
                with self.subTest(direct=direct, kind=kind):
                    self.b._direct_cdp = direct
                    self.b._send.reset_mock()
                    with patch.object(self.b.wire, 'reserve') as reserve:
                        self.b._paused('session', self.request(kind=kind))
                    reserve.assert_not_called()
                    self.b._send.assert_called_once_with('session', 'Fetch.failRequest',
                        {'requestId':'fetch-1', 'errorReason':'NameNotResolved'})
                    self.assertIsNone(self.b.error)
                    self.assertFalse(self.b._requests)
        self.b._received({'sessionId':'session', 'message':json.dumps({
            'method':'Network.loadingFailed', 'params':{'requestId':'net-1',
                'errorText':'net::ERR_NAME_NOT_RESOLVED'}})})
        self.assertIsNone(self.b.error)
        self.assertFalse(self.b.observations())
        self.assertEqual(self.b.wire.ledger.summary('liepin')['request']['day'], 0)
        page = PageSnapshot(self.b.adapter.search_base, '<p>no results</p>',
                            business=self.b.observations(), business_required=True)
        with self.assertRaisesRegex(CrawlError, 'page_not_ready'):
            self.b.adapter.cards(page)

    def test_unreviewed_requests_do_not_inherit_the_dns_failure_exception(self):
        self.b._load_robots()
        for e in (self.request(method='POST'), self.request(kind='Document'),
                  self.request(url=URL+'/extra'), self.request(url=URL.replace(HOST, 'www.liepin.com'))):
            with self.subTest(request=e):
                self.b.error = None
                self.b._halted = False
                self.b._paused('session', e)
                self.assertEqual(self.b.error, 'native_operation_unreviewed')
                self.assertTrue(self.b._halted)

    def test_other_resolver_failures_remain_fatal(self):
        for code in ('dns_error', 'encrypted_dns_unavailable', 'tls_error',
                     'non_public_address', 'local_proxy_auth_failed', 'paused'):
            with self.subTest(code=code):
                self.b.tunnel.resolve_host.side_effect = FetchError(code)
                self.scratch.goto.side_effect = CrawlError(code)
                with self.assertRaisesRegex(CrawlError, code):
                    self.b._load_robots()

    def test_a_different_origin_is_not_treated_as_the_missing_config(self):
        self.b.contract = fixtures.contract()
        self.b.wire.rules.clear()
        with self.assertRaisesRegex(CrawlError, NX):
            self.b._load_robots()
        self.b.tunnel.resolve_host.assert_not_called()

    def test_successful_resolution_still_requires_real_robots(self):
        self.resolved()
        self.b._load_robots()
        self.assertIn(ORIGIN, self.b.wire.rules)
        self.b._paused('session', self.request())
        self.b._send.assert_called_with('session', 'Fetch.continueRequest', {'requestId':'fetch-1'})
        self.assertEqual(self.b.wire.ledger.summary('liepin')['request']['day'], 1)

    def test_previous_global_tunnel_error_cannot_hide_a_robots_refusal(self):
        self.resolved()
        self.b.tunnel.last_error = NX
        self.scratch.goto.side_effect = CrawlError('http_403')
        with self.assertRaisesRegex(CrawlError, 'http_403'):
            self.b._load_robots()

    def test_revocation_and_cancel_during_dns_take_precedence(self):
        for cancelled in (True, False):
            with self.subTest(cancelled=cancelled):
                self.b.error = None
                self.b._halted = False
                self.b.cancelled.clear()
                self.b.policy_check = lambda: True
                def fail(_):
                    if cancelled:
                        self.b.cancelled.set()
                    else:
                        self.b.policy_check = lambda: False
                    raise FetchError(NX)
                self.b.tunnel.resolve_host.side_effect = fail
                with self.assertRaisesRegex(CrawlError, 'paused' if cancelled else 'native_policy_changed'):
                    self.b._load_robots()

    def test_failed_abort_command_is_still_fatal(self):
        self.b._load_robots()
        self.b._send.side_effect = RuntimeError('protocol failure')
        self.b._paused('session', self.request())
        self.assertEqual(self.b.error, 'native_protocol_error')
        self.assertTrue(self.b._halted)

    def test_next_open_checks_the_missing_origin_again(self):
        self.b._load_robots()
        self.resolved()
        self.b._load_robots()
        self.b._paused('session', self.request())
        self.b._send.assert_called_with('session', 'Fetch.continueRequest', {'requestId':'fetch-1'})
        self.assertEqual(self.b.tunnel.resolve_host.call_count, 2)

    def test_only_explicit_read_dependencies_can_declare_failure_handling(self):
        rule = self.b.contract.match(URL, 'GET', 'Fetch')
        self.assertTrue(rule.handles_dns_failure)
        for change in ({'methods':('POST',)}, {'resources':('Document',)},
                       {'role':'login'}, {'authentication':True}, {'resources':()}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                replace(rule, **change)
        mixed = replace(self.b.contract, rules=(*self.b.contract.rules,
            NativeRule('required_read', HOST, r'/required')))
        self.assertEqual(self.b.contract.dns_failure_origins, (ORIGIN,))
        self.assertNotIn(ORIGIN, mixed.dns_failure_origins)


class ResolverProbeTests(unittest.TestCase):
    setUp = fixtures.TunnelTests.setUp
    tearDown = fixtures.TunnelTests.tearDown

    def test_probe_uses_same_validated_policy_without_opening_target_socket(self):
        with patch('socket.getaddrinfo', return_value=[
                (2,1,6,'',('93.184.216.34',443))]), patch('socket.create_connection') as dial:
            self.assertEqual(self.guard.resolve_host(fixtures.HOST), ('93.184.216.34',))
        dial.assert_not_called()

    def test_probe_cannot_turn_unknown_or_private_hosts_into_availability(self):
        for host, addresses in (('not-approved.test', [('93.184.216.34',443)]),
                                (fixtures.HOST, [('10.0.0.1',443)])):
            with self.subTest(host=host), patch('socket.getaddrinfo',
                    return_value=[(2,1,6,'',address) for address in addresses]), patch('socket.create_connection') as dial:
                with self.assertRaises(FetchError):
                    self.guard.resolve_host(host)
                dial.assert_not_called()
