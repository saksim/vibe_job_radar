"""Real local proxy/TLS authentication with artificial credentials and targets."""
import base64
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
import http.client
import io
import json
import os
from pathlib import Path
import pickle
import socket
import subprocess
import sys
import tempfile
import threading
import traceback
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

import test_loopback_proxy as http_fixture
import test_loopback_socks as socks_fixture
from vibe_job_radar.proxy_credentials import ProxyCredentials, USERNAME_ENV, PASSWORD_ENV
from vibe_job_radar.loopback_proxy import LocalProxyError, LoopbackProxy
from vibe_job_radar.loopback_socks import LoopbackSocks5
from vibe_job_radar.network import FetchError, SafeHTTP
from vibe_job_radar.network_environment import inspect_environment
from vibe_job_radar.network_policy import NetworkPolicy
from vibe_job_radar.guided.native_tunnel import NativeTunnel
from vibe_job_radar.guided.rate import RateLedger, Limits
from vibe_job_radar.guided.transport import PinnedTransport
from vibe_job_radar.guided.contracts import CrawlError
from vibe_job_radar.public_tasks import PublicTasks
from vibe_job_radar.workspace import Workspace
from vibe_job_radar.guided.saved_session import SavedSession
from test_native_acquisition import adapter as native_adapter

USER='ARTIFICIAL-PROXY-USER'
PASSWORD='ARTIFICIAL-PROXY-SECRET:ONLY'
AUTH={USERNAME_ENV:USER,PASSWORD_ENV:PASSWORD,'VIBE_RADAR_HTTP_PROXY':'','VIBE_RADAR_SOCKS_PROXY':''}
BASIC='Basic '+base64.b64encode((USER+':'+PASSWORD).encode('ascii')).decode('ascii')
HOST=http_fixture.HOST


def through_native_guard(fixture, *, expected=200):
    """Real native CONNECT guard and upstream proxy; client validates final TLS."""
    guard=NativeTunnel((HOST,),NetworkPolicy.capture(),threading.Event(),timeout=3)
    try:
        with fixture.real_dial(guard.server.server_address,3) as raw:
            raw.sendall((f'CONNECT {HOST}:443 HTTP/1.1\r\nHost: {HOST}:443\r\n'
                        'Proxy-Authorization: '+guard._authorization+'\r\n\r\n').encode('ascii'))
            header=b''
            while not header.endswith(b'\r\n\r\n'):
                data=raw.recv(1)
                if not data:break
                header+=data
            fixture.assertIn(str(expected).encode('ascii'),header.split(b'\r\n')[0])
            if expected!=200:
                return guard.last_error
            context=getattr(fixture,'client',None) or fixture.trust
            with context.wrap_socket(raw,server_hostname=HOST) as tls:
                tls.sendall(f'GET /native HTTP/1.1\r\nHost: {HOST}\r\nConnection: close\r\n\r\n'.encode('ascii'))
                response=http.client.HTTPResponse(tls);response.begin()
                fixture.assertEqual(response.status,200)
                fixture.assertTrue(json.loads(response.read())['ok'])
            return guard._authorization
    finally:
        guard.close()


class CredentialContractTests(unittest.TestCase):
    def setUp(self):
        values=dict(os.environ)
        for key in AUTH:values.pop(key,None)
        env=patch.dict(os.environ,values,clear=True);env.start();self.addCleanup(env.stop)

    def test_fixed_repr_nonserializable_secrets_and_redacted_diagnostics(self):
        with patch.dict(os.environ,{**AUTH,'VIBE_RADAR_HTTP_PROXY':'http://127.0.0.1:1234'}):
            policy=NetworkPolicy.capture()
            report=inspect_environment(discover=lambda:{})
        text=repr(policy)+repr(policy.proxy)+repr(policy.proxy.credentials)+json.dumps(report)
        for secret in (USER,PASSWORD,BASIC):self.assertNotIn(secret,text)
        self.assertTrue(report['selected_policy']['proxy_authentication_configured'])
        self.assertEqual(report['selected_policy']['proxy_credentials_scope'],'explicit_application_loopback_only')
        with self.assertRaises(TypeError):pickle.dumps(policy.proxy.credentials)
        with self.assertRaises(TypeError):json.dumps(policy.proxy.credentials)
        with self.assertRaises(AttributeError):policy.proxy.credentials._password=b'changed'

    def test_credentials_require_explicit_endpoint_not_automatic_or_no_proxy(self):
        with patch.dict(os.environ,{USERNAME_ENV:USER,PASSWORD_ENV:PASSWORD}):
            discover=MagicMock(return_value={'https':'http://127.0.0.1:4321','no':'*'})
            policy=NetworkPolicy.capture(discover=discover)
        discover.assert_not_called()
        self.assertEqual(policy.error,'local_proxy_credentials_require_explicit')
        with self.assertRaises(LocalProxyError):policy.for_host(HOST)

    def test_incomplete_or_invalid_secrets_stop_before_dns_for_all_clients(self):
        for credentials in ({USERNAME_ENV:USER},{PASSWORD_ENV:PASSWORD},
                            {USERNAME_ENV:'',PASSWORD_ENV:''}):
            with self.subTest(fields=tuple(credentials)),patch.dict(os.environ,
                    {'VIBE_RADAR_HTTP_PROXY':'http://127.0.0.1:1234',**credentials}):
                policy=NetworkPolicy.capture()
                self.assertEqual(policy.error,'local_proxy_credentials_invalid')
                with patch('socket.getaddrinfo') as dns,patch('socket.create_connection') as dial:
                    with self.assertRaises(FetchError):
                        SafeHTTP({HOST},interval=0,network_policy=policy).request('https://'+HOST+'/jobs')
                    with tempfile.TemporaryDirectory() as tmp:
                        wire=PinnedTransport(SimpleNamespace(key='fixture',domains=(HOST,),resource_domains=()),
                            RateLedger(Path(tmp)/'rates.sqlite'),threading.Event())
                        wire.bind_policy(policy)
                        with self.assertRaises(CrawlError):wire.fetch('https://'+HOST+'/jobs')
                    guard=NativeTunnel((HOST,),policy,threading.Event())
                    try:
                        with self.assertRaises(FetchError):guard._open(HOST)
                    finally:guard.close()
                dns.assert_not_called();dial.assert_not_called()

    def test_bounded_ascii_contract_and_no_header_injection(self):
        for user,password in ((None,PASSWORD),(USER,None),(USER,''),('',PASSWORD),
                              ('u:wrong',PASSWORD),(USER,'x\r\nHeader: evil'),
                              (USER,'\x00'),('u'*256,PASSWORD),(USER,'p'*256),('中文',PASSWORD)):
            with self.subTest(length=len(str(user))):
                with self.assertRaises(ValueError) as error:ProxyCredentials(user,password)
                self.assertNotIn(PASSWORD,str(error.exception))
        self.assertTrue(ProxyCredentials('u'*255,'p'*255).socks_frame())

    def test_captured_credentials_do_not_follow_later_environment_changes(self):
        with patch.dict(os.environ,{**AUTH,'VIBE_RADAR_HTTP_PROXY':'http://127.0.0.1:1234'}):
            first=NetworkPolicy.capture();again=NetworkPolicy.capture()
            self.assertEqual(first.fingerprint,again.fingerprint)
            with patch.dict(os.environ,{PASSWORD_ENV:'changed'}):second=NetworkPolicy.capture()
            with patch('vibe_job_radar.proxy_credentials._BINDING_KEY',b'other artificial process'):
                restarted=NetworkPolicy.capture()
        self.assertNotEqual(first.fingerprint,second.fingerprint)
        self.assertNotEqual(first.fingerprint,restarted.fingerprint)
        self.assertEqual(first.proxy.credentials.basic_header(),BASIC)

    def test_private_targets_rejected_before_credentials_are_sent(self):
        for cls in (LoopbackProxy,LoopbackSocks5):
            for target in ('127.0.0.1','10.0.0.1','198.18.0.9',HOST):
                proxy=cls('127.0.0.1',1234,ProxyCredentials(USER,PASSWORD))
                with self.subTest(kind=cls.__name__,target=target),patch('socket.create_connection') as dial:
                    with self.assertRaises(LocalProxyError):proxy.open_tunnel(target,1)
                    dial.assert_not_called()

    def test_concurrent_native_connects_cannot_replay_rejected_credentials(self):
        proxy=LoopbackProxy('127.0.0.1',1234,ProxyCredentials(USER,PASSWORD))
        policy=NetworkPolicy('explicit_application',proxy)
        for code in ('local_proxy_auth_failed','local_socks_truncated_reply','local_socks_protocol_error'):
            guard=NativeTunnel((HOST,),policy,threading.Event(),timeout=2)
            try:
                addresses=[(socket.AF_INET,socket.SOCK_STREAM,6,'',(http_fixture.IP4,443))]
                with self.subTest(code=code),patch('socket.getaddrinfo',return_value=addresses), \
                        patch.object(LoopbackProxy,'open_tunnel',side_effect=LocalProxyError(code)) as upstream:
                    with ThreadPoolExecutor(max_workers=4) as pool:
                        attempts=[pool.submit(guard._open,HOST) for _ in range(4)]
                        for result in attempts:
                            with self.assertRaises(FetchError) as error:result.result()
                            self.assertEqual(error.exception.code,code)
                    upstream.assert_called_once()
            finally:guard.close()

    def test_public_task_keeps_actionable_auth_reason_without_secret_text(self):
        for code in ('local_proxy_auth_failed','local_proxy_credentials_invalid','local_proxy_credentials_require_explicit'):
            with self.subTest(code=code),tempfile.TemporaryDirectory() as tmp:
                example=SimpleNamespace(run=MagicMock(side_effect=FetchError(code,PASSWORD)))
                tasks=PublicTasks(Workspace(tmp),example_factory=lambda *args,**kwargs:example)
                try:
                    tasks.start({'consent':True});tasks._thread.join(3)
                    self.assertFalse(tasks._thread.is_alive())
                    task=tasks.state()['task']
                    self.assertEqual(task['code'],code)
                    self.assertIn('代理',task['message'])
                    self.assertNotIn(PASSWORD,tasks.path.read_text(encoding='utf-8'))
                finally:tasks.close()

    def test_authenticated_cookie_snapshot_is_not_silently_reused_by_another_process(self):
        with tempfile.TemporaryDirectory() as tmp,patch.dict(os.environ,
                {**AUTH,'VIBE_RADAR_HTTP_PROXY':'http://127.0.0.1:1234'}):
            lease=SavedSession(tmp,native_adapter(),backend='bridge',browser='bundled',network=NetworkPolicy.capture().fingerprint)
            try:
                lease.save([{'name':'artificial','value':'ARTIFICIAL COOKIE ONLY','domain':native_adapter().domains[0],
                    'path':'/','expires':-1,'httpOnly':True,'secure':True,'sameSite':'Lax'}])
                envelope=lease.path.read_text(encoding='utf-8')
            finally:lease.close()
            for secret in (USER,PASSWORD,BASIC):self.assertNotIn(secret,envelope)
            program='''import sys
from test_native_acquisition import adapter
from vibe_job_radar.guided.saved_session import SavedSession
from vibe_job_radar.guided.contracts import CrawlError
from vibe_job_radar.network_policy import NetworkPolicy
lease=SavedSession(sys.argv[1],adapter(),backend='bridge',browser='bundled',network=NetworkPolicy.capture().fingerprint)
try:
    try: lease.restore()
    except CrawlError as error: print(error.code)
    else: raise AssertionError('unexpected cross-process authentication binding')
finally: lease.close()
'''
            environment=dict(os.environ)
            environment['PYTHONPATH']=os.pathsep.join([str(Path(__file__).resolve().parents[1]/'src'),str(Path(__file__).resolve().parent)])
            result=subprocess.run([sys.executable,'-c',program,tmp],env=environment,capture_output=True,text=True,
                                  encoding='utf-8',timeout=15,check=True)
            self.assertEqual(result.stdout.strip(),'saved_session_incompatible')

    def test_cancelled_native_handshake_wait_sends_no_credentials(self):
        proxy=LoopbackProxy('127.0.0.1',1234,ProxyCredentials(USER,PASSWORD))
        cancelled=threading.Event()
        guard=NativeTunnel((HOST,),NetworkPolicy('explicit_application',proxy),cancelled,timeout=2)
        try:
            guard._proxy_auth_lock.acquire()
            with patch.object(LoopbackProxy,'open_tunnel') as upstream,ThreadPoolExecutor(max_workers=1) as pool:
                pending=pool.submit(guard._open_proxy,proxy,http_fixture.IP4,2)
                cancelled.set();guard._proxy_auth_lock.release()
                with self.assertRaises(FetchError) as error:pending.result()
                self.assertEqual(error.exception.code,'paused');upstream.assert_not_called()
        finally:guard.close()


class HTTPProxyAuthenticationTests(unittest.TestCase):
    perform=http_fixture.RealProxyTests.perform
    tearDown=http_fixture.RealProxyTests.tearDown

    def setUp(self):
        http_fixture.RealProxyTests.setUp(self)
        env=patch.dict(os.environ,AUTH);env.start();self.addCleanup(env.stop)
        self.expected_proxy_auth=BASIC

    def test_real_authenticated_connect_tls_and_origin_header_isolation(self):
        value=self.perform(lambda:SafeHTTP({HOST},interval=0).json('https://'+HOST+'/jobs'))
        self.assertTrue(value['ok']);self.assertEqual(len(self.connects),1)
        self.assertEqual(self.connects[0][1]['proxy-authorization'],BASIC)
        self.assertNotIn('proxy-authorization',{k.lower() for k in self.requests[0]['headers']})
        self.assertEqual(self.sni,[HOST]);self.assertEqual(self.dials,[self.proxy.server_address])

    def test_bridge_posts_once_and_never_forwards_proxy_credentials(self):
        ledger=RateLedger(Path(self.tmp.name)/'rates.sqlite',Limits(request_interval=0))
        wire=PinnedTransport(SimpleNamespace(key='fixture',domains=(HOST,),resource_domains=()),ledger,threading.Event())
        result=self.perform(lambda:wire.fetch('https://'+HOST+'/job',method='POST',body=b'artificial once'))
        self.assertEqual(result.status,200);self.assertEqual(len(self.requests),1)
        self.assertEqual(self.requests[0]['body'],b'artificial once')
        self.assertNotIn('Proxy-Authorization',self.requests[0]['headers'])
        self.assertEqual(ledger.summary('fixture')['request']['day'],1)

    def test_rejected_authentication_is_once_and_never_direct(self):
        self.expected_proxy_auth='Basic rejected'
        with self.assertRaises(FetchError) as error:
            self.perform(lambda:SafeHTTP({HOST},interval=0).request('https://'+HOST+'/jobs'))
        self.assertEqual(error.exception.code,'local_proxy_auth_failed')
        self.assertEqual(len(self.connects),1);self.assertEqual(self.requests,[])
        self.assertEqual(self.dials,[self.proxy.server_address])

    def test_reflected_proxy_error_and_global_debug_do_not_print_credentials(self):
        for status in (407,500):
            self.proxy_status=status;self.proxy_reason=BASIC
            output=io.StringIO()
            with patch.object(http.client.HTTPConnection,'debuglevel',1),redirect_stdout(output):
                try:self.perform(lambda:SafeHTTP({HOST},interval=0).request('https://'+HOST+'/jobs'))
                except FetchError as error:trace=''.join(traceback.format_exception(type(error),error,error.__traceback__))
                else:self.fail('proxy refusal was accepted')
            for secret in (USER,PASSWORD,BASIC):self.assertNotIn(secret,output.getvalue()+trace)
        self.assertEqual(len(self.connects),2);self.assertEqual(self.requests,[])

    def test_authentication_does_not_weaken_original_host_tls(self):
        with self.assertRaises(FetchError) as error:
            self.perform(lambda:SafeHTTP({HOST},interval=0).request('https://'+HOST+'/jobs'),trust=False)
        self.assertEqual(error.exception.code,'tls_verification_failed')
        self.assertEqual(len(self.connects),1);self.assertEqual(self.requests,[])

    def test_real_native_guard_has_separate_browser_and_upstream_credentials(self):
        native_auth=self.perform(lambda:through_native_guard(self))
        self.assertNotEqual(native_auth,BASIC)
        self.assertEqual(self.connects[0][1]['proxy-authorization'],BASIC)
        self.assertNotIn('Proxy-Authorization',self.requests[0]['headers'])
        self.assertEqual(self.sni,[HOST])

    def test_native_guard_preserves_auth_failure_without_fallback(self):
        self.expected_proxy_auth='Basic rejected'
        error=self.perform(lambda:through_native_guard(self,expected=502))
        self.assertEqual(error,'local_proxy_auth_failed')
        self.assertEqual(len(self.connects),1);self.assertEqual(self.requests,[])


class SocksProxyAuthenticationTests(unittest.TestCase):
    perform=socks_fixture.RealSocksTests.perform
    tearDown=socks_fixture.RealSocksTests.tearDown

    def setUp(self):
        socks_fixture.RealSocksTests.setUp(self)
        env=patch.dict(os.environ,AUTH);env.start();self.addCleanup(env.stop)
        self.method=2;self.expected_credentials=(USER.encode('ascii'),PASSWORD.encode('ascii'))

    def test_real_fragmented_userpass_numeric_connect_and_tls(self):
        self.fragment=True
        value=self.perform(lambda:SafeHTTP({HOST},interval=0).json('https://'+HOST+'/jobs'))
        self.assertTrue(value['ok']);self.assertEqual(self.greetings,[b'\x05\x01\x02'])
        self.assertEqual(self.authentication,[self.expected_credentials])
        self.assertEqual(self.connects,[(socks_fixture.IP4,443,1)])
        self.assertEqual(self.sni,[HOST]);self.assertNotIn('Proxy-Authorization',self.requests[0][2])

    def test_wrong_password_stops_before_connect_without_auth_retry(self):
        self.expected_credentials=(b'wrong',b'wrong')
        with self.assertRaises(FetchError) as error:
            self.perform(lambda:SafeHTTP({HOST},interval=0).request('https://'+HOST+'/jobs'))
        self.assertEqual(error.exception.code,'local_proxy_auth_failed')
        self.assertEqual(len(self.authentication),1);self.assertEqual(self.connects,[])
        self.assertEqual(self.requests,[]);self.assertEqual(self.dials,[self.proxy.server_address])

    def test_anonymous_downgrade_cannot_receive_credentials_or_connect(self):
        self.method=0
        with self.assertRaises(FetchError) as error:
            self.perform(lambda:SafeHTTP({HOST},interval=0).request('https://'+HOST+'/jobs'))
        self.assertEqual(error.exception.code,'local_proxy_auth_failed')
        self.assertEqual(self.authentication,[]);self.assertEqual(self.connects,[])

    def test_auth_reply_version_is_checked_before_connect(self):
        self.auth_version=5
        with self.assertRaises(FetchError) as error:
            self.perform(lambda:SafeHTTP({HOST},interval=0).request('https://'+HOST+'/jobs'))
        self.assertEqual(error.exception.code,'local_socks_protocol_error')
        self.assertEqual(len(self.authentication),1);self.assertEqual(self.connects,[])

    def test_authentication_does_not_restart_handshake_deadline(self):
        now=[0.0];raw=MagicMock();parts=iter((b'\x05',b'\x02',b'\x01',b'\x00'))
        def receive(n):now[0]+=.4;return next(parts)
        raw.recv.side_effect=receive
        proxy=LoopbackSocks5('127.0.0.1',1234,ProxyCredentials(USER,PASSWORD))
        with patch('socket.create_connection',return_value=raw),patch('time.monotonic',side_effect=lambda:now[0]):
            with self.assertRaises(LocalProxyError) as error:proxy.open_tunnel(socks_fixture.IP4,1)
        self.assertEqual(error.exception.code,'local_socks_timeout')
        self.assertEqual(raw.sendall.call_count,2);raw.close.assert_called_once()

    def test_real_native_guard_keeps_userpass_outside_target_tls(self):
        self.perform(lambda:through_native_guard(self))
        self.assertEqual(self.authentication,[self.expected_credentials])
        self.assertEqual(len(self.connects),1);self.assertEqual(self.sni,[HOST])
        self.assertNotIn('Proxy-Authorization',self.requests[0][2])


if __name__=='__main__':unittest.main()
