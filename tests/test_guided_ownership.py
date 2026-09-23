"""Two real services/processes share state, never the ownership of a browser."""
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock,patch

from test_guided import FakeBackend,fixture_adapter
from vibe_job_radar.guided.adapters import Registry
from vibe_job_radar.guided.ownership import GuidedTaskBusy
from vibe_job_radar.guided.service import GuidedService
from vibe_job_radar.workspace import Workspace,InputError

QUERY={'platform':'fixture','keyword':'时间序列','roles':['time_series'],'consent':True,
       'rights_note':'人工离线所有权测试','max_pages':1,'max_jobs':1}


class GuidedOwnershipTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.workspace=Workspace(self.temp.name)
        self.workspace.config['platforms']['fixture']={'label':'人工测试站点','domains':['jobs.fixture.test']}
        self.entered,self.release=threading.Event(),threading.Event()
        entered,release=self.entered,self.release
        class HeldBackend(FakeBackend):
            def open(self,*args,**kwargs):
                entered.set()
                if not release.wait(10):raise RuntimeError('artificial browser was not released')
                return super().open(*args,**kwargs)
        self.first=GuidedService(self.workspace,registry=Registry([fixture_adapter()]),backend_factory=HeldBackend)
        self.factory=Mock(side_effect=FakeBackend)
        self.other=GuidedService(self.workspace,registry=Registry([fixture_adapter()]),backend_factory=self.factory)
        self.addCleanup(self.cleanup_services)

    def cleanup_services(self):
        self.release.set();self.first.close();self.other.close()

    def idle(self,service):
        deadline=time.monotonic()+10
        while service.state()['busy'] and time.monotonic()<deadline:time.sleep(.01)
        self.assertFalse(service.state()['busy'],'owned worker did not finish')

    def start(self):
        ident=self.first.create(QUERY)['id'];self.assertTrue(self.entered.wait(5));return ident

    def test_observer_never_marks_living_task_interrupted_or_stops_its_browser(self):
        ident=self.start();path=self.first._path(ident);before=path.read_bytes()
        view=self.other.state();self.assertTrue(view['owned_elsewhere'])
        self.assertEqual(view['jobs'][0]['status'],'running')
        for action in ('pause','stop','resume','search','login','collect','forget_session'):
            with self.subTest(action=action),self.assertRaises(GuidedTaskBusy):
                self.other.action({'id':ident,'action':action,'confirm':True})
        self.assertEqual(path.read_bytes(),before);self.factory.assert_not_called()
        self.assertTrue(self.first.state()['busy']);self.assertTrue(self.first._backends)
        self.release.set();self.idle(self.first)
        self.assertEqual(self.first._load(ident)['status'],'ready')

    def test_new_query_components_and_diagnostic_changes_cannot_steal_session(self):
        ident=self.start();before=self.first._path(ident).read_bytes()
        operations=[lambda:self.other.create(QUERY),lambda:self.other.check_browser({}),
                    lambda:self.other.check_browser({'channel':'msedge','consent':True}),
                    lambda:self.other.install({'consent':True}),
                    lambda:self.other.diagnostics({'id':ident,'enabled':False})]
        with patch.object(self.other,'_installer') as install,patch.object(self.other,'_health_probe') as probe:
            for operation in operations:
                with self.assertRaises(GuidedTaskBusy):operation()
            install.assert_not_called();probe.assert_not_called()
        self.assertEqual(self.first._path(ident).read_bytes(),before)
        self.assertEqual(len(list(self.first.root.glob('*.json'))),1)

    def test_password_is_not_parsed_queued_or_stored_by_non_owner(self):
        ident=self.start();secret='ARTIFICIAL-NOT-A-REAL-PASSWORD'
        with patch('vibe_job_radar.guided.service.LoginCredentials.from_input') as credentials:
            with self.assertRaises(GuidedTaskBusy):
                self.other.action({'id':ident,'action':'login_password','username':'fixture',
                                   'password':secret,'credential_consent':True})
            credentials.assert_not_called()
        self.assertTrue(self.other._queue.empty());self.factory.assert_not_called()
        for path in self.first.root.glob('*.json'):self.assertNotIn(secret,path.read_text(encoding='utf-8'))

    def test_idle_and_paused_browser_stays_owned_until_explicit_stop(self):
        ident=self.start();self.release.set();self.idle(self.first)
        self.assertTrue(self.other.state()['owned_elsewhere'])
        self.first.action({'id':ident,'action':'pause'});self.idle(self.first)
        self.assertEqual(self.first._load(ident)['status'],'paused')
        with self.assertRaises(GuidedTaskBusy):self.other.action({'id':ident,'action':'resume'})
        self.first.action({'id':ident,'action':'stop'});self.idle(self.first)
        self.assertFalse(self.other.state()['owned_elsewhere']);self.assertFalse(self.first._backends)
        self.other.action({'id':ident,'action':'search'});self.idle(self.other)
        self.assertEqual(self.other._load(ident)['keyword'],QUERY['keyword'])
        self.assertEqual(self.factory.call_count,1)

    def test_closing_observer_preserves_owner_and_report_survives_owner_exit(self):
        ident=self.start();self.other.close()
        self.assertTrue(self.first.state()['busy']);self.release.set();self.idle(self.first)
        state=self.first._load(ident);selected=[row['id'] for row in state['cards']]
        self.first.ledger.reserve('fixture','page')
        self.first.action({'id':ident,'action':'collect','selected':selected});self.idle(self.first)
        report=self.first._load(ident)['report_id'];self.assertTrue(report)
        self.first.close()
        observer=GuidedService(self.workspace,registry=Registry([fixture_adapter()]),backend_factory=self.factory)
        try:
            self.assertFalse(observer.state()['owned_elsewhere'])
            observer.action({'id':ident,'action':'resume'})
            self.assertEqual(observer._load(ident)['report_id'],report)
            self.assertEqual(observer.ledger.summary('fixture')['page']['day'],1)
            self.assertTrue(self.workspace.report(report));self.factory.assert_not_called()
        finally:observer.close()

    def test_thread_start_failure_releases_claim_and_does_not_queue_credentials(self):
        with patch.object(self.first,'_spawn',side_effect=RuntimeError('artificial thread failure')):
            with self.assertRaises(RuntimeError):self.first.create(QUERY)
        self.assertFalse(self.first._busy);self.assertIsNone(self.first._ownership.lease)
        self.assertTrue(self.first._queue.empty())
        self.other.create(QUERY);self.idle(self.other);self.assertEqual(self.factory.call_count,1)

    def test_timed_out_close_keeps_ownership_until_actual_worker_exit(self):
        ident=self.start();self.first.close()
        self.assertTrue(self.first._busy)
        with self.assertRaises(GuidedTaskBusy):self.other.action({'id':ident,'action':'resume'})
        self.release.set()
        deadline=time.monotonic()+5
        while self.other.state()['owned_elsewhere'] and time.monotonic()<deadline:time.sleep(.01)
        self.assertFalse(self.other.state()['owned_elsewhere'])
        self.assertFalse(self.first._backends)

    def test_uncertain_browser_close_is_not_a_successful_session_handoff(self):
        ident=self.start();self.release.set();self.idle(self.first)
        backend=self.first._backends[ident]
        try:
            with patch.object(backend,'close',side_effect=RuntimeError('artificial close failure')):
                self.first.action({'id':ident,'action':'stop'});self.idle(self.first)
            self.assertTrue(self.first.state()['closure_uncertain'])
            self.assertNotEqual(self.first._load(ident)['status'],'stopped')
            with self.assertRaises(GuidedTaskBusy):self.other.action({'id':ident,'action':'resume'})
            with self.assertRaises(InputError):self.first.action({'id':ident,'action':'resume'})
            self.first.close();self.assertTrue(self.other.state()['owned_elsewhere'])
        finally:
            # This fixture has no browser process. Explicit handle cleanup
            # stands for process exit; the separate subprocess test checks OS release.
            backend.close();self.first._ownership.release()

    def test_two_instances_serialize_checkpoint_polling_and_replacement(self):
        ident=self.start();self.release.set();self.idle(self.first);errors=[]
        state=self.first._load(ident)
        def poll():
            try:
                for _ in range(60):self.other.state()
            except Exception as exc:errors.append(exc)
        reader=threading.Thread(target=poll);reader.start()
        try:
            for number in range(60):self.first._save(state,wait_seconds=number)
        finally:reader.join(10)
        self.assertFalse(reader.is_alive());self.assertEqual(errors,[])
        self.assertEqual(self.other._load(ident)['wait_seconds'],59)

    def test_actual_process_lease_prevents_restart_claim_until_process_exits(self):
        with patch.object(self.first,'_submit'):ident=self.first.create(QUERY)['id']
        before=self.first._path(ident).read_bytes()
        source=Path(__file__).resolve().parents[1]/'src'
        code=('from pathlib import Path; import sys; '
              'from vibe_job_radar.guided.ownership import Ownership; '
              'owner=Ownership(Path(sys.argv[1])); owner.acquire(); '
              'print("claimed",flush=True); sys.stdin.read()')
        child=subprocess.Popen([sys.executable,'-c',code,str(self.first.root)],stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,env={**os.environ,'PYTHONPATH':str(source),'PYTHONUTF8':'1'})
        received=queue.Queue()
        reader=threading.Thread(target=lambda:received.put(child.stdout.readline()),daemon=True);reader.start()
        try:
            self.assertEqual(received.get(timeout=5).rstrip(b'\r\n'),b'claimed')
            self.assertTrue(self.other.state()['owned_elsewhere'])
            self.assertEqual(self.other.state()['jobs'][0]['status'],'queued')
            with self.assertRaises(GuidedTaskBusy):self.other.action({'id':ident,'action':'resume'})
            self.assertEqual(self.first._path(ident).read_bytes(),before)
        finally:
            child.terminate();child.wait(10);child.stdin.close();child.stdout.close();reader.join(2)
        self.assertFalse(self.other.state()['owned_elsewhere'])
        self.assertEqual(self.other.state()['jobs'][0]['status'],'interrupted')
        self.assertEqual(self.first._path(ident).read_bytes(),before)
        self.other.action({'id':ident,'action':'resume'});self.idle(self.other)
        self.assertEqual(self.factory.call_count,1)


if __name__=='__main__':unittest.main()
