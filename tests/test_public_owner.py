"""Same-workspace task ownership, including real process death; artificial data."""
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from test_local_public import payload,query
from vibe_job_radar.local_public import LocalPublicDataClient
from vibe_job_radar.public_tasks import PublicTasks,PublicTaskBusy
from vibe_job_radar.public_schedule import PublicSchedule,DAY
from vibe_job_radar.workspace import InputError,Workspace


CHILD = r'''
import json,os,sys,threading
from unittest.mock import Mock,patch
from test_local_public import payload,query
from vibe_job_radar.workspace import Workspace
from vibe_job_radar.local_public import LocalPublicDataClient
from vibe_job_radar.public_tasks import PublicTasks
entered,release=threading.Event(),threading.Event()
def held(url):
 entered.set()
 if not release.wait(30):raise AssertionError('parent did not release fixture')
 return payload()
with patch('urllib.request.getproxies',return_value={}):
 w=Workspace(sys.argv[1]);transport=Mock();transport.json.side_effect=held
 tasks=PublicTasks(w,hybrid_client=LocalPublicDataClient(w,transport=transport,clock=lambda:float(sys.argv[2])))
 try:
  ident=tasks.search({'consent':True,'query':query().payload()})['id']
  assert entered.wait(5)
  print(json.dumps({'id':ident}),flush=True)
  action=sys.stdin.readline().strip()
  if action=='crash':os._exit(23)
  if action=='cancel':tasks.cancel({'id':ident})
  release.set();tasks._thread.join(15)
  assert not tasks._thread.is_alive()
  print(json.dumps(tasks.snapshot()),flush=True)
 finally:release.set();tasks.close()
'''


class OwnerTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup)
        self.workspace=Workspace(tmp.name);self.now=[time.time()]
        env=patch.dict(os.environ,{k:v for k,v in os.environ.items() if not k.startswith('VIBE_RADAR_')},clear=True)
        env.start();self.addCleanup(env.stop)
        proxy=patch('urllib.request.getproxies',return_value={});proxy.start();self.addCleanup(proxy.stop)
        self.entered,self.release=threading.Event(),threading.Event()
        self.first,self.first_transport=self.instance();self.second,self.second_transport=self.instance()
        self.addCleanup(self.release.set)

    def instance(self):
        transport=Mock();transport.json.return_value=payload()
        task=PublicTasks(self.workspace,hybrid_client=LocalPublicDataClient(self.workspace,transport=transport,clock=lambda:self.now[0]))
        self.addCleanup(task.close);return task,transport

    def start(self,task=None):return (task or self.first).search({'consent':True,'query':query().payload()})['id']

    def wait(self,task=None):
        task=task or self.first;task._thread.join(15);self.assertFalse(task._thread.is_alive());return task.snapshot()

    def hold(self,url):
        self.entered.set()
        if not self.release.wait(15):raise AssertionError('fixture not released')
        return payload()

    def running(self):
        self.first_transport.json.side_effect=self.hold
        ident=self.start();self.assertTrue(self.entered.wait(5));return ident

    def cancelled(self):
        ident=self.running();self.first.cancel({'id':ident});self.release.set()
        self.assertEqual(self.wait()['status'],'cancelled');return ident

    def child(self):
        root=Path(__file__).resolve().parents[1]
        env={**os.environ,'PYTHONPATH':os.pathsep.join([str(root/'src'),str(root/'tests')])}
        child=subprocess.Popen([sys.executable,'-u','-c',CHILD,str(self.workspace.root),str(self.now[0])],
            env=env,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        def cleanup():
            if child.poll() is None:child.kill()
            child.communicate(timeout=5)
        self.addCleanup(cleanup)
        line=child.stdout.readline()
        self.assertTrue(line,'child failed before task start')
        return child,json.loads(line)['id']

    def test_second_instance_cannot_overwrite_or_cancel_running_task(self):
        ident=self.running();before=self.first.path.read_bytes()
        for other in (self.second,self.instance()[0]):
            state=other.snapshot();self.assertEqual(state['id'],ident)
            self.assertTrue(state['owned_elsewhere']);self.assertEqual(state['status'],'running')
            self.assertFalse(state['can_cancel']);self.assertFalse(state['can_resume'])
            with self.assertRaises(PublicTaskBusy):self.start(other)
            with self.assertRaises(PublicTaskBusy):other.cancel({'id':ident})
            with self.assertRaises(InputError):other.resume({'id':ident,'consent':True})
            self.assertIsNone(other._thread)
        self.assertFalse(self.first._cancel.is_set());self.assertEqual(self.first.path.read_bytes(),before)
        self.second_transport.json.assert_not_called();self.release.set();self.wait()

    def test_late_observer_reads_final_report_and_can_start_after_owner_finishes(self):
        ident=self.running();self.second.snapshot();self.release.set();finished=self.wait()
        self.assertEqual(self.second.snapshot()['report_id'],finished['report_id'])
        self.assertFalse(self.second.snapshot()['owned_elsewhere'])
        next_id=self.start(self.second);self.assertNotEqual(next_id,ident)
        self.assertEqual(self.wait(self.second)['status'],'completed')
        self.second_transport.json.assert_not_called()  # Existing cache, no quota reset.
        self.assertTrue(self.workspace.report(finished['report_id']))

    def test_confirmed_resume_after_owner_cancel_reuses_same_id_and_cache(self):
        ident=self.cancelled()
        self.assertTrue(self.second.snapshot()['can_resume'])
        self.second.resume({'id':ident,'consent':True});state=self.wait(self.second)
        self.assertEqual((state['id'],state['attempt'],state['status']),(ident,2,'completed'))
        self.second_transport.json.assert_not_called()

    def test_old_instance_cannot_resume_or_cancel_replaced_task(self):
        old=self.cancelled();self.second.snapshot()
        new=self.start();finished=self.wait();before=self.first.path.read_bytes()
        with self.assertRaises(InputError):self.second.resume({'id':old,'consent':True})
        with self.assertRaises(InputError):self.second.cancel({'id':old})
        self.assertNotEqual(old,new);self.assertEqual(self.first.path.read_bytes(),before)
        self.assertEqual(self.second.snapshot()['report_id'],finished['report_id'])

    def test_resume_rechecks_disk_after_ownership_acquisition(self):
        old=self.cancelled();self.second.snapshot();original=self.second._submit
        new=[]
        def racing(*args,**kwargs):
            new.append(self.start());self.wait();return original(*args,**kwargs)
        with patch.object(self.second,'_submit',side_effect=racing),self.assertRaises(InputError):
            self.second.resume({'id':old,'consent':True})
        self.assertEqual(self.second.snapshot()['id'],new[0]);self.assertIsNone(self.second._lease)
        self.second_transport.json.assert_not_called()

    def test_failure_to_save_or_start_thread_releases_lease(self):
        for failing in ('save','thread'):
            context=(patch.object(self.first,'_save',side_effect=OSError('artificial disk error')) if failing=='save'
                     else patch('vibe_job_radar.public_tasks.threading.Thread.start',side_effect=RuntimeError('artificial start failure')))
            with self.subTest(failing=failing),context,self.assertRaises((OSError,RuntimeError)):self.start()
            self.assertIsNone(self.first._lease);self.assertFalse(self.second.busy())
        self.start(self.second);self.assertEqual(self.wait(self.second)['status'],'completed')

    def test_deleted_record_does_not_resume_stale_in_memory_state(self):
        ident=self.cancelled();self.second.snapshot();self.first.path.unlink()
        with self.assertRaises(InputError):self.second.resume({'id':ident,'consent':True})
        self.assertEqual(self.second.snapshot()['status'],'idle');self.second_transport.json.assert_not_called()

    def test_actual_process_owner_blocks_submission_and_graceful_exit_preserves_report(self):
        child,ident=self.child();before=self.first.path.read_bytes()
        self.assertTrue(self.second.snapshot()['owned_elsewhere'])
        with self.assertRaises(PublicTaskBusy):self.start(self.second)
        self.assertEqual(self.first.path.read_bytes(),before)
        stdout,stderr=child.communicate('finish\n',timeout=15)
        self.assertEqual(child.returncode,0,stderr);complete=json.loads(stdout)
        self.assertEqual(self.second.snapshot()['id'],ident)
        self.assertEqual(self.second.snapshot()['report_id'],complete['report_id'])
        self.assertTrue(self.workspace.report(complete['report_id']))

    def test_actual_process_crash_releases_owner_but_requires_explicit_resume(self):
        child,ident=self.child();child.communicate('crash\n',timeout=5)
        self.assertEqual(child.returncode,23)
        state=self.second.snapshot();self.assertEqual(state['status'],'interrupted')
        self.assertTrue(state['can_resume']);self.assertFalse(state['owned_elsewhere'])
        self.assertIsNone(self.second._thread);self.second_transport.json.assert_not_called()
        self.now[0]+=31  # Honor the existing failed-attempt minimum interval.
        self.second.resume({'id':ident,'consent':True});complete=self.wait(self.second)
        self.assertEqual((complete['id'],complete['attempt'],complete['status']),(ident,2,'completed'))
        with closing(sqlite3.connect(self.workspace.root/'public_examples'/'rates.sqlite')) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM visits').fetchone()[0],2)

    def test_schedule_waits_for_other_owner_without_rewriting_plan_or_task(self):
        scheduler=PublicSchedule(self.workspace,self.second,clock=lambda:self.now[0]);self.addCleanup(scheduler.close)
        scheduler.configure({'consent':True,'query':query().payload(),'revision':0});self.now[0]+=DAY
        self.running();before=self.first.path.read_bytes();revision=scheduler.state()['revision']
        scheduler.tick();self.assertEqual(scheduler.state()['revision'],revision)
        self.assertEqual(self.first.path.read_bytes(),before)
        # Race: another owner acquired between the advisory check and submit.
        with patch.object(self.second,'busy',return_value=False):scheduler.tick()
        self.assertEqual(scheduler.state()['status'],'scheduled');self.assertEqual(scheduler._read()['attempt_id'],'')
        self.assertEqual(self.first.path.read_bytes(),before)
        self.release.set();self.wait();scheduler.tick();self.wait(self.second);scheduler.tick()
        self.assertEqual(len(scheduler.state()['history']),1);self.second_transport.json.assert_not_called()

    def test_scheduler_handoff_waits_for_still_owned_task_and_keeps_report(self):
        scheduler=PublicSchedule(self.workspace,self.second,clock=lambda:self.now[0]);self.addCleanup(scheduler.close)
        scheduler.configure({'consent':True,'query':query().payload(),'revision':0})
        ident=self.running();value=scheduler._read()
        value.update(status='running',code='running',attempt_id='a'*32,active_task_id=ident)
        scheduler._write(value);scheduler.recover()
        self.assertEqual(scheduler.state()['status'],'running')
        scheduler.disable({'revision':scheduler.state()['revision']});scheduler.tick()
        self.assertFalse(self.first._cancel.is_set())  # This instance cannot steal another owner's controls.
        self.release.set();complete=self.wait();scheduler.tick()
        self.assertEqual(scheduler.state()['status'],'disabled')
        self.assertEqual(scheduler.state()['history'][0]['report_id'],complete['report_id'])

    def test_concurrent_observer_reads_do_not_break_atomic_saves(self):
        self.running();errors=[]
        def observer():
            try:
                for _ in range(60):self.assertTrue(self.second.snapshot()['owned_elsewhere'])
            except BaseException as exc:errors.append(exc)
        worker=threading.Thread(target=observer);worker.start()
        for i in range(60):self.first._save(phase='fixture_progress_'+str(i))
        worker.join(10);self.assertFalse(worker.is_alive());self.assertEqual(errors,[])
        self.release.set();self.assertEqual(self.wait()['status'],'completed')
