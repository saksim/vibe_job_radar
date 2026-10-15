"""Domain scope cannot expand into paths, DNS or a different frozen route."""
import json
import os
import tempfile
import threading
import unittest
from unittest.mock import patch

from vibe_job_radar import pac_native, pac_url_worker
from vibe_job_radar.pac import PacSnapshot
from vibe_job_radar.pac_domain import DomainPacSnapshot, CONSENT, DNS_CONSENT
from vibe_job_radar.pac_url import UrlPacSnapshot
from vibe_job_radar.network_policy import NetworkPolicy
from vibe_job_radar.network import SafeHTTP, FetchError
from vibe_job_radar.network_settings import read_settings
from vibe_job_radar.loopback_proxy import LocalProxyError
from vibe_job_radar.workspace import Workspace, InputError

SCRIPT='function FindProxyForURL(url,host){return "DIRECT";}'


class PortableDomainPacTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup)
        self.workspace=Workspace(tmp.name);self.addCleanup(self.workspace.close_network)
        env=patch.dict(os.environ,{k:v for k,v in os.environ.items() if not k.startswith('VIBE_RADAR_')},clear=True)
        env.start();self.addCleanup(env.stop)
        discovery=patch('urllib.request.getproxies',return_value={});discovery.start();self.addCleanup(discovery.stop)

    def save(self,script=SCRIPT,*,dns=False):
        method=self.workspace.network_domain_pac_dns_preferences if dns else self.workspace.network_domain_pac_preferences
        return method(dict(name='domain.pac',script=script,
            revision=read_settings(self.workspace)['revision'],consent=True))

    def auto(self):
        return self.workspace.network_proxy_preferences(dict(mode='auto',endpoint='',consent=False,
            revision=read_settings(self.workspace)['revision']))

    def policy(self):
        return NetworkPolicy('explicit_workspace',pac=DomainPacSnapshot(SCRIPT,lambda:True),pac_id='grant',pac_domain_mode=True)

    def test_canonical_domain_keeps_route_without_exposing_path_query_or_native_difference(self):
        policy=self.policy()
        with patch.object(pac_url_worker,'evaluate',return_value='PROXY 127.0.0.1:8001') as evaluate:
            for url in ('https://example.com/jobs?secret=ONE','https://example.com/other?secret=TWO'):
                self.assertIs(policy.for_request(url),policy)
                self.assertEqual(policy.for_request(url).for_host('EXAMPLE.COM').port,8001)
            self.assertIsNone(policy.require_native_support())
            self.assertEqual(policy.for_host('example.com').port,8001)
            evaluate.assert_called_once()
            self.assertEqual(evaluate.call_args.args[1:3],('https://example.com/','example.com'))
            self.assertEqual(policy.describe()['pac_scope'],'explicit_trusted_portable_domain')
            self.assertNotIn('secret',repr(policy)+repr(policy.pac)+json.dumps(policy.describe()))

    def test_cache_is_bounded_by_domains_and_failed_results_do_not_fall_back(self):
        policy=self.policy()
        with patch.object(pac_url_worker,'evaluate',return_value='DIRECT') as evaluate:
            for n in range(128):self.assertIsNone(policy.for_host('h'+str(n)+'.example'))
            with self.assertRaisesRegex(LocalProxyError,'pac_cache_limit'):policy.for_host('overflow.example')
            self.assertIsNone(policy.for_host('h0.example'));self.assertEqual(evaluate.call_count,128)
        failed=self.policy()
        with patch.object(pac_url_worker,'evaluate',return_value='UNKNOWN local:9; DIRECT') as evaluate:
            for _ in range(2):
                with self.assertRaisesRegex(LocalProxyError,'pac_invalid_result'):failed.for_host('example.com')
            evaluate.assert_called_once()

    def test_scope_identities_are_separate_and_cancellation_revokes_cached_selection(self):
        policies=[NetworkPolicy('explicit_workspace',pac=factory(SCRIPT,lambda:True),pac_id='same')
            for factory in (PacSnapshot,UrlPacSnapshot,DomainPacSnapshot,
                lambda script,permission:DomainPacSnapshot(script,permission,True))]
        self.assertEqual(len({p.fingerprint for p in policies}),4)
        for close in (False,True):
            policy=self.policy();event=threading.Event();policy.bind_cancellation(event)
            with patch.object(pac_url_worker,'evaluate',return_value='DIRECT'):
                self.assertIsNone(policy.for_host('example.com'))
            if close:policy.close()
            else:event.set()
            with self.assertRaisesRegex(LocalProxyError,'pac_revoked'):policy.for_host('example.com')

    def test_import_and_reopen_are_offline_and_old_grants_do_not_expand(self):
        with patch.object(pac_url_worker,'available',return_value=True),patch.object(pac_native,'available',return_value=True),\
             patch.object(pac_url_worker,'evaluate') as evaluate,patch.object(pac_native,'evaluate') as old,\
             patch('socket.getaddrinfo') as dns:
            self.workspace.network_pac_preferences(dict(name='old.pac',script=SCRIPT,revision=0,consent=True))
            previous=self.workspace.network_policy()
            for enabled,schema,consent in ((False,9,CONSENT),(True,10,DNS_CONSENT)):
                state=self.save(dns=enabled)
                self.assertEqual(state['schema_version'],schema);self.assertEqual(state['proxy_consent_version'],consent)
                reloaded=Workspace(self.workspace.root)
                try:self.assertEqual(reloaded.network_state()['proxy_mode'],state['proxy_mode'])
                finally:reloaded.close_network()
            with self.assertRaisesRegex(LocalProxyError,'pac_revoked'):previous.for_host('example.com')
            evaluate.assert_not_called();old.assert_not_called();dns.assert_not_called()
        self.assertFalse(self.workspace.db.exists())

    def test_scope_schema_consent_and_source_binding_fail_closed(self):
        with patch.object(pac_url_worker,'available',return_value=True):
            for enabled in (False,True):
                self.save(dns=enabled);path=self.workspace.root/'network-preferences.json';original=read_settings(self.workspace)
                for changed in ({'schema_version':3},{'schema_version':6},{'proxy_mode':'pac_url'},
                                {'proxy_consent_version':DNS_CONSENT if not enabled else CONSENT}):
                    path.write_text(json.dumps({**original,**changed}),encoding='utf8')
                    with self.assertRaises(InputError):read_settings(self.workspace)
                path.write_text(json.dumps(original),encoding='utf8')
            old=self.workspace.network_policy()
            (self.workspace.root/'network-pac'/(original['pac_sha256']+'.js')).write_text('changed',encoding='utf8')
            self.assertEqual(self.workspace.network_state()['policy']['code'],'pac_file_invalid')
            with self.assertRaisesRegex(LocalProxyError,'pac_revoked'):old.for_host('example.com')
            self.auto()

    def test_missing_engine_blocks_before_target_dns_and_rollback_stays_available(self):
        with patch.object(pac_url_worker,'available',return_value=True):self.save()
        with patch.object(pac_url_worker,'available',return_value=False):
            with self.assertRaises(InputError):self.save()
            with patch('socket.getaddrinfo') as dns:
                with self.assertRaisesRegex(FetchError,'pac_domain_unavailable') as failure:
                    SafeHTTP({'example.com'},interval=0,network_policy=self.workspace.network_policy()).request('https://example.com/private')
            from vibe_job_radar.guided.diagnostic_trace import DiagnosticTrace
            trace=DiagnosticTrace('a'*32,'liepin')
            frame=trace.begin('network_policy','transport');trace.finish(frame,failure.exception)
            first=trace.snapshot()['first_failure']
            self.assertEqual(first['code'],'pac_domain_unavailable')
            self.assertIs(first['local_block'],True)
            self.assertEqual(first['policy'],'pac_domain_component')
            dns.assert_not_called();self.assertEqual(self.auto()['proxy_mode'],'auto')

    def test_dns_preferences_preserve_grant_while_reimport_rollback_and_owner_close_revoke(self):
        with patch.object(pac_url_worker,'available',return_value=True),patch.object(pac_url_worker,'evaluate',return_value='DIRECT'):
            first=self.save();old=self.workspace.network_policy()
            self.workspace.network_preferences(dict(mode='public_doh',revision=first['revision'],consent=True))
            self.assertEqual(read_settings(self.workspace)['pac_id'],first['pac_id'])
            self.assertIsNone(old.for_host('example.com'))
            self.save()
            with self.assertRaisesRegex(LocalProxyError,'pac_revoked'):old.for_host('example.com')
            old=self.workspace.network_policy();self.auto()
            with self.assertRaisesRegex(LocalProxyError,'pac_revoked'):old.for_host('example.com')
            self.save();old=self.workspace.network_policy();self.workspace.close_network()
            with self.assertRaisesRegex(LocalProxyError,'pac_revoked'):old.for_host('example.com')

    def test_actual_worker_dns_needs_separate_scope(self):
        if not pac_url_worker.available():
            with self.assertRaises(InputError):self.save()
            return
        script='function FindProxyForURL(url,host){return url==="https://example.com/" && isInNet(dnsResolve("localhost"),"127.0.0.0","255.0.0.0") ? "DIRECT":"INVALID";}'
        self.save(script)
        with self.assertRaisesRegex(LocalProxyError,'pac_invalid_script'):self.workspace.network_policy().for_host('example.com')
        self.save(script,dns=True)
        self.assertIsNone(self.workspace.network_policy().for_request('https://example.com/private?q=secret').for_host('example.com'))

    def test_actual_http_and_socks_share_domain_route_preserve_tls_and_never_fallback(self):
        import test_loopback_proxy as http_fixture
        import test_loopback_socks as socks_fixture
        if not pac_url_worker.available():
            with self.assertRaises(InputError):self.save()
            return
        for keyword,cls in (('PROXY',http_fixture.RealProxyTests),('SOCKS5',socks_fixture.RealSocksTests)):
            fixture=cls();fixture.setUp()
            try:
                raw=keyword+' '+fixture.setting.split('://',1)[1]+'; DIRECT'
                script='function FindProxyForURL(url,host){return url==='+json.dumps('https://'+http_fixture.HOST+'/')+'?'+json.dumps(raw)+':"UNKNOWN path-leaked:9; DIRECT";}'
                self.save(script);policy=self.workspace.network_policy()
                def request(path):
                    with patch.dict(os.environ):
                        for name in list(os.environ):
                            if name.startswith('VIBE_RADAR_'):os.environ.pop(name)
                        return SafeHTTP({http_fixture.HOST},interval=0,network_policy=policy).json('https://'+http_fixture.HOST+path)
                for path in ('/jobs?secret=one','/detail?secret=two'):
                    self.assertTrue(fixture.perform(lambda:request(path))['ok'])
                self.assertEqual(fixture.sni,[http_fixture.HOST]*2);self.assertEqual(len(fixture.requests),2)
                if keyword=='PROXY':fixture.proxy_status=503
                else:fixture.reply=5
                with self.assertRaises(FetchError):fixture.perform(lambda:request('/jobs'))
                self.assertEqual(len(fixture.requests),2)
                self.assertTrue(all(address==fixture.proxy.server_address for address in fixture.dials))
            finally:fixture.tearDown();fixture.doCleanups()

    def test_actual_api_proof_is_protected_and_never_connects_outside_owned_server(self):
        if not pac_url_worker.available():return
        import http.client
        import socket
        import sys
        from pathlib import Path
        from vibe_job_radar.workbench import LocalServer
        sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
        from domain_pac_acceptance import verify_app,valid_evidence
        server=LocalServer(self.workspace,public_client=None)
        thread=threading.Thread(target=server.serve_forever,kwargs={'poll_interval':.01},daemon=True);thread.start()
        outer=self
        class App:
            def json(self,path,data=None,*,token=True,origin=True):
                connection=http.client.HTTPConnection(server.authority,timeout=15)
                try:
                    headers={'Origin':server.origin if origin else 'https://foreign.invalid','Content-Type':'application/json'}
                    if token:headers['X-Radar-Token']=server.token
                    connection.request('GET' if data is None else 'POST',path,None if data is None else json.dumps(data),headers)
                    response=connection.getresponse()
                    outer.assertEqual(response.status,200 if token and origin else 403)
                    return json.loads(response.read())
                finally:connection.close()
        original=socket.socket.connect;destinations=[]
        def only_owned(sock,address):
            outer.assertEqual(address,('127.0.0.1',server.server_port));destinations.append(address)
            return original(sock,address)
        try:
            with patch('socket.socket.connect',only_owned):
                app=App()
                for endpoint in ('/api/network/pac-domain','/api/network/pac-domain-dns'):
                    data=dict(name='domain.pac',script=SCRIPT,revision=0,consent=True)
                    app.json(endpoint,data,token=False);app.json(endpoint,data,origin=False)
                proof=verify_app(app)
            self.assertTrue(valid_evidence(proof,require_frozen=False));self.assertFalse(valid_evidence(proof))
            self.assertTrue(destinations);self.assertEqual(self.workspace.network_state()['proxy_mode'],'auto')
        finally:server.shutdown();server.server_close();thread.join(5)
