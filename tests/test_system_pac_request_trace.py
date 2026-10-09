"""Controlled diagnostics tests; no actual PAC registry or browser is touched."""
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from pathlib import Path
import json,os,subprocess,sys,threading,unittest
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
import system_pac_request_trace as trace

class TraceTests(unittest.TestCase):
    def test_same_declared_client_does_not_attribute_another_owned_child_to_cached_worker(self):
        ids=iter([3,4]);born={1:10,2:20,3:30,4:40}
        t=trace.FixtureRequestTrace(owner_lookup=lambda *unused:next(ids),creation_time=born.get,parents=lambda:{1:0,2:1,3:2,4:1})
        t.register('verifier',1);t.register('cached_worker',2);t.stage('cached_worker_running')
        for _ in range(2):t.observe(('127.0.0.1',3),('127.0.0.1',4),'VibeJobRadar-PAC')
        d=t.snapshot()
        self.assertEqual(d['requests_observed'],2)
        self.assertEqual([e['connection_owner_scope'] for e in d['events']],['cached_worker','verifier'])
        self.assertEqual([e['owner_relation'] for e in d['events']],['descendant_snapshot']*2)
        self.assertEqual([e['declared_client'] for e in d['events']],['project_pac']*2)

    def test_recycled_registered_pid_is_unknown(self):
        current={2:20};t=trace.FixtureRequestTrace(owner_lookup=lambda *a:2,creation_time=current.get,parents=lambda:{2:0})
        t.register('cached_worker',2);current[2]=30;t.observe(('127.0.0.1',3),('127.0.0.1',4),'VibeJobRadar-PAC')
        e=t.snapshot()['events'][0]
        self.assertEqual((e['connection_owner_scope'],e['owner_relation']),('unknown','identity_changed'))

    def test_newer_parent_identity_does_not_create_false_descendant(self):
        scope,relation=trace.owner_scope(3,{'cached_worker':(2,10)},{2:30,3:20}.get,lambda:{3:2,2:0})
        self.assertEqual((scope,relation),('unknown','identity_changed'))

    def test_exact_registered_identity_does_not_require_parent_enumeration(self):
        def denied():raise PermissionError('private process list')
        self.assertEqual(trace.owner_scope(2,{'cached_worker':(2,20)},lambda _:20,denied),('cached_worker','same_process'))

    def test_lookup_failures_preserve_each_request_without_sensitive_fields(self):
        def denied(*a):raise OSError('SECRET_URL/path?token=SECRET')
        t=trace.FixtureRequestTrace(owner_lookup=denied,creation_time=lambda p:None,parents=denied)
        t.register('cached_worker',22);t.stage('cached_worker_running')
        for _ in range(2):t.observe(('127.0.0.1',34567),('127.0.0.1',45678),'Authorization: SECRET')
        d=t.snapshot();self.assertEqual(d['requests_observed'],2);self.assertEqual(len(d['events']),2)
        self.assertTrue(all(e['connection_owner_scope']=='unknown' and e['owner_relation']=='lookup_failed' for e in d['events']))
        self.assertEqual(d['root_registration_failures'],1);self.assertFalse(d['historical_cause_confirmed'])
        self.assertNotIn('SECRET',json.dumps(d));self.assertNotIn('34567',json.dumps(d))
        self.assertTrue(all(set(e)=={'sequence','stage','elapsed_ms','declared_client','connection_owner_scope','owner_relation'} for e in d['events']))

    def test_event_bound_does_not_hide_total_requests(self):
        t=trace.FixtureRequestTrace(owner_lookup=lambda *a:None)
        for _ in range(20):t.observe(('127.0.0.1',3),('127.0.0.1',4),None)
        d=t.snapshot();self.assertEqual(d['requests_observed'],20);self.assertEqual(d['events_dropped'],4);self.assertEqual(len(d['events']),16)
        self.assertEqual([e['sequence'] for e in d['events']],list(range(1,17)))
        self.assertTrue(all(e['declared_client']=='missing' for e in d['events']))

    def test_missing_or_cyclic_parent_facts_remain_unknown(self):
        for parents in [None,{3:4,4:3}]:
            with self.subTest(parents=parents):
                result=trace.owner_scope(3,{'cached_worker':(2,10)},lambda p:20,lambda:parents)
                self.assertEqual(result[0],'unknown')

    def test_real_windows_loopback_connections_bind_to_registered_child_or_verifier(self):
        if os.name!='nt':
            self.assertIsNone(trace.windows_connection_owner(('127.0.0.1',3),('127.0.0.1',4)))
            self.assertIsNone(trace.windows_creation_time(os.getpid()));self.assertIsNone(trace.windows_parents());return
        t=trace.FixtureRequestTrace();t.register('verifier',os.getpid())
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*unused):pass
            def do_GET(self):
                t.observe(self.client_address,self.connection.getsockname(),self.headers.get('User-Agent'))
                self.send_response(200);self.send_header('Content-Length','2');self.end_headers();self.wfile.write(b'ok')
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler);server.daemon_threads=True
        thread=threading.Thread(target=server.serve_forever,kwargs={'poll_interval':.01},daemon=True);thread.start();children=[]
        request="import http.client,sys;sys.stdin.readline();c=http.client.HTTPConnection('127.0.0.1',int(sys.argv[1]),timeout=5);c.request('GET','/private?token=SECRET',headers={'User-Agent':'VibeJobRadar-PAC'});r=c.getresponse();assert r.status==200 and r.read()==b'ok';c.close()"
        # The registered child creates another client process; the second direct
        # client has the same declared label and must not become that worker.
        grandchild="import subprocess,sys;sys.stdin.readline();q=subprocess.run([sys.executable,'-c',sys.argv[2],sys.argv[1]],input=b'go\\n',timeout=8,check=True)"
        try:
            # Create both owned roots before either exits, so a reused PID
            # cannot make this unrelated-client control ambiguous.
            for code,args in [(grandchild,[str(server.server_port),request]),(request,[str(server.server_port)])]:
                p=subprocess.Popen([sys.executable,'-c',code,*args],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,cwd=Path(__file__).resolve().parent,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0));children.append(p)
            t.register('cached_worker',children[0].pid)
            for p,phase in zip(children,('cached_worker_running','cached_worker_finished')):
                t.stage(phase);out,err=p.communicate(b'go\n',timeout=12)
                self.assertEqual(p.returncode,0,err.decode('utf8','replace')[-200:])
            d=t.snapshot()
            self.assertEqual(d['requests_observed'],2);self.assertEqual(d['root_registration_failures'],0)
            self.assertEqual([e['connection_owner_scope'] for e in d['events']],['cached_worker','verifier'])
            self.assertEqual([e['owner_relation'] for e in d['events']],['descendant_snapshot']*2)
            self.assertNotIn('SECRET',json.dumps(d))
        finally:
            server.shutdown();server.server_close();thread.join(2)
            for p in children:
                if p.poll() is None:p.kill();p.wait(5)

    def test_real_fixture_preserves_two_requests_when_ownership_is_unavailable(self):
        import http.client
        from test_system_pac import SourceServer
        t=trace.FixtureRequestTrace(owner_lookup=lambda *a:None);t.stage('cached_worker_running')
        fixture=SourceServer(observer=t.observe)
        try:
            for _ in range(2):
                c=http.client.HTTPConnection('127.0.0.1',fixture.server.server_port,timeout=5)
                c.request('GET','/private?token=SECRET',headers={'User-Agent':'VibeJobRadar-PAC'})
                reply=c.getresponse();self.assertEqual(reply.status,200);self.assertEqual(reply.read(),fixture.body);c.close()
            self.assertEqual(len(fixture.requests),2)
            d=t.snapshot();self.assertEqual(d['requests_observed'],2);self.assertFalse(fixture.observer_failed)
            self.assertTrue(all(e['connection_owner_scope']=='unknown' for e in d['events']))
            self.assertNotIn('SECRET',json.dumps(d))
        finally:fixture.close()

    def test_observer_error_does_not_replace_the_fixture_HTTP_result_or_count(self):
        import http.client
        from test_system_pac import SourceServer
        def failed(*args):raise RuntimeError('SECRET diagnostics error')
        fixture=SourceServer(observer=failed)
        try:
            c=http.client.HTTPConnection('127.0.0.1',fixture.server.server_port,timeout=5)
            c.request('GET','/private');reply=c.getresponse()
            self.assertEqual(reply.status,200);self.assertEqual(reply.read(),fixture.body);c.close()
            self.assertEqual(len(fixture.requests),1);self.assertTrue(fixture.observer_failed)
        finally:fixture.close()

    def test_passive_worker_observation_keeps_original_cached_report_and_zero_requests(self):
        import tempfile
        from verify_windows_portable import verify_queued_worker
        t=trace.FixtureRequestTrace()
        root_source=Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as tmp,patch.dict(os.environ,
                {k:v for k,v in os.environ.items() if not k.startswith('VIBE_RADAR_')},clear=True):
            root=Path(tmp);cwd=root/'another directory';cwd.mkdir()
            result=verify_queued_worker(Path(sys.executable),root,cwd,dict(os.environ),
                command_prefix=[sys.executable,str(root_source/'scripts/start_workbench.py')],request_trace=t)
            self.assertEqual(result,{'completed_queries':1,'network_requests':0,'full_text_job_groups':1,'daily_plan_enabled':False})
            self.assertEqual(t.snapshot()['requests_observed'],0)


    def test_primary_PAC_count_failure_survives_diagnostic_failure_and_fixture_cleanup(self):
        import ast,contextlib
        from types import SimpleNamespace
        import verify_windows_portable as verifier
        tree=ast.parse(Path(verifier.__file__).read_text(encoding='utf8'))
        blocks=[n for n in ast.walk(tree) if isinstance(n,ast.If) and isinstance(n.test,ast.Name) and n.test.id=='verify_system_pac']
        self.assertEqual(len(blocks),1)
        function=ast.FunctionDef(name='check',args=ast.arguments(posonlyargs=[],args=[ast.arg(arg=n) for n in ('app','result','exe','root','cwd','env')],vararg=None,kwonlyargs=[],kw_defaults=[],kwarg=None,defaults=[]),body=blocks[0].body,decorator_list=[])
        module=ast.fix_missing_locations(ast.Module(body=[function],type_ignores=[]))
        fixture=SimpleNamespace(requests=[],source=SimpleNamespace(config_id='fixed'),observer_failed=False);cleaned=[]
        @contextlib.contextmanager
        def configured(**kwargs):
            try:yield fixture
            finally:cleaned.append(True)
        def ui(*args):fixture.requests.append('first')
        def cached(*args,**kwargs):
            fixture.requests.append('extra')
            return {'completed_queries':1,'network_requests':0,'full_text_job_groups':1,'daily_plan_enabled':False}
        class FailedSnapshot(trace.FixtureRequestTrace):
            def snapshot(self):raise RuntimeError('SECRET diagnostic failure')
        namespace={'os':os,'verify_queued_worker':cached}
        exec(compile(module,'<original-system-PAC-count-control>','exec'),namespace)
        result={'checks':[]}
        with patch('system_pac_acceptance.configured_source',configured),patch('system_pac_acceptance.verify_app',ui),patch('system_pac_request_trace.FixtureRequestTrace',FailedSnapshot):
            with self.assertRaisesRegex(AssertionError,'cached worker implicitly downloaded PAC'):
                namespace['check'](SimpleNamespace(proc=SimpleNamespace(pid=os.getpid())),result,None,Path('.'),None,{})
        self.assertEqual(cleaned,[True]);self.assertEqual(len(fixture.requests),2)
        self.assertEqual(result['system_pac_source_request_counts'],{'before_ui_check':0,'after_ui_check':1,'after_cached_worker':2})
        self.assertEqual(result['stage'],'configured_system_pac_request_count')
        self.assertNotIn('system_pac_verified',result)
        self.assertEqual(result['system_pac_request_trace'],{'capture_failed':True,'historical_cause_confirmed':False})
        self.assertNotIn('SECRET',json.dumps(result))

if __name__=='__main__':unittest.main(verbosity=2)
