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

with patch.object(sys, 'path', [str(Path(__file__).resolve().parents[1] / 'scripts'), *sys.path]):
    from native_fixture_lifecycle import close_service


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

    def close_idle_with_barrier(self, *, session_lease=False, fail=False):
        ident=self.start();self.release.set();self.idle(self.first)
        backend=self.first._backends[ident]
        entered,release=threading.Event(),threading.Event()
        worker=self.first._thread;original_join=worker.join
        close=backend.close
        def held_close():
            entered.set()
            if not release.wait(30):raise AssertionError('owned cleanup not released')
            if fail:raise RuntimeError('artificial close failure')
            if not session_lease:close()
        if session_lease:
            from types import SimpleNamespace
            self.first._session_leases[ident]=SimpleNamespace(close=held_close)
        def exhausted_join(timeout):
            # Model the existing five-second wait ending while cleanup is
            # still pending, without adding a five-second sleep to every CI.
            self.assertEqual(timeout,5)
            self.assertTrue(entered.wait(10))
        before=self.first._path(ident).read_bytes()
        try:
            with patch.object(backend,'close',close if session_lease else held_close):
                with patch.object(worker,'join',side_effect=exhausted_join):
                    self.first.close()
                self.assertTrue(entered.is_set())
                self.assertTrue(worker.is_alive())
                self.assertTrue(self.other.state()['owned_elsewhere'])
                with self.assertRaises(GuidedTaskBusy):
                    self.other.action({'id':ident,'action':'search'})
                self.assertEqual(self.first._path(ident).read_bytes(),before)
                release.set();original_join(timeout=10)
                self.assertFalse(worker.is_alive())
                self.assertEqual(self.first.state()['closure_uncertain'],fail)
                self.assertEqual(self.other.state()['owned_elsewhere'],fail)
                self.assertFalse(self.first._backends)
        finally:
            release.set();original_join(timeout=10)
            # Artificial failed handles have no external process. Release
            # only this fixture's lease after the worker is fully gone.
            if not worker.is_alive():self.first._ownership.release()

    def test_idle_shutdown_keeps_owner_until_browser_close_finishes(self):
        self.close_idle_with_barrier()

    def test_native_fixture_handoff_waits_for_actual_worker_and_owner_release(self):
        ident=self.start();self.release.set();self.idle(self.first)
        backend=self.first._backends[ident]
        entered,release=threading.Event(),threading.Event()
        worker=self.first._thread;join=worker.join;close=backend.close
        observation={}
        def held_close():
            entered.set()
            if not release.wait(10):raise AssertionError('cleanup not released')
            close()
        def bounded_join(timeout):
            if timeout==5:
                self.assertTrue(entered.wait(5))
                self.assertTrue(self.other.state()['owned_elsewhere'])
                with self.assertRaises(GuidedTaskBusy):self.other.create(QUERY)
                return  # Model the existing product close budget expiring.
            release.set()
            join(timeout)
        try:
            with patch.object(backend,'close',side_effect=held_close):
                with patch.object(worker,'join',side_effect=bounded_join):
                    close_service(self.first,observation,timeout=10)
            self.assertTrue(observation['pending_after_close'])
            self.assertTrue(observation['ownership_released'])
            self.assertFalse(observation['worker_alive'])
            self.other.create(QUERY);self.idle(self.other)
            self.assertEqual(self.factory.call_count,1)
        finally:
            release.set();join(10)

    def test_native_fixture_rejects_uncertain_close_without_releasing_owner(self):
        ident=self.start();self.release.set();self.idle(self.first)
        backend=self.first._backends[ident];observation={}
        try:
            with patch.object(backend,'close',side_effect=RuntimeError('artificial close failure')):
                with self.assertRaisesRegex(RuntimeError,'closure is uncertain'):
                    close_service(self.first,observation,timeout=10)
            self.assertTrue(observation['closure_uncertain'])
            self.assertFalse(observation['ownership_released'])
            with self.assertRaises(GuidedTaskBusy):self.other.create(QUERY)
        finally:
            backend.close();self.first._ownership.release()

    def test_native_fixture_waits_after_service_clears_its_worker_reference(self):
        retired,release=threading.Event(),threading.Event()
        original=self.first._worker
        def held_thread_exit():
            original()
            retired.set()
            if not release.wait(10):raise AssertionError('retired thread not released')
        self.first._worker=held_thread_exit
        self.start();self.release.set();self.idle(self.first)
        worker=self.first._thread;join=worker.join;observation={}
        def bounded_join(timeout):
            if timeout==5:
                self.assertTrue(retired.wait(5))
                self.assertIsNone(self.first._thread)
                self.assertTrue(worker.is_alive())
                return
            release.set();join(timeout)
        try:
            with patch.object(worker,'join',side_effect=bounded_join):
                close_service(self.first,observation,timeout=10)
            self.assertTrue(observation['pending_after_close'])
            self.assertFalse(worker.is_alive())
            self.assertTrue(observation['ownership_released'])
        finally:
            release.set();join(10)

    def test_late_close_failure_keeps_original_owner_after_join_returns(self):
        self.close_idle_with_barrier(fail=True)

    def test_idle_shutdown_keeps_owner_during_session_lease_cleanup(self):
        for failed in (False,True):
            with self.subTest(failed=failed):
                # Each subcase needs an independent owner and shutdown state.
                if failed:
                    self.first.close();self.other.close()
                    self.first=GuidedService(self.workspace,registry=Registry([fixture_adapter()]),backend_factory=FakeBackend)
                    self.other=GuidedService(self.workspace,registry=Registry([fixture_adapter()]),backend_factory=self.factory)
                    # start() expects the original fixture's open notification.
                self.close_idle_with_barrier(session_lease=True,fail=failed)

    def test_retry_acceptance_waits_for_explicit_stop_and_preserves_report(self):
        scripts=Path(__file__).resolve().parents[1]/'scripts'
        sys.path.insert(0,str(scripts))
        try:
            from run_native_read_retry import stop_retained_task
        finally:
            sys.path.remove(str(scripts))
        ident=self.start();self.release.set();self.idle(self.first)
        state=self.first._load(ident)
        self.first.action({'id':ident,'action':'collect','selected':[state['cards'][0]['id']]})
        self.idle(self.first);original=self.first._load(ident)
        self.assertEqual(original['status'],'completed')
        report=self.workspace.root/'reports'/original['report_id']
        before={p.relative_to(report):p.read_bytes() for p in report.rglob('*') if p.is_file()}
        rates=self.first.ledger.summary('fixture')
        backend=self.first._backends[ident];close=backend.close
        requests=list(backend.opens);entered,release=threading.Event(),threading.Event()
        values,errors=[],[]
        def held_close():
            entered.set()
            if not release.wait(30):raise AssertionError('test stop not released')
            close()
        def wait(task,predicate):
            self.idle(self.first);state=self.first._load(task)
            self.assertTrue(predicate(state));return state
        def finish():
            try:values.append(stop_retained_task(self.first,ident,wait))
            except BaseException as error:errors.append(error)
        waiter=threading.Thread(target=finish)
        try:
            with patch.object(backend,'close',held_close):
                waiter.start();self.assertTrue(entered.wait(10))
                self.assertTrue(waiter.is_alive())
                self.assertTrue(self.other.state()['owned_elsewhere'])
                release.set();waiter.join(timeout=10)
                self.assertFalse(waiter.is_alive());self.assertFalse(errors,errors)
            self.assertEqual(values[0]['status'],'stopped')
            self.assertEqual(values[0]['outcome'],original['outcome'])
            self.assertEqual(values[0]['report_id'],original['report_id'])
            self.assertEqual(before,{p.relative_to(report):p.read_bytes() for p in report.rglob('*') if p.is_file()})
            self.assertEqual(self.first.ledger.summary('fixture'),rates)
            self.assertEqual(backend.opens,requests)
            self.assertTrue(backend.closed);self.assertFalse(self.first._backends)
        finally:
            release.set();waiter.join(timeout=10)

    def test_waiting_observer_enters_before_next_checkpoint_replacement(self):
        # Synchronize on the existing fair admission queue, then verify the
        # real service reader/writer order rather than relying on sleeps.
        from vibe_job_radar.record_lock import _gate
        from vibe_job_radar.guided import service as module
        ident=self.start();self.release.set();self.idle(self.first)
        state=self.first._load(ident);gate=_gate(self.first.root);order=[];errors=[]
        original_read,original_write=module.decode_checkpoint,module.atomic_json
        def observed_read(*args,**kwargs):
            if threading.current_thread() is reader:order.append('observer')
            return original_read(*args,**kwargs)
        def observed_write(*args,**kwargs):
            order.append('writer');return original_write(*args,**kwargs)
        def poll():
            try:self.other.state()
            except BaseException as exc:errors.append(exc)
        reader=threading.Thread(target=poll)
        with patch.object(module,'decode_checkpoint',side_effect=observed_read), \
                patch.object(module,'atomic_json',side_effect=observed_write):
            try:
                with self.first._records():
                    reader.start()
                    with gate.condition:
                        self.assertTrue(gate.condition.wait_for(lambda:len(gate.tickets)==2,timeout=2),
                            'observer was not admitted to the shared checkpoint queue')
                self.first._save(state,wait_seconds=73)
            finally:
                reader.join(5)
        self.assertFalse(reader.is_alive());self.assertEqual(errors,[])
        self.assertEqual(order,['observer','writer'])
        self.assertEqual(self.other._load(ident)['wait_seconds'],73)

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
