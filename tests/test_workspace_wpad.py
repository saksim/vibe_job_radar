"""Two explicit WPAD grants, revision/expiry isolation, snapshots and HTTP API."""
import http.client
import json
import os
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from vibe_job_radar import pac_native, system_pac, wpad, wpad_native
from vibe_job_radar.loopback_proxy import LocalProxyError
from vibe_job_radar.network_settings import read_settings
from vibe_job_radar.workspace import Workspace, InputError
from vibe_job_radar.workbench import LocalServer

SCRIPT = 'function FindProxyForURL(url,host){return "DIRECT";}'


class WorkspaceWpadTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.workspace = Workspace(temporary.name)
        self.addCleanup(self.workspace.close_network)
        self.addCleanup(patch.stopall)
        patch.dict(os.environ, {k:v for k,v in os.environ.items() if not k.startswith('VIBE_RADAR_')}, clear=True).start()
        patch('urllib.request.getproxies', return_value={}).start()
        patch.object(pac_native, 'available', return_value=True).start()
        patch.object(wpad_native, 'available', return_value=True).start()
        self.source = system_pac.Source('https://pac.example/private.pac?token=SYNTHETIC')
        self.discovery = patch.object(wpad_native, 'discover', return_value=self.source).start()
        self.fetch = patch.object(system_pac, 'fetch', return_value=SCRIPT).start()
        self.evaluate = patch.object(pac_native, 'evaluate', return_value='DIRECT').start()

    def revision(self):
        return read_settings(self.workspace)['revision']

    def preview(self):
        self.workspace.network_wpad_discover(dict(revision=self.revision(), consent=True))
        self.workspace.wpad_discovery._thread.join(2)
        self.assertFalse(self.workspace.wpad_discovery._thread.is_alive())
        return self.workspace.network_state()['wpad']

    def save(self):
        candidate = self.preview()['candidate']
        return self.workspace.network_wpad_preferences(dict(revision=self.revision(), candidate=candidate, consent=True))

    def auto(self):
        return self.workspace.network_proxy_preferences(dict(mode='auto', endpoint='', consent=False, revision=self.revision()))

    def test_default_preview_and_confirmation_separate_network_from_execution_and_persistence(self):
        default = self.workspace.network_state()
        self.assertEqual(default['proxy_mode'], 'auto')
        self.discovery.assert_not_called()
        preview = self.preview()
        self.assertEqual(self.discovery.call_count, 1)
        self.assertEqual(preview['origin'], 'https://pac.example')
        self.assertNotIn('SYNTHETIC', json.dumps(preview))
        self.assertFalse((self.workspace.root/'network-preferences.json').exists())
        saved = self.workspace.network_wpad_preferences(dict(revision=0, candidate=preview['candidate'], consent=True))
        self.assertEqual(saved['schema_version'], 5)
        self.assertEqual(saved['policy']['pac_scope'], 'explicit_windows_wpad_confirmed_source')
        self.fetch.assert_not_called(); self.evaluate.assert_not_called()
        text = (self.workspace.root/'network-preferences.json').read_text(encoding='utf-8')
        for secret in ('private.pac', 'SYNTHETIC', 'pac.example', 'FindProxyForURL', preview['candidate']):
            self.assertNotIn(secret, text)
        reopened = Workspace(self.workspace.root)
        self.assertEqual(reopened.network_state()['pac_id'], saved['pac_id'])
        self.assertNotIn('candidate', reopened.network_state()['wpad'])
        self.assertEqual(self.discovery.call_count, 1)

    def test_discovery_and_trust_require_separate_typed_consent_without_caller_url(self):
        for data in ({}, {'revision':0,'consent':False}, {'revision':True,'consent':True},
                     {'revision':0,'consent':1}, {'revision':1,'consent':True},
                     {'revision':0,'consent':True,'url':self.source.url}):
            with self.subTest(data=data), self.assertRaises(InputError): self.workspace.network_wpad_discover(data)
        self.discovery.assert_not_called()
        preview = self.preview()
        data = dict(revision=0, candidate=preview['candidate'], consent=True)
        for change in ({'consent':False}, {'consent':1}, {'revision':True}, {'candidate':'f'*32},
                       {'url':self.source.url}, {'config_id':self.source.config_id}):
            with self.subTest(change=change), self.assertRaises(InputError):
                self.workspace.network_wpad_preferences({**data, **change})
        self.fetch.assert_not_called(); self.evaluate.assert_not_called()

    def test_candidate_expires_after_five_minutes_and_cannot_cross_workspace_or_restart(self):
        elapsed = [0.]
        with patch.object(wpad, 'time', SimpleNamespace(monotonic=lambda:elapsed[0])):
            preview = self.preview()
            data = dict(revision=0, candidate=preview['candidate'], consent=True)
            for workspace in (Workspace(self.workspace.root), Workspace(self.workspace.root/'other')):
                with self.assertRaises(InputError): workspace.network_wpad_preferences(data)
            elapsed[0] = 300
            with self.assertRaises(InputError): self.workspace.network_wpad_preferences(data)
            self.assertEqual(self.workspace.network_state()['wpad']['code'], 'wpad_candidate_expired')
        self.fetch.assert_not_called()

    def test_revision_change_stale_token_and_failed_write_preserve_original_grant(self):
        preview = self.preview()
        self.workspace.network_preferences(dict(mode='fake_ip_doh', revision=0, consent=True))
        with self.assertRaises(InputError):
            self.workspace.network_wpad_preferences(dict(revision=1, candidate=preview['candidate'], consent=True))
        preview = self.preview()
        data = dict(revision=1, candidate=preview['candidate'], consent=True)
        with patch('vibe_job_radar.network_settings.atomic_json', side_effect=OSError('fixture')):
            with self.assertRaises(OSError): self.workspace.network_wpad_preferences(data)
        self.assertEqual(self.revision(), 1)
        self.assertEqual(self.workspace.network_state()['wpad']['candidate'], preview['candidate'])
        saved = self.workspace.network_wpad_preferences(data)
        self.assertEqual(saved['mode'], 'fake_ip_doh')
        with self.assertRaises(InputError): self.workspace.network_wpad_preferences(data)

    def test_all_old_modes_do_not_imply_discovery_permission(self):
        self.workspace.network_pac_preferences(dict(name='old.pac', script=SCRIPT, revision=0, consent=True))
        self.workspace.network_state()
        with patch.object(system_pac, 'current_source', return_value=self.source):
            self.workspace.network_system_pac_preferences(dict(revision=1, config_id=self.source.config_id, consent=True))
            self.workspace.network_state()
        self.auto()
        self.discovery.assert_not_called()
        self.fetch.assert_not_called(); self.evaluate.assert_not_called()

    def test_one_session_discovers_downloads_once_caches_hosts_and_fresh_session_rechecks(self):
        saved = self.save(); self.discovery.reset_mock()
        policy = self.workspace.network_policy()
        policy.describe(); policy.fingerprint; policy.ensure_active()
        self.discovery.assert_not_called()
        self.assertIsNone(policy.for_host('example.com'))
        self.assertIsNone(policy.for_host('example.com'))
        self.assertIsNone(policy.for_host('other.example'))
        self.discovery.assert_called_once(); self.fetch.assert_called_once()
        self.assertEqual(self.evaluate.call_count, 2)
        self.assertEqual(self.evaluate.call_args.args[1], 'https://other.example/')
        self.workspace.network_policy().for_host('example.com')
        self.assertEqual(self.discovery.call_count, 2)
        self.assertEqual(self.workspace.network_state()['pac_id'], saved['pac_id'])

    def test_changed_source_never_downloads_and_failure_is_cached_across_hosts(self):
        self.save(); self.discovery.reset_mock()
        self.discovery.return_value = system_pac.Source('https://pac.example/private.pac?token=OTHER')
        policy = self.workspace.network_policy()
        for host in ('example.com', 'other.example'):
            with self.assertRaisesRegex(LocalProxyError, 'wpad_source_changed'): policy.for_host(host)
        self.discovery.assert_called_once(); self.fetch.assert_not_called(); self.evaluate.assert_not_called()

    def test_failed_discovery_or_download_is_not_repeated_per_asset(self):
        self.save(); self.discovery.reset_mock()
        self.discovery.side_effect = LocalProxyError('wpad_not_found')
        policy = self.workspace.network_policy()
        for host in ('example.com', 'other.example'):
            with self.assertRaisesRegex(LocalProxyError, 'wpad_not_found'): policy.for_host(host)
        self.discovery.assert_called_once(); self.fetch.assert_not_called()
        self.discovery.side_effect = None
        self.fetch.side_effect = LocalProxyError('system_pac_tls_failed')
        current = self.workspace.network_policy()
        for host in ('example.com', 'other.example'):
            with self.assertRaisesRegex(LocalProxyError, 'system_pac_tls_failed'): current.for_host(host)
        self.fetch.assert_called_once(); self.evaluate.assert_not_called()

    def test_revoke_reconfirm_cancel_and_close_block_even_cached_routes(self):
        self.save(); policy = self.workspace.network_policy(); policy.for_host('example.com')
        self.auto()
        with self.assertRaisesRegex(LocalProxyError, 'pac_revoked'): policy.for_host('example.com')
        self.assertEqual(read_settings(self.workspace)['schema_version'], 2)
        self.assertNotIn('wpad_config_id', read_settings(self.workspace))
        self.save(); old = self.workspace.network_policy(); self.save()
        with self.assertRaisesRegex(LocalProxyError, 'pac_revoked'): old.ensure_active()
        current = self.workspace.network_policy(); event = threading.Event()
        current.bind_cancellation(event); event.set()
        with self.assertRaisesRegex(LocalProxyError, 'pac_revoked'): current.for_host('example.com')
        current = self.workspace.network_policy(); current.close()
        with self.assertRaisesRegex(LocalProxyError, 'pac_revoked'): current.ensure_active()
        current = self.workspace.network_policy(); self.workspace.close_network()
        with self.assertRaisesRegex(LocalProxyError, 'pac_revoked'): current.ensure_active()

    def test_explicit_discovery_of_changed_source_stops_existing_local_snapshot(self):
        self.save(); current = self.workspace.network_policy(); current.for_host('example.com')
        self.discovery.return_value = system_pac.Source('https://new.example/wpad.dat')
        self.preview()
        with self.assertRaisesRegex(LocalProxyError, 'pac_revoked'): current.for_host('example.com')
        self.fetch.assert_called_once()

    def test_pending_discovery_is_cancellable_while_state_remains_read_only(self):
        entered = threading.Event()
        def pending(permitted):
            entered.set()
            while permitted():
                if not entered.wait(.01): break
                # Yield independently from the signal used by the test.
                threading.Event().wait(.005)
            raise LocalProxyError('pac_revoked')
        self.discovery.side_effect = pending
        self.workspace.network_wpad_discover(dict(revision=0, consent=True))
        self.assertTrue(entered.wait(1))
        self.assertTrue(self.workspace.network_state()['wpad']['discovery_running'])
        with self.assertRaises(InputError): self.workspace.network_wpad_discover(dict(revision=0, consent=True))
        self.workspace.network_wpad_cancel({})
        self.workspace.wpad_discovery._thread.join(2)
        self.assertFalse(self.workspace.wpad_discovery._thread.is_alive())
        self.assertEqual(self.workspace.network_state()['wpad']['code'], 'wpad_cancelled')
        self.fetch.assert_not_called()
        self.assertFalse((self.workspace.root/'network-preferences.json').exists())

    def test_dns_edit_preserves_grant_and_other_modes_remove_only_incompatible_fields(self):
        saved = self.save(); policy = self.workspace.network_policy()
        self.workspace.network_preferences(dict(mode='fake_ip_doh', revision=1, consent=True))
        self.assertEqual(read_settings(self.workspace)['pac_id'], saved['pac_id']); policy.ensure_active()
        self.workspace.network_pac_preferences(dict(name='old.pac', script=SCRIPT, revision=2, consent=True))
        self.assertNotIn('wpad_config_id', read_settings(self.workspace))
        copy = next((self.workspace.root/'network-pac').glob('*.js')); raw = copy.read_bytes()
        self.save()
        self.assertNotIn('pac_name', read_settings(self.workspace)); self.assertEqual(copy.read_bytes(), raw)
        with patch.object(system_pac, 'current_source', return_value=self.source):
            self.workspace.network_system_pac_preferences(dict(revision=self.revision(), config_id=self.source.config_id, consent=True))
        self.assertNotIn('wpad_config_id', read_settings(self.workspace))

    def test_strict_v5_corruption_foreign_schema_and_unsupported_os_stop_network(self):
        self.save(); original = read_settings(self.workspace); path = self.workspace.root/'network-preferences.json'
        for change in ({'schema_version':4}, {'schema_version':6}, {'wpad_config_id':'bad'},
                       {'proxy_consent_version':system_pac.CONSENT}, {'proxy_endpoint':'http://127.0.0.1:80'},
                       {'pac_id':True}, {'proxy_mode':'auto'}, {'source_url':self.source.url}):
            path.write_text(json.dumps({**original, **change}), encoding='utf-8')
            with self.subTest(change=change), self.assertRaises(InputError): read_settings(self.workspace)
        path.write_text(json.dumps(original), encoding='utf-8')
        with patch.object(wpad_native, 'available', return_value=False):
            self.assertEqual(self.workspace.network_state()['policy']['code'], 'wpad_unavailable')
            with self.assertRaises(LocalProxyError): self.workspace.network_policy().for_host('example.com')
            self.auto()
        self.fetch.assert_not_called()

    def test_conflicting_credentials_invalid_host_and_revocation_during_discovery_no_download(self):
        with patch.dict(os.environ, {'VIBE_RADAR_PROXY_PASSWORD':'SYNTHETIC'}):
            with self.assertRaises(InputError): self.preview()
        self.discovery.assert_not_called()
        self.save(); self.discovery.reset_mock()
        policy = self.workspace.network_policy()
        with self.assertRaises(LocalProxyError): policy.for_host('https://private.example/path')
        self.discovery.assert_not_called()
        def revoke(permitted):
            self.auto()
            return self.source
        self.discovery.side_effect = revoke
        with self.assertRaisesRegex(LocalProxyError, 'pac_revoked'): policy.for_host('example.com')
        self.fetch.assert_not_called()

    def test_api_token_origin_revision_check_and_cancel_keep_mutation_lock_responsive(self):
        server = LocalServer(self.workspace, public_client=None)
        thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval':.01}, daemon=True); thread.start()
        def post(path, data, auth=True):
            conn = http.client.HTTPConnection(server.authority, timeout=10)
            try:
                headers = {'Content-Type':'application/json', 'Origin':server.origin}
                if auth: headers['X-Radar-Token'] = server.token
                conn.request('POST', path, json.dumps(data), headers); response = conn.getresponse()
                return response.status, json.loads(response.read())
            finally: conn.close()
        try:
            data = dict(revision=0, consent=True)
            self.assertEqual(post('/api/network/wpad/discover', data, False)[0], 403)
            self.discovery.assert_not_called()
            self.assertEqual(post('/api/network/wpad/discover', data)[0], 200)
            self.workspace.wpad_discovery._thread.join(2)
            preview = self.workspace.network_state()['wpad']
            self.assertEqual(post('/api/network/wpad', dict(revision=0, candidate=preview['candidate'], consent=True))[0], 200)
            self.assertEqual(post('/api/network/pac/check', dict(revision=1, url='https://private/'))[0], 400)
            status, checked = post('/api/network/pac/check', dict(revision=1))
            self.assertEqual(status, 200); self.assertTrue(checked['passed']); self.assertFalse(checked['target_requested'])
            self.fetch.assert_called_once()
            self.assertEqual(post('/api/network/wpad/cancel', {})[0], 200)
        finally: server.shutdown(); server.server_close(); thread.join(5)


if __name__ == '__main__': unittest.main()
