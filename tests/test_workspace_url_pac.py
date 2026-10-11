"""Independent scope grants, rollback and real API/transport for URL PAC."""
import http.client
import json
import os
import tempfile
import threading
import unittest
from unittest.mock import patch, MagicMock
import test_loopback_proxy as http_fixture
import test_loopback_socks as socks_fixture
from vibe_job_radar import pac_native, pac_url_worker
from vibe_job_radar.pac_url import CONSENT
from vibe_job_radar.network import SafeHTTP, FetchError
from vibe_job_radar.network_settings import read_settings
from vibe_job_radar.loopback_proxy import LocalProxyError
from vibe_job_radar.workbench import LocalServer
from vibe_job_radar.workspace import Workspace, InputError

SCRIPT='function FindProxyForURL(url,host){return "DIRECT";}'


class WorkspaceUrlPacTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup)
        self.workspace=Workspace(tmp.name);self.addCleanup(self.workspace.close_network)
        env=patch.dict(os.environ,{k:v for k,v in os.environ.items() if not k.startswith('VIBE_RADAR_')},clear=True)
        env.start();self.addCleanup(env.stop)
        discovery=patch('urllib.request.getproxies',return_value={});discovery.start();self.addCleanup(discovery.stop)

    def save(self,script=SCRIPT,**changes):
        data=dict(name='local.pac',script=script,revision=read_settings(self.workspace)['revision'],consent=True)
        return self.workspace.network_url_pac_preferences({**data,**changes})

    def auto(self):
        return self.workspace.network_proxy_preferences(dict(mode='auto',endpoint='',consent=False,
            revision=read_settings(self.workspace)['revision']))

    def test_grant_and_state_are_offline_and_old_domain_grant_is_not_expanded(self):
        with patch.object(pac_url_worker,'available',return_value=True),\
             patch.object(pac_url_worker,'evaluate') as full,patch.object(pac_native,'available',return_value=True),\
             patch.object(pac_native,'evaluate') as old,patch('socket.getaddrinfo') as dns:
            self.workspace.network_pac_preferences(dict(name='old.pac',script=SCRIPT,revision=0,consent=True))
            previous=self.workspace.network_policy()
            self.assertEqual(read_settings(self.workspace)['schema_version'],3)
            self.assertEqual(previous.describe()['transport'],'pac_by_host')
            state=self.save()
            reloaded=Workspace(self.workspace.root)
            try:self.assertEqual(reloaded.network_state()['proxy_mode'],'pac_url')
            finally:reloaded.close_network()
            self.assertEqual(state['schema_version'],6);self.assertEqual(state['proxy_consent_version'],CONSENT)
            self.assertEqual(state['policy']['transport'],'pac_by_url')
            with self.assertRaisesRegex(LocalProxyError,'pac_revoked'):previous.for_host('example.com')
        full.assert_not_called();old.assert_not_called();dns.assert_not_called()
        self.assertFalse(self.workspace.db.exists())
        self.assertNotIn('FindProxyForURL',json.dumps(state))

    def test_separate_consent_schema_and_file_binding_are_strict(self):
        with patch.object(pac_url_worker,'available',return_value=True):
            for changed in ({'consent':False},{'consent':1},{'revision':True},{'extra':1},
                            {'name':'../bad.pac'},{'script':'x'*65537}):
                with self.subTest(changed=changed),self.assertRaises(InputError):self.save(**changed)
            self.save();path=self.workspace.root/'network-preferences.json';original=read_settings(self.workspace)
            for changed in ({'schema_version':3},{'proxy_mode':'pac'},{'proxy_consent_version':'workspace-trusted-domain-pac-v1'}):
                path.write_text(json.dumps({**original,**changed}),encoding='utf8')
                with self.assertRaises(InputError):read_settings(self.workspace)
            path.write_text(json.dumps(original),encoding='utf8')
            old=self.workspace.network_policy()
            (self.workspace.root/'network-pac'/(original['pac_sha256']+'.js')).write_text('changed',encoding='utf8')
            self.assertEqual(self.workspace.network_state()['policy']['code'],'pac_file_invalid')
            with self.assertRaisesRegex(LocalProxyError,'pac_revoked'):old.for_request('https://example.com/a')
            self.auto()

    def test_missing_engine_stops_existing_mode_and_allows_offline_rollback(self):
        with patch.object(pac_url_worker,'available',return_value=True):self.save()
        with patch.object(pac_url_worker,'available',return_value=False):
            with self.assertRaises(InputError):self.save()
            self.assertEqual(self.workspace.network_policy().error,'pac_url_unavailable')
            with patch('socket.getaddrinfo') as dns:
                with self.assertRaisesRegex(FetchError,'pac_url_unavailable'):
                    SafeHTTP({'example.com'},interval=0,network_policy=self.workspace.network_policy()).request('https://example.com/a')
            dns.assert_not_called()
            self.assertEqual(self.auto()['schema_version'],2)
            self.assertEqual(self.workspace.network_state()['proxy_mode'],'auto')

    def test_dns_only_save_preserves_scope_reimport_and_rollback_revoke_existing_route(self):
        with patch.object(pac_url_worker,'available',return_value=True),\
             patch.object(pac_url_worker,'evaluate',return_value='DIRECT'):
            initial=self.save();old=self.workspace.network_policy()
            route=old.for_request('https://example.com/first')
            self.workspace.network_preferences(dict(mode='public_doh',revision=1,consent=True))
            self.assertEqual(read_settings(self.workspace)['pac_id'],initial['pac_id'])
            self.assertEqual(read_settings(self.workspace)['schema_version'],6)
            self.assertIsNone(route.for_host('example.com'))
            self.save()
            with self.assertRaisesRegex(LocalProxyError,'pac_revoked'):route.for_host('example.com')
            current=self.workspace.network_policy();self.auto()
            with self.assertRaisesRegex(LocalProxyError,'pac_revoked'):current.for_request('https://example.com/first')

    def test_new_api_rejects_foreign_origin_requires_token_and_checks_only_explicitly(self):
        server=LocalServer(self.workspace,public_client=None)
        thread=threading.Thread(target=server.serve_forever,kwargs={'poll_interval':.01},daemon=True);thread.start()
        def post(path,data,token=True,origin=True):
            c=http.client.HTTPConnection(server.authority,timeout=10)
            try:
                headers={'Content-Type':'application/json'}
                if token:headers['X-Radar-Token']=server.token
                headers['Origin']=server.origin if origin else 'https://foreign.invalid'
                c.request('POST',path,json.dumps(data),headers);r=c.getresponse()
                return r.status,json.loads(r.read())
            finally:c.close()
        try:
            with patch.object(pac_url_worker,'available',return_value=True),\
                 patch.object(pac_url_worker,'evaluate',return_value='DIRECT') as evaluate:
                data=dict(name='local.pac',script=SCRIPT,revision=0,consent=True)
                self.assertEqual(post('/api/network/pac-url',data,token=False)[0],403)
                self.assertEqual(post('/api/network/pac-url',data,origin=False)[0],403)
                self.assertEqual(post('/api/network/pac-url',data)[0],200);evaluate.assert_not_called()
                status,result=post('/api/network/pac/check',{'revision':1})
                self.assertEqual(status,200);self.assertTrue(result['passed']);self.assertFalse(result['target_requested'])
                self.assertEqual(evaluate.call_args.args[1:3],
                    ('https://pac-check.invalid/path?probe=explicit','pac-check.invalid'))
        finally:server.shutdown();server.server_close();thread.join(5)

    def test_actual_engine_selected_http_and_socks_keep_tls_and_no_fallback(self):
        if not pac_url_worker.available():
            with self.assertRaises(InputError):self.save()
            return
        for keyword,cls in (('PROXY',http_fixture.RealProxyTests),('SOCKS5',socks_fixture.RealSocksTests)):
            fixture=cls();fixture.setUp()
            try:
                raw=keyword+' '+fixture.setting.split('://',1)[1]+'; DIRECT'
                target='https://'+http_fixture.HOST+'/jobs?path=selected'
                source='function FindProxyForURL(url,host){return url==='+json.dumps(target)+'?'+json.dumps(raw)+':"UNKNOWN local:9; DIRECT";}'
                self.save(source)
                def request(url=target):
                    with patch.dict(os.environ):
                        for name in list(os.environ):
                            if name.startswith('VIBE_RADAR_'):os.environ.pop(name)
                        return SafeHTTP({http_fixture.HOST},interval=0,network_policy=self.workspace.network_policy()).json(url)
                self.assertTrue(fixture.perform(request)['ok'])
                self.assertEqual(fixture.dials,[fixture.proxy.server_address]);self.assertEqual(fixture.sni,[http_fixture.HOST])
                self.assertEqual(len(fixture.requests),1)
                with self.assertRaisesRegex(FetchError,'pac_invalid_result'):
                    fixture.perform(lambda:request(target.replace('selected','different')))
                self.assertEqual(len(fixture.requests),1)
                if keyword=='PROXY':fixture.proxy_status=503
                else:fixture.reply=5
                with self.assertRaises(FetchError):fixture.perform(request)
                self.assertEqual(len(fixture.requests),1)
                self.assertTrue(all(d==fixture.proxy.server_address for d in fixture.dials))
            finally:fixture.tearDown();fixture.doCleanups()

    def test_native_login_refuses_before_quota_backend_or_dns_even_with_missing_file(self):
        from vibe_job_radar.guided.service import GuidedService
        from vibe_job_radar.guided.contracts import CrawlError
        from vibe_job_radar.network_policy import use_policy
        with patch.object(pac_url_worker,'available',return_value=True):
            state=self.save()
            service=GuidedService(self.workspace)
            self.addCleanup(service.close)
            service._submit=MagicMock()
            task=service.create(dict(platform='liepin',keyword='test',roles=['time_series'],
                max_jobs=1,consent=True,rights_note='authored control',backend='native',native_consent=True))
            for missing in (False,True):
                if missing:
                    (self.workspace.root/'network-pac'/(state['pac_sha256']+'.js')).unlink()
                policy=self.workspace.network_policy()
                from vibe_job_radar.guided.diagnostic_trace import DiagnosticTrace
                trace=DiagnosticTrace(task['id'],'liepin')
                service._trace_for=MagicMock(return_value=trace)
                with use_policy(policy),patch.object(service.ledger,'reserve') as reserve,\
                     patch.object(service,'_backend') as backend,patch('socket.getaddrinfo') as dns:
                    with self.assertRaisesRegex(CrawlError,'pac_url_native_unsupported'):
                        service._run('login',service._load(task['id']),None)
                reserve.assert_not_called();backend.assert_not_called();dns.assert_not_called()
                first=trace.snapshot()['first_failure']
                self.assertEqual(first['code'],'pac_url_native_unsupported')
                self.assertIs(first['local_block'],True)
                self.assertEqual(first['policy'],'pac_url_native_scope')


    def test_actual_calendar_app_proof_keeps_checks_offline_and_rolls_back(self):
        if not pac_url_worker.available(): return
        import socket
        import importlib.util
        from pathlib import Path
        script=Path(__file__).resolve().parents[1]/'scripts/url_pac_acceptance.py'
        spec=importlib.util.spec_from_file_location('calendar_app_acceptance',script)
        acceptance=importlib.util.module_from_spec(spec)
        spec.loader.exec_module(acceptance)
        server=LocalServer(self.workspace,public_client=None)
        self.addCleanup(server.server_close)
        thread=threading.Thread(target=server.serve_forever,
                                kwargs={'poll_interval':.01},daemon=True)
        thread.start()
        self.addCleanup(thread.join,5)
        self.addCleanup(server.shutdown)
        outer=self
        class App:
            def json(self,path,data=None):
                connection=http.client.HTTPConnection(server.authority,timeout=15)
                try:
                    headers={'X-Radar-Token':server.token,'Origin':server.origin,
                             'Content-Type':'application/json'}
                    connection.request('GET' if data is None else 'POST',path,
                                       None if data is None else json.dumps(data),headers)
                    response=connection.getresponse()
                    outer.assertEqual(response.status,200)
                    return json.loads(response.read())
                finally:connection.close()
        original_connect=socket.socket.connect
        destinations=[]
        def only_owned_api(sock,address):
            outer.assertEqual(address,('127.0.0.1',server.server_port))
            destinations.append(address)
            return original_connect(sock,address)
        with patch('socket.socket.connect',only_owned_api):
            proof=acceptance.verify_app(App())
        self.assertTrue(acceptance.valid_evidence(proof,require_frozen=False))
        self.assertTrue(proof['calendar_helpers_checked'])
        self.assertTrue(proof['calendar_invalid_rejected'])
        self.assertFalse(proof['frozen_executable'])
        self.assertEqual(proof['target_request_evidence'],'api-check-response-flags')
        self.assertTrue(destinations)
        self.assertEqual(self.workspace.network_state()['proxy_mode'],'auto')
