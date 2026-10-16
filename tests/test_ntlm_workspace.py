"""Explicit NTLM workspace version, independent Windows reopen, and old-mode rollback."""
import json
import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import patch
from test_workspace_proxy_auth import AuthCase
from vibe_job_radar import ntlm_worker, network_settings
from vibe_job_radar.workspace import InputError


class NtlmWorkspaceTests(AuthCase):
    def test_actual_protected_restart_identity_clear_and_rollback(self):
        if os.name != 'nt':
            with patch('vibe_job_radar.workspace_proxy_auth.available', return_value=False), self.assertRaisesRegex(InputError, 'Windows'):
                self.save(mode='http_ntlm_auth')
            return
        self.save(mode='http_ntlm_auth')
        original = self.raw()
        data = json.loads(original)
        self.assertEqual(data['schema_version'], 11)
        self.assertNotIn(b'ARTIFICIAL-PROXY-SECRET', original)
        if not ntlm_worker.available():
            self.assertEqual(self.workspace.network_policy().error, 'local_proxy_ntlm_unavailable')
            return
        policy = self.workspace.network_policy()
        program = '''import sys
from vibe_job_radar.workspace import Workspace
w=Workspace(sys.argv[1]);p=w.network_policy()
assert p.describe()['proxy_authentication_method']=='ntlmv2' and p.error is None
assert p.background_fingerprint==sys.argv[2] and p.fingerprint!=sys.argv[3]
assert p._saved_session_network()==sys.argv[4]
assert 'proxy_auth' not in w.network_state()
print('restored_ntlm')
'''
        env = dict(os.environ)
        env['PYTHONPATH'] = str(Path(__file__).resolve().parents[1] / 'src') + os.pathsep + env.get('PYTHONPATH', '')
        child = subprocess.run([sys.executable, '-c', program, str(self.workspace.root),
            policy.background_fingerprint, policy.fingerprint, policy._saved_session_network()],
            env=env, capture_output=True, text=True, encoding='utf-8', timeout=20,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        self.assertEqual(child.returncode, 0, child.stderr)
        self.assertEqual(child.stdout.strip(), 'restored_ntlm')
        self.assertEqual(self.raw(), original)
        cleared = self.workspace.network_proxy_auth_clear(dict(revision=1, consent=True))
        self.assertEqual(self.workspace.network_policy().error, 'workspace_proxy_auth_missing')
        self.workspace.network_proxy_preferences(dict(mode='auto', endpoint='',
            revision=cleared['revision'], consent=False))
        self.assertEqual(network_settings.read_settings(self.workspace)['schema_version'], 2)
        self.assertIsNone(self.workspace.network_policy().proxy)

    def test_schema_and_missing_component_do_not_borrow_old_auth_consent(self):
        if os.name != 'nt':
            with patch('vibe_job_radar.workspace_proxy_auth.available', return_value=False), self.assertRaisesRegex(InputError, 'Windows'):
                self.save(mode='http_ntlm_auth')
            return
        with patch.object(ntlm_worker, 'available', return_value=False):
            state = self.save(mode='http_ntlm_auth')
            self.assertEqual(state['policy']['code'], 'local_proxy_ntlm_unavailable')
            self.assertNotIn('proxy_auth', state)
        original = self.raw()
        path = self.workspace.root / 'network-preferences.json'
        value = json.loads(original)
        value['schema_version'] = 8
        path.write_text(json.dumps(value), encoding='utf-8')
        with self.assertRaises(InputError):
            network_settings.read_settings(self.workspace)
        path.write_bytes(original)
        value = json.loads(original)
        value['proxy_consent_version'] = 'workspace-loopback-current-user-dpapi-v1'
        path.write_text(json.dumps(value), encoding='utf-8')
        with self.assertRaises(InputError):
            network_settings.read_settings(self.workspace)
