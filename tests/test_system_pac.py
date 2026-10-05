"""Configured-source bootstrap, bounded real workers and session immutability."""
import contextlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import socket
import ssl
import threading
import time
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from vibe_job_radar import system_pac as pac, pac_native
from vibe_job_radar.loopback_proxy import LocalProxyError
from vibe_job_radar.network_policy import NetworkPolicy

SCRIPT = 'function FindProxyForURL(url,host){return "DIRECT";}'


class SourceServer:
    def __init__(self):
        self.body = SCRIPT.encode(); self.status = 200; self.headers = []
        self.requests = []; self.delay = False; self.entered = threading.Event(); self.release = threading.Event()
        owner = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_): pass
            def do_GET(self):
                owner.requests.append((self.path, dict(self.headers)))
                owner.entered.set()
                if owner.delay: owner.release.wait(12)
                try:
                    self.send_response(owner.status)
                    headers = owner.headers or [('Content-Length', str(len(owner.body)))]
                    for name, value in headers: self.send_header(name, value)
                    self.end_headers(); self.wfile.write(owner.body)
                except OSError: pass
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval':.01}, daemon=True)
        self.thread.start()
        self.url = 'http://127.0.0.1:%s/private-script.pac?token=SYNTHETIC-SECRET' % self.server.server_port
        self.source = pac.Source(self.url)

    def close(self):
        self.release.set(); self.server.shutdown(); self.server.server_close(); self.thread.join(2)


class SourceTests(unittest.TestCase):
    def setUp(self):
        self.fixture = SourceServer(); self.addCleanup(self.fixture.close)

    def test_url_validation_and_redaction(self):
        source = self.fixture.source
        self.assertNotIn('SYNTHETIC', repr(source)); self.assertNotIn('private-script', source.origin)
        self.assertEqual(len(source.config_id), 64)
        for url in ('file:///a.pac', 'http://u:p@localhost/x', 'http://localhost:0/x', 'https://host:/a',
                    'http://host/a#', 'http://host/\nx', 'http://host/ x', 'http://host\\evil/x',
                    'http://[fe80::1%25eth0]/a', 'http://host:65536/x', '', 'x'*4097):
            with self.subTest(url=url), self.assertRaisesRegex(LocalProxyError, 'system_pac_source_invalid'):
                pac.Source(url)
        with patch.object(pac, '_configured_url', return_value=self.fixture.url):
            view = pac.preview()
        self.assertTrue(view['configured']); self.assertNotIn('SYNTHETIC', json.dumps(view))
        self.assertEqual(self.fixture.requests, [])

    def test_native_configuration_read_is_offline_and_fixed_error_on_other_os(self):
        # Personal configuration is only read; never log its returned URL/origin.
        with patch('socket.getaddrinfo', side_effect=AssertionError('configuration must be offline')):
            view = pac.preview()
        self.assertIn('configured', view)
        if os.name != 'nt': self.assertEqual(view['code'], 'system_pac_unavailable')
        with patch.object(pac, '_configured_url', side_effect=LocalProxyError('system_pac_missing')):
            self.assertEqual(pac.preview()['code'], 'system_pac_missing')

    def test_native_allocated_strings_freed_even_when_api_fails(self):
        import ctypes
        for success in (True,False):
            buffers=[ctypes.create_unicode_buffer(value) for value in (self.fixture.url,'proxy','bypass')]
            pointers=[ctypes.addressof(buffer) for buffer in buffers]
            api=MagicMock();kernel=MagicMock()
            def read(pointer):
                pointer._obj.url,pointer._obj.proxy,pointer._obj.bypass=pointers
                return success
            api.WinHttpGetIEProxyConfigForCurrentUser.side_effect=read
            with patch.object(pac.os,'name','nt'),patch.object(ctypes,'WinDLL',side_effect=[api,kernel],create=True):
                if success:self.assertEqual(pac._configured_url(),self.fixture.url)
                else:
                    with self.assertRaisesRegex(LocalProxyError,'system_pac_unavailable'):pac._configured_url()
            self.assertEqual([call.args[0] for call in kernel.GlobalFree.call_args_list],pointers)

    def test_real_worker_downloads_one_exact_source_without_ambient_auth(self):
        with patch.dict(os.environ, {'HTTP_PROXY':'http://u:SECRET@127.0.0.1:9', 'HTTPS_PROXY':'http://127.0.0.1:9'}):
            self.assertEqual(pac.fetch(self.fixture.source, lambda:True), SCRIPT)
        self.assertEqual(len(self.fixture.requests), 1)
        path, headers = self.fixture.requests[0]
        self.assertEqual(path, '/private-script.pac?token=SYNTHETIC-SECRET')
        self.assertFalse({'authorization','proxy-authorization','cookie'} & {k.lower() for k in headers})
        self.assertEqual(headers['Accept-Encoding'], 'identity')

    def test_rejected_status_never_redirects_or_authenticates(self):
        for code in (301,302,307,401,407,500):
            self.fixture.status = code
            self.fixture.headers = [('Location','http://127.0.0.1:9/forbidden'),('WWW-Authenticate','Negotiate'),('Content-Length','0')]
            with self.subTest(code=code), self.assertRaisesRegex(LocalProxyError, 'system_pac_http_rejected'):
                pac._download(self.fixture.url)
        self.assertEqual(len(self.fixture.requests), 6)

    def test_worker_rejects_oversize_invalid_encoding_and_truncated_body(self):
        for body, headers in ((b'x'*65537, []), (b'\xff', []), (b'abc', [('Content-Length','9')]),
                              (b'abc', [('Content-Length','3'),('Content-Length','3')]),
                              (b'abc', [('Content-Encoding','gzip'),('Content-Length','3')]),
                              (b'abc', [('Transfer-Encoding','chunked'),('Content-Length','3')])):
            self.fixture.body, self.fixture.headers = body, headers
            with self.subTest(body_size=len(body), headers=headers), self.assertRaisesRegex(LocalProxyError, 'system_pac_content_invalid'):
                pac.fetch(self.fixture.source, lambda:True)

    def test_utf8_bom_and_valid_chunked_response(self):
        body = ('\ufeff'+SCRIPT).encode()
        self.fixture.headers = [('Transfer-Encoding','chunked')]
        self.fixture.body = f'{len(body):x}\r\n'.encode()+body+b'\r\n0\r\n\r\n'
        self.assertEqual(pac.fetch(self.fixture.source, lambda:True), SCRIPT)

    def test_source_address_rule_is_separate_from_business_target_rule(self):
        for value in ('127.0.0.1','::1','10.0.0.1','172.16.0.1','192.168.4.1','fd01::1','8.8.8.8'):
            self.assertTrue(pac._source_address(value), value)
        for value in ('0.0.0.0','::','169.254.169.254','fe80::1','224.0.0.1','ff01::1','198.18.0.1','::ffff:127.0.0.1'):
            self.assertFalse(pac._source_address(value), value)
        answers = [(socket.AF_INET,socket.SOCK_STREAM,6,'',('127.0.0.1',80)),
                   (socket.AF_INET,socket.SOCK_STREAM,6,'',('169.254.169.254',80))]
        with patch('socket.getaddrinfo', return_value=answers), patch('socket.socket') as dial:
            with self.assertRaisesRegex(LocalProxyError,'system_pac_address_rejected'): pac._download(self.fixture.url)
        dial.assert_not_called()
        from vibe_job_radar.network import validate_public_url, FetchError
        with self.assertRaises(FetchError): validate_public_url(self.fixture.url, {'127.0.0.1'})

    def test_total_deadline_and_revocation_cleanup_owned_worker(self):
        # Deterministic deadline audit independent of a loaded runner's speed.
        for revoke in (False, True):
            context=MagicMock();receiver=MagicMock();sender=MagicMock();process=context.Process.return_value
            process.pid=42; process.is_alive.return_value=True; context.Pipe.return_value=(receiver,sender)
            clock=MagicMock(); elapsed=[0.]; clock.monotonic.side_effect=lambda:elapsed[0]
            def poll(wait): elapsed[0]+=min(wait,.01); return False
            receiver.poll.side_effect=poll; calls=[0]
            def allowed(): calls[0]+=1; return not revoke or calls[0]<3
            with patch.object(pac,'time',clock), patch.object(pac,'DEADLINE',.04), patch.object(pac.multiprocessing,'get_context',return_value=context):
                with self.assertRaisesRegex(LocalProxyError,'pac_revoked' if revoke else 'system_pac_timeout'):
                    pac.fetch(self.fixture.source, allowed)
            process.terminate.assert_called_once();process.close.assert_called_once();receiver.close.assert_called_once()
            self.assertTrue(pac._SLOTS.acquire(blocking=False));pac._SLOTS.release()

    def test_real_pending_download_is_stopped_on_revocation(self):
        self.fixture.delay=True; stop=threading.Event(); result=[]
        def run():
            try: pac.fetch(self.fixture.source, lambda:not stop.is_set())
            except LocalProxyError as exc: result.append(exc.code)
        thread=threading.Thread(target=run);thread.start()
        try:
            self.assertTrue(self.fixture.entered.wait(6));stop.set();thread.join(3)
            self.assertFalse(thread.is_alive());self.assertEqual(result,['pac_revoked'])
        finally: stop.set();self.fixture.release.set();thread.join(10)

    def test_native_raw_result_survives_download_and_frozen_snapshot(self):
        if os.name != 'nt':
            with self.assertRaisesRegex(LocalProxyError,'pac_unavailable'):
                pac_native.evaluate(SCRIPT,'https://example.com/',lambda:True)
            return
        self.fixture.body=b'function FindProxyForURL(url,host){return "SOCKS5 127.0.0.1:1080; DIRECT";}'
        with patch.object(pac,'current_source',return_value=self.fixture.source):
            snapshot=pac.SystemPacSnapshot(self.fixture.source,lambda:True)
            self.assertEqual(NetworkPolicy.transport_name(snapshot.for_host('example.com')),'loopback_socks5_proxy')
            self.assertEqual(snapshot.for_host('example.com').port,1080)
        self.assertEqual(len(self.fixture.requests),1)

    def test_https_source_uses_original_sni_and_rejects_untrusted_certificate(self):
        from test_loopback_proxy import RealProxyTests, HOST
        fixture=RealProxyTests();fixture.setUp()
        fixture.payload=SCRIPT.encode()
        real_dns=socket.getaddrinfo
        def dns(host, port, **kwargs):
            self.assertEqual(host, HOST)
            return real_dns('127.0.0.1',port,**kwargs)
        url=f'https://{HOST}:{fixture.target.server_port}/artificial.pac'
        try:
            with patch('socket.getaddrinfo',side_effect=dns), patch.object(pac,'create_client_context',return_value=fixture.client):
                self.assertEqual(pac._download(url),SCRIPT)
            self.assertEqual(fixture.sni,[HOST]);self.assertEqual(len(fixture.requests),1)
            with patch('socket.getaddrinfo',side_effect=dns), patch.object(pac,'create_client_context',return_value=ssl.create_default_context()):
                with self.assertRaises(ssl.SSLError):pac._download(url)
            self.assertEqual(len(fixture.requests),1)
        finally:fixture.tearDown();fixture.doCleanups()

    def test_real_timeout_terminates_instead_of_waiting_for_http_headers(self):
        self.fixture.delay=True
        # The owned process includes Python startup, DNS and headers in one bound.
        with patch.object(pac,'DEADLINE',1.5):
            start=time.monotonic()
            with self.assertRaisesRegex(LocalProxyError,'system_pac_timeout'):
                pac.fetch(self.fixture.source,lambda:True)
            self.assertLess(time.monotonic()-start,4)
        self.assertLessEqual(len(self.fixture.requests),1)

    def test_original_http_transport_uses_configured_source_and_full_artificial_body(self):
        from test_loopback_proxy import RealProxyTests, HOST
        from test_loopback_socks import RealSocksTests
        from vibe_job_radar.network import SafeHTTP, FetchError
        from vibe_job_radar.workspace import Workspace
        clean={k:v for k,v in os.environ.items() if not k.startswith('VIBE_RADAR_')}
        for keyword,cls in (('PROXY',RealProxyTests),('SOCKS5',RealSocksTests)):
            fixture=cls();fixture.setUp()
            expected='Independently authored complete job description requiring Cursor architecture and review. '*6
            fixture.payload=json.dumps({'description':expected}).encode()
            raw=keyword+' '+fixture.setting.split('://',1)[1]+'; DIRECT'
            self.fixture.body=('function FindProxyForURL(url,host){return '+json.dumps(raw)+';}').encode()
            try:
                with tempfile.TemporaryDirectory() as tmp,patch.object(pac,'current_source',return_value=self.fixture.source), \
                        patch.object(pac_native,'available',return_value=True),patch.dict(os.environ,clean,clear=True):
                    workspace=Workspace(tmp)
                    workspace.network_system_pac_preferences(dict(config_id=self.fixture.source.config_id,revision=0,consent=True))
                    def request():
                        with patch.dict(os.environ,clean,clear=True):
                            return SafeHTTP({HOST},interval=0,network_policy=workspace.network_policy()).json('https://'+HOST+'/jobs?private=not-for-pac')
                    seam=patch.object(pac_native,'evaluate',return_value=raw) if os.name!='nt' else contextlib.nullcontext()
                    with seam:
                        self.assertEqual(fixture.perform(request)['description'],expected)
                        if keyword=='PROXY':fixture.proxy_status=503
                        else:fixture.reply=5
                        with self.assertRaises(FetchError):fixture.perform(request)
                    self.assertEqual(len(fixture.requests),1)
                    self.assertTrue(all(address==fixture.proxy.server_address for address in fixture.dials))
            finally:fixture.tearDown();fixture.doCleanups()
        self.assertEqual(len(self.fixture.requests),4)


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.source=pac.Source('http://127.0.0.1:9/secret.pac?private=key')
        self.config=patch.object(pac,'current_source',return_value=self.source).start();self.addCleanup(patch.stopall)
        self.fetch=patch.object(pac,'fetch',return_value=SCRIPT).start()
        self.evaluate=patch.object(pac_native,'evaluate',return_value='DIRECT').start()
        self.snapshot=pac.SystemPacSnapshot(self.source,lambda:True)
        self.policy=NetworkPolicy('explicit_workspace',pac=self.snapshot,pac_id='grant')

    def test_description_and_fingerprint_are_offline_stable_and_not_content_hash(self):
        before=self.policy.fingerprint;view=self.policy.describe()
        self.fetch.assert_not_called();self.evaluate.assert_not_called()
        self.assertNotIn('pac_sha256', view);self.assertNotIn('private',json.dumps(view))
        self.assertIsNone(self.policy.for_host('example.com'))
        self.assertEqual(self.policy.fingerprint,before)
        self.assertEqual(self.evaluate.call_args.args[1],'https://example.com/')

    def test_new_session_obtains_update_current_session_keeps_first_download(self):
        self.policy.for_host('one.example');self.fetch.return_value=SCRIPT+'\n// version 2'
        self.policy.for_host('two.example');self.fetch.assert_called_once()
        self.assertEqual(self.evaluate.call_args.args[0],SCRIPT)
        pac.SystemPacSnapshot(self.source,lambda:True).for_host('two.example')
        self.assertEqual(self.fetch.call_count,2);self.assertIn('version 2',self.evaluate.call_args.args[0])

    def test_failed_download_is_not_retried_by_another_asset(self):
        self.fetch.side_effect=LocalProxyError('system_pac_http_rejected')
        for host in ('one.example','two.example','one.example'):
            with self.assertRaisesRegex(LocalProxyError,'system_pac_http_rejected'):self.policy.for_host(host)
        self.fetch.assert_called_once();self.evaluate.assert_not_called()

    def test_bad_route_and_changed_source_stop_before_any_fallback(self):
        self.evaluate.return_value='SOCKS localhost:1080; DIRECT'
        with self.assertRaisesRegex(LocalProxyError,'pac_invalid_result'):self.policy.for_host('one.example')
        self.config.return_value=pac.Source('http://127.0.0.1:9/changed.pac')
        with self.assertRaisesRegex(LocalProxyError,'pac_revoked'):self.policy.for_host('one.example')
        self.fetch.assert_called_once();self.evaluate.assert_called_once()

    def test_cancel_and_close_invalidate_cache_without_download(self):
        event=threading.Event();self.policy.bind_cancellation(event)
        self.policy.for_host('example.com');event.set()
        with self.assertRaisesRegex(LocalProxyError,'pac_revoked'):self.policy.for_host('example.com')
        other=pac.SystemPacSnapshot(self.source,lambda:True);other.close()
        with self.assertRaisesRegex(LocalProxyError,'pac_revoked'):other.for_host('example.com')
        self.fetch.assert_called_once()

    def test_concurrent_hosts_share_one_source_read(self):
        result=[];errors=[]
        def run(host):
            try:result.append(self.policy.for_host(host))
            except Exception as exc:errors.append(type(exc).__name__)
        threads=[threading.Thread(target=run,args=(f'{i}.example',)) for i in range(6)]
        for thread in threads:thread.start()
        for thread in threads:thread.join(3)
        self.assertEqual(errors,[]);self.assertEqual(len(result),6);self.fetch.assert_called_once()

    def test_standalone_bridge_cancellation_during_source_read_prevents_target_dial(self):
        from pathlib import Path
        from types import SimpleNamespace
        from vibe_job_radar.guided.transport import PinnedTransport
        from vibe_job_radar.guided.rate import RateLedger, Limits
        from vibe_job_radar.guided.contracts import CrawlError
        from vibe_job_radar.network_policy import use_policy
        cancelled=threading.Event()
        def download(source, permitted):
            cancelled.set()
            if not permitted(): raise LocalProxyError('pac_revoked')
            return SCRIPT
        self.fetch.side_effect=download
        with tempfile.TemporaryDirectory() as tmp:
            adapter=SimpleNamespace(key='fixture',domains=('example.com',),resource_domains=())
            bridge=PinnedTransport(adapter,RateLedger(Path(tmp)/'rates.sqlite',Limits(request_interval=0)),cancelled)
            with use_policy(self.policy),patch('vibe_job_radar.guided.transport.validate_public_url',return_value=('example.com','8.8.8.8','/')), \
                    patch('socket.create_connection',side_effect=AssertionError('target dial after cancellation')) as dial:
                with self.assertRaisesRegex(CrawlError,'pac_revoked'):bridge.fetch('https://example.com/')
            dial.assert_not_called();self.evaluate.assert_not_called()


if __name__=='__main__':unittest.main()
