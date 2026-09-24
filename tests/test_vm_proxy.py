"""Explicit host peers without weakening target validation or ambient discovery."""
import json
import os
import socket
import ssl
import unittest
from dataclasses import replace
from unittest.mock import patch

import test_workspace_proxy as workspace_fixture
import test_loopback_proxy as http_fixture
import test_loopback_socks as socks_fixture
from vibe_job_radar.loopback_proxy import LoopbackProxy, LocalProxyError
from vibe_job_radar.loopback_socks import LoopbackSocks5
from vibe_job_radar.network import FetchError, SafeHTTP
from vibe_job_radar.network_policy import NetworkPolicy
from vibe_job_radar.network_settings import read_settings, PROXY_CONSENT, VM_CONSENT
from vibe_job_radar.vm_proxy import VmHTTPProxy, VmSocks5Proxy
from vibe_job_radar.workspace import Workspace, InputError


class VmProxyTests(unittest.TestCase):
    setUp = workspace_fixture.WorkspaceProxyTests.setUp
    save = workspace_fixture.WorkspaceProxyTests.save

    def test_explicit_rfc1918_modes_save_reload_offline_and_separate_consent(self):
        for mode,cls in (('vm_http',VmHTTPProxy),('vm_socks5',VmSocks5Proxy)):
            for host in ('10.0.2.2','172.16.0.1','172.31.255.254','192.168.56.1'):
                with self.subTest(mode=mode,host=host),patch('socket.getaddrinfo') as dns,patch('socket.create_connection') as dial:
                    value=self.save(mode,cls.scheme+'://'+host+':18990/')
                    policy=Workspace(self.workspace.root).network_policy()
                    self.assertIs(type(policy.proxy),cls)
                    self.assertEqual(value['proxy_consent_version'],VM_CONSENT)
                    self.assertEqual(policy.describe()['transport'],'vm_host_'+cls.scheme+'_proxy')
                    self.assertFalse(policy.describe()['network_tested'])
                    dns.assert_not_called();dial.assert_not_called()

    def test_invalid_or_ambiguous_host_addresses_never_written_or_resolved(self):
        hosts=('127.0.0.1','localhost','host.docker.internal','8.8.8.8','169.254.1.1','100.64.1.1',
               '198.18.1.1','0.0.0.0','172.15.255.255','172.32.0.1','192.169.0.1','[fd00::1]',
               '[::ffff:192.168.1.1]','10.01.2.2','167772674','10.0.2.2%25eth0')
        for mode,scheme in (('vm_http','http'),('vm_socks5','socks5')):
            for host in hosts:
                with self.subTest(mode=mode,host=host),patch('socket.getaddrinfo') as dns,patch('socket.create_connection') as dial:
                    with self.assertRaises(InputError):self.save(mode,scheme+'://'+host+':18990')
                    dns.assert_not_called();dial.assert_not_called()
        self.assertFalse((self.workspace.root/'network-preferences.json').exists())

    def test_credentials_paths_pac_and_remote_dns_cannot_be_smuggled(self):
        for cls in (VmHTTPProxy,VmSocks5Proxy):
            for raw in ('http://user:SYNTHETIC@10.0.2.2:1','http://10.0.2.2:1/path',
                        'http://10.0.2.2:1?secret=SYNTHETIC','http://10.0.2.2:1#fragment',
                        'socks5h://10.0.2.2:1','https://10.0.2.2:1','http://10.0.2.2:0',
                        'http://10.0.2.2:65536','http://10.0.2.2','http://10.0.2.2:\n1'):
                with self.subTest(cls=cls,raw=raw),self.assertRaises(LocalProxyError) as error:cls.from_url(raw)
                self.assertNotIn('SYNTHETIC',str(error.exception))

    def test_constructors_cannot_attach_credentials_or_bad_ports(self):
        for cls in (VmHTTPProxy,VmSocks5Proxy):
            for port in (True,0,65536,'80'):
                with self.assertRaises(LocalProxyError):cls('10.0.2.2',port)
            proxy=cls('10.0.2.2',18990)
            with self.assertRaises(LocalProxyError):replace(proxy,credentials=object())
            with self.assertRaises(LocalProxyError):proxy.with_environment_credentials()
            with self.assertRaises(LocalProxyError):cls.from_environment()

    def test_old_loopback_and_automatic_parsers_still_reject_private_peer(self):
        for cls,scheme in ((LoopbackProxy,'http'),(LoopbackSocks5,'socks5')):
            endpoint=scheme+'://10.0.2.2:18990'
            with self.assertRaises(LocalProxyError):cls.from_url(endpoint)
            policy=NetworkPolicy.capture(discover=lambda:{'https':endpoint})
            self.assertIsNotNone(policy.error);self.assertIsNone(policy.proxy)
            with self.assertRaises(InputError):self.save(scheme,endpoint)

    def test_private_fake_or_hostname_targets_rejected_before_any_dial(self):
        for cls in (VmHTTPProxy,VmSocks5Proxy):
            for target in ('10.0.2.2','192.168.0.1','198.18.0.1','127.0.0.1','::1',http_fixture.HOST):
                with self.subTest(cls=cls,target=target),patch('socket.create_connection') as dial:
                    with self.assertRaisesRegex(LocalProxyError,'non_public_address'):cls('10.0.2.2',18990).open_tunnel(target,1)
                    dial.assert_not_called()

    def test_consents_are_not_interchangeable_and_snapshot_stays_bound(self):
        self.save('vm_http','http://10.0.2.2:18990');old=self.workspace.network_policy()
        value=read_settings(self.workspace);value['proxy_consent_version']=PROXY_CONSENT
        path=self.workspace.root/'network-preferences.json';path.write_text(json.dumps(value),encoding='utf-8')
        with self.assertRaises(InputError):self.workspace.network_policy()
        value['proxy_consent_version']=VM_CONSENT;path.write_text(json.dumps(value),encoding='utf-8')
        self.save('vm_http','http://192.168.56.1:18990')
        current=self.workspace.network_policy()
        self.assertNotEqual(old.fingerprint,current.fingerprint)
        self.assertEqual(old.proxy.host,'10.0.2.2')
        with self.assertRaises(InputError):self.save('vm_http','http://10.0.2.2:18991',revision=1)
        self.assertEqual(self.workspace.network_policy().proxy.host,'192.168.56.1')

    def test_environment_conflict_never_transfers_local_credentials(self):
        self.save('vm_socks5','socks5://10.0.2.2:18990')
        with patch.dict(os.environ,{'VIBE_RADAR_PROXY_PASSWORD':'SYNTHETIC'}):
            with self.assertRaises(InputError):self.save('vm_http','http://10.0.2.2:18990')
            with patch('socket.getaddrinfo') as dns,patch('socket.create_connection') as dial:
                with self.assertRaisesRegex(FetchError,'workspace_proxy_environment_conflict'):
                    SafeHTTP({http_fixture.HOST},network_policy=self.workspace.network_policy()).request('https://'+http_fixture.HOST+'/')
                dns.assert_not_called();dial.assert_not_called()

    def test_real_protocol_tls_success_refusal_and_untrusted_certificate(self):
        # Local portability test maps ONLY this selected private peer to a
        # loopback fixture. Separate CI network namespaces test actual routing.
        for mode,cls in (('vm_http',http_fixture.RealProxyTests),('vm_socks5',socks_fixture.RealSocksTests)):
            with self.subTest(mode=mode):
                fixture=cls();fixture.setUp();dials=[]
                try:
                    self.save(mode,('http' if mode=='vm_http' else 'socks5')+'://10.0.2.2:18990')
                    def dns(host,*args,**kwargs):
                        if host==http_fixture.HOST:return [(socket.AF_INET,socket.SOCK_STREAM,6,'',(http_fixture.IP4,443))]
                        return fixture.real_gai(host,*args,**kwargs)
                    def dial(endpoint,*args,**kwargs):
                        dials.append(endpoint)
                        self.assertEqual(endpoint,('10.0.2.2',18990))
                        return fixture.real_dial(fixture.proxy.server_address,*args,**kwargs)
                    context=fixture.client if mode=='vm_http' else fixture.trust
                    with patch('socket.getaddrinfo',side_effect=dns),patch('socket.create_connection',side_effect=dial),patch('vibe_job_radar.network.create_client_context',return_value=context):
                        request=lambda:SafeHTTP({http_fixture.HOST},interval=0,network_policy=self.workspace.network_policy()).json('https://'+http_fixture.HOST+'/jobs')
                        self.assertTrue(request()['ok']);self.assertEqual(fixture.sni,[http_fixture.HOST])
                        with patch('vibe_job_radar.network.create_client_context',return_value=ssl.create_default_context()):
                            with self.assertRaisesRegex(FetchError,'tls_verification_failed'):request()
                        fixture.proxy_status=407;fixture.reply=5
                        with self.assertRaises(FetchError) as error:request()
                        self.assertEqual(error.exception.code,'vm_proxy_connection_failed' if mode=='vm_http' else 'local_socks_request_rejected')
                    self.assertEqual(len(dials),3);self.assertEqual(len(fixture.requests),1)
                finally:fixture.tearDown();fixture.doCleanups()

    def test_connection_errors_are_host_specific_and_no_fallback(self):
        for cls in (VmHTTPProxy,VmSocks5Proxy):
            with patch('socket.create_connection',side_effect=ConnectionRefusedError('PRIVATE')) as dial:
                with self.assertRaisesRegex(LocalProxyError,'^vm_proxy_connection_failed$'):cls('10.0.2.2',18990).open_tunnel(http_fixture.IP4,1)
                self.assertEqual(dial.call_count,1)
        with patch('socket.create_connection',side_effect=TimeoutError('PRIVATE')):
            with self.assertRaisesRegex(LocalProxyError,'^vm_proxy_timeout$'):VmSocks5Proxy('10.0.2.2',18990).open_tunnel(http_fixture.IP4,1)
