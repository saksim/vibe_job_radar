"""Workspace proxy persistence, shared revision and transport selection."""
import http.client
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

import test_collection_network_settings as collection_tests
import test_loopback_proxy as http_fixture
import test_loopback_socks as socks_fixture
from vibe_job_radar.loopback_proxy import LocalProxyError
from vibe_job_radar.loopback_socks import LoopbackSocks5
from vibe_job_radar.network import SafeHTTP, FetchError
from vibe_job_radar.network_policy import use_policy
from vibe_job_radar.network_settings import read_settings
from vibe_job_radar.workbench import LocalServer
from vibe_job_radar.workspace import Workspace, InputError

VARIABLES=('VIBE_RADAR_HTTP_PROXY','VIBE_RADAR_SOCKS_PROXY','VIBE_RADAR_PROXY_USERNAME','VIBE_RADAR_PROXY_PASSWORD')


class WorkspaceProxyTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup)
        self.workspace=Workspace(tmp.name)
        clean={k:v for k,v in os.environ.items() if k not in VARIABLES}
        env=patch.dict(os.environ,clean,clear=True);env.start();self.addCleanup(env.stop)
        discovery=patch('urllib.request.getproxies',return_value={});discovery.start();self.addCleanup(discovery.stop)

    def save(self,mode='http',endpoint='http://127.0.0.1:18990',revision=None,**extra):
        if revision is None:revision=read_settings(self.workspace)['revision']
        return self.workspace.network_proxy_preferences(dict(mode=mode,endpoint=endpoint,
            revision=revision,consent=mode!='auto',**extra))

    def test_save_reload_is_offline_canonical_and_has_no_credentials(self):
        before=dict(os.environ)
        with patch('socket.getaddrinfo',side_effect=AssertionError('no DNS')),patch('socket.create_connection',side_effect=AssertionError('no probe')):
            state=self.save(endpoint='http://localhost:18990/')
            reloaded=Workspace(self.workspace.root).network_policy()
        self.assertEqual(state['schema_version'],2)
        self.assertEqual(state['proxy_endpoint'],'http://127.0.0.1:18990')
        self.assertEqual((reloaded.proxy.host,reloaded.proxy.port),('127.0.0.1',18990))
        self.assertIsNone(reloaded.proxy.credentials)
        self.assertEqual(reloaded.source,'explicit_workspace')
        self.assertFalse(state['network_tested']);self.assertEqual(before,dict(os.environ))

    def test_socks5_ipv6_is_fixed_without_automatic_discovery_or_bypass(self):
        with patch('urllib.request.getproxies',side_effect=AssertionError('no discovery')):
            self.save('socks5','socks5://[::1]:18991')
            policy=self.workspace.network_policy()
        self.assertIsInstance(policy.proxy,LoopbackSocks5)
        self.assertEqual(policy.describe()['transport'],'loopback_socks5_proxy')
        self.assertIs(policy.for_host('example.test'),policy.proxy)

    def test_reject_unreviewed_endpoints_secrets_and_extra_fields_without_writing(self):
        for mode,endpoint in (('http','http://10.0.2.2:7890'),('http','http://example.test:80'),
            ('http','http://user:SYNTHETIC@127.0.0.1:18990'),('socks5','socks5h://127.0.0.1:18990'),
            ('http','http://127.0.0.1:18990/path'),('http','http://127.0.0.1:18990?token=SYNTHETIC'),
            ('http','http://127.0.0.1:0'),('auto','http://127.0.0.1:18990'),('pac','http://127.0.0.1:18990')):
            with self.subTest(mode=mode,endpoint=endpoint),self.assertRaises(InputError) as error:self.save(mode,endpoint)
            self.assertNotIn('SYNTHETIC',str(error.exception))
        with self.assertRaises(InputError):self.save(password='SYNTHETIC')
        self.assertFalse((self.workspace.root/'network-preferences.json').exists())

    def test_consent_is_required_for_fixed_proxy(self):
        with self.assertRaises(InputError):
            self.workspace.network_proxy_preferences({'mode':'http','endpoint':'http://127.0.0.1:18990','revision':0,'consent':False})
        self.assertFalse((self.workspace.root/'network-preferences.json').exists())

    def test_dns_and_proxy_preserve_each_other_with_one_revision(self):
        self.workspace.network_preferences({'mode':'fake_ip_doh','revision':0,'consent':True})
        self.assertEqual(read_settings(self.workspace)['schema_version'],1)
        self.save(revision=1)
        with self.assertRaises(InputError):self.workspace.network_preferences({'mode':'system','revision':1,'consent':False})
        self.workspace.network_preferences({'mode':'system','revision':2,'consent':False})
        state=self.workspace.network_state()
        self.assertEqual(state['revision'],3);self.assertEqual(state['proxy_mode'],'http')
        self.assertEqual(state['schema_version'],2)
        with self.assertRaises(InputError):self.save('auto','',revision=2)
        self.save('auto','',revision=3)
        self.assertEqual(self.workspace.network_state()['proxy_endpoint'],'')
        self.assertIsNone(self.workspace.network_policy().proxy)

    def test_other_workspace_and_existing_snapshot_do_not_inherit_new_route(self):
        self.save();old=self.workspace.network_policy()
        with tempfile.TemporaryDirectory() as tmp,use_policy(old):
            other=Workspace(tmp);self.assertIsNone(other.network_policy().proxy)
            other.network_proxy_preferences({'mode':'socks5','endpoint':'socks5://127.0.0.1:18992','revision':0,'consent':True})
            self.assertNotEqual(other.network_policy().fingerprint,old.fingerprint)
        self.save('socks5','socks5://127.0.0.1:18993')
        self.assertEqual(old.proxy.port,18990)
        self.assertEqual(self.workspace.network_policy().proxy.port,18993)

    def test_existing_explicit_environment_or_any_credential_blocks_fixed_save(self):
        for name in VARIABLES:
            value='http://127.0.0.1:18990' if name==VARIABLES[0] else 'SYNTHETIC'
            with self.subTest(name=name),patch.dict(os.environ,{name:value}),self.assertRaises(InputError) as error:
                self.save()
            self.assertNotIn(value,str(error.exception))
        self.assertFalse((self.workspace.root/'network-preferences.json').exists())

    def test_credentials_added_later_stop_before_target_dns_instead_of_leaking(self):
        self.save()
        with patch.dict(os.environ,{'VIBE_RADAR_PROXY_PASSWORD':'SYNTHETIC'}):
            policy=self.workspace.network_policy()
            self.assertEqual(policy.error,'workspace_proxy_environment_conflict')
            with patch('socket.getaddrinfo') as dns,patch('socket.create_connection') as dial:
                with self.assertRaisesRegex(FetchError,'workspace_proxy_environment_conflict'):
                    SafeHTTP({'example.test'},network_policy=policy).request('https://example.test/')
            dns.assert_not_called();dial.assert_not_called()
            self.save('auto','')  # Explicit rollback remains available despite the environment conflict.
            self.assertEqual(self.workspace.network_state()['proxy_mode'],'auto')

    def test_invalid_saved_proxy_does_not_silently_use_system_route(self):
        self.save();path=self.workspace.root/'network-preferences.json';saved=read_settings(self.workspace)
        for changes in ({'schema_version':3},{'proxy_mode':'unknown'},{'proxy_consent_version':''},
                        {'proxy_endpoint':'http://user:SYNTHETIC@127.0.0.1:1'}, {'proxy_endpoint':'http://localhost:18990'}):
            path.write_text(json.dumps({**saved,**changes}),encoding='utf-8')
            with self.subTest(changes=changes),self.assertRaises(InputError) as error:self.workspace.network_policy()
            self.assertNotIn('SYNTHETIC',str(error.exception))

    def test_state_uses_one_consistent_read_of_settings(self):
        self.save()
        with patch('vibe_job_radar.network_settings.read_settings',wraps=read_settings) as read:
            state=self.workspace.network_state()
        self.assertEqual(read.call_count,1)
        self.assertEqual(state['policy']['policy_id'],self.workspace.network_policy().fingerprint)

    def test_advanced_feed_and_cli_consume_saved_proxy_without_resetting_client(self):
        fixture=collection_tests.CollectionNetworkSettingsTests();fixture.setUp();self.addCleanup(fixture.doCleanups)
        workspace=fixture.workspace
        workspace.network_proxy_preferences({'mode':'http','endpoint':'http://127.0.0.1:18994','revision':0,'consent':True})
        task=fixture.start('feed');seen=[]
        def provider(client,url,**kwargs):
            seen.append(client)
            self.assertEqual(client.network_policy.proxy.port,18994 if len(seen)==1 else 18995)
            return {'jobs':[],'next_cursor':'next' if len(seen)==1 else None}
        with patch.object(SafeHTTP,'json',provider):
            fixture.collector.step({'id':task['id']})
            workspace.network_proxy_preferences({'mode':'socks5','endpoint':'socks5://127.0.0.1:18995','revision':1,'consent':True})
            fixture.collector.step({'id':task['id']})
        self.assertIs(seen[0],seen[1])
        task=fixture.start('feed')
        with patch.object(SafeHTTP,'json',provider),patch('builtins.print'):
            collection_tests.main(['--workspace',str(workspace.root),'--run-id',task['id']])
        self.assertEqual(len(seen),3)

    def test_authenticated_local_http_endpoint_saves_and_rejects_secret_input(self):
        server=LocalServer(self.workspace,public_client=None)
        thread=threading.Thread(target=server.serve_forever,kwargs={'poll_interval':.01},daemon=True);thread.start()
        try:
            def post(endpoint):
                body=json.dumps({'mode':'http','endpoint':endpoint,'revision':0,'consent':True})
                conn=http.client.HTTPConnection(server.authority,timeout=5)
                try:
                    conn.request('POST','/api/network/proxy',body,{'Content-Type':'application/json','Origin':server.origin,'X-Radar-Token':server.token})
                    response=conn.getresponse();return response.status,response.read().decode()
                finally:conn.close()
            status,body=post('http://user:SYNTHETIC@127.0.0.1:1')
            self.assertEqual(status,400);self.assertNotIn('SYNTHETIC',body)
            status,body=post('http://127.0.0.1:18990')
            self.assertEqual(status,200);self.assertEqual(json.loads(body)['proxy_mode'],'http')
        finally:server.shutdown();server.server_close();thread.join(5)

    def test_saved_settings_drive_real_http_and_socks_tunnels_with_verified_tls(self):
        for mode,cls in (('http',http_fixture.RealProxyTests),('socks5',socks_fixture.RealSocksTests)):
            with self.subTest(mode=mode):
                fixture=cls();fixture.setUp()
                try:
                    self.save(mode,fixture.setting)
                    def request():
                        # Fixture supplies only controlled DNS/TCP/TLS. Route
                        # selection itself comes from the saved workspace.
                        with patch.dict(os.environ):
                            for name in VARIABLES:os.environ.pop(name,None)
                            return SafeHTTP({http_fixture.HOST},interval=0,
                                network_policy=Workspace(self.workspace.root).network_policy()).json('https://'+http_fixture.HOST+'/jobs')
                    self.assertTrue(fixture.perform(request)['ok'])
                    self.assertEqual(fixture.dials,[fixture.proxy.server_address])
                    self.assertEqual(fixture.sni,[http_fixture.HOST])
                    self.assertEqual(len(fixture.requests),1)
                finally:fixture.tearDown();fixture.doCleanups()
