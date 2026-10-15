"""Real artificial NTLMv2 proxy, original TLS, bridge and native guard."""
import base64
from dataclasses import replace
import json
import os
from pathlib import Path
import select
import socket
import socketserver
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import test_loopback_proxy as fixture
import test_proxy_auth as basic
from vibe_job_radar.loopback_proxy import LocalProxyError
from vibe_job_radar.ntlm_proxy import LoopbackNtlmProxy, _challenge
from vibe_job_radar import ntlm_worker
from vibe_job_radar.proxy_credentials import ProxyCredentials, USERNAME_ENV, PASSWORD_ENV, AUTH_ENV
from vibe_job_radar.network_policy import NetworkPolicy
from vibe_job_radar.network import SafeHTTP, FetchError
from vibe_job_radar.guided.transport import PinnedTransport
from vibe_job_radar.guided.rate import RateLedger, Limits

USER = r'RADAR\fixture'
PASSWORD = 'ArtificialOnlyPassword!42'
HOST = fixture.HOST


class NtlmConfigurationTests(unittest.TestCase):
    def test_missing_component_stops_explicit_route_before_dns(self):
        values = {USERNAME_ENV: USER, PASSWORD_ENV: PASSWORD, AUTH_ENV: 'ntlmv2',
                  'VIBE_RADAR_HTTP_PROXY': 'http://127.0.0.1:19878', 'VIBE_RADAR_SOCKS_PROXY': ''}
        with patch.dict(os.environ, values), patch.object(ntlm_worker, 'available', return_value=False), \
                patch('socket.getaddrinfo') as dns, patch('socket.create_connection') as dial:
            policy = NetworkPolicy.capture()
            self.assertEqual(policy.error, 'local_proxy_ntlm_unavailable')
            with self.assertRaises(FetchError):
                SafeHTTP({HOST}, network_policy=policy).request('https://' + HOST + '/')
            dns.assert_not_called()
            dial.assert_not_called()

    def test_identity_separates_ntlm_without_changing_basic_or_digest(self):
        credential = ProxyCredentials(USER, PASSWORD)
        from vibe_job_radar.loopback_proxy import LoopbackProxy
        from vibe_job_radar.digest_proxy import LoopbackDigestProxy
        policies = [NetworkPolicy('explicit_application', cls('127.0.0.1', 19878, credential))
                    for cls in (LoopbackProxy, LoopbackDigestProxy, LoopbackNtlmProxy)]
        self.assertEqual(len({p.fingerprint for p in policies}), 3)
        self.assertEqual(len({p._saved_session_network() for p in policies}), 3)
        self.assertEqual(policies[2].describe()['proxy_authentication_method'], 'ntlmv2')
        for p in policies:
            self.assertNotIn(USER, repr(p) + json.dumps(p.describe()))
            self.assertNotIn(PASSWORD, repr(p) + json.dumps(p.describe()))
        with patch('socket.create_connection') as dial, patch.object(credential.__class__, 'ntlm_session') as worker:
            for target in ('127.0.0.1', '198.18.0.2', '10.0.0.1', '::1'):
                with self.assertRaises(LocalProxyError):
                    policies[2].proxy.open_tunnel(target, 1)
            dial.assert_not_called()
            worker.assert_not_called()

    def test_malformed_ambiguous_or_large_challenges_stop(self):
        for offers in ([], ['NTLM'], ['Negotiate abc'], ['NTLM @@@'],
                       ['NTLM QQ==', 'NTLM Qg=='], ['NTLM ' + 'A' * 20000],
                       ['NTLM QQ==\r\nheader: x']):
            with self.subTest(offers_count=len(offers)), self.assertRaises(LocalProxyError):
                _challenge({'proxy-authenticate': offers})
        self.assertEqual(_challenge({'proxy-authenticate': ['Basic realm="ignored"', 'NTLM QQ==']}), b'A')

    def test_owned_worker_close_reclaims_slot_and_process(self):
        if not ntlm_worker.available():
            with self.assertRaises(LocalProxyError) as err:
                ProxyCredentials(USER, PASSWORD).ntlm_session(time.monotonic() + 5)
            self.assertEqual(err.exception.code, 'local_proxy_ntlm_unavailable')
            return
        session = ProxyCredentials(USER, PASSWORD).ntlm_session(time.monotonic() + 8)
        child = session._process
        self.assertTrue(child.is_alive())
        session.close()
        session.close()
        self.assertTrue(session._closed)
        self.assertTrue(ntlm_worker._SLOTS.acquire(blocking=False))
        ntlm_worker._SLOTS.release()
        with self.assertRaises(LocalProxyError):
            session.respond(b'NTLMSSP\0' + (2).to_bytes(4, 'little'))


class HTTPNtlmProxyTests(unittest.TestCase):
    perform = fixture.RealProxyTests.perform
    tearDown = fixture.RealProxyTests.tearDown

    def setUp(self):
        fixture.RealProxyTests.setUp(self)
        self.authentication = []
        self.ntlm_checks = []
        self.reject_auth = False
        self.anonymous_success = False
        self.challenge_override = None
        self.close_challenge = False
        self.duplicate_length = False
        self.challenge_count = 0
        self.challenge_payload = b'artificial proxy challenge body'
        credentials = Path(self.tmp.name) / 'ntlm-server-credentials'
        credentials.write_text('RADAR:fixture:' + PASSWORD + '\n', encoding='utf-8')
        env = patch.dict(os.environ, {USERNAME_ENV: USER, PASSWORD_ENV: PASSWORD,
            AUTH_ENV: 'ntlmv2', 'VIBE_RADAR_SOCKS_PROXY': '',
            'NTLM_USER_FILE': str(credentials), 'LM_COMPAT_LEVEL': '5'})
        env.start()
        self.addCleanup(env.stop)
        owner = self

        class Tunnel(socketserver.StreamRequestHandler):
            def read_request(self):
                first = self.rfile.readline(4096).decode('ascii').strip()
                if not first:
                    return None
                headers = {}
                for _ in range(30):
                    line = self.rfile.readline(16384)
                    if line in (b'\r\n', b''):
                        break
                    key, value = line.decode('ascii').split(':', 1)
                    headers[key.lower()] = value.strip()
                expected = ('[' + owner.symbol + ']' if ':' in owner.symbol else owner.symbol) + ':443'
                if first.split()[:2] != ['CONNECT', expected]:
                    raise AssertionError('unexpected artificial target')
                # Never retain authentication token bytes in fixture output.
                owner.connects.append((first, {'proxy-authorization': '<artificial-ntlm-redacted>'}))
                authorization = headers.get('proxy-authorization', '')
                if not authorization.startswith('NTLM '):
                    raise AssertionError('wrong selected authentication')
                return base64.b64decode(authorization[5:], validate=True)

            def handle(self):
                from spnego import server
                from spnego.exceptions import NegotiateOptions, SpnegoError
                from spnego._ntlm_raw.messages import Authenticate
                self.connection.settimeout(8)
                owner.proxy_peers.append(self.client_address[0])
                first = self.read_request()
                if first is None:
                    return
                owner.challenge_count += 1
                if owner.anonymous_success:
                    self.wfile.write(b'HTTP/1.1 200 OK\r\n\r\n')
                    self.wfile.flush()
                    return
                context = server(protocol='ntlm', options=NegotiateOptions.use_ntlm)
                challenge = context.step(first)
                offer = owner.challenge_override or ('NTLM ' + base64.b64encode(challenge).decode('ascii'))
                extra = ('Connection: close\r\n' if owner.close_challenge else '')
                if owner.duplicate_length:
                    extra += 'Content-Length: 0\r\n'
                header = ('HTTP/1.1 407 Authenticate\r\nProxy-Authenticate: ' + offer +
                          '\r\nContent-Length: ' + str(len(owner.challenge_payload)) + '\r\n' + extra + '\r\n')
                self.wfile.write(header.encode('ascii') + owner.challenge_payload)
                self.wfile.flush()
                response = self.read_request()
                if response is None:
                    return
                parsed = Authenticate.unpack(response)
                owner.authentication.append('<artificial-ntlm-response>')
                owner.ntlm_checks.append({'ntlm_v2': parsed.nt_challenge_response[16:20] == b'\x01\x01\x00\x00',
                                          'workstation_omitted': not parsed.workstation})
                accepted = not owner.reject_auth
                try:
                    context.step(response)
                except SpnegoError:
                    accepted = False
                if not accepted:
                    self.wfile.write(b'HTTP/1.1 407 Rejected\r\nContent-Length: 0\r\n\r\n')
                    self.wfile.flush()
                    return
                upstream = owner.real_dial(getattr(owner, 'upstream_address', owner.target.server_address), 2)
                try:
                    self.wfile.write(b'HTTP/1.1 200 Established\r\n\r\n')
                    self.wfile.flush()
                    peers = {self.connection: upstream, upstream: self.connection}
                    while True:
                        ready, _, _ = select.select(list(peers), [], [], 3)
                        if not ready:
                            return
                        for source in ready:
                            block = source.recv(65536)
                            if not block:
                                return
                            peers[source].sendall(block)
                finally:
                    upstream.close()

        self.proxy.RequestHandlerClass = Tunnel

    def require_component(self):
        if ntlm_worker.available():
            return True
        with self.assertRaises(FetchError) as err:
            self.perform(lambda: SafeHTTP({HOST}, interval=0).request('https://' + HOST + '/'))
        self.assertEqual(err.exception.code, 'local_proxy_ntlm_unavailable')
        self.assertEqual(self.connects, [])
        return False

    def test_http_bridge_and_native_guard_use_same_connection_and_original_tls(self):
        if not self.require_component():
            return
        result = self.perform(lambda: SafeHTTP({HOST}, interval=0).json('https://' + HOST + '/jobs'))
        self.assertTrue(result['ok'])
        adapter = SimpleNamespace(key='fixture', domains=(HOST,), resource_domains=())
        wire = PinnedTransport(adapter, RateLedger(Path(self.tmp.name) / 'rate.sqlite', Limits(request_interval=0)),
                               threading.Event())
        self.assertEqual(self.perform(lambda: wire.fetch('https://' + HOST + '/detail')).status, 200)
        self.perform(lambda: basic.through_native_guard(self))
        self.assertEqual(len(self.requests), 3)
        self.assertEqual(len(self.proxy_peers), 3)
        self.assertEqual(len(self.connects), 6)
        self.assertEqual(self.sni, [HOST] * 3)
        self.assertTrue(all(x['ntlm_v2'] and x['workstation_omitted'] for x in self.ntlm_checks))
        for request in self.requests:
            self.assertNotIn('Proxy-Authorization', request['headers'])

    def test_wrong_credentials_rejected_once_without_origin_or_direct_fallback(self):
        if not self.require_component():
            return
        with patch.dict(os.environ, {PASSWORD_ENV: 'ArtificialWrongPassword!43'}):
            with self.assertRaises(FetchError) as err:
                self.perform(lambda: SafeHTTP({HOST}, interval=0).request('https://' + HOST + '/'))
        self.assertEqual(err.exception.code, 'local_proxy_auth_failed')
        self.assertEqual(len(self.proxy_peers), 1)
        self.assertEqual(len(self.connects), 2)
        self.assertEqual(self.requests, [])

    def test_anonymous_success_is_not_authenticated_success(self):
        if not self.require_component():
            return
        self.anonymous_success = True
        with self.assertRaises(FetchError) as err:
            self.perform(lambda: SafeHTTP({HOST}, interval=0).request('https://' + HOST + '/'))
        self.assertEqual(err.exception.code, 'local_proxy_auth_failed')
        self.assertEqual(self.authentication, [])
        self.assertEqual(self.requests, [])

    def test_closed_or_ambiguous_407_stops_before_credential_response(self):
        if not self.require_component():
            return
        for field in ('close_challenge', 'duplicate_length'):
            setattr(self, field, True)
            with self.subTest(field=field), self.assertRaises(FetchError) as err:
                self.perform(lambda: SafeHTTP({HOST}, interval=0).request('https://' + HOST + '/'))
            self.assertEqual(err.exception.code, 'local_proxy_auth_failed')
            setattr(self, field, False)
        self.assertEqual(self.authentication, [])
        self.assertEqual(self.requests, [])

    def test_tls_rejection_preserves_certificate_checks(self):
        if not self.require_component():
            return
        with self.assertRaises(FetchError) as err:
            self.perform(lambda: SafeHTTP({HOST}, interval=0).request('https://' + HOST + '/'), trust=False)
        self.assertEqual(err.exception.code, 'tls_verification_failed')
        self.assertEqual(len(self.connects), 2)
        self.assertEqual(self.requests, [])


if __name__ == '__main__':
    unittest.main()
