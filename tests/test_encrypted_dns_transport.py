"""Real local TLS and HTTP CONNECT for artificial DNS/target exchanges.

Only this test harness maps public IP symbols to a loopback TLS fixture. The
production resolver has no redirect-to-loopback setting or test trust switch.
"""
import http.server
import ipaddress
import json
import select
import socket
import socketserver
import ssl
import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from test_encrypted_dns import HOST, IP4, IP6, fake_answers, record, wire
from vibe_job_radar.encrypted_dns import BOOTSTRAP, DOH_HOST, PublicResolver
from vibe_job_radar.network import FetchError, SafeHTTP
from vibe_job_radar.network_policy import NetworkPolicy, use_policy
from vibe_job_radar.loopback_proxy import LoopbackProxy
from vibe_job_radar.guided.transport import PinnedTransport
from vibe_job_radar.guided.rate import RateLedger, Limits
from vibe_job_radar.guided.contracts import CrawlError
from vibe_job_radar.workbench import LocalServer
from vibe_job_radar.workspace import Workspace


class EncryptedRoundTripTests(unittest.TestCase):
    def setUp(self):
        self.dials=[];self.posts=[];self.targets=[];self.sni=[];self.connects=[]
        self.status=200;self.mime='application/dns-message';self.age='0';self.cache='max-age=60'
        self.private=False;self.denied=False;self.target_status=200;self.cookies=[];self.socks=False
        self.real_dial=socket.create_connection;self.real_dns=socket.getaddrinfo;owner=self
        class Origin(http.server.BaseHTTPRequestHandler):
            def log_message(self,*a):pass
            def do_POST(self):
                body=self.rfile.read(int(self.headers['Content-Length']))
                owner.posts.append((self.path,body,dict(self.headers)))
                kind=int.from_bytes(body[-4:-2],'big')
                records=None
                if owner.private:records=[record(HOST,kind,ipaddress.ip_address('::1' if kind==28 else '10.0.0.1').packed)]
                data=wire(kind=kind,records=records)
                self.send_response(owner.status);self.send_header('Content-Type',owner.mime)
                self.send_header('Content-Length',str(len(data)));self.send_header('Age',owner.age)
                self.send_header('Cache-Control',owner.cache);self.send_header('Set-Cookie','NEVER-FORWARD=secret')
                self.send_header('Location','https://never-follow.example/dns');self.end_headers();self.wfile.write(data)
            def do_GET(self):
                owner.targets.append((self.path,dict(self.headers)));data=b'{"fixture":"verified-target"}'
                self.send_response(owner.target_status);self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)
        self.origin=http.server.ThreadingHTTPServer(('127.0.0.1',0),Origin);self.origin.daemon_threads=True
        pem=Path(__file__).with_name('fixtures')/'encrypted_dns_test_only.pem'
        tls=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);tls.load_cert_chain(str(pem))
        tls.set_servername_callback(lambda sock,name,ctx:self.sni.append(name))
        self.origin.socket=tls.wrap_socket(self.origin.socket,server_side=True)
        self.trust=ssl.create_default_context(cafile=str(pem))
        class Proxy(socketserver.StreamRequestHandler):
            def handle(self):
                self.connection.settimeout(2)
                if owner.socks:
                    if self.rfile.read(3)!=b'\x05\x01\x00':return
                    self.wfile.write(b'\x05\x00');self.wfile.flush()
                    header=self.rfile.read(4)
                    if len(header)!=4 or header[:3]!=b'\x05\x01\x00':return
                    length={1:4,4:16}.get(header[3])
                    if length is None:return
                    address=str(ipaddress.ip_address(self.rfile.read(length)))
                    port=int.from_bytes(self.rfile.read(2),'big')
                    owner.connects.append(f'SOCKS5 {address}:{port}')
                    if address not in {*BOOTSTRAP,IP4,IP6} or port!=443:return
                    self.wfile.write(bytes((5,2 if owner.denied else 0,0,1))+b'\x7f\x00\x00\x01\x00\x00');self.wfile.flush()
                    if owner.denied:return
                else:
                    line=self.rfile.readline().decode().strip();owner.connects.append(line)
                    while self.rfile.readline().strip():pass
                    if owner.denied:
                        self.wfile.write(b'HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n');self.wfile.flush();return
                    self.wfile.write(b'HTTP/1.1 200 Connection Established\r\n\r\n');self.wfile.flush()
                upstream=owner.real_dial(owner.origin.server_address,2)
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
        self.proxy=Server(('127.0.0.1',0),Proxy)
        self.threads=[]
        for server in (self.origin,self.proxy):
            thread=threading.Thread(target=server.serve_forever,kwargs={'poll_interval':.01},daemon=True);thread.start();self.threads.append(thread)
        self.resolver=PublicResolver()
        self.policy=NetworkPolicy(encrypted_dns=True,resolver=self.resolver)

    def tearDown(self):
        for server in (self.proxy,self.origin):server.shutdown();server.server_close()
        for thread in self.threads:thread.join(timeout=3)

    def perform(self, callback, *, proxy=False, trust=True):
        def gai(host,*a,**kw):
            if host==HOST:return fake_answers('198.18.0.42')
            if host==DOH_HOST:raise AssertionError('bootstrap must not recurse into system DNS')
            return self.real_dns(host,*a,**kw)
        def dial(endpoint,*a,**kw):
            self.dials.append(endpoint)
            if proxy:
                if endpoint!=self.proxy.server_address:raise AssertionError('proxy route leaked to direct')
                return self.real_dial(endpoint,*a,**kw)
            if endpoint[0] not in {*BOOTSTRAP,IP4,IP6} or endpoint[1]!=443:raise AssertionError('unexpected destination')
            return self.real_dial(self.origin.server_address,*a,**kw)
        context=self.trust if trust else ssl.create_default_context()
        contexts=[self.trust,self.trust,ssl.create_default_context()] if trust=='resolver-only' else None
        if proxy:
            from vibe_job_radar.loopback_socks import LoopbackSocks5
            factory=LoopbackSocks5 if self.socks else LoopbackProxy
            self.policy=replace(self.policy,source='automatic_static',proxy=factory('127.0.0.1',self.proxy.server_address[1]),bypass=(DOH_HOST,))
        with patch('socket.getaddrinfo',side_effect=gai),patch('socket.create_connection',side_effect=dial),patch('vibe_job_radar.network.create_client_context',side_effect=contexts,return_value=context):
            return callback()

    def get(self, **kw):
        return self.perform(lambda:SafeHTTP({HOST},interval=0,network_policy=self.policy).json('https://'+HOST+'/jobs?filter=PRIVATE-QUERY'),**kw)

    def test_fake_dns_to_actual_verified_tls_with_no_data_leak(self):
        self.assertEqual(self.get()['fixture'],'verified-target')
        self.assertEqual(len(self.posts),2);self.assertEqual(len(self.targets),1)
        self.assertEqual(self.sni,[DOH_HOST,DOH_HOST,HOST])
        for path,body,headers in self.posts:
            self.assertEqual(path,'/dns-query');self.assertNotIn(b'PRIVATE-QUERY',body)
            self.assertNotIn('Cookie',headers);self.assertNotIn('Authorization',headers)
        self.assertNotIn('Cookie',self.targets[0][1])
        self.assertNotIn('198.18.0.42',[h for h,p in self.dials])

    def test_same_selected_proxy_for_dns_and_target_despite_no_proxy_resolver(self):
        self.get(proxy=True)
        self.assertEqual(self.dials,[self.proxy.server_address]*3)
        self.assertEqual(len(self.connects),3);self.assertTrue(all('198.18.' not in line for line in self.connects))

    def test_same_selected_socks5_transports_dns_and_target(self):
        self.socks=True;self.get(proxy=True)
        self.assertEqual(self.dials,[self.proxy.server_address]*3)
        self.assertEqual(len(self.posts),2);self.assertEqual(len(self.targets),1)
        self.assertTrue(all(line.startswith('SOCKS5 ') for line in self.connects))
        self.assertEqual(self.sni,[DOH_HOST,DOH_HOST,HOST])

    def test_pac_cannot_choose_another_route_for_encrypted_dns(self):
        from vibe_job_radar.pac import PacSnapshot
        from vibe_job_radar import pac_native
        raw=f'PROXY 127.0.0.1:{self.proxy.server_address[1]}; DIRECT'
        script='function FindProxyForURL(url,host){return "DIRECT";}'
        seen=[]
        def evaluate(source,url,permission):
            seen.append(url)
            return raw if url=='https://'+HOST+'/' else 'DIRECT'
        self.policy=replace(self.policy,source='explicit_workspace',pac=PacSnapshot(script,lambda:True),pac_id='fixture')
        with patch.object(pac_native,'evaluate',side_effect=evaluate):
            self.assertEqual(self.get(proxy=True)['fixture'],'verified-target')
        self.assertEqual(seen,['https://'+HOST+'/'])
        self.assertEqual(self.dials,[self.proxy.server_address]*3)
        self.assertEqual(self.sni,[DOH_HOST,DOH_HOST,HOST])

    def test_socks5_refusal_never_falls_back_to_direct(self):
        self.socks=True;self.denied=True
        with self.assertRaisesRegex(FetchError,'encrypted_dns_route_failed'):self.get(proxy=True)
        self.assertEqual(self.dials,[self.proxy.server_address]);self.assertEqual(self.posts,[])

    def test_successful_doh_does_not_disable_target_certificate_validation(self):
        with self.assertRaisesRegex(FetchError,'tls_verification_failed'):self.get(trust='resolver-only')
        self.assertEqual(len(self.posts),2);self.assertEqual(self.targets,[])

    def test_403_proxy_does_not_direct_retry_or_send_target(self):
        self.denied=True
        with self.assertRaisesRegex(FetchError,'encrypted_dns_route_failed'):self.get(proxy=True)
        self.assertEqual(self.dials,[self.proxy.server_address]);self.assertEqual(self.targets,[])

    def test_bad_dns_tls_never_reaches_target(self):
        with self.assertRaisesRegex(FetchError,'encrypted_dns_tls_failed'):self.get(trust=False)
        self.assertEqual(self.posts,[]);self.assertEqual(self.targets,[])

    def test_private_doh_answers_rejected_before_target(self):
        self.private=True
        with self.assertRaisesRegex(FetchError,'encrypted_dns_non_public_answer'):self.get()
        self.assertEqual(len(self.posts),1);self.assertEqual(self.targets,[])

    def test_redirect_response_not_followed(self):
        self.status=302
        with self.assertRaisesRegex(FetchError,'encrypted_dns_http_rejected'):self.get()
        self.assertEqual(len(self.posts),1);self.assertEqual(self.targets,[])

    def test_mime_and_age_are_not_ignored(self):
        self.mime='text/html'
        with self.assertRaisesRegex(FetchError,'invalid_response'):self.get()
        self.assertEqual(self.targets,[])

    def test_no_store_responses_not_cached(self):
        self.cache='no-store';self.get()
        self.assertEqual(self.resolver._cache,{})

    def test_browser_bridge_same_real_repair_and_target_tls(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger=RateLedger(Path(tmp)/'rate.sqlite',Limits(request_interval=0))
            def run():
                with use_policy(self.policy):
                    transport=PinnedTransport(SimpleNamespace(key='fixture',domains=(HOST,),resource_domains=()),ledger,threading.Event())
                    return transport.fetch('https://'+HOST+'/job')
            self.assertEqual(self.perform(run,proxy=True).status,200)
        self.assertEqual(len(self.posts),2);self.assertEqual(len(self.targets),1)

    def test_source_403_stays_hard_refusal_without_repair_retry(self):
        self.target_status=403
        with self.assertRaisesRegex(FetchError,'http_403'):self.get()
        self.assertEqual(len(self.targets),1)


class SettingsHTTPTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.workspace=Workspace(self.tmp.name)
        self.server=LocalServer(self.workspace,public_client=None)
        self.worker=threading.Thread(target=self.server.serve_forever,kwargs={'poll_interval':.01},daemon=True);self.worker.start()
    def tearDown(self):
        self.server.shutdown();self.server.server_close();self.worker.join(timeout=3);self.tmp.cleanup()
    def call(self,path,data=None,token=True,origin=None):
        import http.client
        conn=http.client.HTTPConnection(*self.server.server_address,timeout=3)
        headers={'X-Radar-Token':self.server.token} if token else {}
        if origin is not None:headers['Origin']=origin
        body=None
        if data is not None:body=json.dumps(data).encode();headers['Content-Type']='application/json'
        try:
            conn.request('POST' if data is not None else 'GET',path,body,headers)
            resp=conn.getresponse();return resp.status,json.loads(resp.read())
        finally:conn.close()
    def test_authentication_origin_and_persistence(self):
        change={'mode':'fake_ip_doh','consent':True,'revision':0}
        self.assertEqual(self.call('/api/network/state',token=False)[0],403)
        self.assertEqual(self.call('/api/network/preferences',change,token=False)[0],403)
        self.assertEqual(self.call('/api/network/preferences',change,origin='https://evil.example')[0],403)
        real=socket.getaddrinfo
        def local(host,*args,**kw):
            if host!='127.0.0.1':raise AssertionError('no external DNS from saving')
            return real(host,*args,**kw)
        with patch('socket.getaddrinfo',side_effect=local):
            status,value=self.call('/api/network/preferences',change)
        self.assertEqual(status,200);self.assertEqual(value['mode'],'fake_ip_doh')
        self.assertEqual(self.call('/api/network/state')[1]['revision'],1)
        self.assertEqual(self.call('/api/network/preferences',change)[0],400)
