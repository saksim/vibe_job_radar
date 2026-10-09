"""Ordinary DoH failures through original resolver/facades; no external network."""
from __future__ import annotations

import errno
import http.client
import json
import socket
import ssl
import tempfile
import threading
import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock, patch

from vibe_job_radar.collection import Collector, TERMINAL
from vibe_job_radar.dns_transport_diagnostic import failure_details, public_details
from vibe_job_radar.dns_wire import ResolutionError
from vibe_job_radar.encrypted_dns import BOOTSTRAP, PublicResolver
from vibe_job_radar.guided.service import GuidedService
from vibe_job_radar.network import FetchError, SafeHTTP, SiteFetcher
from vibe_job_radar.network_policy import NetworkPolicy
from vibe_job_radar.workspace import Workspace
from test_encrypted_dns import fake_answers, HOST, IP4


def refused():
    return ConnectionRefusedError(errno.ECONNREFUSED, 'PRIVATE path Cookie:SECRET')


def connection(stage='connection_setup', error=None):
    conn = Mock()
    conn.sock = None
    conn.connection_attempts = [dict(ip=BOOTSTRAP[0], phase='tcp', outcome='connect_failed')]
    reply = conn.getresponse.return_value
    reply.status = 200
    reply.getheaders.return_value = [('Content-Type', 'application/dns-message')]
    owner, method = {
        'connection_setup': (conn, 'connect'), 'dns_request': (conn, 'request'),
        'response_headers': (conn, 'getresponse'), 'response_body': (reply, 'read1'),
    }[stage]
    getattr(owner, method).side_effect = error or refused()
    return conn


class FactsTests(unittest.TestCase):
    def test_fixed_categories_do_not_export_exception_text_or_private_type_names(self):
        class PrivateCustomError(ConnectionResetError):
            pass
        cases = [
            (TimeoutError('PRIVATE'), 'TimeoutError', 'timeout'),
            (refused(), 'ConnectionRefusedError', 'connection_refused'),
            (PrivateCustomError('SECRET'), 'ConnectionResetError', 'connection_interrupted'),
            (BrokenPipeError('PRIVATE'), 'BrokenPipeError', 'connection_interrupted'),
            (ConnectionAbortedError('PRIVATE'), 'ConnectionAbortedError', 'connection_interrupted'),
            (http.client.RemoteDisconnected('SECRET'), 'RemoteDisconnected', 'connection_interrupted'),
            (http.client.IncompleteRead(b'PRIVATE body'), 'IncompleteRead', 'incomplete_response'),
            (http.client.BadStatusLine('PRIVATE headers'), 'BadStatusLine', 'http_protocol'),
            (http.client.HTTPException('PRIVATE'), 'HTTPException', 'http_protocol'),
            (OSError('PRIVATE'), 'OSError', 'os_error'),
        ]
        for exc, name, category in cases:
            with self.subTest(name=name):
                d = failure_details(exc, phase='response_body')
                self.assertEqual((d['error_type'], d['category']), (name, category))
                self.assertNotIn('PRIVATE', json.dumps(d))
                self.assertNotIn('SECRET', json.dumps(d))
                self.assertNotIn('PrivateCustomError', json.dumps(d))
                self.assertFalse(d['cause_confirmed'])
                self.assertFalse(d['source_request_started'])

    def test_stage_is_operation_entry_not_completed_send_or_root_cause(self):
        for phase, expected, attempted in [
            ('tls_context', 'tls_context', False),
            ('tls_handshake', 'connection_setup', False),
            ('dns_request', 'dns_request', True),
            ('response_headers', 'response_headers', True),
            ('response_body', 'response_body', True),
            ('PRIVATE arbitrary stage', 'unknown', False), ([], 'unknown', False),
        ]:
            with self.subTest(phase=phase):
                d = failure_details(refused(), phase=phase)
                self.assertEqual(d['phase'], expected)
                self.assertEqual(d['dns_request_attempted'], attempted)
                self.assertEqual(d['tls_handshake_completed'], attempted)
                self.assertFalse(d['cause_confirmed'])

    def test_tls_and_non_transport_errors_stay_out_of_ordinary_classification(self):
        for exc in (ssl.SSLError('PRIVATE'), ssl.SSLCertVerificationError('PRIVATE'), ValueError('PRIVATE')):
            with self.subTest(error=type(exc).__name__), self.assertRaises(TypeError):
                failure_details(exc, phase='tls_handshake')

    def test_public_rebuild_drops_payload_overrides_and_only_copies_safe_rows(self):
        d = failure_details(refused(), phase='tls_handshake', policy_id='a'*16)
        d.update(endpoint_host='PRIVATE', endpoint_port=1, next_action='SECRET',
                 headers={'Cookie': 'SECRET'}, payload='PRIVATE', cause_confirmed=True,
                 source_request_started=True, tls_handshake_completed=True,
                 connection_attempts=[
                     dict(ip=BOOTSTRAP[0], phase='tcp', outcome='connect_failed', url='PRIVATE'),
                     dict(ip='127.0.0.1', phase='tcp', outcome='connect_failed'),
                     dict(ip=BOOTSTRAP[1], phase='tls', outcome='connected'),
                     dict(ip=[], phase='tcp', outcome='connect_failed'),
                 ])
        clean = public_details(d)
        self.assertEqual(len(clean['connection_attempts']), 2)
        self.assertEqual(clean['endpoint_host'], 'cloudflare-dns.com')
        self.assertEqual(clean['endpoint_port'], 443)
        self.assertEqual(clean['policy_id'], 'a'*16)
        self.assertFalse(clean['source_request_started'])
        self.assertFalse(clean['cause_confirmed'])
        self.assertFalse(clean['tls_handshake_completed'])
        self.assertNotIn('SECRET', json.dumps(clean))
        self.assertNotIn('PRIVATE', json.dumps(clean))
        clean['connection_attempts'][0]['outcome'] = 'changed'
        self.assertEqual(d['connection_attempts'][0]['outcome'], 'connect_failed')

    def test_attempts_are_bounded_and_malformed_json_cannot_mask_original_failure(self):
        d = failure_details(refused(), phase='tls_handshake')
        valid = dict(ip=BOOTSTRAP[0], phase='tcp', outcome='connect_failed')
        for key in ('ip', 'phase', 'outcome'):
            for bad in ([], {}, None, 1, True, 'PRIVATE'):
                with self.subTest(key=key, bad=bad):
                    row = dict(valid, **{key: bad})
                    d['connection_attempts'] = [row]
                    self.assertEqual(public_details(d)['connection_attempts'], [])
        d['connection_attempts'] = [valid]*100
        self.assertEqual(len(public_details(d)['connection_attempts']), 4)
        for bad in (None, {}, 'PRIVATE', [None, [], 'PRIVATE']):
            d['connection_attempts'] = bad
            self.assertEqual(public_details(d)['connection_attempts'], [])

    def test_invalid_envelopes_dates_and_category_pairs_are_rejected(self):
        valid = failure_details(refused(), phase='tls_handshake')
        for key, value in [
            ('kind', 'other'), ('schema_version', True), ('schema_version', 2),
            ('phase', []), ('phase', 'PRIVATE'), ('error_type', {}),
            ('error_type', 'CustomError'), ('category', 'timeout'),
            ('observed_at', 'PRIVATE'), ('observed_at', '2026-09-27'),
            ('observed_at', '2026-99-99T12:00:00Z'), ('observed_at', '1'*41),
        ]:
            with self.subTest(key=key, value=value):
                self.assertIsNone(public_details(dict(valid, **{key: value})))
        for value in (None, [], 'PRIVATE'):
            self.assertIsNone(public_details(value))

    def test_only_bounded_integer_codes_and_policy_digest_are_exported(self):
        d = failure_details(refused(), phase='tls_handshake')
        for value in (True, False, 1.5, 'SECRET', 2**32, -2**31-1, [], {}, 'a'*64):
            with self.subTest(value=value):
                clean = public_details(dict(d, errno=value, winerror=value, policy_id=value))
                self.assertNotIn('errno', clean)
                self.assertNotIn('winerror', clean)
                self.assertEqual(clean['policy_id'], '')
        for value in (0, -2**31, 2**32-1, 10061):
            clean = public_details(dict(d, errno=value, winerror=value))
            self.assertEqual((clean['errno'], clean['winerror']), (value, value))

    def test_reuse_flags_are_typed_and_observation_time_is_not_replaced(self):
        d = failure_details(refused(), phase='tls_handshake')
        clean = public_details(dict(d, reused_failure=True, matches_current_policy=False))
        self.assertTrue(clean['reused_failure'])
        self.assertFalse(clean['matches_current_policy'])
        self.assertEqual(clean['observed_at'], d['observed_at'])
        clean = public_details(dict(d, reused_failure='SECRET', matches_current_policy=0))
        self.assertFalse(clean['reused_failure'])
        self.assertNotIn('matches_current_policy', clean)


class OfflineTestCase(unittest.TestCase):
    def keep(self, manager):
        # Python 3.10 is supported; TestCase.enterContext starts in 3.11.
        value = manager.__enter__()
        self.addCleanup(manager.__exit__, None, None, None)
        return value


class ResolverEvidenceTests(OfflineTestCase):
    def setUp(self):
        self.now = 100.0
        self.resolver = PublicResolver(clock=lambda: self.now, permission=lambda: True)
        self.policy = NetworkPolicy(encrypted_dns=True, resolver=self.resolver)
        self.conn = connection()
        self.factory = self.keep(patch('vibe_job_radar.network.PinnedHTTPSConnection', return_value=self.conn))
        self.keep(patch('socket.getaddrinfo', return_value=fake_answers('198.18.1.244')))
        self.no_network = self.keep(patch('socket.socket.connect', side_effect=AssertionError('No network')))

    def failure(self, policy=None):
        with self.assertRaises(ResolutionError) as caught:
            self.resolver.resolve(HOST, policy or self.policy)
        return caught.exception

    def test_all_original_transport_phases_keep_code_and_single_reservation(self):
        cases = [
            ('connection_setup', refused()),
            ('dns_request', BrokenPipeError(errno.EPIPE, 'SECRET')),
            ('response_headers', http.client.RemoteDisconnected('PRIVATE')),
            ('response_body', http.client.IncompleteRead(b'PRIVATE')),
        ]
        for phase, exc in cases:
            with self.subTest(phase=phase):
                self.resolver = PublicResolver(clock=lambda: self.now)
                self.conn = connection(phase, exc)
                self.factory.reset_mock(); self.factory.return_value = self.conn
                d = self.failure()
                self.assertEqual(d.code, 'encrypted_dns_unavailable')
                self.assertEqual(d.diagnostic['phase'], phase)
                self.assertEqual(len(self.resolver._requests), 1)
                self.assertEqual(self.resolver._cache, {})
                self.factory.assert_called_once()
                self.conn.close.assert_called_once()
                self.assertLessEqual(self.conn.request.call_count, 1)
                self.assertLessEqual(self.conn.getresponse.call_count, 1)
                self.assertNotIn('PRIVATE', json.dumps(d.diagnostic))
        self.no_network.assert_not_called()

    def test_context_initialization_failure_is_not_reported_as_tcp_or_tls_result(self):
        self.factory.side_effect = OSError(errno.EMFILE, 'PRIVATE filename')
        d = self.failure()
        self.assertEqual(d.code, 'encrypted_dns_unavailable')
        self.assertEqual(d.diagnostic['phase'], 'tls_context')
        self.assertEqual(d.diagnostic['connection_attempts'], [])
        self.assertFalse(d.diagnostic['tls_handshake_completed'])
        self.conn.connect.assert_not_called(); self.conn.close.assert_not_called()

    def test_optional_metadata_failure_preserves_original_failure_and_cooldown(self):
        with patch('vibe_job_radar.dns_transport_diagnostic.failure_details', side_effect=ValueError('PRIVATE')):
            first = self.failure()
        self.assertEqual(first.code, 'encrypted_dns_unavailable')
        self.assertIsNone(first.diagnostic)
        again = self.failure()
        self.assertEqual(again.code, 'encrypted_dns_cooldown')
        self.assertIsNone(again.diagnostic)
        self.factory.assert_called_once()

    def test_cooldown_reads_keep_original_time_without_extra_connection_or_budget(self):
        first, again = self.failure(), self.failure()
        self.assertEqual(first.code, 'encrypted_dns_unavailable')
        self.assertEqual(again.code, 'encrypted_dns_cooldown')
        self.assertEqual(first.diagnostic['observed_at'], again.diagnostic['observed_at'])
        self.assertFalse(first.diagnostic['reused_failure'])
        self.assertEqual(first.diagnostic['policy_id'], self.policy.fingerprint)
        self.assertTrue(again.diagnostic['reused_failure'])
        self.assertTrue(again.diagnostic['matches_current_policy'])
        self.assertEqual(len(self.resolver._requests), 1)
        self.factory.assert_called_once(); self.conn.request.assert_not_called()

    def test_changed_policy_is_marked_old_not_a_new_probe(self):
        first = self.failure()
        other = replace(self.policy, source='explicit_application')
        again = self.failure(other)
        self.assertNotEqual(other.fingerprint, self.policy.fingerprint)
        self.assertEqual(again.diagnostic['policy_id'], first.diagnostic['policy_id'])
        self.assertTrue(again.diagnostic['reused_failure'])
        self.assertFalse(again.diagnostic['matches_current_policy'])
        self.factory.assert_called_once()

    def test_mutating_returned_nested_evidence_does_not_change_saved_failure(self):
        first = self.failure()
        first.diagnostic['connection_attempts'][0]['ip'] = 'PRIVATE'
        first.diagnostic['error_type'] = 'SECRET'
        again = self.failure()
        self.assertEqual(again.diagnostic['connection_attempts'][0]['ip'], BOOTSTRAP[0])
        self.assertEqual(again.diagnostic['error_type'], 'ConnectionRefusedError')

    def test_clear_drops_evidence_but_keeps_cooldown_and_reserved_budget(self):
        self.failure(); self.resolver.clear()
        again = self.failure()
        self.assertEqual(again.code, 'encrypted_dns_cooldown')
        self.assertIsNone(again.diagnostic)
        self.assertEqual(len(self.resolver._requests), 1)
        self.factory.assert_called_once()

    def test_revocation_and_cancellation_do_not_publish_stale_evidence_or_reconnect(self):
        self.failure(); self.resolver.permission = lambda: False
        d = self.failure()
        self.assertEqual(d.code, 'encrypted_dns_disabled')
        self.assertIsNone(d.diagnostic)
        cancelled = threading.Event(); cancelled.set()
        with self.assertRaisesRegex(ResolutionError, 'paused') as caught:
            self.resolver.resolve(HOST, self.policy, cancelled=cancelled)
        self.assertIsNone(caught.exception.diagnostic)
        self.factory.assert_called_once()

    def test_expired_cooldown_new_failure_is_a_new_observation_without_automatic_retry(self):
        # The resolver clock advances here; the wall clock may have coarse
        # resolution. Control observations instead of depending on sub-ms ticks.
        stamps = ['2026-09-27T07:00:00+00:00', '2026-09-27T07:00:31+00:00']
        with patch('vibe_job_radar.dns_transport_diagnostic.utc_now', side_effect=stamps) as observed:
            first = self.failure()
            self.now += 31
            self.conn.connect.side_effect = TimeoutError('PRIVATE')
            again = self.failure()
        self.assertEqual(observed.call_count, 2)
        self.assertEqual([first.diagnostic['observed_at'], again.diagnostic['observed_at']], stamps)
        self.assertEqual(again.code, 'encrypted_dns_unavailable')
        self.assertEqual(again.diagnostic['error_type'], 'TimeoutError')
        self.assertFalse(again.diagnostic['reused_failure'])
        self.assertNotEqual(first.diagnostic['observed_at'], again.diagnostic['observed_at'])
        self.assertEqual(self.factory.call_count, 2)
        self.assertEqual(len(self.resolver._requests), 2)

    def test_tls_error_still_uses_existing_hard_failure_and_tls_evidence(self):
        self.conn.connect.side_effect = ssl.SSLEOFError(8, 'PRIVATE')
        d = self.failure()
        self.assertEqual(d.code, 'encrypted_dns_tls_failed')
        self.assertEqual(d.diagnostic['category'], 'peer_closed')
        self.assertNotEqual(d.diagnostic.get('kind'), 'encrypted_dns_transport_failure')
        self.assertEqual(self.failure().code, 'encrypted_dns_tls_failed')
        self.factory.assert_called_once()


class FacadeTests(OfflineTestCase):
    def setUp(self):
        self.keep(patch('socket.socket.connect', side_effect=AssertionError('No real network')))
        self.keep(patch('socket.getaddrinfo', return_value=fake_answers('198.18.1.244')))

    def test_public_fetch_failure_retains_structured_facts_without_source_connection(self):
        r = PublicResolver()
        p = NetworkPolicy(encrypted_dns=True, resolver=r)
        client = SafeHTTP({HOST}, network_policy=p, interval=0)
        fetcher = SiteFetcher({HOST}, transport=client)
        conn = connection()
        with patch('vibe_job_radar.network.PinnedHTTPSConnection', return_value=conn) as factory:
            with self.assertRaisesRegex(FetchError, 'encrypted_dns_unavailable'):
                fetcher.fetch('https://'+HOST+'/job')
        d = fetcher.last_diagnostic
        self.assertEqual(d['outcome'], 'encrypted_dns_unavailable')
        self.assertEqual(d['phase'], 'robots')
        self.assertEqual(d['http_attempts'], 1)  # Entry count, not a sent source request.
        self.assertEqual(d['resolution_failure'], client.last_resolution['failure'])
        self.assertFalse(d['resolution_failure']['source_request_started'])
        self.assertNotIn('PRIVATE', json.dumps(d))
        factory.assert_called_once()
        self.assertEqual(factory.call_args.args[0], 'cloudflare-dns.com')
        conn.request.assert_not_called()

    def test_unrelated_failure_does_not_relabel_stale_transport_facts(self):
        stale = failure_details(refused(), phase='tls_handshake')
        transport = SimpleNamespace(last_resolution={'failure': stale},
                                    public_get=Mock(side_effect=FetchError('host_circuit_open')))
        fetcher = SiteFetcher({HOST}, transport=transport)
        with self.assertRaisesRegex(FetchError, 'host_circuit_open'):
            fetcher.fetch('https://'+HOST+'/job')
        self.assertNotIn('resolution_failure', fetcher.last_diagnostic)

    def test_later_system_resolution_clears_previous_failure_before_source_request(self):
        r = PublicResolver(); p = NetworkPolicy(encrypted_dns=True, resolver=r)
        client = SafeHTTP({HOST}, network_policy=p, interval=0)
        with patch('vibe_job_radar.network.PinnedHTTPSConnection', return_value=connection()):
            with self.assertRaises(FetchError): client.public_get('https://'+HOST+'/job')
        self.assertIn('failure', client.last_resolution)
        conn = Mock()
        response = conn.getresponse.return_value
        response.status = 200; response.getheaders.return_value = []
        response.read.side_effect = [b'body', b'']
        with patch('socket.getaddrinfo', return_value=fake_answers(IP4)), \
             patch('vibe_job_radar.network.PinnedHTTPSConnection', return_value=conn):
            client.public_get('https://'+HOST+'/job')
        self.assertNotIn('failure', client.last_resolution)
        self.assertEqual(client.last_resolution['source'], 'system_dns')

    def test_real_collector_url_flow_records_failure_and_no_jd_or_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            w = Workspace(tmp)
            w.network_preferences({'mode':'fake_ip_doh', 'revision':0, 'consent':True})
            collector = Collector(w)
            state = collector.start(dict(mode='urls', urls='https://www.liepin.com/job/1912345678.shtml',
                roles=['architect'], platforms=['liepin'], permit_platforms=['liepin'],
                consent=True, detail_budget=1, rights_note='Artificial DNS regression only.'))
            conn = connection()
            with patch('vibe_job_radar.network.PinnedHTTPSConnection', return_value=conn) as factory, \
                 patch('vibe_job_radar.network_policy.NetworkPolicy.capture', return_value=NetworkPolicy()):
                for _ in range(6):
                    state = collector.step({'id':state['id']})
                    if state['status'] in TERMINAL: break
            self.assertIn(state['status'], TERMINAL)
            self.assertEqual(state['details'][0]['status'], 'encrypted_dns_unavailable')
            d = state['details'][0]['fetch_diagnostic']['resolution_failure']
            self.assertEqual(d['phase'], 'connection_setup')
            self.assertFalse(d['source_request_started'])
            self.assertEqual(state['saved_detail_count'], 0)
            self.assertFalse(state['report_id'])
            from vibe_job_radar.guided.rate import RateLedger
            rate = RateLedger(w.root/'guided/rates.sqlite').summary('liepin')
            self.assertEqual(rate['page']['day'], 1)
            self.assertEqual(rate['request']['day'], 1)
            self.assertEqual(len(w.dns_resolver._requests), 1)
            factory.assert_called_once(); conn.request.assert_not_called()
            self.assertNotIn('PRIVATE', json.dumps(state))

    def test_existing_diagnostic_facade_shows_current_then_old_policy_facts(self):
        with tempfile.TemporaryDirectory() as tmp:
            w = Workspace(tmp); s = GuidedService(w)
            self.addCleanup(s.close)
            w.network_preferences({'mode':'fake_ip_doh', 'revision':0, 'consent':True})
            conn = connection()
            with patch('vibe_job_radar.network.PinnedHTTPSConnection', return_value=conn) as factory, \
                 patch('vibe_job_radar.network_policy.NetworkPolicy.capture', return_value=NetworkPolicy()):
                d = s.diagnose({'platform':'boss'})
                again = s.diagnose({'platform':'boss'})
                policy = replace(w.network_policy(), source='explicit_application')
                with patch.object(w, 'network_policy', return_value=policy):
                    changed = s.diagnose({'platform':'boss'})
            self.assertEqual(d['code'], 'encrypted_dns_unavailable')
            self.assertFalse(d['passed'])
            self.assertFalse(d['target_connection_tested'])
            self.assertFalse(d['browser_tested'])
            self.assertNotIn('tls_diagnostic', d['effective_resolution'])
            facts = d['effective_resolution']['transport_diagnostic']
            self.assertEqual(facts['phase'], 'connection_setup')
            self.assertIn('连接建立', d['message'])
            self.assertEqual(again['code'], 'encrypted_dns_cooldown')
            self.assertIn('上次失败', again['message'])
            self.assertIn('不作为新策略已经失败', changed['message'])
            self.assertFalse(changed['effective_resolution']['transport_diagnostic']['matches_current_policy'])
            self.assertEqual(facts['observed_at'], again['effective_resolution']['transport_diagnostic']['observed_at'])
            self.assertEqual(s.ledger.summary('boss')['request']['day'], 0)
            self.assertFalse(w.db.exists())
            factory.assert_called_once(); conn.request.assert_not_called()

    def test_malformed_optional_failure_does_not_mask_facade_error_or_trigger_repair(self):
        with tempfile.TemporaryDirectory() as tmp:
            w = Workspace(tmp); s = GuidedService(w)
            self.addCleanup(s.close)
            w.network_preferences({'mode':'fake_ip_doh', 'revision':0, 'consent':True})
            bad = failure_details(refused(), phase='tls_handshake')
            bad['phase'] = []
            with patch.object(w.dns_resolver, 'resolve', side_effect=ResolutionError('encrypted_dns_unavailable', diagnostic=bad)), \
                 patch('vibe_job_radar.network_policy.NetworkPolicy.capture', return_value=NetworkPolicy()), \
                 patch('vibe_job_radar.network.PinnedHTTPSConnection') as factory:
                d = s.diagnose({'platform':'boss'})
            self.assertEqual(d['code'], 'encrypted_dns_unavailable')
            self.assertNotIn('transport_diagnostic', d['effective_resolution'])
            self.assertNotIn('tls_diagnostic', d['effective_resolution'])
            factory.assert_not_called()



class ResponseBoundaryTests(OfflineTestCase):
    def setUp(self):
        self.keep(patch('socket.socket.connect', side_effect=AssertionError('No real network')))
        self.keep(patch('socket.getaddrinfo', return_value=fake_answers('198.18.1.244')))

    def response(self, body, declared_length):
        import io
        length_header = (b'' if declared_length is None else
                         b'Content-Length: ' + str(declared_length).encode('ascii') + b'\r\n')
        wire = (b'HTTP/1.1 200 OK\r\nContent-Type: application/dns-message\r\n'
                + length_header + b'Connection: close\r\n\r\n' + body)
        class BufferedSocket:
            def makefile(self, *args, **kwargs):
                return io.BytesIO(wire)
        response = http.client.HTTPResponse(BufferedSocket())
        response.begin()
        self.addCleanup(response.close)
        return response

    def configured(self, response=None):
        resolver = PublicResolver(clock=lambda: 100.0, permission=lambda: True)
        policy = NetworkPolicy(encrypted_dns=True, resolver=resolver)
        conn = Mock()
        conn.sock = None
        conn.connection_attempts = [dict(ip=BOOTSTRAP[0], phase='tls', outcome='connected')]
        if response is not None:
            conn.getresponse.return_value = response
        return resolver, policy, conn

    def test_post_connect_timeout_failure_keeps_tls_success_without_dns_request(self):
        for error, code in [
            (OSError(errno.EBADF, 'PRIVATE timeout setup'), 'encrypted_dns_unavailable'),
            (ssl.SSLError('PRIVATE timeout setup'), 'encrypted_dns_tls_failed'),
        ]:
            with self.subTest(code=code):
                resolver, policy, conn = self.configured()
                conn.sock = Mock()
                conn.sock.settimeout.side_effect = error
                with patch('vibe_job_radar.network.PinnedHTTPSConnection', return_value=conn) as factory:
                    with self.assertRaises(ResolutionError) as caught:
                        resolver.resolve(HOST, policy)
                failure = caught.exception
                self.assertEqual(failure.code, code)
                self.assertEqual(failure.diagnostic['phase'], 'request_setup')
                self.assertTrue(failure.diagnostic['tls_handshake_completed'])
                self.assertFalse(failure.diagnostic['dns_request_attempted'])
                self.assertFalse(failure.diagnostic['cause_confirmed'])
                self.assertEqual(failure.diagnostic['connection_attempts'][0]['outcome'], 'connected')
                self.assertNotIn('PRIVATE', json.dumps(failure.diagnostic))
                self.assertEqual(len(resolver._requests), 1)
                factory.assert_called_once()
                conn.connect.assert_called_once()
                conn.request.assert_not_called()
                conn.getresponse.assert_not_called()
                conn.close.assert_called_once()

    def test_fixed_length_eof_records_transport_failure_before_dns_parsing(self):
        from vibe_job_radar.encrypted_dns import parse_answer
        response = self.response(b'\x00'*7, 20)
        resolver, policy, conn = self.configured(response)
        with patch('vibe_job_radar.network.PinnedHTTPSConnection', return_value=conn) as factory, \
             patch('vibe_job_radar.encrypted_dns.parse_answer', wraps=parse_answer) as parse:
            with self.assertRaises(ResolutionError) as caught:
                resolver.resolve(HOST, policy)
            failure = caught.exception
            self.assertEqual(failure.code, 'encrypted_dns_unavailable')
            self.assertEqual(failure.diagnostic['phase'], 'response_body')
            self.assertEqual(failure.diagnostic['error_type'], 'IncompleteRead')
            self.assertEqual(failure.diagnostic['category'], 'incomplete_response')
            self.assertTrue(failure.diagnostic['tls_handshake_completed'])
            self.assertTrue(failure.diagnostic['dns_request_attempted'])
            self.assertFalse(failure.diagnostic['source_request_started'])
            self.assertEqual(response.length, 13)
            self.assertTrue(response.isclosed())
            parse.assert_not_called()
            with self.assertRaises(ResolutionError) as repeated:
                resolver.resolve(HOST, policy)
            self.assertEqual(repeated.exception.code, 'encrypted_dns_cooldown')
            self.assertEqual(repeated.exception.diagnostic['observed_at'],
                             failure.diagnostic['observed_at'])
            self.assertTrue(repeated.exception.diagnostic['reused_failure'])
        self.assertEqual(len(resolver._requests), 1)
        self.assertEqual(resolver._cache, {})
        factory.assert_called_once()
        conn.request.assert_called_once()
        conn.close.assert_called_once()

    def test_complete_or_unbounded_malformed_dns_retains_hard_failure(self):
        from vibe_job_radar.encrypted_dns import parse_answer
        for declared_length in (7, None):
            with self.subTest(declared_length=declared_length):
                response = self.response(b'\x00'*7, declared_length)
                resolver, policy, conn = self.configured(response)
                with patch('vibe_job_radar.network.PinnedHTTPSConnection', return_value=conn) as factory, \
                     patch('vibe_job_radar.encrypted_dns.parse_answer', wraps=parse_answer) as parse:
                    with self.assertRaises(ResolutionError) as caught:
                        resolver.resolve(HOST, policy)
                    self.assertEqual(caught.exception.code, 'encrypted_dns_invalid_response')
                    self.assertIsNone(caught.exception.diagnostic)
                    parse.assert_called_once()
                    with self.assertRaises(ResolutionError) as repeated:
                        resolver.resolve(HOST, policy)
                    self.assertEqual(repeated.exception.code, 'encrypted_dns_invalid_response')
                self.assertEqual(len(resolver._requests), 1)
                self.assertEqual(resolver._cache, {})
                factory.assert_called_once()
                conn.request.assert_called_once()
                conn.close.assert_called_once()

if __name__ == '__main__':
    unittest.main()
