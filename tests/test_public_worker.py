"""Independent worker lifecycle and real process ownership, artificial source."""
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from test_local_public import payload,query
from vibe_job_radar.local_public import API_URL,LocalPublicDataClient
from vibe_job_radar.public_schedule import DAY,PublicSchedule
from vibe_job_radar.public_queue import PublicQueue
from vibe_job_radar.public_tasks import PublicTasks
from vibe_job_radar.public_worker import PublicWorker
from vibe_job_radar import public_worker
from vibe_job_radar.workspace import Workspace
from vibe_job_radar.utils import atomic_json


def wait_for(condition, timeout=20):
    deadline=time.monotonic()+timeout
    while time.monotonic()<deadline:
        try:
            value=condition()
            if value:return value
        except (FileNotFoundError,PermissionError,json.JSONDecodeError):pass
        time.sleep(.05)
    raise AssertionError('controlled worker did not reach expected state')


def seed(workspace):
    tasks=PublicTasks(workspace,hybrid_client=LocalPublicDataClient(workspace))
    schedule=PublicSchedule(workspace,tasks,clock=lambda:time.time()-DAY-10)
    try:
        schedule.configure({'query':query().payload(),'consent':True,'revision':0})
    finally:schedule.close();tasks.close()


def fixture_child(root,label,mode):
    """Test-only child: no listening socket, real scheduler/task/storage/report."""
    class Wire:
        def json(self,url):
            assert url==API_URL
            with (root/'fixture-requests.txt').open('a',encoding='utf-8') as stream:stream.write('request\n')
            if mode=='hold':wait_for(lambda:(root/'release-fixture').exists(),30)
            return payload()
    with patch('urllib.request.getproxies',return_value={}),patch.object(socket.socket,'bind',side_effect=AssertionError('unexpected listening socket')):
        workspace=Workspace(root)
        worker=PublicWorker(workspace,client=LocalPublicDataClient(workspace,transport=Wire()))
        def stopper():
            wait_for(lambda:(root/('stop-'+label)).exists(),60)
            worker.request_stop()
        threading.Thread(target=stopper,daemon=True).start()
        def emit(value):
            if value['event']=='worker_started':(root/('ready-'+label)).write_text('ready',encoding='utf-8')
            if value['event']=='worker_state':atomic_json(root/('state-'+label+'.json'),value)
            print(json.dumps(value),flush=True)
        return worker.run(emit=emit)


def signal_child(root):
    numbers=[getattr(signal,name) for name in ('SIGINT','SIGTERM','SIGBREAK') if hasattr(signal,name)]
    original={number:signal.getsignal(number) for number in numbers}
    for number in numbers:
        worker=PublicWorker(Workspace(root))
        events=[]
        def emit(value):
            events.append(value)
            if value['event']=='worker_started':
                # A direct Event.set() from the signal handler would re-enter
                # this non-reentrant lock and hang the isolated test child.
                with worker.stop._cond:signal.raise_signal(number)
        with patch.object(public_worker,'PublicWorker',return_value=worker):
            assert public_worker.run_cli(root,emit=emit)==0
        assert events[-1]=={'event':'worker_stopped'}
        assert all(signal.getsignal(n)==handler for n,handler in original.items())
        assert not worker.schedule.is_running() and not worker.queue.is_running() and not worker.tasks.is_running()
    print(json.dumps({'success':True,'signals_checked':len(numbers)}))
    return 0


class WorkerTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup)
        self.workspace=Workspace(tmp.name)
        clean={k:v for k,v in os.environ.items() if not k.startswith('VIBE_RADAR_')}
        env=patch.dict(os.environ,clean,clear=True);env.start();self.addCleanup(env.stop)
        proxy=patch('urllib.request.getproxies',return_value={});proxy.start();self.addCleanup(proxy.stop)
        self.wire=Mock();self.wire.json.return_value=payload()
        self.worker=PublicWorker(self.workspace,client=LocalPublicDataClient(self.workspace,transport=self.wire))
        self.events=[];self.results=[];self.thread=None
        self.addCleanup(self.stop)

    def start(self):
        self.thread=threading.Thread(target=lambda:self.results.append(self.worker.run(emit=self.events.append)),daemon=True)
        self.thread.start();wait_for(lambda:self.events)

    def stop(self):
        self.worker.request_stop()
        if self.thread:
            self.thread.join(25)
            self.assertFalse(self.thread.is_alive())
        else:self.worker.tasks.close()

    def test_default_worker_listens_nowhere_and_does_not_enable_a_plan(self):
        with patch.object(socket.socket,'bind',side_effect=AssertionError('must not bind')):
            self.start();wait_for(lambda:any(e['event']=='worker_state' for e in self.events));self.stop()
        self.assertEqual(self.results,[0]);self.wire.json.assert_not_called()
        self.assertEqual(self.worker.schedule.state()['status'],'disabled')
        self.assertFalse(self.worker.schedule.path.exists())
        self.assertEqual(self.worker.queue.state()['status'],'idle');self.assertFalse(self.worker.queue.root.exists())
        self.assertFalse((self.workspace.root/'jobs.sqlite').exists())
        self.assertFalse(self.worker.schedule.is_running());self.assertFalse(self.worker.tasks.is_running())
        self.assertFalse(self.worker.queue.is_running())
        self.assertEqual(self.events[0],{'event':'worker_started','http_server':False,'browser':False})

    def test_due_confirmed_query_reuses_original_report_and_logs_no_query(self):
        seed(self.workspace);self.start()
        wait_for(lambda:len(self.worker.schedule.state()['history'])==1)
        self.stop();state=self.worker.schedule.state()
        self.assertEqual(self.results,[0]);self.wire.json.assert_called_once_with(API_URL)
        self.assertEqual(state['status'],'scheduled')
        report=self.workspace.report(state['history'][0]['report_id'])
        self.assertEqual(report['manifest']['stats']['selected_source_records'],2)
        # The two authored records deliberately share identical JD text.
        self.assertEqual(report['manifest']['stats']['full_text_job_groups'],1)
        text=json.dumps(self.events)
        for private in ('Architect','Cursor','http://','https://',str(self.workspace.root),'query'):
            self.assertNotIn(private,text)
        self.assertTrue((self.workspace.root/'public_examples/rates.sqlite').exists())

    def test_stop_before_start_does_not_dispatch_due_query(self):
        seed(self.workspace);self.worker.request_stop()
        self.assertEqual(self.worker.run(emit=self.events.append),0)
        self.wire.json.assert_not_called()
        self.assertFalse(self.worker.schedule.state()['history'])

    def test_requested_stop_after_state_read_is_not_a_scheduler_failure(self):
        def stop_at_observation(value):
            self.events.append(value)
            if value['event']=='worker_state':
                self.worker.request_stop()
                self.worker.schedule._thread.join(5)
                self.assertFalse(self.worker.schedule.is_running())
        self.assertEqual(self.worker.run(emit=stop_at_observation),0)
        self.assertNotIn('worker_failed',[event['event'] for event in self.events])
        self.wire.json.assert_not_called()

    def test_scheduler_exit_without_worker_stop_is_still_a_failure(self):
        def stop_only_scheduler(value):
            self.events.append(value)
            if value['event']=='worker_state':self.worker.schedule.close()
        self.assertEqual(self.worker.run(emit=stop_only_scheduler),2)
        self.assertIn({'event':'worker_failed','code':'scheduler_unavailable'},self.events)
        self.wire.json.assert_not_called()

    def test_queue_exit_without_worker_stop_is_reported_and_closes_all_workers(self):
        def stop_only_queue(value):
            self.events.append(value)
            if value['event']=='worker_state':self.worker.queue.close()
        self.assertEqual(self.worker.run(emit=stop_only_queue),2)
        self.assertIn({'event':'worker_failed','code':'queue_unavailable'},self.events)
        self.assertFalse(self.worker.schedule.is_running());self.assertFalse(self.worker.tasks.is_running())
        self.wire.json.assert_not_called()

    def test_confirmed_queue_runs_without_plan_or_http_and_logs_only_fixed_fields(self):
        self.worker.queue.enqueue({'revision':0,'consent':True,'query':query().payload()})
        with patch.object(socket.socket,'bind',side_effect=AssertionError('must not bind')):
            self.start();wait_for(lambda:len(self.worker.queue.state()['history'])==1);self.stop()
        self.assertEqual(self.results,[0]);self.wire.json.assert_called_once_with(API_URL)
        state=self.worker.queue.state();self.assertEqual(state['status'],'idle')
        self.assertTrue(self.workspace.report(state['history'][0]['report_id']))
        self.assertFalse(self.worker.schedule.path.exists())
        for private in ('Architect','https://',str(self.workspace.root),'source_scope'):
            self.assertNotIn(private,json.dumps(self.events))

    def test_corrupt_queue_is_preserved_and_prevents_due_plan_from_starting(self):
        seed(self.workspace);self.worker.queue.root.mkdir();self.worker.queue.path.write_bytes(b'PRIVATE QUEUE RECORD')
        self.assertEqual(self.worker.run(emit=self.events.append),2)
        self.assertEqual(self.worker.queue.path.read_bytes(),b'PRIVATE QUEUE RECORD')
        self.assertFalse(self.worker.schedule.is_running());self.wire.json.assert_not_called()
        self.assertNotIn('PRIVATE',json.dumps(self.events))

    def test_shutdown_during_read_preserves_checkpoint_without_replaying(self):
        seed(self.workspace);entered=threading.Event();release=threading.Event()
        self.addCleanup(release.set)
        def hold(url):entered.set();release.wait(10);return payload()
        self.wire.json.side_effect=hold;self.start();self.assertTrue(entered.wait(5))
        self.worker.request_stop();self.assertTrue(self.worker.tasks._cancel.wait(5));release.set();self.stop()
        self.assertEqual(self.results,[0]);self.assertEqual(self.wire.json.call_count,1)
        self.assertTrue(self.worker.tasks.path.exists());self.assertTrue(self.worker.schedule.path.exists())
        resumed=PublicWorker(self.workspace,client=LocalPublicDataClient(self.workspace,transport=self.wire))
        resumed.schedule.recover()
        self.assertEqual(resumed.schedule.state()['status'],'paused')
        resumed.schedule.tick();self.assertEqual(self.wire.json.call_count,1);resumed.tasks.close()

    def test_corrupt_record_exits_nonzero_without_overwrite_or_raw_error(self):
        self.worker.schedule.root.mkdir()
        self.worker.schedule.path.write_bytes(b'PRIVATE SECRET NOT SQLITE')
        original=self.worker.schedule.path.read_bytes()
        self.assertEqual(self.worker.run(emit=self.events.append),2)
        self.assertEqual(self.worker.schedule.path.read_bytes(),original)
        self.wire.json.assert_not_called();self.assertNotIn('SECRET',json.dumps(self.events))


class ProcessWorkerTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup)
        self.root=Path(tmp.name);self.workspace=Workspace(self.root);self.processes=[];self.outcomes={}
        self.addCleanup(self.cleanup)
        with patch('urllib.request.getproxies',return_value={}):seed(self.workspace)

    def launch(self,label,mode='normal'):
        env={k:v for k,v in os.environ.items() if not k.startswith('VIBE_RADAR_')}
        env['PYTHONUTF8']='1'
        process=subprocess.Popen([sys.executable,str(Path(__file__).resolve()),'--fixture-worker',str(self.root),label,mode],
            stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,encoding='utf-8',env=env)
        self.processes.append((label,process))
        wait_for(lambda:(self.root/('ready-'+label)).exists())
        return process

    def cleanup(self):
        (self.root/'release-fixture').write_text('release',encoding='utf-8')
        for label,process in self.processes:
            (self.root/('stop-'+label)).write_text('stop',encoding='utf-8')
        for label,process in self.processes:
            try:output,_=process.communicate(timeout=30)
            except subprocess.TimeoutExpired:process.kill();output,_=process.communicate(timeout=5)
            facts=[]
            for line in output.splitlines():
                try:value=json.loads(line)
                except ValueError:continue
                if isinstance(value,dict):
                    facts.append({k:value[k] for k in ('event','code','plan_status') if k in value})
            self.outcomes[label]=facts[-12:]

    def request_count(self):
        path=self.root/'fixture-requests.txt'
        return len(path.read_text(encoding='utf-8').splitlines()) if path.exists() else 0

    def schedule_state(self):
        tasks=PublicTasks(self.workspace,hybrid_client=LocalPublicDataClient(self.workspace))
        try:return PublicSchedule(self.workspace,tasks).state()
        finally:tasks.close()

    def test_two_independent_processes_acquire_only_once_and_keep_report(self):
        first=self.launch('first','hold');wait_for(lambda:self.request_count()==1)
        second=self.launch('second')
        wait_for(lambda:json.loads((self.root/'state-second.json').read_text(encoding='utf-8'))['code']=='owner_busy')
        self.assertEqual(self.request_count(),1)
        (self.root/'release-fixture').write_text('release',encoding='utf-8')
        state=wait_for(lambda:(s if (s:=self.schedule_state())['history'] else None))
        self.assertEqual(state['status'],'scheduled');self.assertEqual(self.request_count(),1)
        self.assertTrue(self.workspace.report(state['history'][0]['report_id']))
        self.cleanup()
        self.assertEqual(first.returncode,0,self.outcomes['first'])
        self.assertEqual(second.returncode,0,self.outcomes['second'])

    def test_killed_owner_leaves_uncertainty_paused_without_second_request(self):
        first=self.launch('first','hold');wait_for(lambda:self.request_count()==1)
        rate=self.root/'public_examples/rates.sqlite';self.assertTrue(rate.exists())
        first.kill();first.communicate(timeout=5)
        original=rate.read_bytes()
        second=self.launch('second')
        wait_for(lambda:self.schedule_state()['status']=='paused')
        self.assertEqual(self.request_count(),1);self.assertEqual(rate.read_bytes(),original)
        self.cleanup();self.assertEqual(second.returncode,0)

    def test_cli_and_workbench_entry_reject_corrupt_plan_without_starting_server(self):
        path=self.root/'public_schedule/schedule.sqlite';path.write_bytes(b'PRIVATE SECRET')
        env=dict(os.environ);env['PYTHONPATH']=os.pathsep.join((str(ROOT/'src'),str(ROOT/'tests')))
        for command in ([sys.executable,str(ROOT/'scripts/run_public_worker.py'),'--workspace',str(self.root)],
                        [sys.executable,str(ROOT/'scripts/start_workbench.py'),'--workspace',str(self.root),'--public-worker'],
                        [sys.executable,'-m','vibe_job_radar','public-worker','--workspace',str(self.root)]):
            run=subprocess.run(command,cwd=ROOT,capture_output=True,text=True,encoding='utf-8',timeout=15,env=env)
            self.assertEqual(run.returncode,2)
            self.assertNotIn('SECRET',run.stdout+run.stderr)
            self.assertNotIn('http://',run.stdout+run.stderr)
            self.assertEqual(path.read_bytes(),b'PRIVATE SECRET')

    def test_real_signals_do_not_reenter_event_locks_and_handlers_are_restored(self):
        with tempfile.TemporaryDirectory(prefix='radar-signal-fixture-') as temp:
            run=subprocess.run([sys.executable,str(Path(__file__).resolve()),'--signal-worker',temp],
                capture_output=True,text=True,encoding='utf-8',timeout=12)
        self.assertEqual(run.returncode,0,run.stderr)
        self.assertTrue(json.loads(run.stdout)['success'])
        self.assertGreaterEqual(json.loads(run.stdout)['signals_checked'],2)


class ProcessQueueWorkerTests(unittest.TestCase):
    launch=ProcessWorkerTests.launch
    cleanup=ProcessWorkerTests.cleanup
    request_count=ProcessWorkerTests.request_count

    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup)
        self.root=Path(tmp.name);self.workspace=Workspace(self.root);self.processes=[];self.outcomes={}
        self.addCleanup(self.cleanup)
        env=patch.dict(os.environ,{k:v for k,v in os.environ.items() if not k.startswith('VIBE_RADAR_')},clear=True)
        env.start();self.addCleanup(env.stop)
        with patch('urllib.request.getproxies',return_value={}):
            worker=PublicWorker(self.workspace)
            try:worker.queue.enqueue({'revision':0,'consent':True,'query':query().payload()})
            finally:worker.tasks.close()

    def queue_state(self):
        tasks=PublicTasks(self.workspace,hybrid_client=LocalPublicDataClient(self.workspace))
        try:return PublicQueue(self.workspace,tasks).state()
        finally:tasks.close()

    def test_two_real_workers_only_dispatch_once_and_observer_can_pause_owner(self):
        first=self.launch('queue-first','hold');wait_for(lambda:self.request_count()==1)
        second=self.launch('queue-second')
        wait_for(lambda:json.loads((self.root/'state-queue-second.json').read_text(encoding='utf-8'))['queue_code']=='owner_busy')
        observer=PublicWorker(self.workspace)
        try:observer.queue.pause({'revision':observer.queue.state()['revision']})
        finally:observer.tasks.close()
        def cancelled():
            tasks=PublicTasks(self.workspace,hybrid_client=LocalPublicDataClient(self.workspace))
            try:return tasks.snapshot()['status']=='cancelling'
            finally:tasks.close()
        wait_for(cancelled);self.assertEqual(self.request_count(),1)
        (self.root/'release-fixture').write_text('release',encoding='utf-8')
        state=wait_for(lambda:(s if (s:=self.queue_state())['history'] else None))
        self.assertEqual(state['status'],'paused');self.assertEqual(state['history'][0]['status'],'cancelled')
        self.cleanup();self.assertEqual(first.returncode,0,self.outcomes['queue-first']);self.assertEqual(second.returncode,0,self.outcomes['queue-second'])
        self.assertFalse((self.root/'public_schedule').exists());self.assertEqual(self.request_count(),1)

    def test_killed_queue_owner_never_replays_and_keeps_unstarted_entry_paused(self):
        with patch('urllib.request.getproxies',return_value={}):
            seed_worker=PublicWorker(self.workspace)
            try:seed_worker.queue.enqueue({'revision':seed_worker.queue.state()['revision'],'consent':True,'query':query(query='Engineer').payload()})
            finally:seed_worker.tasks.close()
        first=self.launch('queue-first','hold');wait_for(lambda:self.request_count()==1)
        rate=self.root/'public_examples/rates.sqlite';first.kill();first.communicate(timeout=5);original=rate.read_bytes()
        second=self.launch('queue-second');wait_for(lambda:self.queue_state()['status']=='paused')
        state=self.queue_state();self.assertEqual(state['history'][0]['status'],'interrupted')
        self.assertEqual(len(state['items']),1);self.assertEqual(state['items'][0]['phase'],'pending')
        self.assertEqual(self.request_count(),1);self.assertEqual(rate.read_bytes(),original)
        self.cleanup();self.assertEqual(second.returncode,0,self.outcomes['queue-second'])


if __name__=='__main__':
    if len(sys.argv)>1 and sys.argv[1]=='--fixture-worker':
        raise SystemExit(fixture_child(Path(sys.argv[2]),sys.argv[3],sys.argv[4]))
    if len(sys.argv)>1 and sys.argv[1]=='--signal-worker':
        raise SystemExit(signal_child(Path(sys.argv[2])))
    unittest.main()
