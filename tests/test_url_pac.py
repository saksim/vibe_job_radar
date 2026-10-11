"""Full URL routing: distinct paths, no fallback, revocation and owned workers."""
import json
import multiprocessing
import socket
import threading
import time
import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from vibe_job_radar import pac_url_worker
from vibe_job_radar.pac_url import UrlPacSnapshot, canonical_url
from vibe_job_radar.pac import PacSnapshot
from vibe_job_radar.loopback_proxy import LocalProxyError
from vibe_job_radar.network_policy import NetworkPolicy, current_policy
from vibe_job_radar.network import SafeHTTP, FetchError, PinnedHTTPSConnection
from vibe_job_radar.guided.transport import PinnedTransport
from vibe_job_radar.guided.native_tunnel import NativeTunnel

SCRIPT = 'function FindProxyForURL(url,host) { return "DIRECT"; }'
HOST = 'example.com'
URL = 'https://example.com/first?private=ONE'


class UrlPacTests(unittest.TestCase):
    def policy(self, permitted=lambda: True):
        return NetworkPolicy('explicit_workspace', pac=UrlPacSnapshot(SCRIPT, permitted), pac_id='grant')

    def test_canonical_url_preserves_path_and_query_but_rejects_unsafe_input(self):
        self.assertEqual(canonical_url('https://EXAMPLE.com:443/a%2Fb?q=A%2BB&x=2'),
                         ('example.com', 'https://example.com/a%2Fb?q=A%2BB&x=2'))
        self.assertEqual(canonical_url('https://example.com?'), ('example.com', 'https://example.com/'))
        for url in ('http://example.com/a','https://u:p@example.com/a','https://@example.com/',
                    'https://example.com:444/','https://example.com/#secret','https://example.com/#',
                    'https://example.com/a\nb','https://example.com/a b','https://example.com\\x/',
                    'https://example.com/'+'x'*8192,None):
            with self.subTest(url=url),self.assertRaisesRegex(LocalProxyError,'pac_url_invalid_target'):
                canonical_url(url)

    def test_distinct_urls_same_host_do_not_share_routes_and_raw_url_is_not_retained(self):
        policy=self.policy()
        with patch.object(pac_url_worker,'evaluate',side_effect=['PROXY 127.0.0.1:8001','SOCKS5 127.0.0.1:8002']) as evaluate:
            self.assertEqual(policy.describe()['transport'],'pac_by_url')
            evaluate.assert_not_called()
            first=policy.for_request(URL);second=policy.for_request(URL.replace('first','second'))
            self.assertEqual(first.for_host(HOST).port,8001)
            self.assertEqual(second.for_host(HOST).port,8002)
            self.assertEqual(policy.for_request(URL).for_host(HOST).port,8001)
            self.assertEqual(evaluate.call_count,2)
            self.assertEqual(evaluate.call_args_list[0].args[1:3],(URL,HOST))
            self.assertNotEqual(first.fingerprint,second.fingerprint)
            public=repr(first)+repr(first.pac)+repr(policy.pac._cache)+json.dumps(first.describe())
            self.assertNotIn('private',public);self.assertNotIn('ONE',public)
            with self.assertRaisesRegex(LocalProxyError,'pac_url_required'):first.for_host('other.example')
            with self.assertRaisesRegex(LocalProxyError,'pac_url_required'):policy.for_host(HOST)

    def test_error_cache_rejects_unknown_result_without_evaluation_retry(self):
        policy=self.policy()
        with patch.object(pac_url_worker,'evaluate',return_value='UNKNOWN local:9; DIRECT') as evaluate:
            for _ in range(2):
                with self.assertRaisesRegex(LocalProxyError,'pac_invalid_result'):policy.for_request(URL)
            evaluate.assert_called_once()

    def test_revocation_applies_to_cached_routes_and_existing_connection_before_dial(self):
        allowed=[True];policy=self.policy(lambda:allowed[0])
        with patch.object(pac_url_worker,'evaluate',return_value='DIRECT'):
            route=policy.for_request(URL)
            conn=PinnedHTTPSConnection(HOST,('93.184.216.34',),1,network_policy=route)
        allowed[0]=False
        with patch('socket.create_connection') as dial:
            with self.assertRaisesRegex(FetchError,'pac_revoked'):conn.connect()
            with self.assertRaisesRegex(LocalProxyError,'pac_revoked'):policy.for_request(URL)
        dial.assert_not_called();conn.close()

    def test_cancel_close_and_permission_errors_stop_route(self):
        for permission in (lambda:False,lambda:1,lambda:1/0):
            with self.assertRaisesRegex(LocalProxyError,'pac_revoked'):self.policy(permission).for_request(URL)
        for mode in ('cancel','close'):
            policy=self.policy();event=threading.Event();policy.bind_cancellation(event)
            with patch.object(pac_url_worker,'evaluate',return_value='DIRECT'):
                bound=policy.for_request(URL)
            if mode=='cancel':event.set()
            else:policy.close()
            with self.assertRaisesRegex(LocalProxyError,'pac_revoked'):bound.for_host(HOST)

    def test_cache_has_bounded_url_keys_and_keeps_first_route(self):
        policy=self.policy()
        with patch.object(pac_url_worker,'evaluate',return_value='DIRECT') as evaluate:
            for n in range(128):policy.for_request('https://example.com/'+str(n))
            with self.assertRaisesRegex(LocalProxyError,'pac_url_cache_limit'):
                policy.for_request('https://example.com/overflow')
            self.assertIsNone(policy.for_request('https://example.com/0').for_host(HOST))
            self.assertEqual(evaluate.call_count,128)
            self.assertTrue(all(len(k)==64 for k in policy.pac._cache))

    def test_native_refuses_before_socket_or_script_and_domain_mode_is_unchanged(self):
        policy=self.policy()
        with patch('socket.socket') as sock,patch.object(pac_url_worker,'evaluate') as evaluate:
            with self.assertRaisesRegex(FetchError,'pac_url_native_unsupported'):
                NativeTunnel([HOST],policy,threading.Event())
        sock.assert_not_called();evaluate.assert_not_called()
        old=NetworkPolicy('explicit_workspace',pac=PacSnapshot(SCRIPT,lambda:True))
        self.assertIs(old.for_request(URL),old);self.assertIsNone(old.require_native_support())

    def test_doh_provider_keeps_original_target_route_and_revocation(self):
        policy=self.policy()
        with patch.object(pac_url_worker,'evaluate',return_value='PROXY 127.0.0.1:8001') as evaluate:
            bound=policy.for_request(URL)
            from vibe_job_radar.encrypted_dns import PublicResolver, ResolutionError
            observed=[]
            def connection(host,ips,timeout,*,network_policy):
                self.assertEqual(host,'cloudflare-dns.com')
                observed.append(network_policy)
                self.assertEqual(network_policy.for_host(host).port,8001)
                conn=MagicMock()
                response=MagicMock(status=503);response.getheaders.return_value=[]
                conn.getresponse.return_value=response
                return conn
            with patch('vibe_job_radar.network.PinnedHTTPSConnection',side_effect=connection):
                with self.assertRaisesRegex(ResolutionError,'encrypted_dns_http_rejected'):
                    PublicResolver()._exchange(HOST,1,bound,time.monotonic()+10)
            self.assertEqual(bound.for_host(HOST).port,8001)
            evaluate.assert_called_once()
        policy.close()
        with self.assertRaisesRegex(LocalProxyError,'pac_revoked'):observed[0].for_host('cloudflare-dns.com')

    def test_doh_cache_reuses_same_route_but_isolates_protocol_and_endpoint(self):
        from vibe_job_radar.encrypted_dns import PublicResolver
        from vibe_job_radar.dns_wire import Answer
        policy=replace(self.policy(),encrypted_dns=True,public_dns=True)
        resolver=PublicResolver()
        routes=['PROXY 127.0.0.1:8001','PROXY 127.0.0.1:8001',
                'SOCKS5 127.0.0.1:8001','PROXY 127.0.0.1:8002']
        def answer(host,kind,*args,**kwargs):
            return Answer(('93.184.216.34',) if kind==1 else (),120,host)
        with patch.object(pac_url_worker,'evaluate',side_effect=routes) as evaluate,\
             patch.object(resolver,'_exchange',side_effect=answer) as exchange,\
             patch('socket.getaddrinfo') as system_dns:
            results=[]
            for index,calls in enumerate((2,2,4,6)):
                bound=policy.for_request('https://example.com/jobs?page='+str(index))
                results.append(resolver.resolve(HOST,bound))
                self.assertEqual(exchange.call_count,calls)
            self.assertEqual([r.cache_reused for r in results],[False,True,False,False])
            self.assertEqual(len(resolver._requests),3)
            self.assertEqual(evaluate.call_count,4)
            system_dns.assert_not_called()

    def test_http_and_bridge_bind_each_request_before_dns_and_connect(self):
        for bridge in (False,True):
            with self.subTest(bridge=bridge):
                policy=self.policy();ports=[]
                response=MagicMock(status=200)
                response.getheaders.return_value=[];response.read.return_value=b'complete'
                def connection(host,ips,timeout):
                    ports.append(current_policy().for_host(host).port)
                    conn=MagicMock();conn.getresponse.return_value=response;return conn
                location='vibe_job_radar.guided.transport' if bridge else 'vibe_job_radar.network'
                if bridge:
                    adapter=SimpleNamespace(key='test',domains=[HOST],resource_domains=[])
                    client=PinnedTransport(adapter,MagicMock(),threading.Event());client.bind_policy(policy)
                    fetch=client.fetch
                else:fetch=SafeHTTP({HOST},interval=0,network_policy=policy).request
                with patch.object(pac_url_worker,'evaluate',side_effect=['PROXY 127.0.0.1:8001','PROXY 127.0.0.1:8002']),\
                     patch('socket.getaddrinfo',return_value=[(socket.AF_INET,socket.SOCK_STREAM,6,'',('93.184.216.34',443))]),\
                     patch(location+'.PinnedHTTPSConnection',side_effect=connection):
                    fetch(URL);fetch(URL.replace('first','second'))
                self.assertEqual(ports,[8001,8002])

    def test_failed_script_stops_http_before_dns_and_connection(self):
        policy=self.policy()
        with patch.object(pac_url_worker,'evaluate',side_effect=LocalProxyError('pac_invalid_script')),\
             patch('socket.getaddrinfo') as dns,patch('socket.create_connection') as connect:
            with self.assertRaisesRegex(FetchError,'pac_invalid_script'):
                SafeHTTP({HOST},interval=0,network_policy=policy).request(URL)
        dns.assert_not_called();connect.assert_not_called()

    def test_actual_optional_engine_paths_helpers_resources_and_cleanup(self):
        before={c.pid for c in multiprocessing.active_children()}
        if not pac_url_worker.available():
            with self.assertRaisesRegex(LocalProxyError,'pac_url_unavailable'):
                pac_url_worker.evaluate(SCRIPT,URL,HOST,lambda:True)
            return
        source="""function FindProxyForURL(url,host) {
            if (!dnsDomainIs(host,'.com') || dnsDomainLevels(host)!==1 ||
                !isPlainHostName('example') || !localHostOrDomainIs('example',host))
                throw new Error('helpers');
            return shExpMatch(url,'https://example.com/first?private=*') ?
                'PROXY 127.0.0.1:8001' : 'SOCKS5 127.0.0.1:8002';
        }"""
        self.assertEqual(pac_url_worker.evaluate(source,URL,HOST,lambda:True),'PROXY 127.0.0.1:8001')
        self.assertEqual(pac_url_worker.evaluate(source,URL.replace('first','second'),HOST,lambda:True),
                         'SOCKS5 127.0.0.1:8002')
        for body in ("dnsResolve(host); return 'DIRECT';", "while(true) {}",
                     "var items=[]; while(true) items.push(new Array(100000).fill(1));",
                     "function f(){return f();} return f();",
                     "throw new Error(url);"):
            with self.subTest(body=body),self.assertRaisesRegex(LocalProxyError,'pac_invalid_script') as error:
                pac_url_worker.evaluate('function FindProxyForURL(url,host){'+body+'}',URL,HOST,lambda:True)
            self.assertNotIn('ONE',str(error.exception))
        self.assertEqual({c.pid for c in multiprocessing.active_children()},before)

    def test_worker_deadline_includes_startup_and_receipt_and_releases_slots(self):
        for values in ([0.,9.], [0.,1.,1.,9.]):
            context=MagicMock();receiver=MagicMock();sender=MagicMock()
            context.Pipe.return_value=(receiver,sender)
            process=context.Process.return_value;process.pid=42;process.is_alive.return_value=True
            receiver.poll.return_value=True;receiver.recv_bytes.return_value=b'{"raw":"DIRECT"}'
            clock=MagicMock();clock.monotonic.side_effect=values
            with patch.object(pac_url_worker,'available',return_value=True),\
                 patch.object(pac_url_worker,'time',clock),\
                 patch.object(pac_url_worker.multiprocessing,'get_context',return_value=context):
                with self.assertRaisesRegex(LocalProxyError,'pac_timeout'):
                    pac_url_worker.evaluate(SCRIPT,URL,HOST,lambda:True)
            process.terminate.assert_called_once();process.close.assert_called_once()
            receiver.close.assert_called_once();sender.close.assert_called_once()
            acquired=[]
            try:
                for _ in range(2):
                    acquired.append(pac_url_worker._SLOTS.acquire(blocking=False))
                self.assertEqual(acquired,[True,True])
            finally:
                for value in acquired:
                    if value:pac_url_worker._SLOTS.release()
