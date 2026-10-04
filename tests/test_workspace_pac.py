"""Persisted PAC consent, compatibility, local API and actual proxy/TLS paths."""
import http.client
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

import test_loopback_proxy as http_fixture
import test_loopback_socks as socks_fixture
from test_pac import SCRIPT
from vibe_job_radar import pac_native
from vibe_job_radar.network import SafeHTTP, FetchError
from vibe_job_radar.network_settings import read_settings
from vibe_job_radar.loopback_proxy import LocalProxyError
from vibe_job_radar.workbench import LocalServer
from vibe_job_radar.workspace import Workspace, InputError


class WorkspacePacTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup)
        self.workspace=Workspace(tmp.name)
        env=patch.dict(os.environ,{k:v for k,v in os.environ.items() if not k.startswith('VIBE_RADAR_')},clear=True)
        env.start();self.addCleanup(env.stop)
        available=patch.object(pac_native,'available',return_value=True);available.start();self.addCleanup(available.stop)
        discovery=patch('urllib.request.getproxies',return_value={});discovery.start();self.addCleanup(discovery.stop)

    def save(self,script=SCRIPT,**changes):
        data={'name':'可信.pac','script':script,'revision':read_settings(self.workspace)['revision'],'consent':True}
        return self.workspace.network_pac_preferences({**data,**changes})

    def auto(self):
        return self.workspace.network_proxy_preferences(dict(mode='auto',endpoint='',consent=False,
            revision=read_settings(self.workspace)['revision']))

    def test_save_and_status_never_execute_or_contact_and_are_workspace_local(self):
        with patch.object(pac_native,'evaluate',side_effect=AssertionError('no execution')),patch('socket.getaddrinfo') as dns:
            state=self.save()
            reloaded=Workspace(self.workspace.root).network_state()
            other=Workspace(self.workspace.root/'other').network_state()
        dns.assert_not_called()
        self.assertEqual(state['schema_version'],3);self.assertEqual(reloaded['pac_sha256'],state['pac_sha256'])
        self.assertEqual(other['proxy_mode'],'auto');self.assertEqual(other['revision'],0)
        self.assertFalse(state['network_tested']);self.assertEqual(state['policy']['code'],'pac_not_evaluated')
        self.assertNotIn('FindProxyForURL',json.dumps(state))
        self.assertEqual((self.workspace.root/'network-pac'/(state['pac_sha256']+'.js')).read_text(encoding='utf-8'),SCRIPT)

    def test_explicit_consent_size_filename_revision_and_env_conflict(self):
        for changes in ({'consent':False},{'consent':1},{'name':'../bad.pac'},{'name':'url://bad.pac'},
                        {'name':'bad.txt'},{'script':'x'*65537},{'extra':1},{'revision':True}):
            with self.subTest(changes=changes),self.assertRaises(InputError):self.save(**changes)
        self.assertFalse((self.workspace.root/'network-preferences.json').exists())
        self.save()
        with self.assertRaises(InputError):self.save(revision=0)
        with patch.dict(os.environ,{'VIBE_RADAR_PROXY_PASSWORD':'SYNTHETIC'}):
            with self.assertRaises(InputError) as error:self.save()
            self.assertNotIn('SYNTHETIC',str(error.exception))
            self.assertEqual(self.workspace.network_policy().error,'workspace_proxy_environment_conflict')
            self.auto()

    def test_dns_save_preserves_grant_but_new_import_and_rollback_revoke(self):
        state=self.save();old=self.workspace.network_policy()
        with patch.object(pac_native,'evaluate',return_value='DIRECT'):
            self.assertIsNone(old.for_host('example.com'))
            self.workspace.network_preferences({'mode':'fake_ip_doh','revision':1,'consent':True})
            self.assertEqual(read_settings(self.workspace)['pac_id'],state['pac_id'])
            self.assertIsNone(old.for_host('example.com'))
            new=self.save();self.assertNotEqual(new['pac_id'],state['pac_id'])
            with self.assertRaisesRegex(LocalProxyError,'pac_revoked'):old.for_host('example.com')
            newer=self.workspace.network_policy()
            rollback=self.auto()
            self.assertEqual(rollback['schema_version'],2);self.assertTrue(self.workspace.network_policy().encrypted_dns)
            self.assertFalse(any(key.startswith('pac_') for key in read_settings(self.workspace)))
            with self.assertRaisesRegex(LocalProxyError,'pac_revoked'):newer.for_host('example.com')
            self.save()
            with self.assertRaisesRegex(LocalProxyError,'pac_revoked'):old.for_host('example.com')

    def test_changed_or_missing_copy_stops_network_but_allows_ui_rollback(self):
        state=self.save();old=self.workspace.network_policy()
        path=self.workspace.root/'network-pac'/(state['pac_sha256']+'.js')
        for content in ('tampered',None):
            if content is None:path.unlink()
            else:path.write_text(content,encoding='utf-8')
            self.assertEqual(self.workspace.network_state()['policy']['code'],'pac_file_invalid')
            with self.assertRaisesRegex(LocalProxyError,'pac_revoked'):old.for_host('example.com')
        self.auto();self.assertEqual(self.workspace.network_state()['proxy_mode'],'auto')

    def test_failed_preference_write_preserves_old_choice_and_unused_copy_is_not_active(self):
        before=self.auto()
        with patch('vibe_job_radar.network_settings.atomic_json',side_effect=OSError('fixture')):
            with self.assertRaises(OSError):self.save()
        self.assertEqual(read_settings(self.workspace)['revision'],before['revision'])
        self.assertEqual(self.workspace.network_state()['proxy_mode'],'auto')
        self.assertEqual(len(list((self.workspace.root/'network-pac').glob('*.js'))),1)

    def test_schema_and_consent_bindings_are_strict(self):
        self.save();path=self.workspace.root/'network-preferences.json';original=read_settings(self.workspace)
        for change in ({'schema_version':2},{'proxy_mode':'auto'},{'proxy_consent_version':''},{'pac_sha256':'../x'},
                       {'pac_id':True},{'pac_name':'secret\x00.pac'},{'proxy_endpoint':'http://127.0.0.1:1'}):
            path.write_text(json.dumps({**original,**change}),encoding='utf-8')
            with self.subTest(change=change),self.assertRaises(InputError):read_settings(self.workspace)

    def test_other_os_does_not_import_or_silently_ignore_existing_pac(self):
        self.save()
        with patch.object(pac_native,'available',return_value=False):
            with self.assertRaises(InputError):self.save()
            self.assertEqual(self.workspace.network_policy().error,'pac_unavailable')
            self.auto()

    def test_explicit_check_has_fixed_domain_and_no_target_connection(self):
        self.save()
        with patch.object(pac_native,'evaluate',return_value='SOCKS5 127.0.0.1:1080') as evaluate,patch('socket.create_connection') as dial:
            result=self.workspace.network_pac_check({'revision':1})
        self.assertTrue(result['passed']);self.assertFalse(result['target_requested'])
        self.assertEqual(result['transport'],'loopback_socks5_proxy')
        self.assertEqual(evaluate.call_args.args[1],'https://pac-check.invalid/');dial.assert_not_called()
        for value in ({'revision':0},{'revision':True},{'revision':1,'url':'https://other.example/'}):
            with self.assertRaises(InputError):self.workspace.network_pac_check(value)
        with patch.object(pac_native,'evaluate',side_effect=LocalProxyError('pac_timeout')):
            self.assertEqual(self.workspace.network_pac_check({'revision':1})['code'],'pac_timeout')

    def test_local_api_requires_token_origin_and_only_explicit_check_executes(self):
        server=LocalServer(self.workspace,public_client=None)
        thread=threading.Thread(target=server.serve_forever,kwargs={'poll_interval':.01},daemon=True);thread.start()
        def post(path,data,token=True):
            conn=http.client.HTTPConnection(server.authority,timeout=10)
            try:
                headers={'Content-Type':'application/json','Origin':server.origin}
                if token:headers['X-Radar-Token']=server.token
                conn.request('POST',path,json.dumps(data),headers);response=conn.getresponse()
                return response.status,json.loads(response.read())
            finally:conn.close()
        try:
            with patch.object(pac_native,'evaluate',return_value='DIRECT') as evaluate:
                data=dict(name='local.pac',script=SCRIPT,revision=0,consent=True)
                self.assertEqual(post('/api/network/pac',data,False)[0],403)
                self.assertEqual(post('/api/network/pac',data)[0],200);evaluate.assert_not_called()
                self.assertEqual(post('/api/network/pac/check',{'revision':1},False)[0],403);evaluate.assert_not_called()
                status,value=post('/api/network/pac/check',{'revision':1})
                self.assertEqual(status,200);self.assertTrue(value['passed']);evaluate.assert_called_once()
        finally:server.shutdown();server.server_close();thread.join(5)

    def test_saved_pac_selects_real_http_and_socks_with_original_tls(self):
        for keyword,cls in (('PROXY',http_fixture.RealProxyTests),('SOCKS5',socks_fixture.RealSocksTests)):
            fixture=cls();fixture.setUp()
            try:
                raw=keyword+' '+fixture.setting.split('://',1)[1]+'; DIRECT'
                self.save('function FindProxyForURL(url,host){return '+json.dumps(raw)+';}')
                def request():
                    with patch.dict(os.environ):
                        for name in list(os.environ):
                            if name.startswith('VIBE_RADAR_'):os.environ.pop(name)
                        return SafeHTTP({http_fixture.HOST},interval=0,
                            network_policy=self.workspace.network_policy()).json('https://'+http_fixture.HOST+'/jobs?secret=not-for-pac')
                # Native evaluator is tested above on Windows; on other OS the
                # same strict parser/routing transport remains testable offline.
                manager=patch.object(pac_native,'evaluate',return_value=raw) if os.name!='nt' else patch.dict(os.environ)
                with manager:self.assertTrue(fixture.perform(request)['ok'])
                self.assertEqual(fixture.dials,[fixture.proxy.server_address]);self.assertEqual(fixture.sni,[http_fixture.HOST])
                self.assertEqual(len(fixture.requests),1)
                if keyword=='PROXY':fixture.proxy_status=503
                else:fixture.reply=5
                with manager if os.name!='nt' else patch.dict(os.environ):
                    with self.assertRaises(FetchError):fixture.perform(request)
                self.assertEqual(len(fixture.requests),1)
                self.assertTrue(all(dial==fixture.proxy.server_address for dial in fixture.dials))
            finally:fixture.tearDown();fixture.doCleanups()


if __name__=='__main__':unittest.main()
