"""Workspace proxy credentials: isolation, atomic changes and real wire integration."""
import base64
import http.client
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from vibe_job_radar import network_settings, workspace_proxy_auth as auth
from vibe_job_radar.loopback_proxy import LocalProxyError
from vibe_job_radar.network import SafeHTTP, FetchError
from vibe_job_radar.proxy_credentials import ProxyCredentials
from vibe_job_radar.workspace import Workspace, InputError
from vibe_job_radar.workbench import LocalServer
import test_proxy_auth as wire
import test_digest_proxy as digest_wire

PRIVATE = (*wire.AUTH, 'VIBE_RADAR_PROXY_AUTH')
USER, SECRET = wire.USER, wire.PASSWORD


class AuthCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.workspace = Workspace(Path(tmp.name)/'first')
        self.clean = {k:v for k,v in os.environ.items() if k not in PRIVATE}
        env = patch.dict(os.environ, self.clean, clear=True); env.start(); self.addCleanup(env.stop)
        discover = patch('urllib.request.getproxies', return_value={}); discover.start(); self.addCleanup(discover.stop)

    def save(self, **changes):
        data = dict(mode='http_auth', endpoint='http://localhost:18990/', username=USER,
                    password=SECRET, revision=network_settings.read_settings(self.workspace)['revision'], consent=True)
        data.update(changes)
        return self.workspace.network_proxy_auth_preferences(data)

    def raw(self):
        return (self.workspace.root/'network-preferences.json').read_bytes()


class WorkspaceProxyAuthTests(AuthCase):
    def setUp(self):
        super().setUp()
        self.protected = {}
        available = patch.object(auth, 'available', return_value=True); available.start(); self.addCleanup(available.stop)
        codec = patch.object(auth, '_protect', side_effect=self.codec); codec.start(); self.addCleanup(codec.stop)

    def codec(self, data, entropy, *, decrypt=False):
        if decrypt:
            raw, expected = self.protected[data]
            if entropy != expected: raise ValueError('binding mismatch')
            return raw
        token = ('opaque-artificial-'+str(len(self.protected))).encode()
        self.protected[token] = (data, entropy)
        return token

    def test_offline_save_reload_returns_only_configured_state(self):
        with patch('socket.getaddrinfo', side_effect=AssertionError('no DNS')), patch('socket.create_connection', side_effect=AssertionError('no dial')):
            result = self.save()
            reopened = Workspace(self.workspace.root)
            policy = reopened.network_policy()
        self.assertEqual(result['schema_version'], 8)
        self.assertTrue(result['proxy_auth_saved'])
        self.assertEqual(result['proxy_endpoint'], 'http://127.0.0.1:18990')
        self.assertEqual(policy.proxy.credentials.basic_header(), wire.BASIC)
        self.assertEqual(policy.describe()['proxy_credentials_scope'], 'explicit_workspace_windows_dpapi_loopback')
        self.assertEqual(result['policy']['policy_id'], policy.fingerprint)
        self.assertNotIn(auth.FIELD, result)
        text = self.raw().decode()+json.dumps(result)+repr(policy)+repr(policy.proxy)
        for private in (USER, SECRET, wire.BASIC): self.assertNotIn(private, text)

    def test_bad_inputs_and_environment_conflict_leave_prior_settings(self):
        self.save(); before = self.raw()
        for changes in ({'consent':False}, {'username':''}, {'password':'bad\nheader'},
                        {'endpoint':'http://10.0.2.2:18990'}, {'endpoint':'http://user:secret@127.0.0.1:18990'},
                        {'mode':'vm_http'}, {'mode':'http'}, {'extra':'not allowed'}, {'revision':0}):
            with self.subTest(changes=tuple(changes)), self.assertRaises(InputError): self.save(**changes)
            self.assertEqual(self.raw(), before)
        with patch.dict(os.environ, {'VIBE_RADAR_PROXY_PASSWORD':'artificial conflict'}):
            with self.assertRaises(InputError): self.save()
            self.assertEqual(self.workspace.network_policy().error, 'workspace_proxy_environment_conflict')
        self.assertEqual(self.raw(), before)
        with self.assertRaises(InputError):
            self.workspace.network_proxy_preferences(dict(mode='http_auth', endpoint='http://127.0.0.1:18990', revision=1, consent=True))
        self.assertEqual(self.raw(), before)

    def test_encrypt_or_atomic_write_failure_keeps_existing_blob(self):
        self.save(); before = self.raw()
        with patch.object(auth, '_protect', side_effect=RuntimeError(SECRET)):
            with self.assertRaises(InputError) as error: self.save(password='replacement')
            self.assertNotIn(SECRET, str(error.exception))
        self.assertEqual(self.raw(), before)
        with patch.object(network_settings, 'atomic_json', side_effect=OSError('artificial write failure')):
            with self.assertRaises(OSError): self.save(password='replacement')
        self.assertEqual(self.raw(), before)
        self.assertEqual(self.workspace.network_policy().proxy.credentials.basic_header(), wire.BASIC)

    def test_envelope_cannot_move_to_another_workspace_endpoint_or_protocol(self):
        self.save(); original = self.raw(); saved = json.loads(original)
        other = Workspace(self.workspace.root.parent/'second')
        (other.root/'network-preferences.json').write_bytes(original)
        variants = [(other, saved), (self.workspace, {**saved, 'proxy_endpoint':'http://127.0.0.1:18991'}),
                    (self.workspace, {**saved, 'proxy_mode':'socks5_auth', 'proxy_endpoint':'socks5://127.0.0.1:18990'}),
                    (self.workspace, {**saved, 'proxy_mode':'http_digest_auth'})]
        for workspace, value in variants:
            (workspace.root/'network-preferences.json').write_text(json.dumps(value), encoding='utf-8')
            with patch('socket.getaddrinfo') as dns, patch('socket.create_connection') as dial:
                policy = workspace.network_policy()
                self.assertEqual(policy.error, 'workspace_proxy_auth_unreadable')
                with self.assertRaisesRegex(FetchError, 'workspace_proxy_auth_unreadable'):
                    SafeHTTP({wire.HOST}, network_policy=policy).request('https://'+wire.HOST+'/jobs')
                dns.assert_not_called(); dial.assert_not_called()

    def test_change_dns_preserves_credentials_change_secret_rebinds_snapshot(self):
        self.save(); old = self.workspace.network_policy(); blob = json.loads(self.raw())[auth.FIELD]
        self.workspace.network_preferences(dict(mode='fake_ip_doh', revision=1, consent=True))
        self.assertEqual(json.loads(self.raw())[auth.FIELD], blob)
        self.assertEqual(self.workspace.network_policy().proxy.credentials.basic_header(), wire.BASIC)
        self.save(password='replacement')
        new = self.workspace.network_policy()
        self.assertNotEqual(new.fingerprint, old.fingerprint)
        self.assertEqual(old.proxy.credentials.basic_header(), wire.BASIC)
        self.assertNotEqual(new.proxy.credentials, old.proxy.credentials)

    def test_background_identity_survives_process_key_change_but_live_identity_does_not(self):
        self.save(); before = self.workspace.network_policy()
        with patch('vibe_job_radar.proxy_credentials._BINDING_KEY', b'different artificial process'):
            restarted = self.workspace.network_policy()
        self.assertNotEqual(before.fingerprint, restarted.fingerprint)
        self.assertEqual(before.background_fingerprint, restarted.background_fingerprint)
        self.assertEqual(before._saved_session_network(), restarted._saved_session_network())
        self.save(password='changed')
        changed = self.workspace.network_policy()
        self.assertNotEqual(before.background_fingerprint, changed.background_fingerprint)
        self.assertNotEqual(before.fingerprint, changed.fingerprint)
        self.assertNotEqual(before._saved_session_network(), changed._saved_session_network())

    def test_clear_stops_selected_route_even_when_blob_cannot_decrypt(self):
        self.save(); saved = json.loads(self.raw())
        saved[auth.FIELD]['payload'] = base64.b64encode(b'artificial invalid blob').decode()
        (self.workspace.root/'network-preferences.json').write_text(json.dumps(saved), encoding='utf-8')
        self.assertEqual(self.workspace.network_policy().error, 'workspace_proxy_auth_unreadable')
        result = self.workspace.network_proxy_auth_clear(dict(revision=1, consent=True))
        self.assertFalse(result['proxy_auth_saved'])
        self.assertEqual(result['proxy_mode'], 'http_auth')
        self.assertEqual(result['proxy_endpoint'], 'http://127.0.0.1:18990')
        self.assertEqual(result['policy']['code'], 'workspace_proxy_auth_missing')
        self.assertIsNone(json.loads(self.raw())[auth.FIELD])
        with patch('socket.create_connection') as dial, patch('socket.getaddrinfo') as dns:
            with self.assertRaisesRegex(FetchError, 'workspace_proxy_auth_missing'):
                SafeHTTP({wire.HOST}, network_policy=self.workspace.network_policy()).request('https://'+wire.HOST+'/')
            dial.assert_not_called(); dns.assert_not_called()
        self.save(password='re-entered')
        self.assertTrue(self.workspace.network_state()['proxy_auth_saved'])

    def test_explicit_other_route_removes_ciphertext_without_changing_dns(self):
        for mode, endpoint in [('http','http://127.0.0.1:18991'), ('auto','')]:
            self.save()
            revision = network_settings.read_settings(self.workspace)['revision']
            self.workspace.network_preferences(dict(mode='public_doh', revision=revision, consent=True))
            revision += 1
            result = self.workspace.network_proxy_preferences(dict(mode=mode, endpoint=endpoint, revision=revision, consent=mode!='auto'))
            self.assertNotIn(auth.FIELD, json.loads(self.raw()))
            self.assertFalse(result['proxy_auth_saved'])
            self.assertEqual(result['mode'], 'public_doh')
            self.assertEqual(result['schema_version'], 2)
        self.save()
        with patch('vibe_job_radar.pac_native.available', return_value=True):
            result = self.workspace.network_pac_preferences(dict(name='artificial.pac', script='function FindProxyForURL(url, host) { return "DIRECT"; }',
                revision=network_settings.read_settings(self.workspace)['revision'], consent=True))
        self.assertNotIn(auth.FIELD, json.loads(self.raw()))
        self.assertFalse(result['proxy_auth_saved'])

    def test_schema_guard_and_unavailable_platform_do_not_fall_back(self):
        self.save(); saved = json.loads(self.raw())
        for value in ({**saved,'schema_version':2}, {**saved,'proxy_mode':'http'},
                      {**saved,auth.FIELD:{'version':True,'protection':'dpapi-current-user','payload':'YQ=='}},
                      {**saved,auth.FIELD:{'version':1,'protection':'dpapi-current-user','payload':'invalid'}}):
            (self.workspace.root/'network-preferences.json').write_text(json.dumps(value), encoding='utf-8')
            with self.assertRaises(InputError): self.workspace.network_policy()
        (self.workspace.root/'network-preferences.json').write_text(json.dumps(saved), encoding='utf-8')
        with patch.object(auth, 'available', return_value=False):
            self.assertEqual(self.workspace.network_policy().error, 'workspace_proxy_auth_unavailable')
            with self.assertRaises(InputError): self.save()
            self.workspace.network_proxy_auth_clear(dict(revision=1, consent=True))
        self.assertIsNone(json.loads(self.raw())[auth.FIELD])

    def test_local_api_authentication_duplicate_fields_and_response_redaction(self):
        server = LocalServer(self.workspace, public_client=None)
        thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval':.01}, daemon=True); thread.start()
        def post(body, authenticated=True):
            connection = http.client.HTTPConnection(server.authority, timeout=5)
            headers = {'Content-Type':'application/json', 'Origin':server.origin}
            if authenticated: headers['X-Radar-Token'] = server.token
            try:
                connection.request('POST','/api/network/proxy-auth',body,headers)
                response = connection.getresponse(); return response.status, response.read().decode()
            finally: connection.close()
        try:
            body = json.dumps(dict(mode='http_auth',endpoint='http://127.0.0.1:18990',username=USER,password=SECRET,revision=0,consent=True))
            self.assertEqual(post(body,False)[0],403)
            self.assertEqual(post(body[:-1]+',"consent":false}')[0],400)
            self.assertFalse((self.workspace.root/'network-preferences.json').exists())
            status, response = post(body)
            self.assertEqual(status,200)
            for private in (USER, SECRET, wire.BASIC, '"proxy_auth":'): self.assertNotIn(private,response)
        finally: server.shutdown(); server.server_close(); thread.join(5)

    def test_saved_credentials_drive_real_http_and_socks_tls_without_environment(self):
        for mode, fixture_type in [('http_auth',wire.HTTPProxyAuthenticationTests),('socks5_auth',wire.SocksProxyAuthenticationTests),('http_digest_auth',digest_wire.HTTPDigestProxyTests)]:
            fixture = fixture_type(); fixture.setUp()
            try:
                def request():
                    with patch.dict(os.environ,self.clean,clear=True):
                        self.save(mode=mode,endpoint=fixture.setting)
                        return SafeHTTP({wire.HOST},interval=0,network_policy=Workspace(self.workspace.root).network_policy()).json('https://'+wire.HOST+'/jobs')
                self.assertTrue(fixture.perform(request)['ok'])
                self.assertEqual(fixture.dials,[fixture.proxy.server_address]*(2 if mode=='http_digest_auth' else 1))
                self.assertEqual(fixture.sni,[wire.HOST])
                self.assertEqual(len(fixture.requests),1)
                request_headers = fixture.requests[0]['headers'] if mode != 'socks5_auth' else fixture.requests[0][2]
                self.assertNotIn('proxy-authorization',{k.lower() for k in request_headers})
            finally: fixture.tearDown(); fixture.doCleanups()


class ActualPlatformProxyAuthTests(AuthCase):
    def test_digest_dpapi_restart_restores_method_and_background_identity(self):
        if os.name != 'nt':
            with self.assertRaisesRegex(InputError,'Windows'): self.save(mode='http_digest_auth')
            return
        self.save(mode='http_digest_auth'); original=self.raw(); policy=self.workspace.network_policy()
        program='''import sys
from vibe_job_radar.workspace import Workspace
from vibe_job_radar.proxy_credentials import ProxyCredentials
w=Workspace(sys.argv[1]);p=w.network_policy()
assert p.describe()['proxy_authentication_method']=='digest'
assert p.background_fingerprint==sys.argv[2] and p.fingerprint!=sys.argv[3]
assert p._saved_session_network()==sys.argv[4]
assert p.proxy.credentials==ProxyCredentials('ARTIFICIAL-PROXY-USER','ARTIFICIAL-PROXY-SECRET:ONLY')
assert 'proxy_auth' not in w.network_state()
print('restored_digest')
'''
        env=dict(os.environ);env['PYTHONPATH']=str(Path(__file__).resolve().parents[1]/'src')
        child=subprocess.run([sys.executable,'-c',program,str(self.workspace.root),policy.background_fingerprint,
            policy.fingerprint,policy._saved_session_network()],env=env,capture_output=True,text=True,encoding='utf-8',
            timeout=20,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        self.assertEqual(child.returncode,0,child.stderr);self.assertEqual(child.stdout.strip(),'restored_digest')
        self.assertEqual(self.raw(),original);self.assertNotIn(SECRET.encode(),original)
        self.workspace.network_proxy_auth_clear(dict(revision=1,consent=True))
        self.assertEqual(self.workspace.network_policy().error,'workspace_proxy_auth_missing')

    def test_current_user_dpapi_survives_process_restart_or_explicitly_unavailable(self):
        if os.name != 'nt':
            with self.assertRaisesRegex(InputError,'Windows'): self.save()
            self.assertFalse((self.workspace.root/'network-preferences.json').exists())
            return
        self.save(); original = self.raw()
        self.assertNotIn(SECRET.encode(),original)
        program = '''from vibe_job_radar.workspace import Workspace
from vibe_job_radar.proxy_credentials import ProxyCredentials
import sys
w=Workspace(sys.argv[1])
p=w.network_policy()
assert p.proxy.credentials == ProxyCredentials("ARTIFICIAL-PROXY-USER","ARTIFICIAL-PROXY-SECRET:ONLY")
assert "proxy_auth" not in w.network_state()
print("restored")
'''
        env = dict(os.environ); env['PYTHONPATH'] = str(Path(__file__).resolve().parents[1]/'src')
        result = subprocess.run([sys.executable,'-c',program,str(self.workspace.root)],env=env,
                                capture_output=True,text=True,encoding='utf-8',timeout=20,check=True)
        self.assertEqual(result.stdout.strip(),'restored')
        self.assertEqual(self.raw(),original)
        other = Workspace(self.workspace.root.parent/'copy')
        (other.root/'network-preferences.json').write_bytes(original)
        self.assertEqual(other.network_policy().error,'workspace_proxy_auth_unreadable')
        self.workspace.network_proxy_auth_clear(dict(revision=1,consent=True))
        self.assertEqual(self.workspace.network_policy().error,'workspace_proxy_auth_missing')

    def test_real_restarted_schedule_and_queue_dispatch_with_unchanged_saved_credentials(self):
        if os.name != 'nt':
            with self.assertRaisesRegex(InputError, 'Windows'): self.save()
            return
        import time
        from unittest.mock import Mock
        from test_local_public import query
        from vibe_job_radar.local_public import LocalPublicDataClient
        from vibe_job_radar.public_schedule import PublicSchedule, DAY
        from vibe_job_radar.public_queue import PublicQueue
        from vibe_job_radar.public_tasks import PublicTasks
        original_root = self.workspace.root
        program = '''import json,sys
from unittest.mock import Mock,patch
from test_local_public import payload
from vibe_job_radar.workspace import Workspace
from vibe_job_radar.local_public import LocalPublicDataClient
from vibe_job_radar.public_tasks import PublicTasks
from vibe_job_radar.public_queue import PublicQueue
from vibe_job_radar.public_schedule import PublicSchedule
w=Workspace(sys.argv[1]);kind=sys.argv[2];now=float(sys.argv[3]);p=w.network_policy()
assert p.background_fingerprint==sys.argv[4] and p.fingerprint!=sys.argv[5]
wire=Mock();wire.json.return_value=payload()
tasks=PublicTasks(w,hybrid_client=LocalPublicDataClient(w,transport=wire,clock=lambda:now))
owner=(PublicSchedule if kind=='schedule' else PublicQueue)(w,tasks,clock=lambda:now)
try:
 with patch('socket.create_connection',side_effect=AssertionError('no external network')):
  owner.tick()
  with tasks._lock: worker=tasks._thread
  assert worker is not None,owner.state()
  worker.join(15);assert not worker.is_alive()
  task=tasks.snapshot();assert task['status']=='completed',task.get('code')
  owner.tick();assert owner.state()['code']!='conditions_changed'
  assert task['report_id'] and w.report(task['report_id'])
  assert wire.json.call_count==1
 print(json.dumps(dict(success=True,kind=kind,report_created=True)))
finally: owner.close();tasks.close()
'''
        for kind in ('schedule', 'queue'):
            self.workspace = Workspace(original_root/kind)
            self.save()
            policy = self.workspace.network_policy(); now = time.time()
            tasks = PublicTasks(self.workspace, hybrid_client=LocalPublicDataClient(self.workspace, transport=Mock()))
            owner = (PublicSchedule if kind == 'schedule' else PublicQueue)(self.workspace, tasks, clock=lambda:now)
            try:
                data = dict(query=query().payload(), consent=True, revision=0)
                if kind == 'schedule': owner.configure(data)
                else: owner.enqueue(data)
            finally: owner.close(); tasks.close()
            env = dict(os.environ)
            checkout = Path(__file__).resolve().parents[1]
            env['PYTHONPATH'] = os.pathsep.join((str(checkout/'src'), str(checkout/'tests')))
            child = subprocess.run([sys.executable,'-c',program,str(self.workspace.root),kind,
                str(now+DAY+1 if kind=='schedule' else now+1),policy.background_fingerprint,policy.fingerprint],
                env=env,capture_output=True,text=True,encoding='utf-8',timeout=25,
                creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            self.assertEqual(child.returncode,0,child.stderr)
            self.assertEqual(json.loads(child.stdout),dict(success=True,kind=kind,report_created=True))
