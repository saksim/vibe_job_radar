"""Real CONNECT and TLS at controlled loopback fixtures, no external requests.

Only fixture DNS maps the test hostname to a public address symbol; only the
fixture proxy maps that symbol to its TLS server. No production bypass exists.
"""
from __future__ import annotations
import http.server
import ipaddress
import json
import os
import select
import socket
import socketserver
import ssl
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace

from vibe_job_radar.loopback_proxy import LocalProxyError, LoopbackProxy
from vibe_job_radar.network import SafeHTTP, FetchError, PinnedHTTPSConnection
from vibe_job_radar.guided.transport import PinnedTransport
from vibe_job_radar.guided.rate import Limits, RateLedger
from vibe_job_radar.guided.contracts import CrawlError

HOST = 'network-fixture.invalid'
IP4 = '93.184.216.34'
IP6 = '2606:4700:4700::1111'
ENV = 'VIBE_RADAR_HTTP_PROXY'


class ProxyConfigurationTests(unittest.TestCase):
    def test_opt_in_default_does_not_consume_system_proxy(self):
        with patch.dict(os.environ, {ENV: '', 'HTTPS_PROXY': 'http://127.0.0.1:1234'}):
            self.assertIsNone(LoopbackProxy.from_environment())

    def test_literal_loopback_and_localhost_are_supported(self):
        for raw, host in [('http://127.0.0.1:7890','127.0.0.1'),('http://localhost:8080','127.0.0.1'),('http://[::1]:3128','::1')]:
            with self.subTest(raw=raw), patch.dict(os.environ,{ENV:raw}):
                self.assertEqual(LoopbackProxy.from_environment().host,host)

    def test_nonlocal_or_ambiguous_configuration_rejected(self):
        for raw in ['http://192.168.1.1:7890','http://8.8.8.8:7890','socks5://127.0.0.1:7890',
                    'https://127.0.0.1:7890','http://127.0.0.1','http://127.0.0.1:0',
                    'http://127.0.0.1:70000','http://127.0.0.1:7890/path','http://127.0.0.1:7890?x=1']:
            with self.subTest(raw=raw),patch.dict(os.environ,{ENV:raw}),self.assertRaises(LocalProxyError):
                LoopbackProxy.from_environment()

    def test_credentials_rejected_without_echo(self):
        with patch.dict(os.environ,{ENV:'http://user:DO-NOT-ECHO@127.0.0.1:7890'}):
            with self.assertRaises(LocalProxyError) as err:LoopbackProxy.from_environment()
        self.assertNotIn('DO-NOT-ECHO',str(err.exception))

    def test_proxy_never_opens_private_or_fake_ip_target(self):
        proxy=LoopbackProxy('127.0.0.1',7890)
        for ip in ('198.18.0.92','127.0.0.1','10.1.2.3','::1'):
            with self.subTest(ip=ip),patch('socket.create_connection') as dial,self.assertRaises(LocalProxyError):
                proxy.open_tunnel(ip,1)
            dial.assert_not_called()

    def test_invalid_proxy_is_not_silently_ignored_by_connection(self):
        with patch.dict(os.environ,{ENV:'socks5://127.0.0.1:7890'}),patch('socket.create_connection') as dial:
            with self.assertRaises(FetchError) as err:PinnedHTTPSConnection(HOST,IP4,1)
        self.assertEqual(err.exception.code,'local_proxy_configuration_invalid');dial.assert_not_called()

    def test_direct_constructor_still_enforces_local_endpoint(self):
        for host,port in [('10.0.0.1',7890),('8.8.8.8',7890),('127.0.0.1',True)]:
            with self.subTest(host=host,port=port),self.assertRaises(LocalProxyError):LoopbackProxy(host,port)

    def test_tunnel_rejects_nonfinite_timeout_before_dial(self):
        for timeout in (0,-1,float('nan'),float('inf'),True):
            with self.subTest(timeout=timeout),patch('socket.create_connection') as dial,self.assertRaises(ValueError):
                LoopbackProxy('127.0.0.1',7890).open_tunnel(IP4,timeout)
            dial.assert_not_called()

    def test_environment_report_distinguishes_opt_in_from_system_detection(self):
        from vibe_job_radar.network_environment import inspect_environment
        with patch.dict(os.environ,{ENV:'http://127.0.0.1:7890'}):
            report=inspect_environment(discover=lambda:{})
        self.assertTrue(report['explicit_loopback_proxy']['enabled'])
        self.assertEqual(report['explicit_loopback_proxy']['port'],7890)
        self.assertFalse(report['collector_applies_static_proxy'])
        self.assertFalse(report['explicit_loopback_proxy']['connectivity_tested'])

    def test_invalid_setting_is_not_reported_as_enabled(self):
        from vibe_job_radar.network_environment import inspect_environment
        with patch.dict(os.environ,{ENV:'http://secret:DO-NOT-ECHO@127.0.0.1:7890'}):
            report=inspect_environment(discover=lambda:{})
        self.assertFalse(report['explicit_loopback_proxy']['valid'])
        self.assertNotIn('DO-NOT-ECHO',json.dumps(report))


class RealProxyTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.requests=[];self.connects=[];self.sni=[]
        self.http_status=200;self.proxy_status=200;self.symbol=IP4;owner=self
        self.expected_proxy_auth=None;self.proxy_reason='refused';self.proxy_peers=[]
        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self,*a):pass
            def answer(self):
                body=self.rfile.read(int(self.headers.get('Content-Length','0')))
                owner.requests.append({'method':self.command,'body':body,'host':self.headers.get('Host'),'headers':dict(self.headers)})
                payload=getattr(owner,'payload',b'{"ok":true,"source":"real-controlled-tls"}')
                content_type=getattr(owner,'content_type','application/json')
                if getattr(owner,'browser_fixture',False) and self.path=='/robots.txt':
                    payload=b'User-agent: *\nAllow: /\n';content_type='text/plain'
                self.send_response(owner.http_status);self.send_header('Content-Length',str(len(payload)))
                self.send_header('Content-Type',content_type);self.end_headers();self.wfile.write(payload)
            do_GET=do_POST=answer
        self.target=http.server.ThreadingHTTPServer(('127.0.0.1',0),Handler)
        self.target.daemon_threads=True
        pem=Path(__file__).with_name('fixtures')/'connection_test_only.pem'
        tls=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);tls.load_cert_chain(pem)
        tls.set_servername_callback(lambda sock,name,context:owner.sni.append(name))
        self.target.socket=tls.wrap_socket(self.target.socket,server_side=True)
        self.client=ssl.create_default_context(cafile=str(pem))
        self.real_gai=socket.getaddrinfo;self.real_dial=socket.create_connection
        class Tunnel(socketserver.StreamRequestHandler):
            def handle(self):
                owner.proxy_peers.append(self.client_address[0])
                first=self.rfile.readline(4096).decode('ascii').strip();headers={}
                for _ in range(30):
                    line=self.rfile.readline(4096)
                    if line in (b'\r\n',b'\n',b''):break
                    k,v=line.decode('ascii').split(':',1);headers[k.lower()]=v.strip()
                owner.connects.append((first,headers))
                status=owner.proxy_status
                if owner.expected_proxy_auth is not None and headers.get('proxy-authorization')!=owner.expected_proxy_auth:
                    status=407
                if status!=200:
                    self.wfile.write(f'HTTP/1.1 {status} {owner.proxy_reason}\r\nContent-Length: 0\r\n\r\n'.encode());self.wfile.flush();return
                expected=(f'[{owner.symbol}]' if ':' in owner.symbol else owner.symbol)+':443'
                if first.split()[:2]!=['CONNECT',expected]:return
                # Deliberate test-only mapping in the controlled proxy server.
                upstream=owner.real_dial(getattr(owner,'upstream_address',owner.target.server_address),2)
                try:
                    self.wfile.write(b'HTTP/1.1 200 Connection established\r\n\r\n');self.wfile.flush()
                    peers={self.connection:upstream,upstream:self.connection}
                    while True:
                        ready,_,_=select.select(list(peers),[],[],3)
                        if not ready:return
                        for source in ready:
                            block=source.recv(65536)
                            if not block:return
                            peers[source].sendall(block)
                except OSError:pass
                finally:upstream.close()
        class Server(socketserver.ThreadingTCPServer):
            allow_reuse_address=True;daemon_threads=True
            def handle_error(self,*a):pass
        self.proxy=Server((getattr(self,'proxy_bind','127.0.0.1'),0),Tunnel)
        self.threads=[]
        for server in (self.target,self.proxy):
            t=threading.Thread(target=server.serve_forever,kwargs={'poll_interval':.01},daemon=True);t.start();self.threads.append(t)
        self.setting=f'http://127.0.0.1:{self.proxy.server_address[1]}'
        self.dials=[]

    def tearDown(self):
        for server in (self.proxy,self.target):server.shutdown();server.server_close()
        for t in self.threads:t.join(timeout=3)
        self.tmp.cleanup()

    def perform(self,call,*,trust=True,setting=None):
        def resolve(host,*a,**kw):
            if host==HOST:
                ip=self.symbol
                return [(socket.AF_INET6 if ':' in ip else socket.AF_INET,socket.SOCK_STREAM,socket.IPPROTO_TCP,'',(ip,443,0,0) if ':' in ip else (ip,443))]
            return self.real_gai(host,*a,**kw)
        def dial(endpoint,*a,**kw):
            self.dials.append(endpoint)
            if not ipaddress.ip_address(endpoint[0]).is_loopback:
                raise AssertionError('unexpected direct egress')
            return self.real_dial(endpoint,*a,**kw)
        context=self.client if trust else ssl.create_default_context()
        with patch.dict(os.environ,{ENV:setting or self.setting}),patch('socket.getaddrinfo',side_effect=resolve),\
             patch('vibe_job_radar.network.create_client_context',return_value=context),patch('socket.create_connection',side_effect=dial):
            return call()

    def test_actual_connect_tls_get_reaches_target(self):
        result=self.perform(lambda:SafeHTTP({HOST},interval=0).json(f'https://{HOST}/jobs'))
        self.assertTrue(result['ok']);self.assertEqual(len(self.requests),1)
        self.assertTrue(self.connects[0][0].startswith(f'CONNECT {IP4}:443 '))
        self.assertEqual(self.requests[0]['host'],HOST);self.assertEqual(self.sni,[HOST])
        self.assertEqual(self.dials,[self.proxy.server_address])

    def test_original_browser_bridge_uses_the_same_tunnel(self):
        adapter=SimpleNamespace(key='fixture',domains=(HOST,),resource_domains=())
        ledger=RateLedger(Path(self.tmp.name)/'rate.sqlite',Limits(request_interval=0))
        wire=PinnedTransport(adapter,ledger,threading.Event())
        result=self.perform(lambda:wire.fetch(f'https://{HOST}/detail'))
        self.assertEqual(result.status,200);self.assertEqual(len(self.connects),1)
        self.assertEqual(ledger.summary('fixture')['request']['day'],1)

    def test_post_once_and_no_proxy_auth_to_origin(self):
        self.perform(lambda:SafeHTTP({HOST},interval=0).request(f'https://{HOST}/api',method='POST',body=b'fixture-body'))
        self.assertEqual(len(self.connects),1);self.assertEqual(len(self.requests),1)
        self.assertEqual(self.requests[0]['body'],b'fixture-body')
        self.assertNotIn('Proxy-Authorization',self.requests[0]['headers'])

    def test_proxy_refusal_never_falls_back_to_direct(self):
        self.proxy_status=407
        with self.assertRaises(FetchError) as err:self.perform(lambda:SafeHTTP({HOST},interval=0).request(f'https://{HOST}/'))
        self.assertEqual(err.exception.code,'local_proxy_connection_failed')
        self.assertEqual(len(self.connects),1);self.assertEqual(self.requests,[])
        self.assertEqual(self.dials,[self.proxy.server_address])

    def test_tls_failure_never_disables_verification_or_retries(self):
        with self.assertRaises(FetchError) as err:self.perform(lambda:SafeHTTP({HOST},interval=0).request(f'https://{HOST}/'),trust=False)
        self.assertEqual(err.exception.code,'tls_verification_failed')
        self.assertEqual(len(self.connects),1);self.assertEqual(self.requests,[])

    def test_http_403_is_not_retried(self):
        self.http_status=403
        with self.assertRaises(FetchError) as err:self.perform(lambda:SafeHTTP({HOST},interval=0).request(f'https://{HOST}/'))
        self.assertEqual(err.exception.code,'http_403');self.assertEqual(len(self.requests),1)
        self.assertEqual(len(self.connects),1)

    def test_fake_ip_is_still_rejected_before_proxy(self):
        self.symbol='198.18.0.92'
        with self.assertRaises(FetchError) as err:self.perform(lambda:SafeHTTP({HOST},interval=0).request(f'https://{HOST}/'))
        self.assertEqual(err.exception.code,'non_public_address')
        self.assertEqual(self.dials,[]);self.assertEqual(self.connects,[])

    def test_ipv6_connect_authority_has_correct_brackets(self):
        self.symbol=IP6
        self.perform(lambda:SafeHTTP({HOST},interval=0).request(f'https://{HOST}/'))
        self.assertTrue(self.connects[0][0].startswith(f'CONNECT [{IP6}]:443 '))
        self.assertEqual(self.connects[0][1]['host'],f'[{IP6}]:443')

    def test_connection_mode_is_explicit(self):
        def call():
            connection=PinnedHTTPSConnection(HOST,IP4,3)
            self.assertEqual(connection.network_mode,'loopback_http_proxy')
            connection.request('GET','/');response=connection.getresponse();response.read();connection.close()
        self.perform(call);self.assertEqual(len(self.connects),1)

    def test_browser_bridge_translates_bad_proxy_configuration(self):
        adapter=SimpleNamespace(key='fixture',domains=(HOST,),resource_domains=())
        ledger=RateLedger(Path(self.tmp.name)/'rate.sqlite',Limits(request_interval=0))
        wire=PinnedTransport(adapter,ledger,threading.Event())
        with self.assertRaises(CrawlError) as err:self.perform(lambda:wire.fetch(f'https://{HOST}/'),setting='socks5://127.0.0.1:7890')
        self.assertEqual(err.exception.code,'local_proxy_configuration_invalid');self.assertEqual(self.dials,[])


if __name__=='__main__':unittest.main()
