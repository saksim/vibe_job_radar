"""PAC return fidelity, route constraints, revocation and bounded native work."""
import json
import os
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

from vibe_job_radar import pac_native
from vibe_job_radar.pac import PacSnapshot, canonical_target, parse_result, validate_script
from vibe_job_radar.loopback_proxy import LocalProxyError, LoopbackProxy
from vibe_job_radar.loopback_socks import LoopbackSocks5
from vibe_job_radar.network import PinnedHTTPSConnection, FetchError, SafeHTTP
from vibe_job_radar.network_policy import NetworkPolicy

SCRIPT = 'function FindProxyForURL(url, host) { return "DIRECT"; }'


class PacTests(unittest.TestCase):
    def test_routes_are_explicit_and_preserve_first(self):
        self.assertIsNone(parse_result('DIRECT'))
        self.assertEqual(parse_result('PROXY localhost:8001; DIRECT'), LoopbackProxy('127.0.0.1',8001))
        self.assertEqual(parse_result('SOCKS5 [::1]:1080; PROXY 127.0.0.1:8001; DIRECT'), LoopbackSocks5('::1',1080))
        self.assertIsNone(parse_result('DIRECT; PROXY 127.0.0.1:8001'))

    def test_no_unknown_protocol_remote_dns_credentials_or_invalid_tail(self):
        invalid = ['SOCKS 127.0.0.1:1', 'SOCKS5H 127.0.0.1:1', 'HTTPS 127.0.0.1:1',
            'UNKNOWN 127.0.0.1:1; DIRECT', 'PROXY 127.0.0.1:1; BAD', 'DIRECT;',
            'PROXY 192.168.1.1:1', 'PROXY 8.8.8.8:1', 'PROXY example.com:1',
            'PROXY 127.0.0.1:0', 'PROXY 127.0.0.1:65536', 'PROXY user:SECRET@127.0.0.1:1',
            'PROXY 127.0.0.1:1/', 'PROXY 127.0.0.1:1?x', 'PROXY 127.0.0.1:1\n',
            'DIRECT;DIRECT;DIRECT;DIRECT;DIRECT', 'direct', 'DIRECT\x7f', '', 'x'*97, None]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(LocalProxyError) as error:
                parse_result(value)
            self.assertEqual(error.exception.code, 'pac_invalid_result')
            self.assertNotIn('SECRET', str(error.exception))

    def test_domain_only_canonical_input(self):
        self.assertEqual(canonical_target('EXAMPLE.COM.'), ('example.com','https://example.com/'))
        self.assertEqual(canonical_target('::1'), ('::1','https://[::1]/'))
        self.assertEqual(canonical_target('招聘.example')[0], 'xn--otu796d.example')
        for value in ('example.com/path?q=private','user@host','host:443','host\n','a..b','',None):
            with self.subTest(value=value), self.assertRaises(LocalProxyError): canonical_target(value)

    def test_script_is_bounded_utf8_and_has_no_nulls(self):
        self.assertEqual(validate_script(SCRIPT),SCRIPT.encode())
        for value in ('',' ', 'x'*65537,'中'*22000,'\x00','\ud800',None):
            with self.assertRaises(ValueError): validate_script(value)

    def test_snapshot_evaluates_once_per_domain_and_does_not_expose_source(self):
        snapshot = PacSnapshot(SCRIPT, lambda: True)
        policy = NetworkPolicy('explicit_workspace', pac=snapshot, pac_id='one')
        with patch.object(pac_native,'evaluate',return_value='PROXY 127.0.0.1:8001') as evaluate:
            self.assertEqual(policy.describe('example.com')['transport'],'pac_by_host')
            evaluate.assert_not_called()
            self.assertEqual(policy.for_host('EXAMPLE.COM').port,8001)
            self.assertEqual(policy.for_host('example.com').port,8001)
            evaluate.assert_called_once()
            self.assertEqual(evaluate.call_args.args[:2],(SCRIPT,'https://example.com/'))
            policy.for_host('other.example')
            self.assertEqual(evaluate.call_count,2)
        self.assertNotIn('FindProxyForURL',repr(snapshot)+repr(policy)+json.dumps(policy.describe()))

    def test_error_is_cached_and_no_fallback_occurs(self):
        snapshot = PacSnapshot(SCRIPT, lambda: True)
        with patch.object(pac_native,'evaluate',return_value='UNKNOWN 127.0.0.1:9; DIRECT') as evaluate:
            for _ in range(2):
                with self.assertRaisesRegex(LocalProxyError,'pac_invalid_result'): snapshot.for_host('x.example')
            evaluate.assert_called_once()

    def test_revocation_stops_cached_route_and_preconstructed_connection(self):
        allowed = [True]
        snapshot = PacSnapshot(SCRIPT, lambda: allowed[0])
        policy = NetworkPolicy('explicit_workspace', pac=snapshot)
        with patch.object(pac_native,'evaluate',return_value='DIRECT') as evaluate:
            connection = PinnedHTTPSConnection('x.example','93.184.216.34',1,network_policy=policy)
            allowed[0] = False
            with self.assertRaisesRegex(LocalProxyError,'pac_revoked'): snapshot.for_host('x.example')
            with patch('socket.create_connection') as dial:
                with self.assertRaisesRegex(FetchError,'pac_revoked'): connection.connect()
            dial.assert_not_called(); evaluate.assert_called_once()
        connection.close()

    def test_permission_failure_and_mid_evaluation_revocation_are_closed(self):
        for permission in (lambda:False, lambda:1, lambda:1/0):
            with self.assertRaisesRegex(LocalProxyError,'pac_revoked'):
                PacSnapshot(SCRIPT,permission).for_host('example.com')
        allowed = [True]
        def revoke(*args): allowed[0]=False; return 'DIRECT'
        with patch.object(pac_native,'evaluate',side_effect=revoke):
            with self.assertRaisesRegex(LocalProxyError,'pac_revoked'):
                PacSnapshot(SCRIPT,lambda:allowed[0]).for_host('example.com')

    def test_host_cache_is_bounded_and_concurrent_same_host_evaluates_once(self):
        snapshot = PacSnapshot(SCRIPT, lambda:True)
        def evaluate(*args): time.sleep(.02); return 'DIRECT'
        found=[]
        with patch.object(pac_native,'evaluate',side_effect=evaluate) as run:
            threads=[threading.Thread(target=lambda:found.append(snapshot.for_host('example.com'))) for _ in range(4)]
            for t in threads:t.start()
            for t in threads:t.join(2)
            self.assertEqual(found,[None]*4);run.assert_called_once()
        with patch.object(pac_native,'evaluate',return_value='DIRECT'):
            for i in range(127):snapshot.for_host(f'h{i}.example')
            with self.assertRaisesRegex(LocalProxyError,'pac_cache_limit'):snapshot.for_host('overflow.example')

    def test_policy_binds_script_and_grant_without_ambient_bypass(self):
        first = NetworkPolicy('explicit_workspace',pac=PacSnapshot(SCRIPT,lambda:True),pac_id='a',bypass=('*',))
        second = NetworkPolicy('explicit_workspace',pac=PacSnapshot(SCRIPT,lambda:True),pac_id='b',bypass=('*',))
        self.assertNotEqual(first.fingerprint,second.fingerprint)
        with patch.object(pac_native,'evaluate',return_value='SOCKS5 127.0.0.1:1080'):
            self.assertIsInstance(first.for_host('example.com'),LoopbackSocks5)

    def test_bad_result_stops_before_target_dns(self):
        policy=NetworkPolicy('explicit_workspace',pac=PacSnapshot(SCRIPT,lambda:True))
        with patch.object(pac_native,'evaluate',return_value='SOCKS localhost:1; DIRECT'),patch('socket.getaddrinfo') as dns:
            with self.assertRaisesRegex(FetchError,'pac_invalid_result'):
                SafeHTTP({'example.com'},network_policy=policy).json('https://example.com/secret?private=yes')
        dns.assert_not_called()

    def test_native_envelope_is_strict_and_preserves_raw_semantics(self):
        nonce='a'*16
        for raw in ('DIRECT','SOCKS5 127.0.0.1:1080; DIRECT','UNKNOWN proxy:8080; DIRECT','x'*96):
            encoded=raw.encode().hex();labels='.'.join(encoded[i:i+48] for i in range(0,len(encoded),48))
            self.assertEqual(pac_native.decode_result('vjr'+nonce+'.'+labels+'.invalid',nonce),raw)
        for host in ('vjr'+nonce+'.00.invalid', 'vjrother.444952454354.invalid','vjr'+nonce+'.ff.invalid',
                     'vjr'+nonce+'.444.952.invalid','DIRECT',None):
            with self.assertRaises(LocalProxyError):pac_native.decode_result(host,nonce)

    def test_worker_deadline_and_revocation_terminate_owned_process(self):
        for revoke in (False,True):
            context=MagicMock();receiver=MagicMock();sender=MagicMock();process=context.Process.return_value
            process.pid=42;process.is_alive.return_value=True
            # Drive the deadline independently of runner scheduling. A real
            # 40 ms sleep budget can expire before the revocation is observed.
            elapsed=[0.];clock=MagicMock()
            clock.monotonic.side_effect=lambda:elapsed[0]
            def poll(wait):elapsed[0]+=min(wait,.01);return False
            receiver.poll.side_effect=poll
            context.Pipe.return_value=(receiver,sender)
            calls=[0]
            def permission():calls[0]+=1;return not revoke or calls[0]<3
            with patch.object(pac_native,'available',return_value=True),patch.object(pac_native,'DEADLINE',.04), \
                    patch.object(pac_native,'time',clock), \
                    patch.object(pac_native.multiprocessing,'get_context',return_value=context):
                with self.assertRaisesRegex(LocalProxyError,'pac_revoked' if revoke else 'pac_timeout'):
                    pac_native.evaluate(SCRIPT,'https://example.com/',permission)
            process.terminate.assert_called_once();process.close.assert_called_once();receiver.close.assert_called_once()
            self.assertTrue(pac_native._SLOTS.acquire(blocking=False));pac_native._SLOTS.release()

    def test_unavailable_and_busy_never_start_a_worker(self):
        with patch.object(pac_native,'available',return_value=False),patch.object(pac_native.multiprocessing,'get_context') as context:
            with self.assertRaisesRegex(LocalProxyError,'pac_unavailable'):pac_native.evaluate(SCRIPT,'https://example.com/',lambda:True)
        context.assert_not_called()
        with patch.object(pac_native,'available',return_value=True),patch.object(pac_native,'_SLOTS') as slots:
            slots.acquire.return_value=False
            with self.assertRaisesRegex(LocalProxyError,'pac_busy'):pac_native.evaluate(SCRIPT,'https://example.com/',lambda:True)
            slots.release.assert_not_called()


class PlatformPacTests(unittest.TestCase):
    def evaluate_platform(self, source):
        if os.name=='nt':
            return pac_native.evaluate(source,'https://target.example/',lambda:True)
        self.assertFalse(pac_native.available())
        with self.assertRaisesRegex(LocalProxyError,'pac_unavailable'):
            pac_native.evaluate(source,'https://target.example/',lambda:True)
        return None

    def test_original_directives_on_windows_and_explicit_refusal_elsewhere(self):
        for raw in ('DIRECT','PROXY 127.0.0.1:8001; DIRECT','SOCKS5 127.0.0.1:1080; DIRECT',
                    'SOCKS 127.0.0.1:1080; DIRECT','UNKNOWN 127.0.0.1:1080; DIRECT'):
            source='function FindProxyForURL(url,host){return '+json.dumps(raw)+';}'
            with self.subTest(raw=raw):
                self.assertEqual(self.evaluate_platform(source),raw if os.name=='nt' else None)

    def test_native_domain_helpers_receive_only_canonical_root(self):
        source=('function FindProxyForURL(url,host){if(dnsDomainIs(host,".example") && '
                'shExpMatch(url,"https://target.example/"))return "PROXY 127.0.0.1:8001";return "DIRECT";}')
        self.assertEqual(self.evaluate_platform(source),'PROXY 127.0.0.1:8001' if os.name=='nt' else None)

    def test_bad_script_and_nonstring_fail_without_direct_fallback(self):
        for source in ('function FindProxyForURL( {', 'function FindProxyForURL(){return 7;}',
                       'function FindProxyForURL(){return "DIRECT\\n";}'):
            expected='pac_invalid_script' if os.name=='nt' else 'pac_unavailable'
            with self.subTest(source=source), self.assertRaisesRegex(LocalProxyError,expected):
                pac_native.evaluate(source,'https://target.example/',lambda:True)


if __name__=='__main__':unittest.main()
