"""Production bridge policy, synthetic responses; no SMS or account requests."""
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from vibe_job_radar.guided.adapters import builtins
from vibe_job_radar.guided.browser import PlaywrightBackend
from vibe_job_radar.guided.contracts import CrawlError
from vibe_job_radar.guided import liepin_login_policy as policy
from vibe_job_radar.guided.native_policy import contract_for
from vibe_job_radar.guided.transport import WireResponse


SMS = 'https://api-passport.liepin.com/api/com.liepin.passport.captcha.send-sms'
VERIFY = 'https://turing.captcha.qcloud.com/cap_union_new_verify'
FRAME = 'https://turing.captcha.qcloud.com/template/drag_ele.html'
ORIGIN = {'origin': 'https://www.liepin.com'}
CONFIG = 'https://dalisi4api.tongdao.cn/static/cfg/v2.json'


class ManualLoginPolicyTests(unittest.TestCase):
    def test_sms_qr_and_two_factor_require_explicit_login(self):
        for path in ('captcha.send-sms', 'captcha.get-smart-captcha',
                     'account.tel-sms-login', 'account.send-two-factor-sms-code',
                     'account.verify-two-factor-sms-code', 'qr.get-qrcode', 'qr.get-mini-qrcode', 'qr.ack'):
            url = 'https://api-passport.liepin.com/api/com.liepin.passport.' + path
            for origin in ('https://www.liepin.com', 'https://c.liepin.com'):
                with self.subTest(path=path, origin=origin):
                    rule = policy.match(url, 'POST', 'xhr', authentication=True, headers={'origin': origin})
                    self.assertEqual(rule.role, 'login')
                    with self.assertRaises(CrawlError):
                        policy.match(url, 'POST', 'xhr', authentication=False, headers={'origin': origin})

    def test_new_hosts_reject_other_paths_methods_origins_and_subdomains(self):
        cases = [
            (SMS, 'GET', 'xhr', ORIGIN),
            (VERIFY, 'POST', 'xhr', {'origin': 'https://outside.example'}),
            (VERIFY, 'DELETE', 'xhr', {'origin': 'https://turing.captcha.qcloud.com'}),
            (VERIFY.replace('/cap_union_new_verify', '/arbitrary'), 'POST', 'xhr', {}),
            (FRAME.replace('turing.captcha', 'unreviewed.turing.captcha'), 'GET', 'document', {}),
            (FRAME.replace('/template/drag_ele.html', '/arbitrary.html'), 'GET', 'document', {}),
            (FRAME.replace('https:', 'http:'), 'GET', 'document', {}),
        ]
        for url, method, kind, headers in cases:
            with self.subTest(url=url, method=method), self.assertRaises(CrawlError):
                policy.match(url, method, kind, authentication=True, headers=headers)

    def test_widget_resources_are_authentication_only(self):
        for url, kind in ((FRAME, 'document'),
                          ('https://turing.captcha.qcloud.com/TCaptcha.js', 'script'),
                          ('https://turing.captcha.gtimg.com/1/dy-ele.2bed7b36.js', 'script')):
            with self.subTest(url=url):
                self.assertIsNotNone(policy.match(url, 'GET', kind, authentication=True, headers={}))
                with self.assertRaises(CrawlError):
                    policy.match(url, 'GET', kind, authentication=False, headers={})

    def test_preflight_cannot_grant_another_write_or_secret_header(self):
        good = {**ORIGIN, 'access-control-request-method': 'POST',
                'access-control-request-headers': 'content-type,x-client-type'}
        self.assertIsNotNone(policy.match(SMS, 'OPTIONS', 'other', authentication=True, headers=good))
        for change in ({'access-control-request-method': 'PUT'},
                       {'access-control-request-headers': 'authorization'}):
            with self.subTest(change=change), self.assertRaises(CrawlError):
                policy.match(SMS, 'OPTIONS', 'other', authentication=True, headers={**good, **change})

    def test_native_checkpoint_contract_is_not_silently_expanded(self):
        contract = contract_for(builtins().get('liepin'))
        self.assertEqual(contract.key, 'liepin_search_login_v6')
        with self.assertRaises(CrawlError):
            contract.match(SMS, 'POST', 'XHR', authentication=True)

    def test_required_security_config_has_only_the_existing_exact_native_scope(self):
        native = contract_for(builtins().get('liepin')).match(CONFIG, 'GET', 'Fetch')
        self.assertEqual(policy.match(CONFIG, 'GET', 'fetch', authentication=False, headers={}), native)
        for url, method, kind in ((CONFIG, 'POST', 'fetch'), (CONFIG, 'GET', 'document'),
                                 (CONFIG.replace('v2.json', 'v3.json'), 'GET', 'fetch'),
                                 (CONFIG.replace('dalisi4api.', 'other.'), 'GET', 'fetch'),
                                 (CONFIG.replace('dalisi4api.', 'sub.dalisi4api.'), 'GET', 'fetch')):
            with self.subTest(url=url, method=method, kind=kind):
                try:
                    result = policy.match(url, method, kind, authentication=True, headers={})
                except CrawlError:
                    continue
                self.assertIsNone(result)


class ManualLoginRouteTests(unittest.TestCase):
    def backend(self, authentication=True):
        b = PlaywrightBackend.__new__(PlaywrightBackend)
        b.adapter = builtins().get('liepin')
        b.auth_mode, b.error, b.wait_error = authentication, None, None
        b.cancelled = threading.Event()
        b.resource_denials, b.redirects, b._pagination_page = set(), 0, None
        b.wire = Mock()
        b.wire.allowed_resource.return_value = True
        b.wire.fetch.return_value = WireResponse(200, {'content-type': 'application/json'}, b'{}')
        b.context = Mock()
        b.page = Mock()
        b.page.main_frame = SimpleNamespace(parent_frame=None, url='https://www.liepin.com/zhaopin/')
        return b

    def route(self, backend, url=SMS, method='POST', kind='xhr', headers=None, child=False):
        route = Mock()
        route.request.url, route.request.method, route.request.resource_type = url, method, kind
        route.request.all_headers.return_value = ORIGIN if headers is None else headers
        route.request.post_data_buffer = b'publisher-generated-body' if method == 'POST' else None
        route.request.frame = (SimpleNamespace(parent_frame=backend.page.main_frame, url='about:blank')
                               if child else backend.page.main_frame)
        backend._route(route)
        return route

    def test_normal_sms_is_forwarded_once_unchanged_only_during_login(self):
        b = self.backend()
        route = self.route(b)
        route.fulfill.assert_called_once()
        b.wire.fetch.assert_called_once_with(SMS, 'POST', ORIGIN, b'publisher-generated-body', required=True)
        b = self.backend(False)
        route = self.route(b)
        route.abort.assert_called_once()
        b.wire.fetch.assert_not_called()

    def test_unapproved_account_writes_remain_blocked(self):
        for suffix in ('account-pwd-register', 'logout', 'set-init-pwd'):
            b = self.backend()
            self.route(b, 'https://api-passport.liepin.com/api/com.liepin.passport.account.' + suffix)
            b.wire.fetch.assert_not_called()
            self.assertEqual(b.error, 'native_operation_unreviewed')

    def test_widget_is_allowed_as_child_but_not_main_navigation(self):
        b = self.backend()
        route = self.route(b, FRAME, 'GET', 'document', {}, child=True)
        route.fulfill.assert_called_once()
        b.wire.reserve.assert_not_called()  # fetch still accounts the HTTP request.
        b = self.backend()
        self.route(b, FRAME, 'GET', 'document', {})
        b.wire.fetch.assert_not_called()
        self.assertEqual(b.error, 'login_origin_changed')

    def test_widget_child_cannot_belong_to_an_unrelated_page(self):
        b = self.backend()
        b.page.main_frame.url = 'https://outside.example/'
        self.route(b, FRAME, 'GET', 'document', {}, child=True)
        b.wire.fetch.assert_not_called()

    def test_captcha_refusal_and_rate_limit_do_not_retry_or_become_optional(self):
        for code in ('http_403', 'http_429'):
            b = self.backend()
            b.wire.fetch.side_effect = CrawlError(code)
            self.route(b, 'https://turing.captcha.qcloud.com/TCaptcha.js', 'GET', 'script', {})
            b.wire.fetch.assert_called_once()
            self.assertTrue(b.wire.fetch.call_args.kwargs['required'])
            self.assertEqual(b.error, code)

    def test_optional_marketing_aborts_without_network_or_poisoning_login(self):
        b = self.backend()
        route = self.route(b, 'https://statistic.liepin.com/statisticPlatform/standardFLog.json')
        route.abort.assert_called_once()
        b.wire.fetch.assert_not_called()
        self.assertIsNone(b.error)

    def test_collection_revokes_captcha_access(self):
        b = self.backend()
        b.collection_mode()
        self.route(b, FRAME, 'GET', 'document', {}, child=True)
        b.wire.fetch.assert_not_called()

    def test_idle_browser_error_does_not_erase_the_known_required_failure(self):
        b = self.backend()
        b.page.is_closed.return_value = False
        b.page.wait_for_timeout.side_effect = RuntimeError('synthetic browser callback failure')
        b.error = 'encrypted_dns_cooldown'
        with self.assertRaisesRegex(CrawlError, 'encrypted_dns_cooldown'): b.pump()
        b.error = None
        with self.assertRaises(RuntimeError): b.pump()

    def test_normal_image_connection_failure_does_not_cancel_human_login(self):
        b = self.backend()
        b.wire.fetch.side_effect = CrawlError('network_error')
        route = self.route(b, 'https://concat.lietou-static.com/fe-www-pc/v6/img/login.png', 'GET', 'image', {})
        route.abort.assert_called_once_with('blockedbyclient')
        route.fulfill.assert_not_called()
        self.assertIsNone(b.error)
        self.assertIn('optional_network_error', b.resource_denials)
        b.wire.fetch.assert_called_once()

    def test_image_exception_never_softens_captcha_script_or_dns_security_failures(self):
        cases = [
            ('https://turing.captcha.qcloud.com/cap_union_new_getcapbysig', 'image', 'network_error'),
            ('https://concat.lietou-static.com/fe-www-pc/v6/js/login.js', 'script', 'network_error'),
            ('https://concat.lietou-static.com/fe-www-pc/v6/img/login.png', 'image', 'non_public_address'),
            ('https://concat.lietou-static.com/fe-www-pc/v6/img/login.png', 'image', 'tls_verification_failed'),
            ('https://concat.lietou-static.com/fe-www-pc/v6/img/login.png', 'image', 'encrypted_dns_expired_answer'),
        ]
        for url, kind, code in cases:
            with self.subTest(kind=kind, code=code):
                b = self.backend(); b.wire.fetch.side_effect = CrawlError(code)
                route = self.route(b, url, 'GET', kind, {})
                self.assertEqual(b.error, code)
                route.fulfill.assert_not_called()

    def test_image_exception_is_only_for_liepin_manual_login(self):
        for other_platform in (False, True):
            with self.subTest(other_platform=other_platform):
                b = self.backend(authentication=other_platform)
                if other_platform:
                    b.adapter = SimpleNamespace(key='other', login_url='https://other.example/')
                b.wire.fetch.side_effect = CrawlError('network_error')
                route = self.route(b, 'https://concat.lietou-static.com/fe-www-pc/v6/img/login.png', 'GET', 'image', {})
                self.assertEqual(b.error, 'network_error')
                route.fulfill.assert_not_called()

    def test_security_config_is_checked_and_forwarded_unchanged_once(self):
        b = self.backend()
        response = b.wire.fetch.return_value = WireResponse(200, {'content-type': 'application/json'}, b'{"real":"config"}')
        route = self.route(b, CONFIG, 'GET', 'fetch', {})
        b.wire.ensure_robots.assert_called_once_with(CONFIG)
        b.wire.fetch.assert_called_once_with(CONFIG, 'GET', {}, None, required=True)
        self.assertEqual(route.fulfill.call_args.kwargs['body'], response.body)
        self.assertIsNone(b.error)

    def test_security_robots_denial_and_hard_failures_stop_without_retry(self):
        for code in ('robots_denied', 'encrypted_dns_expired_answer', 'tls_verification_failed', 'http_403'):
            b = self.backend()
            if code == 'robots_denied':
                b.wire.ensure_robots.side_effect = CrawlError(code)
            else:
                b.wire.fetch.side_effect = CrawlError(code)
            route = self.route(b, CONFIG, 'GET', 'fetch', {})
            self.assertEqual(b.error, code)
            self.assertLessEqual(b.wire.fetch.call_count, 1)
            if code == 'robots_denied': b.wire.fetch.assert_not_called()
            route.fulfill.assert_not_called()

    def test_only_config_nxdomain_is_delivered_as_failure_for_untouched_publisher_handler(self):
        for phase in ('ensure_robots', 'fetch'):
            b = self.backend()
            getattr(b.wire, phase).side_effect = CrawlError('encrypted_dns_name_not_found')
            route = self.route(b, CONFIG, 'GET', 'fetch', {})
            route.abort.assert_called_once_with('namenotresolved')
            route.fulfill.assert_not_called()
            self.assertIsNone(b.error)
        b = self.backend()
        b.wire.fetch.side_effect = CrawlError('encrypted_dns_name_not_found')
        self.route(b)
        self.assertEqual(b.error, 'encrypted_dns_name_not_found')
