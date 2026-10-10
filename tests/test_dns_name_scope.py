"""A missing DNS name must not label another allowed origin nonexistent."""
from dataclasses import replace
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from urllib.parse import urlsplit

import test_encrypted_dns as dns_fixtures
import test_native_config_dns as native_fixtures
from vibe_job_radar.dns_wire import Answer, ResolutionError
from vibe_job_radar.guided.native_tunnel import NativeTunnel

MISSING = 'missing.example.org'
OTHER = 'search.example.org'
NX = 'encrypted_dns_name_not_found'


class NameFailureScopeTests(unittest.TestCase):
    setUp = dns_fixtures.ResolverTests.setUp

    def positive(self, host, kind, *_args, **_kwargs):
        return Answer((dns_fixtures.IP4 if kind == 1 else dns_fixtures.IP6,), 60, host)

    def missing(self, host=MISSING, policy=None):
        with self.assertRaisesRegex(ResolutionError, NX):
            self.r.resolve(host, policy or self.p)

    def test_missing_name_does_not_prevent_a_different_host_lookup(self):
        def answer(host, kind, *args, **kwargs):
            if host == MISSING:
                raise ResolutionError(NX)
            return self.positive(host, kind)
        self.ex.side_effect = answer
        self.missing()
        value = self.r.resolve(OTHER, self.p)
        self.assertEqual(value.addresses, (dns_fixtures.IP4, dns_fixtures.IP6))
        self.assertEqual([call.args[0] for call in self.ex.call_args_list], [MISSING, OTHER, OTHER])
        self.assertEqual(len(self.r._requests), 2)
        self.assertNotIn((MISSING, self.p.fingerprint, dns_fixtures.PROVIDER), self.r._cache)

    def test_same_name_keeps_original_failure_without_extra_lookup_or_budget(self):
        self.ex.side_effect = ResolutionError(NX)
        self.missing()
        self.ex.side_effect = self.positive
        self.missing(MISSING.upper())
        self.now[0] += 29
        self.missing()
        self.assertEqual(self.ex.call_count, 1)
        self.assertEqual(len(self.r._requests), 1)
        self.now[0] += 1
        self.assertEqual(self.r.resolve(MISSING, self.p).addresses[0], dns_fixtures.IP4)
        self.assertEqual(self.ex.call_count, 3)
        self.assertEqual(len(self.r._requests), 2)

    def test_failure_is_bound_to_the_resolving_network_policy(self):
        self.ex.side_effect = ResolutionError(NX)
        self.missing()
        self.ex.side_effect = self.positive
        changed = replace(self.p, source='explicit_application')
        self.assertEqual(self.r.resolve(MISSING, changed).addresses[0], dns_fixtures.IP4)
        self.missing()  # The old route still retains its original failure.
        self.assertEqual(self.ex.call_count, 3)

    def test_clear_retains_name_backoff_and_budget_without_blocking_other_names(self):
        self.ex.side_effect = ResolutionError(NX)
        self.missing()
        self.r.clear()
        self.ex.side_effect = self.positive
        self.missing()
        self.assertEqual(self.r.resolve(OTHER, self.p).addresses[0], dns_fixtures.IP4)
        self.assertEqual(self.ex.call_count, 3)
        self.assertEqual(len(self.r._requests), 2)

    def test_provider_security_and_transport_failures_still_apply_across_names(self):
        for code in ('encrypted_dns_tls_failed', 'encrypted_dns_non_public_answer',
                     'encrypted_dns_invalid_response', 'encrypted_dns_http_rejected',
                     'encrypted_dns_refused', 'encrypted_dns_timeout', 'encrypted_dns_unavailable'):
            with self.subTest(code=code):
                self.now[0] += 31
                self.ex.reset_mock()
                self.ex.side_effect = ResolutionError(code)
                with self.assertRaisesRegex(ResolutionError, code):
                    self.r.resolve(MISSING, self.p)
                again = 'encrypted_dns_cooldown' if code in ('encrypted_dns_timeout', 'encrypted_dns_unavailable') else code
                with self.assertRaisesRegex(ResolutionError, again):
                    self.r.resolve(OTHER, self.p)
                self.assertEqual(self.ex.call_count, 1)

    def test_later_provider_failure_takes_precedence_over_name_backoff(self):
        self.ex.side_effect = ResolutionError(NX)
        self.missing()
        self.ex.side_effect = ResolutionError('encrypted_dns_tls_failed')
        with self.assertRaisesRegex(ResolutionError, 'encrypted_dns_tls_failed'):
            self.r.resolve(OTHER, self.p)
        with self.assertRaisesRegex(ResolutionError, 'encrypted_dns_tls_failed'):
            self.r.resolve(MISSING, self.p)
        self.assertEqual(self.ex.call_count, 2)

    def test_revocation_and_cancel_precede_the_name_backoff(self):
        self.ex.side_effect = ResolutionError(NX)
        self.missing()
        self.r.permission = lambda: False
        with self.assertRaisesRegex(ResolutionError, 'encrypted_dns_disabled'):
            self.r.resolve(MISSING, self.p)
        self.r.permission = lambda: True
        cancelled = threading.Event()
        cancelled.set()
        with self.assertRaisesRegex(ResolutionError, 'paused'):
            self.r.resolve(OTHER, self.p, cancelled=cancelled)
        self.assertEqual(self.ex.call_count, 1)

    def test_private_system_answer_is_not_hidden_by_earlier_name_failure(self):
        self.ex.side_effect = ResolutionError(NX)
        self.missing()
        with patch('socket.getaddrinfo', return_value=dns_fixtures.fake_answers('10.0.0.1')):
            with self.assertRaisesRegex(ResolutionError, 'non_public_address'):
                self.r.resolve(MISSING, self.p)
        self.assertEqual(self.ex.call_count, 1)

    def test_distinct_missing_names_still_share_the_full_lookup_budget(self):
        self.ex.side_effect = ResolutionError(NX)
        for i in range(60):
            self.missing(f'missing-{i}.example.org')
        with self.assertRaisesRegex(ResolutionError, 'encrypted_dns_budget'):
            self.r.resolve(OTHER, self.p)
        self.assertEqual(self.ex.call_count, 60)
        self.assertEqual(len(self.r._requests), 60)

    def test_clock_rollback_is_not_hidden_by_name_backoff(self):
        self.ex.side_effect = ResolutionError(NX)
        self.missing()
        self.now[0] -= 1
        with self.assertRaisesRegex(ResolutionError, 'encrypted_dns_clock_rollback'):
            self.r.resolve(OTHER, self.p)
        self.assertEqual(self.ex.call_count, 1)

    def test_missing_second_family_never_exposes_the_first_family_as_success(self):
        self.ex.side_effect = [Answer((dns_fixtures.IP4,), 60, MISSING), ResolutionError(NX)]
        self.missing()
        self.assertFalse(self.r._cache)
        self.ex.side_effect = self.positive
        self.assertEqual(self.r.resolve(OTHER, self.p).addresses[0], dns_fixtures.IP4)
        self.assertEqual(self.ex.call_count, 4)


class NativePreflightNameScopeTests(unittest.TestCase):
    def setUp(self):
        native_fixtures.ConfigDNSTests.setUp(self)
        dns_fixtures.ResolverTests.setUp(self)

    def test_actual_resolver_and_native_preflight_keep_missing_config_offline(self):
        tunnel = NativeTunnel.__new__(NativeTunnel)
        tunnel.hosts = frozenset(self.b.contract.hosts)
        tunnel.policy = self.p
        tunnel.cancelled = self.b.cancelled
        tunnel._stop = threading.Event()
        tunnel.last_error = ''
        self.b.tunnel = tunnel
        self.b.wire.rules.clear()
        def exchange(host, kind, *_args, **_kwargs):
            if host == native_fixtures.HOST:
                raise ResolutionError(NX)
            return Answer((dns_fixtures.IP4,), 60, host)
        self.ex.side_effect = exchange
        navigations = []
        def goto(url, **_kwargs):
            host = urlsplit(url).hostname
            tunnel.resolve_host(host)
            navigations.append(host)
            return SimpleNamespace(status=404, header_value=lambda _: 'text/html', body=lambda: b'Not Found')
        self.b._new_page = Mock(side_effect=lambda: Mock(goto=goto))
        with patch('socket.create_connection') as dial:
            self.b._load_robots()
        dial.assert_not_called()
        self.assertNotIn(native_fixtures.HOST, navigations)
        self.assertIn('api-c.liepin.com', navigations)
        self.assertIn('api-dok.liepin.com', navigations)
        self.assertNotIn(native_fixtures.ORIGIN, self.b.wire.rules)
        self.assertEqual(self.b._unresolved_origins, {native_fixtures.ORIGIN})
        self.assertIsNone(self.b.error)
