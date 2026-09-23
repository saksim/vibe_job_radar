"""Actual local SOCKS5/TLS, with artificial targets only. No external network."""
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
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from vibe_job_radar.loopback_proxy import LoopbackProxy, LocalProxyError
from vibe_job_radar.loopback_socks import LoopbackSocks5, select_loopback_proxy
from vibe_job_radar.network import SafeHTTP, FetchError, PinnedHTTPSConnection
from vibe_job_radar.guided.transport import PinnedTransport
from vibe_job_radar.guided.rate import Limits, RateLedger

HOST='network-fixture.invalid'
IP4='93.184.216.34'
IP6='2606:4700:4700::1111'
ENV='VIBE_RADAR_SOCKS_PROXY'
HTTP_ENV='VIBE_RADAR_HTTP_PROXY'


class ConfigurationTests(unittest.TestCase):
    def test_no_config_does_not_guess_system_proxy(self):
        with patch.dict(os.environ,{ENV:'',HTTP_ENV:'','ALL_PROXY':'socks5://127.0.0.1:9999'}):
            self.assertIsNone(select_loopback_proxy())
    def test_supported_local_endpoints(self):
        for url,host in [('socks5://127.0.0.1:1080','127.0.0.1'),('socks5://localhost:8080','127.0.0.1'),('socks5://[::1]:1080','::1')]:
            with self.subTest(url=url),patch.dict(os.environ,{ENV:url,HTTP_ENV:''}):
                self.assertEqual(select_loopback_proxy().host,host)
    def test_nonlocal_credentials_and_remote_dns_rejected_without_echo(self):
        values=['socks5h://127.0.0.1:1080','socks5://192.168.1.1:1080','socks5://8.8.8.8:1080',
                'socks5://name:DO_NOT_ECHO@127.0.0.1:1080','socks5://localhost',
                'socks5://localhost:0','socks5://localhost:70000','socks5://localhost:1080/a',
                'socks5://localhost:1080?password=DO_NOT_ECHO','socks5://localhost:1080#fragment',
                'socks5://local\nhost:1080']
        for value in values:
            with self.subTest(value=value),patch.dict(os.environ,{ENV:value,HTTP_ENV:''}):
                with self.assertRaises(LocalProxyError) as err:select_loopback_proxy()
                self.assertNotIn('DO_NOT_ECHO',str(err.exception))
    def test_http_and_socks_conflict_not_silent_precedence(self):
        with patch.dict(os.environ,{ENV:'socks5://127.0.0.1:1080',HTTP_ENV:'http://127.0.0.1:7890'}),patch('socket.create_connection') as dial:
            with self.assertRaises(FetchError) as err:PinnedHTTPSConnection(HOST,IP4,1)
        self.assertEqual(err.exception.code,'local_proxy_configuration_conflict');dial.assert_not_called()
    def test_legacy_http_path_remains(self):
        with patch.dict(os.environ,{ENV:'',HTTP_ENV:'http://127.0.0.1:7890'}):
            selected=select_loopback_proxy()
        self.assertIs(type(selected),LoopbackProxy)
    def test_model_rejects_private_proxy_not_only_environment(self):
        with self.assertRaises(LocalProxyError):LoopbackSocks5('192.168.1.1',1080)
    def test_bad_target_rejected_before_any_socks_bytes(self):
        for target in ('198.18.0.92','127.0.0.1','10.0.0.1','::1',HOST):
            with self.subTest(target=target),patch('socket.create_connection') as dial:
                with self.assertRaises(LocalProxyError):LoopbackSocks5('127.0.0.1',1080).open_tunnel(target,1)
                dial.assert_not_called()
    def test_invalid_timeout_rejected(self):
        for timeout in (0,-1,True,float('nan'),float('inf')):
            with self.subTest(timeout=timeout),self.assertRaises(ValueError):LoopbackSocks5('127.0.0.1',1080).open_tunnel(IP4,timeout)
    def test_truncated_handshake_closes_socket(self):
        raw=MagicMock();raw.recv.return_value=b''
        with patch('socket.create_connection',return_value=raw):
            with self.assertRaises(LocalProxyError) as err:LoopbackSocks5('127.0.0.1',1080).open_tunnel(IP4,1)
        self.assertEqual(err.exception.code,'local_socks_truncated_reply');raw.close.assert_called_once()
    def test_timeout_cannot_reset_between_partial_reads(self):
        now=[0.0];raw=MagicMock()
        def receive(n):now[0]+=.75;return b'\x05'
        raw.recv.side_effect=receive
        with patch('socket.create_connection',return_value=raw),patch('time.monotonic',side_effect=lambda:now[0]):
            with self.assertRaises(LocalProxyError):LoopbackSocks5('127.0.0.1',1080).open_tunnel(IP4,1)
        raw.close.assert_called_once()


class RealSocksTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.requests=[];self.connects=[];self.dials=[];self.sni=[]
        self.reply=0;self.method=0;self.version=5;self.bound_type=1;self.reserved=0;self.truncate=False
        self.symbol=IP4;self.http_status=200;self.fragment=False;self.pause_handshake=False;owner=self
        self.expected_credentials=None;self.authentication=[];self.greetings=[]
        self.auth_status=0;self.auth_version=1
        class Origin(http.server.BaseHTTPRequestHandler):
            def log_message(self,*a):pass
            def answer(self):
                value=self.rfile.read(int(self.headers.get('Content-Length','0')))
                owner.requests.append((self.command,value,dict(self.headers)))
                data=getattr(owner,'payload',b'{"ok":true,"fixture":"socks-through-tls"}')
                content_type=getattr(owner,'content_type','application/json')
                if getattr(owner,'browser_fixture',False) and self.path=='/robots.txt':
                    data=b'User-agent: *\nAllow: /\n';content_type='text/plain'
                self.send_response(owner.http_status);self.send_header('Content-Length',str(len(data)))
                self.send_header('Content-Type',content_type);self.end_headers();self.wfile.write(data)
            do_GET=do_POST=answer
        self.origin=http.server.ThreadingHTTPServer(('127.0.0.1',0),Origin);self.origin.daemon_threads=True
        pem=Path(__file__).with_name('fixtures')/'connection_test_only.pem'
        tls=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);tls.load_cert_chain(pem)
        tls.set_servername_callback(lambda sock,name,context:owner.sni.append(name))
        self.origin.socket=tls.wrap_socket(self.origin.socket,server_side=True)
        self.trust=ssl.create_default_context(cafile=str(pem))
        self.real_gai=socket.getaddrinfo;self.real_dial=socket.create_connection
        class Socks(socketserver.StreamRequestHandler):
            def emit(self,data):
                for item in ([data] if not owner.fragment else [bytes([b]) for b in data]):
                    self.wfile.write(item);self.wfile.flush()
                    if owner.fragment:time.sleep(.001)
            def handle(self):
                self.connection.settimeout(2)
                hello=self.rfile.read(3)
                owner.greetings.append(hello)
                if hello not in (b'\x05\x01\x00',b'\x05\x01\x02'):return
                if owner.pause_handshake:time.sleep(.3);return
                if owner.truncate:self.emit(b'\x05');return
                self.emit(bytes((owner.version,owner.method)))
                if owner.version!=5:return
                if owner.method==2:
                    if hello!=b'\x05\x01\x02':return
                    version=self.rfile.read(1);length=self.rfile.read(1)
                    if version!=b'\x01' or not length:return
                    username=self.rfile.read(length[0]);length=self.rfile.read(1)
                    if not length:return
                    password=self.rfile.read(length[0]);owner.authentication.append((username,password))
                    status=owner.auth_status
                    if owner.expected_credentials is not None and (username,password)!=owner.expected_credentials:
                        status=1
                    self.emit(bytes((owner.auth_version,status)))
                    if status or owner.auth_version!=1:return
                elif owner.method!=0:return
                header=self.rfile.read(4)
                if len(header)!=4:return
                n={1:4,4:16}.get(header[3]);
                if n is None:return
                target=str(ipaddress.ip_address(self.rfile.read(n)))
                port=int.from_bytes(self.rfile.read(2),'big');owner.connects.append((target,port,header[3]))
                if target!=owner.symbol or port!=443:return
                bound={1:b'\x7f\x00\x00\x01',4:b'\0'*15+b'\1',3:b'\x04test',9:b''}[owner.bound_type]
                self.emit(bytes((5,owner.reply,owner.reserved,owner.bound_type))+bound+b'\x01\xbb')
                if owner.reply or owner.reserved or owner.bound_type==9:return
                # Only this artificial proxy maps the public symbol to loopback.
                upstream=owner.real_dial(getattr(owner,'upstream_address',owner.origin.server_address),2)
                try:
                    peers={self.connection:upstream,upstream:self.connection}
                    while True:
                        ready,_,_=select.select(list(peers),[],[],2)
                        if not ready:return
                        for source in ready:
                            data=source.recv(65536)
                            if not data:return
                            peers[source].sendall(data)
                except OSError:pass
                finally:upstream.close()
        class Server(socketserver.ThreadingTCPServer):
            allow_reuse_address=True;daemon_threads=True
            def handle_error(self,*a):pass
        self.proxy=Server(('127.0.0.1',0),Socks)
        self.threads=[]
        for server in (self.origin,self.proxy):
            thread=threading.Thread(target=server.serve_forever,kwargs={'poll_interval':.01},daemon=True)
            thread.start();self.threads.append(thread)
        self.setting=f'socks5://127.0.0.1:{self.proxy.server_address[1]}'
    def tearDown(self):
        for server in (self.proxy,self.origin):server.shutdown();server.server_close()
        for thread in self.threads:thread.join(timeout=3)
        self.tmp.cleanup()
    def perform(self,operation,*,trust=True):
        def gai(host,*a,**kw):
            if host==HOST:return [(socket.AF_INET6 if ':' in self.symbol else socket.AF_INET,socket.SOCK_STREAM,6,'',(self.symbol,443))]
            return self.real_gai(host,*a,**kw)
        def dial(endpoint,*a,**kw):
            self.dials.append(endpoint)
            if endpoint!=self.proxy.server_address:raise AssertionError('unexpected direct connection')
            return self.real_dial(endpoint,*a,**kw)
        context=self.trust if trust else ssl.create_default_context()
        with patch.dict(os.environ,{ENV:self.setting,HTTP_ENV:''}),patch('socket.getaddrinfo',side_effect=gai),patch('socket.create_connection',side_effect=dial),patch('vibe_job_radar.network.create_client_context',return_value=context):return operation()
    def request(self,**kwargs):return self.perform(lambda:SafeHTTP({HOST},interval=0).json(f'https://{HOST}/jobs'),**kwargs)
    def test_get_has_real_socks_handshake_and_target_tls(self):
        self.assertTrue(self.request()['ok']);self.assertEqual(self.connects,[(IP4,443,1)]);self.assertEqual(self.sni,[HOST]);self.assertEqual(len(self.requests),1)
    def test_browser_bridge_uses_identical_socks_route(self):
        adapter=SimpleNamespace(key='fixture',domains=(HOST,),resource_domains=())
        ledger=RateLedger(Path(self.tmp.name)/'rate.sqlite',Limits(request_interval=0))
        wire=PinnedTransport(adapter,ledger,threading.Event())
        self.assertEqual(self.perform(lambda:wire.fetch(f'https://{HOST}/job')).status,200)
        self.assertEqual(len(self.connects),1);self.assertEqual(ledger.summary('fixture')['request']['day'],1)
    def test_ipv6_destination_is_numeric_not_domain_atyp(self):
        self.symbol=IP6;self.request();self.assertEqual(self.connects,[(IP6,443,4)])
    def test_fragmented_reply_is_reassembled(self):
        self.fragment=True;self.assertTrue(self.request()['ok'])
    def test_ipv6_bound_address_is_consumed(self):
        self.bound_type=4;self.assertTrue(self.request()['ok'])
    def test_domain_bound_address_is_consumed_not_followed(self):
        self.bound_type=3;self.assertTrue(self.request()['ok']);self.assertEqual(self.dials,[self.proxy.server_address])
    def test_post_body_only_sent_once(self):
        self.perform(lambda:SafeHTTP({HOST},interval=0).request(f'https://{HOST}/once',method='POST',body=b'fixture-body'))
        self.assertEqual(len(self.requests),1);self.assertEqual(self.requests[0][1],b'fixture-body')
    def test_auth_required_does_not_fall_back_or_send_http(self):
        self.method=2
        with self.assertRaises(FetchError) as err:self.request()
        self.assertEqual(err.exception.code,'local_socks_auth_unsupported');self.assertEqual(self.requests,[]);self.assertEqual(len(self.dials),1)
    def test_denied_connect_does_not_change_destination(self):
        self.reply=2
        with self.assertRaises(FetchError) as err:self.request()
        self.assertEqual(err.exception.code,'local_socks_request_rejected');self.assertEqual(self.requests,[]);self.assertEqual(len(self.dials),1)
    def test_invalid_version_is_distinct_error(self):
        self.version=4
        with self.assertRaises(FetchError) as err:self.request()
        self.assertEqual(err.exception.code,'local_socks_protocol_error')
    def test_invalid_reserved_field_is_rejected(self):
        self.reserved=1
        with self.assertRaises(FetchError) as err:self.request()
        self.assertEqual(err.exception.code,'local_socks_protocol_error')
    def test_invalid_bound_address_type_is_rejected(self):
        self.bound_type=9
        with self.assertRaises(FetchError) as err:self.request()
        self.assertEqual(err.exception.code,'local_socks_protocol_error')
    def test_truncated_reply_not_misreported_as_dns(self):
        self.truncate=True
        with self.assertRaises(FetchError) as err:self.request()
        self.assertEqual(err.exception.code,'local_socks_truncated_reply')
    def test_handshake_timeout_does_not_reconnect(self):
        self.pause_handshake=True
        with self.assertRaises(FetchError) as err:self.perform(lambda:SafeHTTP({HOST},interval=0,timeout=.1).request(f'https://{HOST}/'))
        self.assertEqual(err.exception.code,'local_socks_timeout');self.assertEqual(len(self.dials),1)
    def test_bad_target_certificate_not_ignored(self):
        with self.assertRaises(FetchError) as err:self.request(trust=False)
        self.assertEqual(err.exception.code,'tls_verification_failed');self.assertEqual(self.requests,[])
    def test_origin_403_does_not_retry_socks(self):
        self.http_status=403
        with self.assertRaises(FetchError) as err:self.request()
        self.assertEqual(err.exception.code,'http_403');self.assertEqual(len(self.dials),1);self.assertEqual(len(self.requests),1)
    def test_fake_ip_still_stops_before_proxy(self):
        self.symbol='198.18.0.92'
        with self.assertRaises(FetchError) as err:self.request()
        self.assertEqual(err.exception.code,'non_public_address');self.assertEqual(self.dials,[])
    def test_mode_is_explicit_not_system_proxy_claim(self):
        with patch.dict(os.environ,{ENV:self.setting,HTTP_ENV:''}):
            connection=PinnedHTTPSConnection(HOST,IP4,1)
        self.assertEqual(connection.network_mode,'loopback_socks5_proxy');connection.close()


if __name__=='__main__':unittest.main()
