"""Independent consent for public DNS; no real external lookup or source data."""
import hashlib
import json
import os
import socket
import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

from test_encrypted_dns import HOST, IP4, IP6, fake_answers
from vibe_job_radar.dns_wire import Answer, ResolutionError
from vibe_job_radar.encrypted_dns import PublicResolver, PROVIDER
from vibe_job_radar.network import FetchError, validate_public_url
from vibe_job_radar.network_policy import NetworkPolicy
from vibe_job_radar.network_settings import CONSENT, PUBLIC_DNS_CONSENT, read_settings
from vibe_job_radar.workspace import Workspace, InputError


def answer(host, kind, *args, **kwargs):
    return Answer((IP4 if kind == 1 else IP6,), 60, host)


class PublicDNSResolverTests(unittest.TestCase):
    def setUp(self):
        self.resolver = PublicResolver()
        self.policy = NetworkPolicy(encrypted_dns=True, public_dns=True, resolver=self.resolver)

    def test_public_lookup_never_requires_target_system_dns(self):
        with patch('socket.getaddrinfo', side_effect=socket.gaierror) as dns,                 patch.object(self.resolver, '_exchange', side_effect=answer) as exchange:
            result = self.resolver.resolve(HOST, self.policy)
        self.assertEqual(result.addresses, (IP4, IP6))
        self.assertEqual(result.source, PROVIDER)
        dns.assert_not_called()
        self.assertEqual(exchange.call_count, 2)

    def test_default_and_prior_fake_ip_contract_are_unchanged(self):
        old = NetworkPolicy(encrypted_dns=True)
        value = [old.source, 'system_route', None, None, (), None, True]
        expected = hashlib.sha256(json.dumps(value, separators=(',', ':')).encode()).hexdigest()[:16]
        self.assertEqual(old.fingerprint, expected)
        self.assertNotEqual(old.fingerprint, replace(old, public_dns=True).fingerprint)
        self.assertFalse(NetworkPolicy.capture(discover=lambda: {}).encrypted_dns)
        with patch('socket.getaddrinfo', side_effect=socket.gaierror),                 patch.object(self.resolver, '_exchange') as exchange:
            with self.assertRaisesRegex(ResolutionError, '^dns_error$'):
                self.resolver.resolve(HOST, old)
        exchange.assert_not_called()

    def test_public_policy_cannot_silently_disable_encrypted_dns(self):
        for enabled, public in ((False, True), ('yes', True), (True, 1)):
            with self.subTest(enabled=enabled, public=public), self.assertRaises(ValueError):
                NetworkPolicy(encrypted_dns=enabled, public_dns=public)
        with self.assertRaises(ValueError):
            replace(self.policy, encrypted_dns=False)

    def test_private_special_and_normalized_literal_names_are_not_disclosed(self):
        values = ('localhost', 'printer.local', 'service.internal', 'x.home.arpa',
                  'hidden.onion', 'x.invalid', '127.0.0.1', '10.0.0.1', '::1',
                  '198.18.0.1', '127.0.0.1.', '8.8.8.8.')
        with patch('socket.getaddrinfo') as dns, patch.object(self.resolver, '_exchange') as exchange:
            for host in values:
                with self.subTest(host=host), self.assertRaises(ResolutionError):
                    self.resolver.resolve(host, self.policy)
        dns.assert_not_called()
        exchange.assert_not_called()

    def test_bad_route_and_disallowed_source_stop_before_any_dns(self):
        with patch('socket.getaddrinfo') as dns, patch.object(self.resolver, '_exchange') as exchange:
            with self.assertRaisesRegex(ResolutionError, 'local_proxy_configuration_invalid'):
                self.resolver.resolve(HOST, replace(self.policy, error='local_proxy_configuration_invalid'))
            with self.assertRaisesRegex(FetchError, 'domain_not_permitted'):
                validate_public_url('https://' + HOST + '/job', {'another.example.org'}, network_policy=self.policy)
        dns.assert_not_called()
        exchange.assert_not_called()

    def test_cache_is_separate_from_prior_fake_ip_resolution(self):
        with patch('socket.getaddrinfo', return_value=fake_answers('198.18.0.42')) as dns,                 patch.object(self.resolver, '_exchange', side_effect=answer) as exchange:
            self.resolver.resolve(HOST, self.policy)
            self.assertTrue(self.resolver.resolve(HOST, self.policy).cache_reused)
            self.assertFalse(self.resolver.resolve(HOST, replace(self.policy, public_dns=False)).cache_reused)
        self.assertEqual(exchange.call_count, 4)
        self.assertEqual(dns.call_count, 1)

    def test_resolver_failure_has_no_system_or_route_fallback(self):
        with patch('socket.getaddrinfo') as dns,                 patch.object(self.resolver, '_exchange', side_effect=ResolutionError('encrypted_dns_tls_failed')) as exchange:
            for _ in range(2):
                with self.assertRaisesRegex(ResolutionError, 'encrypted_dns_tls_failed'):
                    self.resolver.resolve(HOST, self.policy)
        dns.assert_not_called()
        self.assertEqual(exchange.call_count, 1)

    def test_revocation_during_tls_prevents_dns_post(self):
        selected = ['public_doh']
        self.resolver.mode_permission = lambda: selected[0]
        connection = Mock()
        connection.connect.side_effect = lambda: selected.__setitem__(0, 'system')
        with patch('socket.getaddrinfo') as dns,                 patch('vibe_job_radar.network.PinnedHTTPSConnection', return_value=connection):
            with self.assertRaisesRegex(ResolutionError, 'encrypted_dns_disabled'):
                self.resolver.resolve(HOST, self.policy)
        connection.request.assert_not_called()
        connection.close.assert_called_once()
        dns.assert_not_called()


class PublicDNSConsentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.workspace = Workspace(Path(self.temp.name) / 'one')
        self.env = patch.dict(os.environ, {}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.proxy = patch('urllib.request.getproxies', return_value={})
        self.proxy.start()
        self.addCleanup(self.proxy.stop)

    def save(self, mode, revision):
        return self.workspace.network_preferences({'mode': mode, 'revision': revision, 'consent': mode != 'system'})

    def test_public_mode_requires_distinct_consent_and_persists_without_network(self):
        with patch('socket.getaddrinfo', side_effect=AssertionError),                 patch('socket.create_connection', side_effect=AssertionError):
            self.save('public_doh', 0)
            settings = read_settings(self.workspace)
            other = Workspace(self.workspace.root)
            self.assertTrue(other.network_policy().public_dns)
            self.assertFalse(Workspace(Path(self.temp.name) / 'two').network_policy().encrypted_dns)
            self.assertEqual(settings['consent_version'], PUBLIC_DNS_CONSENT)
            self.assertNotEqual(PUBLIC_DNS_CONSENT, CONSENT)
            self.assertEqual(other.network_state()['policy']['resolution'], 'explicit_public_doh')
        self.assertNotIn(HOST, json.dumps(settings))

    def test_old_consent_cannot_be_relabelled_or_applied_to_public_mode(self):
        self.save('fake_ip_doh', 0)
        before = (self.workspace.root / 'network-preferences.json').read_bytes()
        for extra in ({'consent': False}, {'endpoint': 'https://other.example.org/dns'}):
            with self.subTest(extra=extra), self.assertRaises(InputError):
                self.workspace.network_preferences({'mode': 'public_doh', 'revision': 1, 'consent': True, **extra})
        self.assertEqual((self.workspace.root / 'network-preferences.json').read_bytes(), before)
        raw = json.loads(before)
        raw['mode'] = 'public_doh'
        (self.workspace.root / 'network-preferences.json').write_text(json.dumps(raw), encoding='utf8')
        with self.assertRaises(InputError):
            self.workspace.network_policy()

    def test_mode_change_revokes_old_policy_cache_without_resetting_budget(self):
        self.save('public_doh', 0)
        old = self.workspace.network_policy()
        resolver = old.resolver
        with patch.object(resolver, '_exchange', side_effect=answer) as exchange,                 patch('socket.getaddrinfo', return_value=fake_answers('198.18.0.42')):
            resolver.resolve(HOST, old)
            self.save('fake_ip_doh', 1)
            fake = self.workspace.network_policy()
            self.assertIs(fake.resolver, resolver)
            with self.assertRaisesRegex(ResolutionError, 'encrypted_dns_disabled'):
                resolver.resolve(HOST, old)
            resolver.resolve(HOST, fake)
            self.assertEqual(len(resolver._requests), 2)
            self.assertEqual(exchange.call_count, 4)
            self.save('public_doh', 2)
            with self.assertRaisesRegex(ResolutionError, 'encrypted_dns_disabled'):
                resolver.resolve(HOST, fake)
            self.save('system', 3)
            with self.assertRaisesRegex(ResolutionError, 'encrypted_dns_disabled'):
                resolver.resolve(HOST, old)
            self.assertEqual(len(resolver._requests), 2)
            self.assertEqual(exchange.call_count, 4)

    def test_native_guard_uses_same_mode_and_keeps_host_permission(self):
        from vibe_job_radar.guided.native_tunnel import NativeTunnel
        self.save('public_doh', 0)
        policy = self.workspace.network_policy()
        guard = NativeTunnel({HOST}, policy, threading.Event())
        try:
            with patch('socket.getaddrinfo', side_effect=AssertionError) as dns,                     patch.object(policy.resolver, '_exchange', side_effect=answer) as exchange:
                self.assertEqual(guard.resolve_host(HOST), (IP4, IP6))
                with self.assertRaisesRegex(FetchError, 'domain_not_permitted'):
                    guard.resolve_host('another.example.org')
                self.save('system', 1)
                with self.assertRaisesRegex(FetchError, 'encrypted_dns_disabled'):
                    guard.resolve_host(HOST)
            dns.assert_not_called()
            self.assertEqual(exchange.call_count, 2)
            self.assertEqual(guard.connections, 0)
        finally:
            guard.close()
