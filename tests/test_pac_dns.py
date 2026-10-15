"""DNS-assisted PAC semantics, explicit scope and destination boundary."""
import json
import multiprocessing
import os
import socket
import tempfile
import unittest
from unittest.mock import patch
from vibe_job_radar import pac_url_worker
from vibe_job_radar.pac_dns import DnsHelpers
from vibe_job_radar.pac_url import DNS_CONSENT
from vibe_job_radar.network import FetchError, validate_public_url
from vibe_job_radar.network_settings import read_settings
from vibe_job_radar.loopback_proxy import LocalProxyError
from vibe_job_radar.workspace import Workspace, InputError


def answer(ip):return [(socket.AF_INET,socket.SOCK_STREAM,6,'',(ip,0))]


class PacDnsTests(unittest.TestCase):
    def test_dns_cache_absence_failure_and_budget(self):
        helper = DnsHelpers()
        with patch('socket.getaddrinfo',return_value=answer('10.12.1.9')) as dns:
            self.assertEqual(helper.resolve('Example.COM.'),'10.12.1.9')
            self.assertEqual(helper.resolve('example.com'),'10.12.1.9')
            self.assertEqual(helper.resolve('127.0.0.1'),'127.0.0.1')
            self.assertIsNone(helper.resolve('https://example.com/path?secret=1'))
            self.assertEqual(dns.call_count,1)
            for n in range(15):helper.resolve('host%d.example'%n)
            self.assertIsNone(helper.resolve('over.example'))
            self.assertEqual(dns.call_count,16)
            self.assertEqual(helper.error,'pac_dns_limit')
        for code,fatal in [(socket.EAI_NONAME,None),(socket.EAI_AGAIN,'pac_dns_failed')]:
            helper=DnsHelpers()
            with patch('socket.getaddrinfo',side_effect=socket.gaierror(code,'private text')) as dns:
                self.assertIsNone(helper.resolve('missing.example'))
                self.assertIsNone(helper.resolve('missing.example'))
                self.assertEqual(dns.call_count,1)
                self.assertEqual(helper.error,fatal)

    @unittest.skipUnless(pac_url_worker.available(),'optional QuickJS engine not installed')
    def test_real_engine_ipv4_helpers_and_script_cannot_swallow_budget_failure(self):
        source = """function FindProxyForURL(url,host) {
            if (dnsResolve(host)!=='10.12.1.9' || !isResolvable(host) ||
                !isInNet(host,'10.12.0.0','255.255.0.0') ||
                isInNet(host,'192.168.0.0','255.255.0.0') ||
                isInNet('1.2.3.400','0.0.0.0','0.0.0.0') ||
                myIpAddress()!=='10.12.1.9') throw Error('semantics');
            return 'PROXY 127.0.0.1:8080';
        }"""
        with patch('socket.getaddrinfo',return_value=answer('10.12.1.9')):
            self.assertEqual(pac_url_worker._evaluate(source,'https://example.com/','example.com',True),
                             'PROXY 127.0.0.1:8080')
            source="function FindProxyForURL(){for(var i=0;i<17;i++)dnsResolve('a'+i+'.example');return 'DIRECT';}"
            with self.assertRaisesRegex(LocalProxyError,'pac_dns_limit'):
                pac_url_worker._evaluate(source,'https://example.com/','example.com',True)
        with self.assertRaisesRegex(LocalProxyError,'pac_invalid_script'):
            pac_url_worker._evaluate("function FindProxyForURL(){dnsResolve('localhost');return 'DIRECT';}",
                                     'https://example.com/','example.com')

    @unittest.skipUnless(pac_url_worker.available(),'optional QuickJS engine not installed')
    def test_real_isolated_worker_resolves_localhost_and_cleans_process(self):
        before={p.pid for p in multiprocessing.active_children()}
        source="function FindProxyForURL(){return isInNet('localhost','127.0.0.0','255.0.0.0')?'DIRECT':'INVALID';}"
        self.assertEqual(pac_url_worker.evaluate(source,'https://example.com/','example.com',lambda:True,
                                                dns_helpers=True),'DIRECT')
        self.assertEqual(before,{p.pid for p in multiprocessing.active_children()})

    @unittest.skipUnless(pac_url_worker.available(),'optional QuickJS engine not installed')
    def test_dns_scope_infinite_script_still_has_parent_deadline(self):
        before={p.pid for p in multiprocessing.active_children()}
        with patch.object(pac_url_worker,'DEADLINE',.8):
            with self.assertRaisesRegex(LocalProxyError,'pac_timeout'):
                pac_url_worker.evaluate('function FindProxyForURL(){while(true){}}',
                    'https://example.com/','example.com',lambda:True,dns_helpers=True)
        self.assertEqual(before,{p.pid for p in multiprocessing.active_children()})

    def test_scope_is_offline_separate_revocable_and_old_file_cannot_expand_it(self):
        with tempfile.TemporaryDirectory() as root,patch.dict(os.environ,{},clear=True), \
                patch.object(pac_url_worker,'available',return_value=True):
            workspace=Workspace(root)
            self.addCleanup(workspace.close_network)
            body=dict(name='route.pac',script="function FindProxyForURL(){return 'DIRECT';}",revision=0,consent=True)
            with patch('socket.getaddrinfo') as dns,patch.object(pac_url_worker,'evaluate') as execute:
                workspace.network_url_pac_preferences(body)
                previous=workspace.network_policy()
                value=workspace.network_url_pac_dns_preferences({**body,'revision':1})
                self.assertEqual(value['schema_version'],7)
                self.assertEqual(value['proxy_consent_version'],DNS_CONSENT)
                self.assertTrue(value['policy']['pac_dns_helpers'])
                workspace.network_state()
                dns.assert_not_called();execute.assert_not_called()
            with self.assertRaisesRegex(LocalProxyError,'pac_revoked'):
                previous.for_request('https://example.com/path')
            current=workspace.network_policy()
            with patch.object(pac_url_worker,'evaluate',return_value='DIRECT') as execute:
                current.for_request('https://example.com/path')
                self.assertTrue(execute.call_args.kwargs['dns_helpers'])
            original=read_settings(workspace)
            path=workspace.root/'network-preferences.json'
            path.write_text(json.dumps({**original,'schema_version':6}),encoding='utf8')
            with self.assertRaises(InputError):workspace.network_policy()
            path.write_text(json.dumps(original),encoding='utf8')
            workspace.network_proxy_preferences(dict(mode='auto',endpoint='',revision=2,consent=False))
            with self.assertRaisesRegex(LocalProxyError,'pac_revoked'):
                current.for_request('https://example.com/path')

    def test_dns_route_selection_does_not_allow_private_https_target(self):
        with tempfile.TemporaryDirectory() as root,patch.dict(os.environ,{},clear=True), \
                patch.object(pac_url_worker,'available',return_value=True):
            workspace=Workspace(root)
            self.addCleanup(workspace.close_network)
            workspace.network_url_pac_dns_preferences(dict(name='route.pac',
                script="function FindProxyForURL(){return 'DIRECT';}",revision=0,consent=True))
            with patch.object(pac_url_worker,'evaluate',return_value='DIRECT'), \
                    patch('socket.getaddrinfo',return_value=answer('192.168.1.3')):
                policy=workspace.network_policy().for_request('https://example.com/')
                with self.assertRaisesRegex(FetchError,'non_public_address'):
                    validate_public_url('https://example.com/',{'example.com'},network_policy=policy)
