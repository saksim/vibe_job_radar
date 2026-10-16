"""Meaningful persistence, isolation and failure tests with artificial secrets."""
import base64
import json
import multiprocessing
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch
from vibe_job_radar import system_credentials as store, workspace_proxy_auth as auth
from vibe_job_radar import network_settings, team_client as team
from vibe_job_radar.team_store import TeamError
from vibe_job_radar.workspace import Workspace, InputError
from vibe_job_radar.network import SafeHTTP, FetchError
from vibe_job_radar.proxy_credentials import ProxyCredentials

KEY = b'0123456789abcdef0123456789abcdef'
TOKEN = 'vjr_' + 'A' * 43


def fast_worker(pipe, account, create):
    pipe.send_bytes(b'K' + KEY); pipe.close()


def stalled_worker(pipe, account, create):
    time.sleep(30)


class SystemCredentialsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)/'workspace'
        self.workspace = Workspace(self.root)
        self.addCleanup(self.workspace.close_network)
        clean = {k:v for k,v in os.environ.items() if not k.startswith('VIBE_RADAR_')}
        env = patch.dict(os.environ, clean, clear=True); env.start(); self.addCleanup(env.stop)
        for target, value in ((store,'available'),):
            mocked = patch.object(target,value,return_value=True); mocked.start(); self.addCleanup(mocked.stop)
        key = patch.object(store,'_key',return_value=KEY); self.key = key.start(); self.addCleanup(key.stop)
        kind = patch.object(auth,'storage_kind',return_value=store.SCHEME); kind.start(); self.addCleanup(kind.stop)
        platform = patch.object(team,'_windows_credentials',return_value=False); platform.start(); self.addCleanup(platform.stop)

    def save(self, mode='http_digest_auth'):
        return self.workspace.network_proxy_auth_preferences(dict(mode=mode,
            endpoint=('socks5' if mode=='socks5_auth' else 'http')+'://127.0.0.1:18990',
            username='ARTIFICIAL-USER',password='ARTIFICIAL-PASSWORD',
            revision=network_settings.read_settings(self.workspace)['revision'],consent=True))

    def install(self, *, encrypted=False):
        config = self.root.parent/'config.json'
        config.write_text(json.dumps(dict(schema_version=1,origin='https://team.fixture.test',
            quota_resources={},public_sources=[],use_public_service=False)),encoding='utf-8')
        credential = self.root.parent/'device.json'
        credential.write_text(json.dumps(dict(schema_version=1,device_token=TOKEN)),encoding='utf-8')
        return team.install_connection(self.root,config,credential,encrypted=encrypted)

    def test_authenticated_ciphertext_is_random_and_bound_to_workspace_purpose_origin(self):
        a = store.seal(self.root,'team-device','https://team.fixture.test',TOKEN.encode())
        b = store.seal(self.root,'team-device','https://team.fixture.test',TOKEN.encode())
        self.assertNotEqual(a,b)
        self.assertNotIn(TOKEN.encode(),base64.b64decode(a))
        self.assertEqual(store.restore(self.root,'team-device','https://team.fixture.test',a),TOKEN.encode())
        for root, purpose, context, payload in (
            (self.root.parent/'other','team-device','https://team.fixture.test',a),
            (self.root,'proxy','https://team.fixture.test',a),
            (self.root,'team-device','https://other.fixture.test',a),
            (self.root,'team-device','https://team.fixture.test',base64.b64encode(b'x'+base64.b64decode(a)[1:]).decode())):
            with self.assertRaisesRegex(store.CredentialStoreError,'credential_store_invalid'):
                store.restore(root,purpose,context,payload)
        self.key.assert_called_with(self.root,create=False)

    def test_proxy_all_modes_preserve_dns_reopen_and_clear_without_plaintext(self):
        with patch('vibe_job_radar.ntlm_worker.available',return_value=True):
            for mode in auth.MODES:
                result = self.save(mode)
                self.assertEqual(result['schema_version'],12)
                policy = self.workspace.network_policy()
                self.assertEqual(policy.proxy.credentials,ProxyCredentials('ARTIFICIAL-USER','ARTIFICIAL-PASSWORD'))
                self.assertEqual(policy.describe()['proxy_credentials_scope'],'explicit_workspace_system_keyring_loopback')
                before = network_settings.read_settings(self.workspace)[auth.FIELD]
                self.workspace.network_preferences(dict(mode='public_doh',revision=result['revision'],consent=True))
                self.assertEqual(network_settings.read_settings(self.workspace)[auth.FIELD],before)
                public = json.dumps(self.workspace.network_state())
                raw = (self.root/'network-preferences.json').read_text(encoding='utf-8')
                for secret in ('ARTIFICIAL-USER','ARTIFICIAL-PASSWORD'):
                    self.assertNotIn(secret,raw+public)
                self.assertNotIn('"proxy_auth":',public)
                cleared = self.workspace.network_proxy_auth_clear(dict(revision=network_settings.read_settings(self.workspace)['revision'],consent=True))
                self.assertEqual(cleared['policy']['code'],'workspace_proxy_auth_missing')

    def test_legacy_schema_cannot_borrow_system_consent_or_clear_it_to_anonymous(self):
        self.save(); path=self.root/'network-preferences.json'; original=path.read_bytes(); saved=json.loads(original)
        for change in ({'schema_version':8},{'proxy_consent_version':auth.CONSENT},
                       {auth.FIELD:{'version':1,'protection':'dpapi-current-user','payload':'YQ=='}}):
            path.write_text(json.dumps({**saved,**change}),encoding='utf-8')
            with self.assertRaises(InputError):network_settings.read_settings(self.workspace)
        path.write_bytes(original)
        self.workspace.network_proxy_preferences(dict(mode='auto',endpoint='',revision=1,consent=False))
        self.assertEqual(network_settings.read_settings(self.workspace)['schema_version'],2)
        self.assertNotIn(auth.FIELD,network_settings.read_settings(self.workspace))

    def test_locked_restore_stops_before_dns_or_dial_and_failed_save_keeps_file(self):
        self.save(); path=self.root/'network-preferences.json'; original=path.read_bytes()
        self.key.side_effect=store.CredentialStoreError('credential_store_locked')
        with patch('socket.getaddrinfo') as dns,patch('socket.create_connection') as dial:
            policy=self.workspace.network_policy()
            self.assertEqual(policy.error,'credential_store_locked')
            with self.assertRaisesRegex(FetchError,'credential_store_locked'):
                SafeHTTP({'target.fixture.test'},network_policy=policy).request('https://target.fixture.test/jobs')
            with self.assertRaisesRegex(InputError,'锁定'):self.save()
            dns.assert_not_called();dial.assert_not_called()
        self.assertEqual(path.read_bytes(),original)

    def test_team_legacy_read_explicit_migration_and_running_connection_rejection(self):
        self.assertEqual(self.install()['credential_storage'],'owner-file')
        connection=team.TeamConnection(self.root); self.assertEqual(connection._token(),TOKEN)
        before=(self.root/'.team/credential.json').read_bytes()
        state=team.local_state(self.root)
        result=team.protect_connection(self.root,expected_revision=state['credential_revision'])
        self.assertEqual(result['credential_storage'],store.SCHEME)
        self.assertTrue(result['restart_required'])
        self.assertNotEqual((self.root/'.team/credential.json').read_bytes(),before)
        self.assertEqual(team.TeamConnection(self.root)._token(),TOKEN)
        with self.assertRaisesRegex(TeamError,'team_connection_changed'):connection._token()
        self.assertFalse(team.protect_connection(self.root)['restart_required'])
        self.assertNotIn(TOKEN,json.dumps(team.local_state(self.root)))

    def test_failed_or_stale_migration_keeps_legacy_file_and_does_not_contact_service(self):
        self.install(); path=self.root/'.team/credential.json'; original=path.read_bytes()
        with patch.object(team.TeamConnection,'request',side_effect=AssertionError('no team network')):
            with self.assertRaisesRegex(TeamError,'team_connection_changed'):
                team.protect_connection(self.root,expected_revision='0'*64)
            with patch.object(team,'atomic_json',side_effect=OSError('artificial write failure')):
                with self.assertRaises(OSError):team.protect_connection(self.root)
            self.assertEqual(path.read_bytes(),original)
            self.key.side_effect=store.CredentialStoreError('credential_store_locked')
            with self.assertRaisesRegex(TeamError,'credential_store_locked'):team.protect_connection(self.root)
        self.assertEqual(path.read_bytes(),original)

    def test_explicit_encrypted_install_and_locked_read_have_no_server_fallback(self):
        self.assertEqual(self.install(encrypted=True)['credential_storage'],store.SCHEME)
        path=self.root/'.team/credential.json'
        self.assertNotIn(TOKEN.encode(),path.read_bytes())
        self.assertEqual(team.TeamConnection(self.root)._token(),TOKEN)
        transport=Mock();connection=team.TeamConnection(self.root,transport=transport)
        self.key.side_effect=store.CredentialStoreError('credential_store_missing')
        with self.assertRaisesRegex(TeamError,'credential_store_missing'):connection.request('/v1/team/state')
        transport.json.assert_not_called()

    def test_unavailable_encrypted_install_does_not_activate_partial_connection(self):
        self.key.side_effect=store.CredentialStoreError('credential_store_unavailable')
        with self.assertRaisesRegex(TeamError,'credential_store_unavailable'):self.install(encrypted=True)
        self.assertFalse((self.root/'.team').exists())
        self.assertEqual(list(self.root.glob('.team-install-*')),[])

    def test_migration_ui_requires_exact_consent_and_revision(self):
        self.install(); ui=team.TeamUI(self.workspace)
        for data in ({'consent':False,'revision':'a'*64},{'consent':True},
                     {'consent':True,'revision':'a'*64,'device_token':TOKEN}):
            with self.assertRaises(InputError):ui.protect(data)
        self.assertEqual(ui.protect({'consent':True,'revision':team.local_state(self.root)['credential_revision']})['credential_storage'],store.SCHEME)


class CredentialWorkerTests(unittest.TestCase):
    def test_actual_spawn_private_pipe_and_timeout_cleanup(self):
        before={child.pid for child in multiprocessing.active_children()}
        with patch.object(store,'available',return_value=True),patch.object(store,'_worker',fast_worker):
            self.assertEqual(store._key(Path.cwd(),create=False),KEY)
        with patch.object(store,'available',return_value=True),patch.object(store,'_worker',stalled_worker),patch.object(store,'_TIMEOUT',.2):
            started=time.monotonic()
            with self.assertRaisesRegex(store.CredentialStoreError,'credential_store_timeout'):store._key(Path.cwd(),create=False)
            self.assertLess(time.monotonic()-started,4)
        self.assertEqual({child.pid for child in multiprocessing.active_children()},before)
