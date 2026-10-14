"""Authored fixtures: real local HTTP and independent device/workspace ledgers."""
import copy
import http.client
import io
import json
import sqlite3
import tempfile
import threading
import unittest
from contextlib import closing
from dataclasses import asdict
from pathlib import Path
from socketserver import ThreadingMixIn
from unittest.mock import patch
from wsgiref.simple_server import WSGIServer,WSGIRequestHandler,make_server

import test_public_hybrid as fixture
from vibe_job_radar.team_store import TeamStore,TeamError
from vibe_job_radar.team_catalog import TeamCatalog
from vibe_job_radar.team_service import TeamApplication,private_json,main as service_main
from vibe_job_radar.team_client import TeamConnection,TeamLedger,AuthenticatedPublicTransport,install_connection,local_state,public_client
from vibe_job_radar.team_sync import TeamSync
from vibe_job_radar.guided.rate import RateLedger,RateLimit,Limits
from vibe_job_radar.guided.contracts import CrawlError
from vibe_job_radar.public_data import PublicDataClient
from vibe_job_radar.public_tasks import PublicTasks
from vibe_job_radar.network import FetchError
from vibe_job_radar.workspace import Workspace


class Quiet(WSGIRequestHandler):
    def log_message(self,*args):pass


class ThreadedWSGI(ThreadingMixIn,WSGIServer):
    daemon_threads=True


class HTTPFixture:
    """Only this test adapter maps the fixed HTTPS origin to artificial loopback.

    The deployed client keeps public-IP TLS validation; no runtime HTTP option.
    """
    def __init__(self,app,clock):
        self.app=app;self.clock=clock;self.lock=threading.Lock();self.calls=[]
        def trusted_proxy_fixture(environ,start):
            # Model the already terminated TLS/proxy envelope, not TLS itself.
            environ['wsgi.url_scheme']='https';environ['HTTP_HOST']='team.fixture.test'
            return app(environ,start)
        self.server=make_server('127.0.0.1',0,trusted_proxy_fixture,server_class=ThreadedWSGI,handler_class=Quiet)
        self.thread=threading.Thread(target=self.server.serve_forever,kwargs={'poll_interval':0.01},daemon=True)
        self.thread.start()
    def close(self):
        self.server.shutdown();self.server.server_close();self.thread.join(3)
    def json(self,url,*,method='GET',headers=None,payload=None,error_codes=None):
        from urllib.parse import urlsplit
        assert urlsplit(url).netloc=='team.fixture.test'
        with self.lock:self.clock[0]+=0.1
        path=urlsplit(url).path
        self.calls.append((method,path,sorted(payload or {})))
        conn=http.client.HTTPConnection('127.0.0.1',self.server.server_port,timeout=5)
        body=json.dumps(payload).encode() if payload is not None else None
        try:
            conn.request(method,path,body=body,headers={**(headers or {}),**({'Content-Type':'application/json'} if body else {})})
            response=conn.getresponse();data=json.loads(response.read())
            if response.status!=200:
                if (error_codes and set(data)=={'error'}
                        and data['error'] in error_codes.get(response.status,())):
                    raise FetchError(data['error'])
                raise FetchError('http_'+str(response.status),retry_after=float(response.getheader('Retry-After') or 0))
            return data
        finally:conn.close()


class TeamTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory(prefix='team-fixture-');self.addCleanup(tmp.cleanup)
        self.root=Path(tmp.name);self.now=[2_000_000_000.0]
        self.store=TeamStore(self.root/'service',clock=lambda:self.now[0]);self.store.initialize()
        self.catalog=TeamCatalog(self.root/'service',clock=lambda:self.now[0]);self.catalog.initialize()
        for member,role in [('alice','admin'),('bob','collector'),('reader','reader')]:
            self.store.member('group',member,role)
        self.store.resource('group','account','liepin',['https://www.liepin.com'])
        for member in ['alice','bob','reader']:
            self.store.grant('group',member,'account');self.store.grant('group',member,'fixture',source=True)
        self.tokens={name:self.store.issue('group',name,name+'-pc') for name in ['alice','bob','reader']}
        self.catalog.publish(fixture.REGISTRY,[fixture.job('one'),fixture.job('two')],observed_at={'fixture':self.now[0]})
        self.app=TeamApplication(self.root/'service',host='team.fixture.test',clock=lambda:self.now[0])
        self.http=HTTPFixture(self.app,self.now);self.addCleanup(self.http.close)

    def connection(self,member,*,public=False):
        workspace=self.root/member;workspace.mkdir(exist_ok=True)
        config=self.root/(member+'-config.json');credential=self.root/(member+'-credential.json')
        if not config.exists():
            private_json(config,{'schema_version':1,'origin':'https://team.fixture.test','quota_resources':{'liepin':'account'},
                          'public_sources':[asdict(fixture.SOURCE)],'use_public_service':public})
            private_json(credential,{'schema_version':1,'device_token':self.tokens[member]})
            install_connection(workspace,config,credential)
        return TeamConnection(workspace,transport=self.http)

    def call(self,path,token=None,*,data=None,raw=None,**extra):
        body=raw if raw is not None else json.dumps(data).encode() if data is not None else b''
        env={'wsgi.url_scheme':'https','HTTP_HOST':'team.fixture.test','PATH_INFO':path,'REQUEST_METHOD':'POST' if body else 'GET',
             'CONTENT_TYPE':'application/json','CONTENT_LENGTH':str(len(body)),'wsgi.input':io.BytesIO(body)}
        if token is not None:env['HTTP_AUTHORIZATION']='Bearer '+token
        env.update(extra);status=[];self.now[0]+=0.1
        result=b''.join(self.app(env,lambda s,h:status.append((s,dict(h)))))
        return int(status[0][0].split()[0]),json.loads(result),status[0][1]

    def test_failed_service_initialization_is_retryable_and_preserves_existing_files(self):
        from vibe_job_radar.team_service import initialize_service
        for component in [TeamStore,TeamCatalog]:
            root=self.root/('init-'+component.__name__)
            root.mkdir();note=root/'operator-note.txt';note.write_text('keep',encoding='utf-8')
            original=component.initialize
            def initialize_then_fail(instance):
                original(instance);raise OSError('fixture disk full')
            with patch.object(component,'initialize',initialize_then_fail):
                with self.assertRaises(OSError):initialize_service(root)
            self.assertEqual(sorted(p.name for p in root.iterdir() if p.name!='.writer.lock'),['operator-note.txt'])
            initialize_service(root)
            TeamApplication(root,host='team.fixture.test')
            before={p.name:p.read_bytes() for p in root.iterdir() if p.is_file() and p.name!='.writer.lock'}
            with self.assertRaisesRegex(TeamError,'team_already_initialized'):initialize_service(root)
            self.assertEqual({p.name:p.read_bytes() for p in root.iterdir() if p.is_file() and p.name!='.writer.lock'},before)
            self.assertEqual(note.read_text(encoding='utf-8'),'keep')

    def test_anonymous_requests_do_not_consume_authenticated_capacity(self):
        for _ in range(2001):
            self.assertEqual(self.call('/v1/team/state')[0],401)
        self.assertEqual(self.call('/v1/team/state','invalid-token')[0],401)
        self.assertEqual(self.call('/v1/team/state',self.tokens['alice'],HTTP_ORIGIN='https://untrusted.test')[0],400)
        self.assertEqual(self.call('/healthz')[0],200)
        self.assertEqual(len(self.app._arrivals),0)
        self.assertEqual(self.connection('alice').request('/v1/team/state')['member'],'alice')
        self.assertEqual(len(self.app._arrivals),1)

    def test_health_and_authenticated_capacity_are_independent(self):
        import time
        self.app._health_arrivals.extend([time.monotonic()]*2000)
        self.assertEqual(self.call('/healthz')[0],429)
        self.assertEqual(self.call('/v1/team/state',self.tokens['alice'])[0],200)
        self.app._arrivals.extend([time.monotonic()]*2000)
        status,body,headers=self.call('/v1/team/state',self.tokens['alice'])
        self.assertEqual((status,body['error'],headers['Retry-After']),(429,'team_capacity_wait','60'))
        self.assertEqual(self.call('/v1/team/state')[0],401)
        self.app._health_arrivals.clear()
        self.assertEqual(self.call('/healthz')[0],200)

    def test_backup_parent_sync_failure_retracts_only_the_published_directory(self):
        from vibe_job_radar.team_service import sync_directory
        for replacement in [False,True]:
            output=self.root/('backup-parent-'+str(replacement));displaced=self.root/'operator-moved-backup'
            def fail_parent(path):
                if path==output.parent:
                    self.assertEqual(sorted(p.name for p in output.iterdir()),['catalog.sqlite','control.sqlite','rates.sqlite'])
                    if replacement:
                        output.rename(displaced);output.mkdir()
                        (output/'operator-note.txt').write_text('keep',encoding='utf-8')
                    raise OSError('fixture parent sync failure')
                return sync_directory(path)
            with patch('vibe_job_radar.team_service.TeamStore',return_value=self.store),patch('vibe_job_radar.team_service.TeamCatalog',return_value=self.catalog),patch('sys.stdout',new=io.StringIO()),patch('sys.stderr',new=io.StringIO()):
                args=['--data',str(self.store.root),'backup','--output',str(output)]
                with patch('vibe_job_radar.team_service.sync_directory',side_effect=fail_parent):
                    self.assertEqual(service_main(args),2)
                self.assertEqual(list(output.parent.glob('.team-backup-*')),[])
                if replacement:
                    self.assertEqual((output/'operator-note.txt').read_text(encoding='utf-8'),'keep')
                    self.assertEqual(sorted(p.name for p in displaced.iterdir()),['catalog.sqlite','control.sqlite','rates.sqlite'])
                else:
                    self.assertFalse(output.exists())
                    self.assertEqual(service_main(args),0)
                    self.assertEqual(sorted(p.name for p in output.iterdir()),['catalog.sqlite','control.sqlite','rates.sqlite'])

    def test_backup_failure_never_publishes_partial_output_and_same_path_can_retry(self):
        original_connect=sqlite3.connect;original_chmod=Path.chmod
        for failure in ['database','chmod']:
            output=self.root/('backup-'+failure);observed=[]
            def connect(path,*args,**kwargs):
                if isinstance(path,Path) and path.parent.name.startswith('.team-backup-') and path.name=='rates.sqlite':
                    observed.append((path.parent/'control.sqlite').is_file())
                    if failure=='database':raise sqlite3.OperationalError('fixture disk full')
                return original_connect(path,*args,**kwargs)
            def chmod(path,*args,**kwargs):
                if path.parent.name.startswith('.team-backup-') and path.name=='rates.sqlite' and failure=='chmod':
                    raise OSError('fixture chmod failed')
                return original_chmod(path,*args,**kwargs)
            with patch('vibe_job_radar.team_service.TeamStore',return_value=self.store),patch('vibe_job_radar.team_service.TeamCatalog',return_value=self.catalog),patch('sys.stdout',new=io.StringIO()),patch('sys.stderr',new=io.StringIO()):
                args=['--data',str(self.store.root),'backup','--output',str(output)]
                with patch('vibe_job_radar.team_service.sqlite3.connect',side_effect=connect),patch.object(Path,'chmod',chmod):
                    self.assertEqual(service_main(args),2)
                self.assertEqual(observed,[True]);self.assertFalse(output.exists())
                self.assertEqual(list(output.parent.glob('.team-backup-*')),[])
                self.assertEqual(service_main(args),0)
                self.assertEqual(sorted(p.name for p in output.iterdir()),['catalog.sqlite','control.sqlite','rates.sqlite'])
                before={p.name:p.read_bytes() for p in output.iterdir()}
                self.assertEqual(service_main(args),2)
                self.assertEqual({p.name:p.read_bytes() for p in output.iterdir()},before)

    def test_failed_initial_database_publication_removes_only_its_own_links(self):
        from vibe_job_radar.team_service import initialize_service
        import os
        root=self.root/'init-links';original=os.link;calls=[]
        def fail_second(source,destination):
            calls.append(str(destination))
            if len(calls)==2:raise OSError('fixture link failure')
            return original(source,destination)
        with patch('vibe_job_radar.team_service.os.link',side_effect=fail_second):
            with self.assertRaises(OSError):initialize_service(root)
        self.assertEqual([p for p in root.iterdir() if p.name!='.writer.lock'],[])
        with patch('sys.stdout',new=io.StringIO()):
            self.assertEqual(service_main(['--data',str(root),'init']),0)
        TeamApplication(root,host='team.fixture.test')

    def test_two_http_devices_share_account_budget_and_local_denial_is_not_charged(self):
        a=self.connection('alice');b=self.connection('bob')
        la=TeamLedger(self.root/'alice/rates.sqlite',a,clock=lambda:self.now[0])
        lb=TeamLedger(self.root/'bob/rates.sqlite',b,clock=lambda:self.now[0])
        la.reserve('liepin','page',origin='https://www.liepin.com')
        with self.assertRaises(RateLimit):lb.reserve('liepin','page',origin='https://www.liepin.com')
        self.assertEqual(lb.summary('liepin')['page']['day'],0)
        calls=len(self.http.calls)
        with self.assertRaises(RateLimit):la.reserve('liepin','page')
        self.assertEqual(len(self.http.calls),calls)  # local denial before service
        self.now[0]+=16;lb.reserve('liepin','page')
        self.assertEqual(b.quota('liepin','summary')['page']['day'],2)
        restarted=TeamApplication(self.root/'service',host='team.fixture.test',clock=lambda:self.now[0])
        self.assertEqual(restarted.store.quota(self.tokens['bob'],{'resource':'account','site':'liepin','operation':'summary'})['result']['page']['day'],2)

    def test_role_tenant_resource_origin_and_unknown_fields_cannot_widen_scope(self):
        base={'resource':'account','site':'liepin','operation':'reserve','kind':'request','origin':'https://www.liepin.com'}
        self.assertEqual(self.call('/v1/team/quota',self.tokens['reader'],data=base)[0],403)
        for change in ({'site':'boss'},{'resource':'ungranted'},{'origin':'https://evil.test'},{'tenant':'elsewhere'}):
            status,body,_=self.call('/v1/team/quota',self.tokens['bob'],data={**base,**change})
            self.assertIn(status,{400,403});self.assertNotIn('evil.test',json.dumps(body))
        self.store.member('other','bob','collector')
        other=self.store.issue('other','bob','other-pc')
        self.assertEqual(self.call('/v1/team/quota',other,data=base)[0],403)
        self.assertEqual(self.call('/v1/jobs/search',self.tokens['bob'],data=fixture.query(source_scope=['other']).payload())[0],403)
        self.assertEqual(self.call('/v1/team/metrics',self.tokens['bob'])[0],403)
        self.assertEqual(self.call('/v1/team/metrics',self.tokens['alice'])[0],200)

    def test_metrics_count_only_devices_of_active_members_in_the_same_tenant(self):
        admin=self.store.issue('group','alice','admin-long',days=90)
        self.store.issue('group','reader','short-device',days=1)
        self.store.member('other','bob','collector')
        self.store.issue('other','bob','other-bob')
        client=self.connection('alice')
        self.assertEqual(client.request('/v1/team/metrics')['devices'],5)
        self.store.member('group','bob','collector',active=False)
        self.assertEqual(client.request('/v1/team/metrics')['devices'],4)
        self.assertEqual(self.call('/v1/team/state',self.tokens['bob'])[0],401)
        self.store.revoke('group','reader-pc')
        self.assertEqual(client.request('/v1/team/metrics')['devices'],3)
        self.now[0]+=2*86400
        self.assertEqual(self.call('/v1/team/metrics',admin)[1]['devices'],2)
        self.store.member('group','bob','collector',active=True)
        self.assertEqual(self.call('/v1/team/metrics',admin)[1]['devices'],3)
        self.assertEqual(self.call('/v1/team/state',self.tokens['bob'])[0],200)

    def test_revocation_expiry_and_member_disable_apply_to_existing_clients(self):
        client=self.connection('alice');self.assertEqual(client.request('/v1/team/state')['member'],'alice')
        self.store.revoke('group','alice-pc')
        with self.assertRaisesRegex(CrawlError,'team_unauthorized'):client.request('/v1/team/state')
        self.store.member('group','bob','collector',active=False)
        self.assertEqual(self.call('/v1/team/state',self.tokens['bob'])[0],401)
        self.now[0]+=91*86400
        self.assertEqual(self.call('/v1/team/state',self.tokens['reader'])[0],401)

    def test_expired_or_deleted_rate_storage_fails_without_reinitializing(self):
        self.store.rate_path.write_bytes(b'')
        code,body,_=self.call('/v1/team/state',self.tokens['alice'])
        self.assertEqual(code,503);self.assertEqual(self.store.rate_path.stat().st_size,0)
        with self.assertRaises(TeamError):TeamApplication(self.root/'service',host='team.fixture.test')

    def test_connection_loss_blocks_upstream_and_retains_prior_local_quotas(self):
        client=self.connection('alice');ledger=TeamLedger(self.root/'alice/rates.sqlite',client,clock=lambda:self.now[0])
        ledger.reserve('liepin','page');self.now[0]+=16
        with patch.object(self.http,'json',side_effect=FetchError('network_error')):
            with self.assertRaisesRegex(CrawlError,'team_unavailable'):ledger.reserve('liepin','page')
        self.assertEqual(ledger.summary('liepin')['page']['day'],1)
        self.assertEqual(client.quota('liepin','summary')['page']['day'],1)

    def test_cooldown_and_publisher_rules_propagate_between_devices(self):
        a=self.connection('alice');b=self.connection('bob')
        la=TeamLedger(self.root/'alice/rates.sqlite',a,clock=lambda:self.now[0]);lb=TeamLedger(self.root/'bob/rates.sqlite',b,clock=lambda:self.now[0])
        la.set_publisher('liepin','https://www.liepin.com',delay=20)
        la.reserve('liepin','request',origin='https://www.liepin.com')
        self.now[0]+=1
        with self.assertRaises(RateLimit):lb.reserve('liepin','request',origin='https://www.liepin.com')
        self.now[0]+=20;la.cool('liepin',600)
        with self.assertRaises(RateLimit):lb.reserve('liepin','page')

    def test_request_boundary_and_duplicate_json_reject_before_data_access(self):
        for extra in ({'wsgi.url_scheme':'http'},{'HTTP_HOST':'attacker.test'},{'HTTP_COOKIE':'SECRET'},
                      {'HTTP_ORIGIN':'https://attacker.test'},{'QUERY_STRING':'secret=SECRET'}):
            status,body,_=self.call('/v1/team/state',self.tokens['alice'],**extra)
            self.assertEqual(status,400);self.assertNotIn('SECRET',json.dumps(body))
        self.assertEqual(self.call('/v1/team/state')[0],401)
        self.assertEqual(self.call('/v1/sync/ack',self.tokens['alice'],raw=b'{"source_scope":["fixture"],"revision":1,"revision":2}')[0],400)
        self.assertEqual(self.call('/healthz')[0],200)

    def test_two_devices_full_delta_and_acknowledgements_are_independent(self):
        a=self.connection('alice');b=self.connection('bob')
        first=TeamSync(self.root/'alice',connection=a).run();self.assertEqual(first['jobs'],2);self.assertTrue(first['acknowledged'])
        self.assertEqual(b.request('/v1/team/state')['watermarks'],[])
        self.catalog.publish(fixture.REGISTRY,[fixture.job('one',title='Updated Architect'),fixture.job('three')],observed_at={'fixture':self.now[0]})
        second=TeamSync(self.root/'alice',connection=a).run()
        self.assertEqual(second['mode'],'changes');self.assertEqual(second['jobs'],2);self.assertTrue(second['acknowledged'])
        other=TeamSync(self.root/'bob',connection=b).run();self.assertEqual(other['mode'],'snapshot');self.assertEqual(other['revision'],second['revision'])
        left=json.loads((self.root/'alice/team_sync/snapshot.json').read_text(encoding='utf-8'))
        right=json.loads((self.root/'bob/team_sync/snapshot.json').read_text(encoding='utf-8'))
        self.assertEqual(sorted(left['jobs'],key=lambda j:j['id']),sorted(right['jobs'],key=lambda j:j['id']))
        self.assertEqual({j['id'] for j in left['jobs']},{'one','three'})

    def test_watermark_cannot_advance_without_a_completed_authorized_read(self):
        value={'source_scope':['fixture'],'revision':1}
        self.assertEqual(self.call('/v1/sync/ack',self.tokens['alice'],data=value)[0],409)
        status,first,_=self.call('/v1/sync/read',self.tokens['alice'],data={'source_scope':['fixture'],'limit':1})
        self.assertEqual(status,200);self.assertFalse(first['complete'])
        self.assertEqual(self.call('/v1/sync/ack',self.tokens['alice'],data=value)[0],409)
        self.assertEqual(self.call('/v1/sync/read',self.tokens['bob'],data={'source_scope':['fixture'],'limit':1,'cursor':first['next_cursor']})[0],409)
        self.catalog.publish(fixture.REGISTRY,[fixture.job('one')],observed_at={'fixture':self.now[0]})
        self.assertEqual(self.call('/v1/sync/read',self.tokens['alice'],data={'source_scope':['fixture'],'limit':1,'cursor':first['next_cursor']})[0],409)

    def test_failed_partial_sync_preserves_the_previous_complete_cache(self):
        client=self.connection('alice');sync=TeamSync(self.root/'alice',connection=client)
        sync.run();path=self.root/'alice/team_sync/snapshot.json';before=path.read_bytes()
        self.catalog.publish(fixture.REGISTRY,[fixture.job('new')],observed_at={'fixture':self.now[0]})
        original=client.request
        def malformed(path,*,payload=None):
            value=original(path,payload=payload)
            if path=='/v1/sync/read':value['events'][0]['job']={'source':'fixture','id':'new','password':'SECRET'}
            return value
        with patch.object(client,'request',side_effect=malformed),self.assertRaises(ValueError):sync.run()
        self.assertEqual(path.read_bytes(),before)

    def test_reset_recovers_damaged_cache_only_after_a_valid_download(self):
        client=self.connection('alice');sync=TeamSync(self.root/'alice',connection=client)
        sync.run();path=self.root/'alice/team_sync/snapshot.json'
        for old in (b'{',b'{}',b'null'):
            with self.subTest(old=old):
                path.write_bytes(old)
                with patch.object(client,'request',side_effect=TeamError('team_unavailable')):
                    with self.assertRaises(TeamError):sync.run(reset=True)
                self.assertEqual(path.read_bytes(),old)
                self.assertEqual(sync.run(reset=True)['jobs'],2)
        # An oversized corrupt file is rejected normally but explicit reset does
        # not read it into memory or require manual removal.
        with path.open('wb') as stream:stream.truncate(51_000_001)
        with self.assertRaisesRegex(TeamError,'team_sync_cache_invalid'):sync.run()
        self.assertEqual(sync.run(reset=True)['jobs'],2)
        self.assertLess(path.stat().st_size,10000)

    def test_sync_uses_server_envelope_with_bounded_skew_and_persists_it(self):
        from datetime import datetime,timedelta,timezone
        local=datetime.now(timezone.utc);server=local+timedelta(seconds=120)
        job=fixture.job('ahead');job['collected_at']=(local+timedelta(seconds=90)).isoformat()
        with patch('vibe_job_radar.public_gateway.utc_now',return_value=server.isoformat()):
            self.catalog.publish(fixture.REGISTRY,[job],observed_at={'fixture':self.now[0]})
        client=self.connection('alice');sync=TeamSync(self.root/'alice',connection=client,clock=local.timestamp)
        with patch('vibe_job_radar.team_catalog.utc_now',return_value=server.isoformat()):
            self.assertEqual(sync.run()['jobs'],1)
        path=self.root/'alice/team_sync/snapshot.json'
        saved=json.loads(path.read_text(encoding='utf-8'))
        self.assertEqual(saved['generated_at'],server.isoformat());self.assertEqual(saved['schema_version'],2)
        # Replay the cached future-to-this-device row using its validated envelope.
        self.assertEqual(sync.run()['mode'],'changes')
        before=path.read_bytes()
        with patch('vibe_job_radar.team_catalog.utc_now',return_value=(local+timedelta(seconds=301)).isoformat()):
            with self.assertRaisesRegex(TeamError,'team_sync_timestamp_invalid'):sync.run(reset=True)
        self.assertEqual(path.read_bytes(),before)
        # A row collected after its own server envelope is still invalid.
        with patch('vibe_job_radar.team_catalog.utc_now',return_value=local.isoformat()):
            with self.assertRaisesRegex(ValueError,'public_timestamp_invalid'):sync.run(reset=True)
        self.assertEqual(path.read_bytes(),before)

    def test_original_sync_cache_migrates_without_losing_jobs(self):
        client=self.connection('alice');sync=TeamSync(self.root/'alice',connection=client)
        sync.run();path=self.root/'alice/team_sync/snapshot.json'
        saved=json.loads(path.read_text(encoding='utf-8'));saved.pop('generated_at');saved['schema_version']=1
        path.write_text(json.dumps(saved),encoding='utf-8')
        self.assertEqual(sync.run()['mode'],'changes')
        upgraded=json.loads(path.read_text(encoding='utf-8'))
        self.assertEqual(upgraded['schema_version'],2);self.assertEqual(upgraded['jobs'],saved['jobs'])

    def test_unchanged_observation_keeps_full_and_delta_clients_convergent(self):
        from datetime import timedelta
        from vibe_job_radar.utils import parse_time
        a=self.connection('alice');b=self.connection('bob')
        TeamSync(self.root/'alice',connection=a).run()
        original=self.catalog.gateway().jobs;updated=copy.deepcopy(original)
        for job in updated:job['collected_at']=(parse_time(job['collected_at'])+timedelta(seconds=1)).isoformat()
        self.now[0]+=16;observed=self.now[0]
        result=self.catalog.publish(fixture.REGISTRY,updated,observed_at={'fixture':observed})
        self.assertEqual(result['changed'],0)
        delta=TeamSync(self.root/'alice',connection=a).run()
        full=TeamSync(self.root/'bob',connection=b).run()
        self.assertEqual(delta['revision'],full['revision']);self.assertTrue(delta['acknowledged'])
        for member in ('alice','bob'):
            saved=json.loads((self.root/member/'team_sync/snapshot.json').read_text(encoding='utf-8'))
            self.assertEqual(saved['jobs'],original)
        self.assertEqual(self.catalog.source_state(['fixture'])[0]['observed_at'],observed)

    def test_aggregate_capacity_includes_gateway_framing_across_sources(self):
        from dataclasses import replace
        from vibe_job_radar.team_catalog import encoded
        from vibe_job_radar.public_gateway import encode_snapshot
        before=self.catalog.gateway().jobs
        source=replace(fixture.SOURCE,key='another');row={**fixture.job('three'),'source':'another'}
        aggregate=before+[row]
        compact=sum(len(encoded(job).encode('utf-8')) for job in aggregate)
        limit=(compact+len(encode_snapshot(aggregate)))//2
        self.assertLess(compact,limit)
        with patch('vibe_job_radar.team_catalog.MAX_SNAPSHOT_BYTES',limit):
            with self.assertRaisesRegex(TeamError,'team_snapshot_too_large'):
                self.catalog.publish({'another':source},[row],observed_at={'another':self.now[0]})
        self.assertEqual(self.catalog.gateway().jobs,before)
        with self.catalog.connection() as (conn,meta):
            self.assertEqual(meta['revision'],'1');self.assertNotIn('another',self.catalog.registry(conn))

    def test_maximum_configured_source_scope_syncs_through_bounded_cursors(self):
        from dataclasses import replace
        self.connection('alice');registry={};jobs=[]
        for index in range(100):
            key='s'+str(index).zfill(3)+'x'*96
            registry[key]=replace(fixture.SOURCE,key=key)
            jobs.append({**fixture.job('job'),'source':key})
            self.store.grant('group','alice',key,source=True)
        self.catalog.publish(registry,jobs,observed_at={key:self.now[0] for key in registry})
        config=self.root/'many-config.json';credential=self.root/'alice-credential.json'
        private_json(config,{'schema_version':1,'origin':'https://team.fixture.test','quota_resources':{},
                             'public_sources':[asdict(source) for source in registry.values()],'use_public_service':True})
        workspace=self.root/'many-sources';install_connection(workspace,config,credential)
        client=TeamConnection(workspace,transport=self.http);cursors=[];original=client.request
        def read(path,*,payload=None):
            response=original(path,payload=payload)
            if path=='/v1/sync/read':cursors.append(response['next_cursor'])
            return response
        with patch.object(client,'request',side_effect=read):
            result=TeamSync(workspace,connection=client).run()
        self.assertEqual(result['jobs'],100);self.assertTrue(result['acknowledged'])
        self.assertEqual(len(cursors),2);self.assertTrue(cursors[0]);self.assertEqual(cursors[-1],'')
        self.assertLess(len(cursors[0]),4096)
        self.assertEqual(len(key),100)
        self.assertEqual(self.catalog.withdraw(key)['withdrawn'],key)
        with self.assertRaises(TeamError):self.store.grant('group','alice',key+'x',source=True)
        with self.assertRaises(TeamError):self.catalog.withdraw(key+'x')
        with self.assertRaises(TeamError):self.store.grant('group','alice',key)

    def test_public_query_enters_the_original_task_and_report_pipeline(self):
        import time
        connection=self.connection('alice',public=True);workspace=Workspace(self.root/'alice')
        client=PublicDataClient(workspace.root/'team_public_cache',connection.origin,connection.registry,
                                transport=AuthenticatedPublicTransport(connection))
        tasks=PublicTasks(workspace,hybrid_client=client);self.addCleanup(tasks.close)
        tasks.search({'consent':True,'query':fixture.query().payload()})
        deadline=time.monotonic()+20
        while time.monotonic()<deadline:
            state=tasks.snapshot()
            if state['status'] not in {'running','queued'}:break
            time.sleep(0.01)
        self.assertEqual(state['status'],'completed',state)
        self.assertEqual(workspace.status()['counts']['full_text'],2)
        self.assertTrue(workspace.status()['runs'])
        self.assertTrue(workspace.report(state['report_id'])['manifest'])
        transmitted=json.dumps(self.http.calls)
        self.assertNotIn('password',transmitted);self.assertNotIn('resume',transmitted)
        for file in workspace.root.rglob('*.json'):
            if '.team' not in file.parts:self.assertNotIn(self.tokens['alice'],file.read_text(encoding='utf-8'))

    def test_optional_configuration_is_offline_and_cannot_silently_fall_back(self):
        connection=self.connection('alice',public=True)
        with patch('socket.socket.connect',side_effect=AssertionError('state must be offline')):
            self.assertTrue(local_state(self.root/'alice')['configured'])
            self.assertIsInstance(public_client(Workspace(self.root/'alice')),PublicDataClient)
        (self.root/'alice/.team/connection.json').write_text('{}',encoding='utf-8')
        self.assertIn('error',local_state(self.root/'alice'))
        unavailable=public_client(Workspace(self.root/'alice'))
        self.assertEqual(unavailable.execution_mode,'remote_service')
        with self.assertRaises(ValueError):unavailable._scope(fixture.query())
        with self.assertRaises(TeamError):connection.request('/v1/team/state')

    def test_member_erasure_keeps_same_named_resource_source_and_operator_audits(self):
        self.store.member('group','operator','reader')
        self.store.issue('group','operator','operator-pc')
        for member in ['alice','operator']:
            for key in [member,member+'-pc']:
                self.store.resource('group',key,'liepin',['https://www.liepin.com'])
                self.store.grant('group','bob',key)
                self.store.grant('group','bob',key,source=True)
            with self.store.connection() as conn:
                before=[tuple(r) for r in conn.execute("SELECT * FROM audit WHERE action NOT IN ('member_changed','device_issued','device_revoked') ORDER BY seq")]
            with patch('sys.stdout',new=io.StringIO()),patch('vibe_job_radar.team_service.TeamStore',return_value=self.store):
                self.assertEqual(service_main(['--data',str(self.store.root),'erase-member','--tenant','group','--member',member]),0)
            with self.store.connection() as conn:
                after=[tuple(r) for r in conn.execute("SELECT * FROM audit WHERE action NOT IN ('member_changed','device_issued','device_revoked') ORDER BY seq")]
                self.assertEqual(after,before)
                self.assertIsNone(conn.execute("SELECT 1 FROM audit WHERE action='member_changed' AND target=?",(member,)).fetchone())
                self.assertIsNone(conn.execute("SELECT 1 FROM audit WHERE action IN ('device_issued','device_revoked') AND target=?",(member+'-pc',)).fetchone())
                self.assertIsNotNone(conn.execute("SELECT 1 FROM audit WHERE action='member_changed' AND target='bob'").fetchone())

    def test_device_sync_scope_history_is_bounded_and_evicted_scope_can_resume(self):
        token=self.tokens['alice'];scopes=[]
        self.store.sync_seen(self.tokens['bob'],['fixture'],1)
        self.store.sync_seen(self.tokens['bob'],['fixture'],1,acknowledge=True)
        for index in range(65):
            scope=['scope-'+str(index)];scopes.append(scope)
            self.store.grant('group','alice',scope[0],source=True)
            self.store.sync_seen(token,scope,1)
            self.store.sync_seen(token,scope,1,acknowledge=True)
        with self.store.connection() as conn:
            for table in ['sync_views','watermarks']:
                self.assertEqual(conn.execute('SELECT COUNT(*) FROM '+table+' WHERE device=?',('alice-pc',)).fetchone()[0],64)
                self.assertEqual(conn.execute('SELECT COUNT(*) FROM '+table+' WHERE device=?',('bob-pc',)).fetchone()[0],1)
        with self.assertRaisesRegex(TeamError,'team_watermark_not_read'):
            self.store.sync_seen(token,scopes[0],1,acknowledge=True)
        self.store.sync_seen(token,scopes[1],2)  # Refresh its position before adding a scope.
        self.store.sync_seen(token,scopes[0],1)
        self.assertTrue(self.store.sync_seen(token,scopes[0],1,acknowledge=True)['acknowledged'])
        self.assertTrue(self.store.sync_seen(token,scopes[1],2,acknowledge=True)['acknowledged'])
        with self.assertRaisesRegex(TeamError,'team_watermark_not_read'):
            self.store.sync_seen(token,scopes[2],1,acknowledge=True)
        self.store.sync_seen(token,scopes[1],1)  # A replay cannot lower the stored revision.
        self.assertTrue(self.store.sync_seen(token,scopes[1],2,acknowledge=True)['acknowledged'])

    def test_member_erasure_revokes_tokens_without_refunding_resource_quota(self):
        client=self.connection('alice');client.reserve('liepin','page')
        with patch('sys.stdout',new=io.StringIO()),patch('vibe_job_radar.team_service.TeamStore',return_value=self.store):
            result=service_main(['--data',str(self.root/'service'),'erase-member','--tenant','group','--member','alice'])
        self.assertEqual(result,0);self.assertEqual(self.call('/v1/team/state',self.tokens['alice'])[0],401)
        self.assertEqual(self.store.quota(self.tokens['bob'],{'resource':'account','site':'liepin','operation':'summary'})['result']['page']['day'],1)

    def test_operator_cannot_silently_relax_limits_or_rebind_existing_account(self):
        self.store.resource('group','account','liepin',['https://www.liepin.com'],limits=Limits(pages_day=10))
        with self.assertRaises(TeamError):self.store.resource('group','account','liepin',['https://www.liepin.com'])
        with self.assertRaises(TeamError):self.store.resource('group','account','boss',['https://www.liepin.com'],limits=Limits(pages_day=10))

    def test_simultaneous_device_reservations_have_only_one_winner(self):
        # Separate database connections contend on the account ledger itself;
        # the HTTP ingress throttle must not supply the losing reservation.
        stores=[TeamStore(self.root/'service',clock=lambda:self.now[0]) for _ in range(2)]
        results=[];barrier=threading.Barrier(2)
        def reserve(store,token):
            barrier.wait()
            try:
                store.quota(token,{'resource':'account','site':'liepin','operation':'reserve','kind':'page'})
                results.append('accepted')
            except RateLimit:results.append('limited')
        threads=[threading.Thread(target=reserve,args=(store,self.tokens[member]))
                 for store,member in zip(stores,('alice','bob'))]
        for thread in threads:thread.start()
        for thread in threads:thread.join(10)
        self.assertTrue(all(not thread.is_alive() for thread in threads));self.assertCountEqual(results,['accepted','limited'])

    def test_withdrawal_stops_search_and_old_delta_delivery(self):
        token=self.tokens['alice']
        self.catalog.withdraw('fixture')
        self.assertEqual(self.call('/v1/jobs/search',token,data=fixture.query().payload())[0],403)
        self.assertEqual(self.call('/v1/sync/read',token,data={'source_scope':['fixture'],'after_revision':1})[0],403)
        with self.catalog.connection() as (conn,meta):
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM jobs').fetchone()[0],0)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM changes').fetchone()[0],0)

    def test_stale_source_and_pruned_delta_require_explicit_refresh(self):
        token=self.store.issue('group','alice','long-lived',days=90)
        self.now[0]+=31*86400
        self.assertEqual(self.call('/v1/jobs/search',token,data=fixture.query().payload())[0],503)
        self.catalog.publish(fixture.REGISTRY,[fixture.job('new')],observed_at={'fixture':self.now[0]})
        # after=1 was pruned; the published observation remains available as a
        # full snapshot while a stale delta cannot be fabricated.
        self.assertEqual(self.call('/v1/sync/read',token,data={'source_scope':['fixture'],'after_revision':1})[0],200)
        self.now[0]+=31*86400
        self.catalog.publish(fixture.REGISTRY,[fixture.job('newest')],observed_at={'fixture':self.now[0]})
        self.assertEqual(self.call('/v1/sync/read',token,data={'source_scope':['fixture'],'after_revision':1})[0],409)

    def test_source_refresh_checks_distribution_before_any_provider_call(self):
        from dataclasses import replace
        from unittest.mock import Mock
        from vibe_job_radar.public_boards import ANTHROPIC
        from vibe_job_radar.team_refresh import refresh_approved
        approval=self.root/'approval.json'
        private_json(approval,{'schema_version':1,'sources':[asdict(ANTHROPIC.source)],'jobs':[]})
        factory=Mock(side_effect=AssertionError('unapproved source must not be contacted'))
        with self.assertRaises(TeamError):refresh_approved(self.root/'service',approval,ANTHROPIC.source.key,client_factory=factory)
        factory.assert_not_called()

    def test_source_refresh_publishes_the_full_validated_catalog(self):
        from dataclasses import replace
        from unittest.mock import Mock
        from vibe_job_radar.local_public import LocalPublicDataClient
        from vibe_job_radar.public_boards import ANTHROPIC
        from vibe_job_radar.team_refresh import refresh_approved
        from test_local_public import payload
        root=self.root/'refresh-service';TeamStore(root).initialize();TeamCatalog(root).initialize()
        approved=replace(ANTHROPIC.source,distribution_approved=True,contract='ARTIFICIAL approval for authored fixtures only')
        approval=self.root/'approved-refresh.json'
        private_json(approval,{'schema_version':1,'sources':[asdict(approved)],'jobs':[]})
        transport=Mock();transport.json.return_value=payload()
        result=refresh_approved(root,approval,ANTHROPIC.source.key,client_factory=lambda workspace:LocalPublicDataClient(workspace,transport=transport))
        self.assertEqual(result['source_requests'],1)
        self.assertEqual(result['jobs'],len(payload()['jobs']))
        self.assertEqual(len(TeamCatalog(root).gateway().jobs),len(payload()['jobs']))
        transport.json.assert_called_once_with(ANTHROPIC.api_url)

    def test_service_token_echo_cannot_be_saved_as_job_content(self):
        connection=self.connection('alice')
        with patch.object(self.http,'json',return_value={'schema_version':1,'text':self.tokens['alice']}):
            with self.assertRaisesRegex(CrawlError,'team_response_invalid'):connection.request('/v1/team/state')

    def test_credential_replacement_requires_restart_instead_of_switching_identity(self):
        a=self.connection('alice');self.connection('bob')
        (self.root/'alice/.team/credential.json').write_bytes((self.root/'bob/.team/credential.json').read_bytes())
        with self.assertRaisesRegex(TeamError,'team_connection_changed'):a.request('/v1/team/state')

    def test_request_reservation_cannot_omit_publisher_origin(self):
        request={'resource':'account','site':'liepin','operation':'reserve','kind':'request'}
        self.assertEqual(self.call('/v1/team/quota',self.tokens['alice'],data=request)[0],400)

    def test_importing_an_old_or_undated_snapshot_does_not_claim_freshness(self):
        self.catalog.publish(fixture.REGISTRY,[fixture.job('old')])
        self.assertEqual(self.call('/v1/jobs/search',self.tokens['alice'],data=fixture.query().payload())[0],503)
        self.catalog.publish(fixture.REGISTRY,[fixture.job('old')],observed_at={'fixture':self.now[0]-8*86400})
        self.assertEqual(self.call('/v1/jobs/search',self.tokens['alice'],data=fixture.query().payload())[0],503)
        with self.assertRaises(TeamError):self.catalog.publish(fixture.REGISTRY,[fixture.job('old')],observed_at={'fixture':self.now[0]+3600})

    def test_public_resources_keep_original_stricter_limits_across_devices(self):
        site='greenhouse_public_example';origin='https://boards-api.greenhouse.io'
        with self.assertRaisesRegex(TeamError,'team_limits_invalid'):
            self.store.resource('group','catalog',site,[origin],limits=Limits())
        self.store.resource('group','catalog',site,[origin])
        for member in ['alice','bob']:self.store.grant('group',member,'catalog')
        request={'resource':'catalog','site':site,'operation':'reserve','kind':'request','origin':origin}
        self.store.quota(self.tokens['alice'],request)
        self.now[0]+=20
        with self.assertRaises(RateLimit):self.store.quota(self.tokens['bob'],request)
        self.now[0]+=10
        self.store.quota(self.tokens['bob'],request)
        resource=next(r for r in self.store.state(self.tokens['bob'])['resources'] if r['id']=='catalog')
        self.assertEqual(resource['limits']['requests_hour'],12)
        self.assertEqual(resource['limits']['requests_day'],24)
        self.assertEqual(resource['usage']['request']['day'],2)

    def test_failed_device_export_rolls_back_and_allows_same_device_id(self):
        output=self.root/'retry-device.json'
        for destination in [self.root/'missing/retry-device.json',output]:
            with patch('vibe_job_radar.team_store.os.fsync',side_effect=OSError('fixture disk failure')):
                with self.assertRaises(OSError):
                    self.store.issue('group','alice','retry-pc',output=destination)
            with self.store.connection() as conn:
                self.assertIsNone(conn.execute('SELECT id FROM devices WHERE id=?',('retry-pc',)).fetchone())
            self.assertFalse(destination.exists())
        token=self.store.issue('group','alice','retry-pc',output=output)
        self.assertEqual(json.loads(output.read_text(encoding='utf-8'))['device_token'],token)
        self.assertEqual(self.store.state(token)['device'],'retry-pc')

    def test_posix_install_syncs_parent_after_activation_and_surfaces_failure(self):
        import os
        from types import SimpleNamespace
        from unittest.mock import Mock
        self.connection('alice')
        for fail in [False,True]:
            workspace=self.root/('install-failure' if fail else 'install-durable')
            def barrier(descriptor):
                self.assertTrue((workspace/'.team/enrolled.json').is_file())
                self.assertTrue((workspace/'.team/credential.json').is_file())
                if fail:raise OSError('fixture parent fsync failure')
            posix=SimpleNamespace(name='posix',O_RDONLY=os.O_RDONLY,O_DIRECTORY=getattr(os,'O_DIRECTORY',0),
                                  open=Mock(return_value=123),fsync=Mock(side_effect=barrier),close=Mock())
            with patch('vibe_job_radar.team_client.os',posix):
                if fail:
                    with self.assertRaises(OSError):
                        install_connection(workspace,self.root/'alice-config.json',self.root/'alice-credential.json')
                else:self.assertTrue(install_connection(workspace,self.root/'alice-config.json',self.root/'alice-credential.json')['configured'])
            posix.open.assert_called_once_with(workspace,posix.O_RDONLY|posix.O_DIRECTORY)
            posix.fsync.assert_called_once_with(123);posix.close.assert_called_once_with(123)
            # Failure is reported; an activated connection is never removed as a
            # fallback to local quotas, even when durability cannot be confirmed.
            self.assertTrue((workspace/'.team').is_dir())

    def test_failed_install_never_activates_partial_connection_and_can_retry(self):
        self.connection('alice');destination=self.root/'new-workspace'
        config=self.root/'alice-config.json';credential=self.root/'alice-credential.json'
        with patch('vibe_job_radar.team_store.os.fsync',side_effect=OSError('fixture disk failure')):
            with self.assertRaises(OSError):install_connection(destination,config,credential)
        self.assertFalse(local_state(destination)['configured'])
        self.assertFalse((destination/'.team').exists())
        install_connection(destination,config,credential)
        client=TeamConnection(destination,transport=self.http)
        self.assertEqual(client.request('/v1/team/state')['member'],'alice')

    def test_concurrent_refresh_cannot_lend_freshness_to_older_gateway_rows(self):
        self.now[0]+=8*86400
        original=self.app.catalog.gateway
        def refresh_after_loading(**kwargs):
            gateway=original(**kwargs)
            self.catalog.publish(fixture.REGISTRY,[fixture.job('new')],observed_at={'fixture':self.now[0]})
            return gateway
        # Reproduce a publish at the former gap between loading rows and a
        # separate metadata check. An already stale snapshot must not escape.
        with patch.object(self.app.catalog,'gateway',side_effect=refresh_after_loading):
            for path,data in [('/v1/jobs/search',fixture.query().payload()),('/v1/jobs/fixture.one',None)]:
                status,body,_=self.call(path,self.tokens['alice'],data=data)
                self.assertEqual(status,503);self.assertEqual(body['error'],'team_catalog_stale')

    def test_expired_delta_reaches_client_and_ui_then_explicit_reset_recovers(self):
        from vibe_job_radar.team_client import TeamUI
        self.tokens['alice']=self.store.issue('group','alice','sync-long',days=90)
        client=self.connection('alice');sync=TeamSync(self.root/'alice',connection=client)
        self.assertEqual(sync.run()['revision'],1)
        for key in ['new','newest']:
            self.now[0]+=31*86400
            self.catalog.publish(fixture.REGISTRY,[fixture.job(key)],observed_at={'fixture':self.now[0]})
        with self.assertRaisesRegex(CrawlError,'team_sync_reset_required'):sync.run()
        workspace=Workspace(self.root/'alice');self.addCleanup(workspace.close_network)
        panel=TeamUI(workspace)
        with patch('vibe_job_radar.team_client.TeamConnection',return_value=client):panel._run(False)
        result=panel.state()['sync']
        self.assertEqual(result['code'],'team_sync_reset_required')
        self.assertIn('完整重置',result['message'])
        recovered=sync.run(reset=True)
        self.assertEqual(recovered['revision'],3);self.assertEqual(recovered['jobs'],1)
        self.assertTrue(recovered['acknowledged'])

    def test_health_and_backup_expire_idle_history_without_refunding_policy(self):
        client=self.connection('alice')
        client.quota('liepin','publisher',origin='https://www.liepin.com',delay=20)
        client.reserve('liepin','request',origin='https://www.liepin.com')
        client.quota('liepin','defer',seconds=600)
        with closing(sqlite3.connect(self.store.rate_path)) as rates:
            policies={table:rates.execute('SELECT * FROM '+table).fetchall()
                      for table in ['publisher_policy','cooldown','clock_seen']}
            self.assertGreater(rates.execute('SELECT COUNT(*) FROM publisher_visits').fetchone()[0],0)
        self.now[0]+=3*86400
        self.assertEqual(self.call('/healthz')[0],200)
        with closing(sqlite3.connect(self.store.rate_path)) as rates:
            self.assertEqual(rates.execute('SELECT COUNT(*) FROM visits').fetchone()[0],0)
            self.assertGreater(rates.execute('SELECT COUNT(*) FROM publisher_visits').fetchone()[0],0)
        self.now[0]+=364*86400
        # A still-current publisher visit must survive while an idle old one expires.
        with closing(sqlite3.connect(self.store.rate_path)) as rates:
            rates.execute('INSERT INTO publisher_visits VALUES(?,?,?)',('recent','https://fixture.test',self.now[0]-1));rates.commit()
        output=self.root/'backup'
        with patch('vibe_job_radar.team_service.TeamStore',return_value=self.store),patch('vibe_job_radar.team_service.TeamCatalog',return_value=self.catalog),patch('sys.stdout',new=io.StringIO()):
            self.assertEqual(service_main(['--data',str(self.store.root),'backup','--output',str(output)]),0)
        for root in [self.store.root,output]:
            with closing(sqlite3.connect(root/'rates.sqlite')) as rates:
                self.assertEqual(rates.execute('SELECT site FROM publisher_visits').fetchall(),[('recent',)])
                for table,rows in policies.items():self.assertEqual(rates.execute('SELECT * FROM '+table).fetchall(),rows)
            with closing(sqlite3.connect(root/'catalog.sqlite')) as catalog:
                self.assertEqual(catalog.execute('SELECT COUNT(*) FROM changes').fetchone()[0],0)
                self.assertEqual(catalog.execute('SELECT COUNT(*) FROM jobs').fetchone()[0],2)
                self.assertEqual(catalog.execute("SELECT value FROM meta WHERE key='floor'").fetchone()[0],'1')
        self.now[0]-=86400
        self.assertEqual(self.call('/healthz')[0],503)

    def test_health_read_expires_audit_without_another_management_event(self):
        with closing(sqlite3.connect(self.store.path)) as conn:
            self.assertGreater(conn.execute('SELECT COUNT(*) FROM audit').fetchone()[0],0)
        self.now[0]+=91*86400
        self.assertEqual(self.call('/healthz')[0],200)
        with closing(sqlite3.connect(self.store.path)) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM audit').fetchone()[0],0)

    def test_invalid_service_domains_do_not_activate_or_block_connection_install(self):
        from vibe_job_radar.team_client import origin
        from urllib.parse import urlsplit
        self.connection('alice');destination=self.root/'domain-retry'
        config=self.root/'domain-config.json';credential=self.root/'alice-credential.json'
        saved=json.loads((self.root/'alice-config.json').read_text(encoding='utf-8'))
        for value in ['https://localhost','https://[2606:4700:4700::1111]','https://8.8.8.8',
                      'https://team..test','https://-team.test','https://team.test-']:
            with self.subTest(origin=value):
                config.write_text(json.dumps({**saved,'origin':value}),encoding='utf-8')
                with self.assertRaisesRegex(TeamError,'team_connection_invalid'):
                    install_connection(destination,config,credential)
                self.assertFalse((destination/'.team').exists())
                with self.assertRaisesRegex(TeamError,'team_host_invalid'):
                    TeamApplication(self.root/'service',host=urlsplit(value).hostname)
        self.assertEqual(origin('https://team.fixture.test:443/'),'https://team.fixture.test')
        config.write_text(json.dumps(saved),encoding='utf-8');install_connection(destination,config,credential)
        self.assertEqual(TeamConnection(destination,transport=self.http).request('/v1/team/state')['member'],'alice')

    def test_team_panel_uses_its_workspace_proxy_policy(self):
        import os
        from vibe_job_radar.team_client import TeamUI
        self.connection('alice')
        workspace=Workspace(self.root/'alice');self.addCleanup(workspace.close_network)
        with patch.dict(os.environ,{},clear=True):
            workspace.network_proxy_preferences({'mode':'http','endpoint':'http://127.0.0.1:18765','revision':0,'consent':True})
            with patch('vibe_job_radar.team_client.SafeHTTP',return_value=self.http) as factory:
                self.assertEqual(TeamUI(workspace).check({'consent':True})['member'],'alice')
            policy=factory.call_args.kwargs['network_policy']
            self.assertEqual(policy.source,'explicit_workspace')
            self.assertEqual(policy.for_host('team.fixture.test').port,18765)

    def test_snapshot_storage_limit_counts_utf8_bytes_and_rolls_back(self):
        from vibe_job_radar.team_catalog import encoded
        before=self.catalog.gateway().jobs
        row=fixture.job('unicode',text='岗位要求'*100)
        body=encoded(row);limit=(len(body)+len(body.encode('utf-8')))//2
        with patch('vibe_job_radar.team_catalog.MAX_SNAPSHOT_BYTES',limit):
            with self.assertRaisesRegex(TeamError,'team_snapshot_too_large'):
                self.catalog.publish(fixture.REGISTRY,[row],observed_at={'fixture':self.now[0]})
        self.assertEqual(self.catalog.gateway().jobs,before)
