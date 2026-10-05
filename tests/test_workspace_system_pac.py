"""System PAC grants use strict persisted versions and the original local API."""
import http.client
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from test_system_pac import SCRIPT
from vibe_job_radar import system_pac, pac_native
from vibe_job_radar.loopback_proxy import LocalProxyError
from vibe_job_radar.network_settings import read_settings
from vibe_job_radar.workspace import Workspace, InputError
from vibe_job_radar.workbench import LocalServer


class WorkspaceSystemPacTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup);self.workspace=Workspace(temp.name)
        self.addCleanup(patch.stopall)
        patch.dict(os.environ,{k:v for k,v in os.environ.items() if not k.startswith('VIBE_RADAR_')},clear=True).start()
        patch('urllib.request.getproxies',return_value={}).start()
        patch.object(pac_native,'available',return_value=True).start()
        self.source=system_pac.Source('https://corporate.example/private.pac?secret=PRIVATE-FIXTURE')
        self.config=patch.object(system_pac,'current_source',return_value=self.source).start()
        self.fetch=patch.object(system_pac,'fetch',return_value=SCRIPT).start()
        self.evaluate=patch.object(pac_native,'evaluate',return_value='DIRECT').start()

    def save(self, **changes):
        return self.workspace.network_system_pac_preferences(dict(config_id=self.source.config_id,
            revision=read_settings(self.workspace)['revision'],consent=True,**changes))

    def auto(self):
        return self.workspace.network_proxy_preferences(dict(mode='auto',endpoint='',consent=False,
            revision=read_settings(self.workspace)['revision']))

    def test_default_and_save_are_offline_without_source_url_or_script_persistence(self):
        self.assertEqual(self.workspace.network_state()['proxy_mode'],'auto')
        state=self.save();loaded=Workspace(self.workspace.root).network_state()
        self.assertEqual(loaded['schema_version'],4);self.assertEqual(loaded['pac_id'],state['pac_id'])
        self.assertNotIn('pac_sha256',loaded)
        self.assertFalse((self.workspace.root/'network-pac').exists())
        saved=(self.workspace.root/'network-preferences.json').read_text(encoding='utf-8')
        for secret in ('PRIVATE-FIXTURE','private.pac','FindProxyForURL','corporate.example'):
            self.assertNotIn(secret,saved)
        self.fetch.assert_not_called();self.evaluate.assert_not_called()
        self.assertEqual(Workspace(self.workspace.root/'other').network_state()['proxy_mode'],'auto')

    def test_consent_schema_revision_source_and_environment_conflicts(self):
        data=dict(config_id=self.source.config_id,revision=0,consent=True)
        for change in ({'consent':False},{'consent':1},{'revision':True},{'revision':1},{'config_id':'f'*64},
                       {'url':self.source.url},{'script':SCRIPT},{'config_id':True}):
            with self.subTest(change=change),self.assertRaises(InputError):
                self.workspace.network_system_pac_preferences({**data,**change})
        with patch.dict(os.environ,{'VIBE_RADAR_PROXY_PASSWORD':'PRIVATE-FIXTURE'}):
            with self.assertRaises(InputError) as exc:self.save()
            self.assertNotIn('PRIVATE-FIXTURE',str(exc.exception))
        self.save()
        with self.assertRaises(InputError):self.workspace.network_system_pac_preferences(data)
        self.fetch.assert_not_called()

    def test_address_change_invalidates_old_session_and_needs_new_consent(self):
        self.save();old=self.workspace.network_policy();old.for_host('example.com')
        self.config.return_value=system_pac.Source('https://corporate.example/new.pac')
        self.assertEqual(self.workspace.network_state()['policy']['code'],'system_pac_changed')
        with self.assertRaisesRegex(LocalProxyError,'pac_revoked'):old.for_host('example.com')
        with self.assertRaises(InputError):self.save()
        self.fetch.assert_called_once()
        self.auto();self.assertEqual(self.workspace.network_state()['proxy_mode'],'auto')

    def test_dns_change_preserves_grant_and_reconfirm_revokes_old_sessions(self):
        first=self.save();old=self.workspace.network_policy()
        self.workspace.network_preferences(dict(mode='fake_ip_doh',revision=1,consent=True))
        self.assertEqual(read_settings(self.workspace)['pac_id'],first['pac_id']);old.ensure_active()
        second=self.save();self.assertNotEqual(first['pac_id'],second['pac_id'])
        with self.assertRaisesRegex(LocalProxyError,'pac_revoked'):old.ensure_active()
        current=self.workspace.network_policy();self.auto()
        self.assertEqual(read_settings(self.workspace)['schema_version'],2)
        self.assertTrue(self.workspace.network_policy().encrypted_dns)
        with self.assertRaisesRegex(LocalProxyError,'pac_revoked'):current.ensure_active()

    def test_manual_and_system_mode_switch_remove_only_incompatible_fields(self):
        self.workspace.network_pac_preferences(dict(name='fixed.pac',script=SCRIPT,revision=0,consent=True))
        copy=next((self.workspace.root/'network-pac').glob('*.js'));before=copy.read_bytes()
        self.save();self.assertNotIn('pac_name',read_settings(self.workspace))
        self.workspace.network_pac_preferences(dict(name='fixed.pac',script=SCRIPT,revision=2,consent=True))
        self.assertNotIn('system_pac_config_id',read_settings(self.workspace))
        self.assertEqual(read_settings(self.workspace)['schema_version'],3);self.assertEqual(copy.read_bytes(),before)

    def test_corrupt_future_or_cross_version_grants_fail_closed(self):
        self.save();original=read_settings(self.workspace);path=self.workspace.root/'network-preferences.json'
        for change in ({'schema_version':3},{'schema_version':2},{'schema_version':5},{'proxy_mode':'pac'},
                       {'system_pac_config_id':'bad'},{'pac_id':False},{'proxy_consent_version':''},
                       {'pac_name':'anything'},{'proxy_endpoint':'http://127.0.0.1:80'}):
            path.write_text(json.dumps({**original,**change}),encoding='utf-8')
            with self.subTest(change=change),self.assertRaises(InputError):read_settings(self.workspace)

    def test_unavailable_system_preserves_offline_rollback(self):
        self.save()
        with patch.object(system_pac,'current_source',side_effect=LocalProxyError('system_pac_unavailable')):
            self.assertEqual(self.workspace.network_state()['policy']['code'],'system_pac_unavailable')
            with self.assertRaises(InputError):self.save()
            self.auto()
        self.fetch.assert_not_called()

    def test_failed_setting_write_leaves_old_choice(self):
        before=self.auto()
        with patch('vibe_job_radar.network_settings.atomic_json',side_effect=OSError('fixture')):
            with self.assertRaises(OSError):self.save()
        self.assertEqual(read_settings(self.workspace)['revision'],before['revision'])
        self.assertEqual(read_settings(self.workspace)['proxy_mode'],'auto');self.fetch.assert_not_called()

    def test_fixed_check_downloads_once_no_caller_target_and_api_requires_token(self):
        server=LocalServer(self.workspace,public_client=None)
        thread=threading.Thread(target=server.serve_forever,kwargs={'poll_interval':.01},daemon=True);thread.start()
        def post(path,data,auth=True):
            conn=http.client.HTTPConnection(server.authority,timeout=10)
            try:
                headers={'Content-Type':'application/json','Origin':server.origin}
                if auth:headers['X-Radar-Token']=server.token
                conn.request('POST',path,json.dumps(data),headers);response=conn.getresponse()
                return response.status,json.loads(response.read())
            finally:conn.close()
        try:
            data=dict(config_id=self.source.config_id,revision=0,consent=True)
            self.assertEqual(post('/api/network/system-pac',data,False)[0],403)
            self.assertEqual(post('/api/network/system-pac',data)[0],200);self.fetch.assert_not_called()
            self.assertEqual(post('/api/network/pac/check',dict(revision=1,url='https://evil.example/'))[0],400)
            status,value=post('/api/network/pac/check',dict(revision=1))
            self.assertEqual(status,200);self.assertTrue(value['passed']);self.assertFalse(value['target_requested'])
            self.fetch.assert_called_once();self.assertEqual(self.evaluate.call_args.args[1],'https://pac-check.invalid/')
        finally:server.shutdown();server.server_close();thread.join(5)


if __name__=='__main__':unittest.main()
