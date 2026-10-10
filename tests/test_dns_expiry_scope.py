"""Expired DNS answers cannot prejudge a different permitted lookup."""
from dataclasses import replace
import threading
import unittest
from unittest.mock import Mock, patch

import test_encrypted_dns as fixtures
from vibe_job_radar.dns_wire import Answer, ResolutionError
from vibe_job_radar.guided.native_tunnel import NativeTunnel
from vibe_job_radar.network import FetchError, SafeHTTP

STALE = 'stale.example.org'
FRESH = 'fresh.example.org'
EXPIRED = 'encrypted_dns_expired_answer'
NX = 'encrypted_dns_name_not_found'


class ExpiryNameScopeTests(unittest.TestCase):
    setUp = fixtures.ResolverTests.setUp

    def positive(self, host, kind, *_args, **_kwargs):
        return Answer((fixtures.IP4 if kind == 1 else fixtures.IP6,), 60, host)

    def expire(self, host=STALE, policy=None):
        with self.assertRaisesRegex(ResolutionError, '^' + EXPIRED + '$'):
            self.r.resolve(host, policy or self.p)

    def expired_then_fresh(self, host, kind, *_args, **_kwargs):
        if host == STALE:
            raise ResolutionError(EXPIRED)
        return self.positive(host, kind)

    def test_another_name_gets_its_own_verified_answers(self):
        self.ex.side_effect = self.expired_then_fresh
        self.expire()
        result = self.r.resolve(FRESH, self.p)
        self.assertEqual(result.addresses, (fixtures.IP4, fixtures.IP6))
        self.assertEqual([c.args[0] for c in self.ex.call_args_list], [STALE, FRESH, FRESH])
        self.assertEqual(len(self.r._requests), 2)
        self.assertNotIn((STALE, self.p.fingerprint, fixtures.PROVIDER), self.r._cache)

    def test_same_name_keeps_thirty_seconds_and_code_after_clear(self):
        self.ex.side_effect = ResolutionError(EXPIRED)
        self.expire()
        self.r.clear()
        self.ex.side_effect = self.positive
        self.expire(STALE.upper() + '.')
        self.now[0] += 29
        self.expire()
        self.assertEqual(self.ex.call_count, 1)
        self.assertEqual(len(self.r._requests), 1)
        self.now[0] += 1
        self.assertEqual(self.r.resolve(STALE, self.p).addresses[0], fixtures.IP4)
        self.assertEqual(self.ex.call_count, 3)
        self.assertEqual(len(self.r._requests), 2)

    def test_original_network_policy_keeps_its_own_failure(self):
        self.ex.side_effect = ResolutionError(EXPIRED)
        self.expire()
        self.ex.side_effect = self.positive
        changed = replace(self.p, source='explicit_application')
        self.assertEqual(self.r.resolve(STALE, changed).addresses[0], fixtures.IP4)
        self.expire()
        self.assertEqual(self.ex.call_count, 3)

    def test_family_wait_expiry_exposes_no_partial_answer_or_cache(self):
        def exchange(host, kind, *_args, **_kwargs):
            if host == STALE:
                if kind == 28:
                    self.now[0] += 2
                return Answer((fixtures.IP4 if kind == 1 else fixtures.IP6,), 1, host)
            return self.positive(host, kind)
        self.ex.side_effect = exchange
        self.expire()
        self.assertFalse(self.r._cache)
        self.assertEqual(self.r.resolve(FRESH, self.p).addresses, (fixtures.IP4, fixtures.IP6))
        self.expire()
        self.assertEqual(self.ex.call_count, 4)

    def test_expired_second_family_does_not_release_first_family(self):
        self.ex.side_effect = [self.positive(STALE, 1), ResolutionError(EXPIRED)]
        self.expire()
        self.assertFalse(self.r._cache)
        self.ex.side_effect = self.positive
        self.assertEqual(self.r.resolve(FRESH, self.p).addresses[0], fixtures.IP4)
        self.expire()
        self.assertEqual(self.ex.call_count, 4)

    def test_genuinely_zero_ttl_answers_remain_uncached_transactions(self):
        def exchange(host, kind, *_args, **_kwargs):
            self.now[0] += .1
            return Answer((fixtures.IP4 if kind == 1 else fixtures.IP6,), 0, host, valid_for=0)
        self.ex.side_effect = exchange
        self.assertEqual(self.r.resolve(FRESH, self.p).addresses, (fixtures.IP4, fixtures.IP6))
        self.assertFalse(self.r._cache)
        self.assertEqual(self.r.resolve(FRESH, self.p).addresses, (fixtures.IP4, fixtures.IP6))
        self.assertEqual(self.ex.call_count, 4)

    def test_revocation_cancel_private_address_and_clock_checks_still_precede_backoff(self):
        self.ex.side_effect = ResolutionError(EXPIRED)
        self.expire()
        self.r.permission = lambda: False
        with self.assertRaisesRegex(ResolutionError, '^encrypted_dns_disabled$'):
            self.r.resolve(FRESH, self.p)
        self.r.permission = lambda: True
        cancelled = threading.Event()
        cancelled.set()
        with self.assertRaisesRegex(ResolutionError, '^paused$'):
            self.r.resolve(FRESH, self.p, cancelled=cancelled)
        with patch('socket.getaddrinfo', return_value=fixtures.fake_answers('10.0.0.1')):
            with self.assertRaisesRegex(ResolutionError, '^non_public_address$'):
                self.r.resolve(FRESH, self.p)
        self.now[0] -= 1
        with self.assertRaisesRegex(ResolutionError, '^encrypted_dns_clock_rollback$'):
            self.r.resolve(STALE, self.p)
        self.assertEqual(self.ex.call_count, 1)

    def test_provider_security_or_transport_failure_still_blocks_other_names(self):
        for code in ('encrypted_dns_tls_failed', 'encrypted_dns_non_public_answer',
                     'encrypted_dns_invalid_response', 'encrypted_dns_http_rejected',
                     'encrypted_dns_route_failed', 'encrypted_dns_refused',
                     'encrypted_dns_timeout', 'encrypted_dns_unavailable'):
            with self.subTest(code=code):
                self.now[0] += 31
                self.ex.reset_mock()
                self.ex.side_effect = ResolutionError(EXPIRED)
                self.expire()
                self.ex.side_effect = ResolutionError(code)
                with self.assertRaisesRegex(ResolutionError, '^' + code + '$'):
                    self.r.resolve(FRESH, self.p)
                again = 'encrypted_dns_cooldown' if code in ('encrypted_dns_timeout', 'encrypted_dns_unavailable') else code
                with self.assertRaisesRegex(ResolutionError, '^' + again + '$'):
                    self.r.resolve(STALE, self.p)
                self.assertEqual(self.ex.call_count, 2)

    def test_all_names_and_failure_types_share_the_original_lookup_budget(self):
        for i in range(60):
            code = EXPIRED if i % 2 else NX
            self.ex.side_effect = ResolutionError(code)
            with self.assertRaisesRegex(ResolutionError, '^' + code + '$'):
                self.r.resolve('name-' + str(i) + '.example.org', self.p)
        with self.assertRaisesRegex(ResolutionError, '^encrypted_dns_budget$'):
            self.r.resolve(FRESH, self.p)
        self.assertEqual(self.ex.call_count, 60)
        self.assertEqual(len(self.r._requests), 60)

    def test_missing_and_expired_names_keep_their_distinct_failures(self):
        self.ex.side_effect = ResolutionError(NX)
        with self.assertRaisesRegex(ResolutionError, '^' + NX + '$'):
            self.r.resolve(STALE, self.p)
        self.ex.side_effect = ResolutionError(EXPIRED)
        self.expire(FRESH)
        with self.assertRaisesRegex(ResolutionError, '^' + NX + '$'):
            self.r.resolve(STALE, self.p)
        self.expire(FRESH)
        self.assertEqual(self.ex.call_count, 2)

    def test_native_tunnel_never_connects_to_the_expired_answer(self):
        self.ex.side_effect = self.expired_then_fresh
        tunnel = NativeTunnel.__new__(NativeTunnel)
        tunnel.hosts = frozenset({STALE, FRESH})
        tunnel.policy = self.p
        tunnel.cancelled = threading.Event()
        tunnel._stop = threading.Event()
        with patch('socket.create_connection') as dial:
            with self.assertRaisesRegex(FetchError, '^' + EXPIRED + '$'):
                tunnel.resolve_host(STALE)
            self.assertEqual(tuple(tunnel.resolve_host(FRESH)), (fixtures.IP4, fixtures.IP6))
        dial.assert_not_called()
        self.assertEqual(self.ex.call_count, 3)

    def test_original_http_path_can_resolve_other_host_without_retrying_failed_one(self):
        self.ex.side_effect = self.expired_then_fresh
        client = SafeHTTP({STALE, FRESH}, network_policy=self.p, interval=0)
        connection = Mock()
        reply = connection.getresponse.return_value
        reply.status = 200
        reply.getheaders.return_value = [('Content-Type', 'application/json')]
        reply.read.return_value = b'{"ok":true}'
        with patch('vibe_job_radar.network.PinnedHTTPSConnection', return_value=connection) as factory:
            with self.assertRaisesRegex(FetchError, '^' + EXPIRED + '$'):
                client.json('https://' + STALE + '/jobs')
            factory.assert_not_called()
            self.assertEqual(client.json('https://' + FRESH + '/jobs'), {'ok': True})
        self.assertEqual(factory.call_count, 1)
        self.assertEqual(factory.call_args.args[0], FRESH)
        self.assertEqual(self.ex.call_count, 3)
