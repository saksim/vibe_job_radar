"""Actual local HTTP races and report integration; authored data, no live account."""
import json
import secrets
import sqlite3
import threading
import unittest
from contextlib import closing
from unittest.mock import Mock, patch

import test_team_service as fixtures
import test_local_public as public_fixture
from vibe_job_radar.team_ownership import TaskLease, service_task
from vibe_job_radar.team_store import TeamStore, TeamError
from vibe_job_radar.team_client import TeamConnection
from vibe_job_radar.guided.contracts import CrawlError
from vibe_job_radar.local_public import LocalPublicDataClient
from vibe_job_radar.public_tasks import PublicTasks
from vibe_job_radar.workspace import Workspace


class OwnershipTests(unittest.TestCase):
    setUp = fixtures.TeamTests.setUp
    connection = fixtures.TeamTests.connection
    call = fixtures.TeamTests.call

    def acquire(self, member, task=None, resource='account', site='liepin'):
        return self.connection(member).request('/v1/team/task',payload={
            'resource':resource,'site':site,'operation':'acquire','task':task or secrets.token_hex(16)})

    def test_http_exclusion_expiry_and_stale_owner_fencing(self):
        alice=self.acquire('alice');value=alice['lease']
        with self.assertRaisesRegex(CrawlError,'team_task_busy'):self.acquire('bob')
        self.assertEqual(self.call('/v1/team/task',self.tokens['reader'],data={
            'resource':'account','site':'liepin','operation':'acquire','task':'a'*32})[0],403)
        reserve={'resource':'account','site':'liepin','operation':'reserve','kind':'request','origin':'https://www.liepin.com'}
        self.assertEqual(self.call('/v1/team/quota',self.tokens['bob'],data=reserve)[1]['error'],'team_task_busy')
        self.assertEqual(self.call('/v1/team/quota',self.tokens['bob'],data={**reserve,'lease':value})[1]['error'],'team_task_lost')
        self.assertEqual(self.call('/v1/team/quota',self.tokens['alice'],data={**reserve,'lease':value})[0],200)
        self.now[0]+=91
        bob=self.acquire('bob')
        self.assertEqual(bob['lease']['generation'],value['generation']+1)
        for operation in ['renew','release']:
            self.assertEqual(self.call('/v1/team/task',self.tokens['alice'],data={
                'resource':'account','site':'liepin','operation':operation,'lease':value})[1]['error'],'team_task_lost')
        self.assertEqual(self.call('/v1/team/quota',self.tokens['alice'],data={**reserve,'lease':value})[1]['error'],'team_task_lost')
        self.assertEqual(self.call('/v1/team/quota',self.tokens['bob'],data={**reserve,'lease':bob['lease']})[0],200)
        state=self.connection('bob').request('/v1/team/state')
        self.assertEqual(state['resources'][0]['usage']['request']['day'],2)
        self.assertTrue(state['resources'][0]['task']['this_device'])

    def test_real_http_concurrent_claims_have_exactly_one_owner(self):
        clients=[self.connection(name) for name in ['alice','bob']]
        barrier=threading.Barrier(2);results=[]
        def claim(client):
            barrier.wait(3)
            try:
                result=client.request('/v1/team/task',payload={
                    'resource':'account','site':'liepin','operation':'acquire','task':secrets.token_hex(16)})
                results.append(('owned',result['lease']['generation']))
            except CrawlError as error:results.append((error.code,None))
        threads=[threading.Thread(target=claim,args=(c,)) for c in clients]
        for thread in threads:thread.start()
        for thread in threads:thread.join(6)
        self.assertFalse(any(t.is_alive() for t in threads))
        self.assertEqual(sum(x[0]=='owned' for x in results),1)
        self.assertTrue(all(x[0] in {'owned','team_task_busy','team_rate_wait'} for x in results))
        # Concurrent API admissions may hit the independent ingress interval.
        # After that interval, the losing attempt must still be excluded by ownership.
        self.now[0]+=1
        for client in clients:
            with self.assertRaisesRegex(CrawlError,'team_task_busy'):
                client.request('/v1/team/task',payload={'resource':'account','site':'liepin',
                    'operation':'acquire','task':secrets.token_hex(16)})

    def test_legacy_migration_keeps_quota_and_does_not_reset_corrupt_schema(self):
        self.store.quota(self.tokens['alice'],{'resource':'account','site':'liepin',
            'operation':'reserve','kind':'request','origin':'https://www.liepin.com'})
        with closing(sqlite3.connect(self.store.path)) as conn:
            conn.execute('DROP TABLE task_leases');conn.execute('PRAGMA user_version=1');conn.commit()
        self.assertEqual(self.store.state(self.tokens['alice'])['resources'][0]['usage']['request']['day'],1)
        with closing(sqlite3.connect(self.store.path)) as conn:
            self.assertEqual(conn.execute('PRAGMA user_version').fetchone()[0],1)
        value=self.acquire('alice')
        with closing(sqlite3.connect(self.store.path)) as conn:
            self.assertEqual(conn.execute('PRAGMA user_version').fetchone()[0],2)
            self.assertNotIn(value['lease']['token'],str(conn.execute('SELECT * FROM task_leases').fetchall()))
        restored=TeamStore(self.store.root,clock=lambda:self.now[0])
        self.assertEqual(restored.state(self.tokens['alice'])['resources'][0]['usage']['request']['day'],1)
        with closing(sqlite3.connect(self.store.path)) as conn:
            conn.execute('PRAGMA user_version=99');conn.commit()
        with self.assertRaisesRegex(TeamError,'team_storage_invalid'):
            service_task(restored,self.tokens['bob'],{'resource':'account','site':'liepin','operation':'acquire','task':'b'*32})
        with closing(sqlite3.connect(self.store.path)) as conn:
            self.assertEqual(conn.execute('PRAGMA user_version').fetchone()[0],99)

    def test_scope_revocation_release_and_legacy_clients(self):
        client=self.connection('alice')
        with TaskLease(client,'liepin') as lease:
            client.reserve('liepin','request','https://www.liepin.com')
            self.store.grant('group','alice','account',remove=True)
            with self.assertRaises(CrawlError):lease.confirm()
        self.assertEqual(self.call('/v1/team/task',self.tokens['alice'],data={
            'resource':'account','site':'liepin','operation':'release','lease':lease.value})[0],403)
        self.now[0]+=91
        with TaskLease(self.connection('bob'),'liepin'):
            pass
        # Old clients still work when no resource task is active; no quota refund.
        self.assertEqual(self.call('/v1/team/quota',self.tokens['bob'],data={
            'resource':'account','site':'liepin','operation':'reserve','kind':'request','origin':'https://www.liepin.com'})[0],200)
        self.assertEqual(self.store.state(self.tokens['bob'])['resources'][0]['usage']['request']['day'],2)

    def test_expired_response_and_heartbeat_failure_stop_client(self):
        client=self.connection('alice')
        tick=[0.0]
        original=client.request
        def delayed(*args,**kwargs):
            result=original(*args,**kwargs);tick[0]+=91;return result
        with patch.object(client,'request',side_effect=delayed):
            with self.assertRaisesRegex(CrawlError,'team_response_invalid'):
                TaskLease(client,'liepin',monotonic=lambda:tick[0]).__enter__()
        self.now[0]+=91
        failed=threading.Event()
        def disconnect(*args,**kwargs):
            failed.set();raise OSError('authored disconnect')
        with patch('vibe_job_radar.team_ownership.RENEW_SECONDS',0.02):
            with TaskLease(self.connection('bob'),'liepin') as lease:
                with patch.object(lease.connection,'request',side_effect=disconnect):
                    self.assertTrue(failed.wait(2))
                    lease._thread.join(2)
                    with self.assertRaisesRegex(CrawlError,'team_task_lost'):lease.check()

    def _public_setup(self):
        self.store.resource('group','public-account','greenhouse_public_example',['https://boards-api.greenhouse.io'])
        for member in ['alice','bob']:
            self.store.grant('group',member,'public-account')
            connection=self.connection(member)
            config=json.loads(connection.path.read_text(encoding='utf-8'))
            config['quota_resources']['greenhouse_public_example']='public-account'
            connection.path.write_text(json.dumps(config),encoding='utf-8')
        workspace=Workspace(self.root/'alice')
        real=TeamConnection
        def connect(root,**kwargs):return real(root,transport=self.http)
        return workspace,connect

    def test_existing_public_task_report_and_account_exclusion(self):
        workspace,connect=self._public_setup();upstream=Mock()
        def fetch(url):
            with self.assertRaisesRegex(CrawlError,'team_task_busy'):
                self.acquire('bob',resource='public-account',site='greenhouse_public_example')
            return public_fixture.payload(1)
        upstream.json.side_effect=fetch
        client=LocalPublicDataClient(workspace,transport=upstream)
        with patch('vibe_job_radar.team_client.TeamConnection',side_effect=connect):
            tasks=PublicTasks(workspace,hybrid_client=client);self.addCleanup(tasks.close)
            tasks.search({'consent':True,'query':public_fixture.query().payload()})
            tasks._thread.join(15)
        state=tasks.snapshot()
        self.assertEqual(state['status'],'completed',state)
        self.assertTrue(state['report_id'])
        self.assertTrue((workspace.root/'reports'/state['report_id']/'dashboard.html').is_file())
        resources=connect(workspace.root).request('/v1/team/state')['resources']
        self.assertFalse(next(r for r in resources if r['id']=='public-account')['task']['active'])
        self.assertEqual(upstream.json.call_count,1)
        self.assertTrue(all('query' not in keys for _,path,keys in self.http.calls if path=='/v1/team/task'))
        # A cached repeat must keep working when coordination is unavailable.
        with patch('vibe_job_radar.team_client.TeamConnection',side_effect=AssertionError('cache stays offline')):
            tasks.search({'consent':True,'query':public_fixture.query().payload()});tasks._thread.join(15)
        cached=tasks.snapshot();self.assertEqual(cached['status'],'completed',cached)
        self.assertTrue(cached['cache_reused']);self.assertEqual(upstream.json.call_count,1)

    def test_takeover_before_report_commit_preserves_cache_but_blocks_new_report(self):
        workspace,connect=self._public_setup();upstream=Mock()
        def fetch(url):
            self.now[0]+=91
            self.acquire('bob',resource='public-account',site='greenhouse_public_example')
            return public_fixture.payload(1)
        upstream.json.side_effect=fetch
        client=LocalPublicDataClient(workspace,transport=upstream)
        with patch('vibe_job_radar.team_client.TeamConnection',side_effect=connect):
            tasks=PublicTasks(workspace,hybrid_client=client);self.addCleanup(tasks.close)
            tasks.search({'consent':True,'query':public_fixture.query().payload()});tasks._thread.join(15)
        state=tasks.snapshot()
        self.assertEqual(state['status'],'failed',state)
        self.assertEqual(state['code'],'team_task_unavailable')
        self.assertFalse(state['report_id'])
        self.assertFalse(list((workspace.root/'reports').iterdir()))
        self.assertTrue(client.cached(public_fixture.query()))


if __name__=='__main__':unittest.main()
