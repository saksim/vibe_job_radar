"""RFC vectors and a real, independently verifying local Digest proxy/TLS source."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import hashlib
import hmac
import json
import os
from pathlib import Path
import socket
import threading
import unittest
from unittest.mock import MagicMock, patch
from urllib.request import parse_http_list, parse_keqv_list
from types import SimpleNamespace

import test_loopback_proxy as fixture
import test_proxy_auth as basic
from vibe_job_radar.digest_auth import DigestError, select_challenge
from vibe_job_radar.digest_proxy import LoopbackDigestProxy
from vibe_job_radar.loopback_proxy import LocalProxyError, LoopbackProxy
from vibe_job_radar.network import SafeHTTP, FetchError
from vibe_job_radar.network_policy import NetworkPolicy
from vibe_job_radar.proxy_credentials import AUTH_ENV, ProxyCredentials
from vibe_job_radar.guided.native_tunnel import NativeTunnel
from vibe_job_radar.guided.rate import RateLedger, Limits
from vibe_job_radar.guided.transport import PinnedTransport

HOST, IP4, IP6 = fixture.HOST, fixture.IP4, fixture.IP6
USER, PASSWORD = basic.USER, basic.PASSWORD


def fields(header):
    """Stdlib parser; deliberately independent of the product parser."""
    return parse_keqv_list(parse_http_list(header.removeprefix('Digest ')))


class DigestVectorTests(unittest.TestCase):
    def test_rfc7616_section_3_9_1_md5_and_sha256(self):
        for algorithm, expected in [
            ('MD5','8ca523f5e9506fed4657c9700eebdbec'),
            ('SHA-256','753927fa0e85d155564e2e272a28d1802ca10daf4496794697cf8db5856cb6c1')]:
            with self.subTest(algorithm=algorithm):
                challenge = select_challenge(['Digest realm="http-auth@example.org", '
                    'nonce="7ypf/xlj9XXwfDPEoM4URrv/xwf94BcCAzFZH4GiTo0v", qop="auth, auth-int", algorithm='+algorithm])
                header, _ = challenge.authorize(b'Mufasa',b'Circle of Life','GET','/dir/index.html',
                    cnonce='f2/wE4q74E6zIJEtWaHKaf5wv/H5QzzpXusqGemxURZJ')
                self.assertEqual(fields(header)['response'], expected)

    def test_mixed_offers_select_strongest_supported_without_basic_fallback(self):
        selected = select_challenge(['Negotiate', 'Basic realm="ignored", Digest realm="a,b", '
            'nonce="n", algorithm=MD5, Digest realm = "strong", nonce="n", algorithm=SHA-256',
            'Digest realm="unknown", nonce="n", algorithm=future'])
        self.assertEqual((selected.realm,selected.algorithm),('strong','SHA-256'))
        with self.assertRaises(DigestError): select_challenge(['Basic realm="only"'])

    def test_invalid_and_oversized_offers_never_generate_credentials(self):
        bad = ['Digest realm="x", nonce="n", nonce="other"',
            'Digest realm="x", nonce=""', 'Digest realm="x", nonce="n", qop="other"',
            'Digest realm="x", nonce="n", charset=latin1', 'Digest realm="x", nonce="unterminated',
            'Digest realm="x", nonce="n"\r\nProxy-Authorization: reflected',
            'Digest realm="x", nonce="'+'n'*2049+'"', 'Digest '+'x'*16384]
        for header in bad:
            with self.subTest(length=len(header)), self.assertRaises(DigestError): select_challenge([header])

    def test_escaped_realm_opaque_and_mutual_proof_are_parsed_without_echo(self):
        challenge = select_challenge(['Digest realm="a,b\\\"c", nonce="n", opaque="x\\\\y", algorithm=SHA-256, qop=auth'])
        header, proof = challenge.authorize(b'user',b'password','CONNECT',IP4+':443')
        self.assertEqual(challenge.realm,'a,b"c'); self.assertIn('opaque="x\\\\y"',header)
        proof.verify(['rspauth="'+proof.response+'", qop=auth, nc=00000001, cnonce="'+proof.cnonce+'"'])
        for invalid in ['rspauth="'+'0'*64+'"', 'qop=auth-int', 'nc=00000002',
                        'cnonce="other"', 'rspauth="a", rspauth="b"']:
            with self.assertRaises(DigestError): proof.verify([invalid])
        for private in ('password', challenge.realm, proof.response):
            self.assertNotIn(private,repr(challenge)+repr(proof))


class DigestConfigurationTests(unittest.TestCase):
    def setUp(self):
        env = patch.dict(os.environ, {k:v for k,v in os.environ.items() if k not in {*basic.AUTH,AUTH_ENV}}, clear=True)
        env.start(); self.addCleanup(env.stop)

    def test_method_requires_explicit_http_and_complete_credentials_before_dns(self):
        for values in [
            {AUTH_ENV:'digest'}, {AUTH_ENV:'digest','VIBE_RADAR_HTTP_PROXY':'http://127.0.0.1:18990'},
            {**basic.AUTH, AUTH_ENV:'unknown','VIBE_RADAR_HTTP_PROXY':'http://127.0.0.1:18990'},
            {**basic.AUTH, AUTH_ENV:'digest','VIBE_RADAR_SOCKS_PROXY':'socks5://127.0.0.1:18990'},
            {**basic.AUTH, AUTH_ENV:'','VIBE_RADAR_HTTP_PROXY':'http://127.0.0.1:18990'}]:
            with patch.dict(os.environ,values), patch('socket.getaddrinfo') as dns, patch('socket.create_connection') as dial:
                policy=NetworkPolicy.capture(discover=lambda:{'https':'http://127.0.0.1:4321'})
                self.assertIsNotNone(policy.error)
                with self.assertRaises(FetchError): SafeHTTP({HOST},network_policy=policy).request('https://'+HOST+'/')
                dns.assert_not_called();dial.assert_not_called()

    def test_auth_scheme_binds_live_and_saved_policy_without_changing_basic_identity(self):
        with patch.dict(os.environ,{**basic.AUTH,'VIBE_RADAR_HTTP_PROXY':'http://127.0.0.1:18990'}):
            old=NetworkPolicy.capture()
            with patch.dict(os.environ,{AUTH_ENV:'digest'}): new=NetworkPolicy.capture()
            again=NetworkPolicy.capture()
        self.assertEqual(old.fingerprint,again.fingerprint)
        self.assertIsInstance(new.proxy,LoopbackDigestProxy)
        with self.assertRaises(AttributeError):new.proxy.auth_scheme='basic'
        self.assertNotEqual(old.fingerprint,new.fingerprint)
        self.assertNotEqual(old._saved_session_network(),new._saved_session_network())
        self.assertEqual(new.describe()['proxy_authentication_method'],'digest')
        for secret in (USER,PASSWORD,basic.BASIC): self.assertNotIn(secret,repr(new.proxy)+json.dumps(new.describe()))


class HTTPDigestProxyTests(unittest.TestCase):
    perform=fixture.RealProxyTests.perform
    tearDown=fixture.RealProxyTests.tearDown

    def setUp(self):
        fixture.RealProxyTests.setUp(self)
        env=patch.dict(os.environ,{**basic.AUTH,AUTH_ENV:'digest'});env.start();self.addCleanup(env.stop)
        self.realm='ARTIFICIAL-DIGEST-REALM';self.nonce='ARTIFICIAL-SERVER-NONCE'
        self.algorithm='SHA-256';self.qop='auth';self.userhash=False
        self.reject_auth=False;self.bad_proof=False;self.anonymous_success=False
        self.challenge_override=None;self.authentication=[];self.challenge_count=0
        self.authenticate_proxy=self.authenticate

    def authenticate(self, first, headers):
        header=headers.get('proxy-authorization')
        if not header:
            self.challenge_count+=1
            challenge=self.challenge_override or ('Digest realm="'+self.realm+'", nonce="'+self.nonce+'", '
                'opaque="ARTIFICIAL-OPAQUE", algorithm='+self.algorithm+
                (', qop="'+self.qop+'"' if self.qop else '')+(', userhash=true' if self.userhash else ''))
            return (200 if self.anonymous_success else 407), [('Proxy-Authenticate',challenge)]
        self.authentication.append(header)
        if self.reject_auth or not header.startswith('Digest '):
            return 407,[('Proxy-Authenticate','Digest realm="stale", nonce="new", stale=true')]
        values=fields(header)
        uri=first.split()[1]
        hash_name={'MD5':'md5','SHA-256':'sha256','SHA-512-256':'sha512_256'}[self.algorithm.removesuffix('-sess')]
        def digest(value):return hashlib.new(hash_name,value.encode('ascii')).hexdigest()
        a1=digest(USER+':'+self.realm+':'+PASSWORD)
        if self.algorithm.endswith('-sess'):a1=digest(a1+':'+self.nonce+':'+values['cnonce'])
        def response(method):
            a2=method+':'+uri+(':'+digest('') if self.qop=='auth-int' else '')
            tail=(values['nc']+':'+values['cnonce']+':'+self.qop+':' if self.qop else '')+digest(a2)
            return digest(a1+':'+self.nonce+':'+tail)
        expected_user=digest(USER+':'+self.realm) if self.userhash else USER
        valid=(values.get('username')==expected_user and values.get('realm')==self.realm
            and values.get('nonce')==self.nonce and values.get('opaque')=='ARTIFICIAL-OPAQUE'
            and values.get('uri')==uri and values.get('algorithm')==self.algorithm
            and (not self.qop or values.get('qop')==self.qop and values.get('nc')=='00000001')
            and hmac.compare_digest(values.get('response',''),response('CONNECT')))
        if not valid:return 407,[]
        proof='0'*len(response('')) if self.bad_proof else response('')
        info='rspauth="'+proof+'"'
        if self.qop:info+=', qop='+self.qop+', nc=00000001, cnonce="'+values['cnonce']+'"'
        return 200,[('Proxy-Authentication-Info',info)]

    def test_real_tls_all_algorithms_sessions_and_qop_without_origin_credentials(self):
        for algorithm,qop,userhash in [('MD5','auth',False),('SHA-256','auth',False),
            ('SHA-512-256','auth',True),('MD5-sess','',False),('SHA-256-sess','auth-int',False),
            ('SHA-512-256-sess','auth',True),('MD5','',False)]:
            with self.subTest(algorithm=algorithm,qop=qop):
                self.algorithm,self.qop,self.userhash=algorithm,qop,userhash
                self.assertTrue(self.perform(lambda:SafeHTTP({HOST},interval=0).json('https://'+HOST+'/jobs'))['ok'])
        self.assertEqual(len(self.requests),7);self.assertEqual(len(self.connects),14)
        self.assertEqual(self.challenge_count,7);self.assertEqual(len(self.authentication),7)
        self.assertEqual(self.sni,[HOST]*7);self.assertEqual(self.dials,[self.proxy.server_address]*14)
        for request in self.requests:self.assertNotIn('proxy-authorization',{k.lower() for k in request['headers']})
        nonces=[fields(header)['cnonce'] for header in self.authentication if 'cnonce=' in header]
        self.assertEqual(len(nonces),len(set(nonces)))

    def test_bridge_posts_once_through_ipv6_numeric_connect(self):
        self.symbol=IP6
        ledger=RateLedger(Path(self.tmp.name)/'rates.sqlite',Limits(request_interval=0))
        wire=PinnedTransport(SimpleNamespace(key='fixture',domains=(HOST,),resource_domains=()),ledger,threading.Event())
        result=self.perform(lambda:wire.fetch('https://'+HOST+'/job',method='POST',body=b'artificial once'))
        self.assertEqual(result.status,200);self.assertEqual(len(self.requests),1)
        self.assertEqual(self.requests[0]['body'],b'artificial once')
        self.assertEqual([row[0].split()[1] for row in self.connects],['['+IP6+']:443']*2)
        self.assertEqual(ledger.summary('fixture')['request']['day'],1)

    def test_refusal_stale_and_wrong_server_proof_stop_without_origin_or_retry(self):
        for attribute in ('reject_auth','bad_proof'):
            with self.subTest(attribute=attribute):
                setattr(self,attribute,True)
                before=len(self.connects)
                with self.assertRaises(FetchError) as error:
                    self.perform(lambda:SafeHTTP({HOST},interval=0).request('https://'+HOST+'/jobs'))
                self.assertEqual(error.exception.code,'local_proxy_auth_failed')
                self.assertEqual(len(self.connects)-before,2);self.assertEqual(self.requests,[])
                setattr(self,attribute,False)

    def test_unsupported_challenge_and_anonymous_success_send_no_credentials(self):
        for challenge in ('Basic realm="only"','Digest realm="x", nonce="n", algorithm=unknown'):
            self.challenge_override=challenge
            with self.assertRaises(FetchError):self.perform(lambda:SafeHTTP({HOST},interval=0).request('https://'+HOST+'/'))
        self.anonymous_success=True
        with self.assertRaises(FetchError):self.perform(lambda:SafeHTTP({HOST},interval=0).request('https://'+HOST+'/'))
        self.assertEqual(len(self.connects),3);self.assertEqual(self.authentication,[]);self.assertEqual(self.requests,[])

    def test_tls_validation_and_native_credentials_remain_separate(self):
        self.perform(lambda:basic.through_native_guard(self))
        self.assertEqual(self.sni,[HOST]);self.assertNotIn('Proxy-Authorization',self.requests[0]['headers'])
        with self.assertRaises(FetchError) as error:
            self.perform(lambda:SafeHTTP({HOST},interval=0).request('https://'+HOST+'/jobs'),trust=False)
        self.assertEqual(error.exception.code,'tls_verification_failed');self.assertEqual(len(self.requests),1)

    def test_parallel_native_requests_send_one_rejected_credential_response(self):
        self.reject_auth=True
        def exercise():
            guard=NativeTunnel((HOST,),NetworkPolicy.capture(),threading.Event(),timeout=3)
            try:
                def attempt(_):
                    try:guard._open(HOST)
                    except FetchError as error:return error.code
                    self.fail('rejected authentication succeeded')
                with ThreadPoolExecutor(max_workers=4) as pool:
                    self.assertEqual(list(pool.map(attempt,range(4))),['local_proxy_auth_failed']*4)
            finally:guard.close()
        self.perform(exercise)
        self.assertEqual(len(self.authentication),1);self.assertEqual(len(self.connects),2);self.assertEqual(self.requests,[])


class DigestHandshakeBoundsTests(unittest.TestCase):
    def proxy(self):return LoopbackDigestProxy('127.0.0.1',1234,ProxyCredentials(USER,PASSWORD))

    def test_one_deadline_spans_challenge_response_and_fragmented_header_reads(self):
        now=[0.0];first=MagicMock();second=MagicMock()
        def challenge(n):
            now[0]+=.6
            return b'HTTP/1.1 407 Required\r\nProxy-Authenticate: Digest realm="x", nonce="n", qop=auth\r\n\r\n'
        def partial(n):now[0]+=.6;return b'HTTP/1.1 200'
        first.recv.side_effect=challenge;second.recv.side_effect=partial
        with patch('socket.create_connection',side_effect=[first,second]) as dial,patch('time.monotonic',side_effect=lambda:now[0]):
            with self.assertRaisesRegex(LocalProxyError,'local_proxy_connection_failed'):self.proxy().open_tunnel(IP4,1)
        self.assertEqual(dial.call_count,2);first.close.assert_called_once();second.close.assert_called_once()
        self.assertLessEqual(dial.call_args_list[1].args[1],.4)

    def test_bounded_headers_no_private_target_or_secret_echo(self):
        for reply in (b'HTTP/1.1 407 Refused\r\nX: '+b'x'*16384,
                      b'HTTP/1.1 407 '+PASSWORD.encode()+b'\r\n Folded: x\r\n\r\n',
                      b'HTTP/1.1 407 Refused\r\nX: bad\x00header\r\n\r\n'):
            raw=MagicMock();raw.recv.side_effect=lambda n:reply[:n]
            with patch('socket.create_connection',return_value=raw) as dial:
                with self.assertRaises(LocalProxyError) as error:self.proxy().open_tunnel(IP4,1)
            self.assertNotIn(PASSWORD,str(error.exception));self.assertEqual(dial.call_count,1);raw.close.assert_called_once()
        for target in ('127.0.0.1','198.18.0.1','10.0.0.1','::1','network-fixture.invalid'):
            with patch('socket.create_connection') as dial,self.assertRaisesRegex(LocalProxyError,'non_public_address'):
                self.proxy().open_tunnel(target,1)
            dial.assert_not_called()


if __name__=='__main__':unittest.main()
