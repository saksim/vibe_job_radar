"""Daily scheduler exercises real tasks/cache/reports with an artificial source."""
from contextlib import closing
from dataclasses import replace
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

from test_local_public import payload, query
import test_workbench as http_fixtures
from vibe_job_radar.local_public import LocalPublicDataClient, SOURCE
from vibe_job_radar.network import FetchError
from vibe_job_radar.public_schedule import DAY, PublicSchedule, ScheduleBusy
from vibe_job_radar.public_tasks import PublicTasks
from vibe_job_radar.workspace import InputError, Workspace


class ScheduleTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup)
        self.workspace=Workspace(tmp.name);self.now=[time.time()]
        clean={k:v for k,v in os.environ.items() if not k.startswith('VIBE_RADAR_')}
        env=patch.dict(os.environ,clean,clear=True);env.start();self.addCleanup(env.stop)
        proxies=patch('urllib.request.getproxies',return_value={});proxies.start();self.addCleanup(proxies.stop)
        self.transport=Mock();self.transport.json.return_value=payload()
        self.client=LocalPublicDataClient(self.workspace,transport=self.transport,clock=lambda:self.now[0])
        self.tasks=PublicTasks(self.workspace,hybrid_client=self.client);self.addCleanup(self.tasks.close)
        self.schedule=PublicSchedule(self.workspace,self.tasks,clock=lambda:self.now[0]);self.addCleanup(self.schedule.close)
        self.entered,self.release=threading.Event(),threading.Event();self.addCleanup(self.release.set)

    def configure(self,**overrides):
        return self.schedule.configure({'query':query().payload(),'consent':True,
            'revision':self.schedule.state()['revision'],**overrides})

    def wait(self):
        # The background scheduler may publish a Thread immediately before
        # start(). Take the same lock as submit before inspecting/joining it.
        with self.tasks._lock:worker=self.tasks._thread
        worker.join(15)
        self.assertFalse(worker.is_alive())
        return self.tasks._state.copy()

    def hold(self,url):
        self.entered.set()
        if not self.release.wait(15):raise AssertionError('fixture transport not released')
        return payload()

    def finish(self):
        task=self.wait();self.schedule.tick();return task

    def test_default_and_configuration_are_offline_first_due_is_24_hours(self):
        self.schedule.start()
        self.assertEqual(self.schedule.state()['status'],'disabled')
        self.assertFalse(self.schedule.path.exists())
        value=self.configure();self.assertEqual(value['next_due'],self.now[0]+DAY)
        self.schedule.close()  # Deterministic tick below, no background race.
        self.schedule._stop.clear()
        self.now[0]+=DAY-1;self.schedule.tick();self.assertIsNone(self.tasks._thread)
        self.transport.json.assert_not_called()
        self.now[0]+=1;self.schedule.tick();task=self.finish()
        self.assertEqual(task['status'],'completed')
        self.assertEqual(self.transport.json.call_count,1)
        state=self.schedule.state();self.assertEqual(state['next_due'],self.now[0]+DAY)
        self.assertEqual(state['history'][0]['report_id'],task['report_id'])
        self.assertTrue(self.workspace.report(task['report_id']))

    def test_week_overdue_runs_once_no_catchup_burst_or_automatic_paging(self):
        self.transport.json.return_value=payload(50)
        self.configure();self.now[0]+=8*DAY;self.schedule.recover();self.schedule.tick()
        task=self.finish();self.assertTrue(task['next_cursor']);self.assertEqual(task['returned_jobs'],20)
        for _ in range(5):self.schedule.tick()
        self.assertEqual(self.transport.json.call_count,1)
        self.assertEqual(len(self.schedule.state()['history']),1)

    def test_invalid_consent_cursor_url_source_and_stale_revision_never_fetch(self):
        for override in ({'consent':False},{'consent':1},{'revision':True},
            {'query':query(cursor='opaque').payload()}, {'query':{**query().payload(),'url':'https://bad.test'}},
            {'query':query(source_scope=('boss',)).payload()}):
            with self.subTest(override=override),self.assertRaises(ValueError):self.configure(**override)
        self.configure()
        with self.assertRaises(InputError):self.configure(revision=0)
        with self.assertRaises(InputError):self.schedule.disable({'revision':0})
        self.transport.json.assert_not_called()

    def test_remote_service_disabled_and_unapproved_source_cannot_schedule(self):
        with patch.object(self.client,'execution_mode','remote_service'),self.assertRaises(InputError):self.configure()
        self.client.registry={SOURCE.key:replace(SOURCE,local_access_approved=False)}
        with self.assertRaises(InputError):self.configure()
        self.transport.json.assert_not_called()

    def test_manual_task_is_never_preempted_then_schedule_uses_same_cache(self):
        self.configure();self.now[0]+=DAY
        self.transport.json.side_effect=self.hold
        manual=self.tasks.search({'consent':True,'query':query().payload()})
        self.assertTrue(self.entered.wait(5));self.schedule.tick()
        self.assertEqual(self.schedule.state()['status'],'scheduled')
        self.assertEqual(self.tasks._state['id'],manual['id'])
        self.release.set();self.wait();self.schedule.tick();task=self.finish()
        self.assertNotEqual(task['id'],manual['id']);self.assertTrue(task['cache_reused'])
        self.assertEqual(self.transport.json.call_count,1)

    def test_stop_inflight_cancels_without_losing_cache_or_starting_next_run(self):
        self.configure();self.now[0]+=DAY;self.transport.json.side_effect=self.hold
        self.schedule.tick();self.assertTrue(self.entered.wait(5))
        self.schedule.disable({'revision':self.schedule.state()['revision']})
        self.assertTrue(self.tasks._cancel.is_set())
        self.release.set();self.assertEqual(self.finish()['status'],'cancelled')
        self.now[0]+=DAY;self.schedule.tick()
        self.assertEqual(self.schedule.state()['status'],'disabled')
        self.assertTrue(self.client.path.exists());self.assertEqual(self.transport.json.call_count,1)

    def test_stop_after_commit_started_preserves_finished_report(self):
        self.configure();self.now[0]+=DAY;original=self.tasks._import
        def held(*args):
            self.entered.set();self.release.wait(15);return original(*args)
        with patch.object(self.tasks,'_import',side_effect=held):
            self.schedule.tick();self.assertTrue(self.entered.wait(5))
            self.schedule.disable({'revision':self.schedule.state()['revision']})
            self.release.set();task=self.finish()
        self.assertEqual(task['status'],'completed');self.assertTrue(self.workspace.report(task['report_id']))
        self.assertEqual(self.schedule.state()['status'],'disabled')

    def test_source_change_or_route_change_pauses_without_fetching(self):
        self.configure();self.now[0]+=DAY
        self.client.registry={SOURCE.key:replace(SOURCE,contract='different contract')}
        self.schedule.tick();self.assertEqual(self.schedule.state()['code'],'conditions_changed')
        self.client.registry={SOURCE.key:SOURCE};self.configure();self.now[0]+=DAY
        policy=self.workspace.network_policy()
        with patch.object(self.workspace,'network_policy',return_value=replace(policy,encrypted_dns=True)):
            self.schedule.tick()
        self.assertEqual(self.schedule.state()['code'],'conditions_changed');self.transport.json.assert_not_called()

    def test_failed_fetch_pauses_and_preserves_the_original_rate_budget(self):
        self.configure();self.now[0]+=DAY;self.transport.json.side_effect=FetchError('http_403')
        self.schedule.tick();self.assertEqual(self.finish()['status'],'failed')
        self.now[0]+=DAY;self.schedule.tick()
        self.assertEqual(self.schedule.state()['code'],'result_attention')
        self.assertEqual(self.transport.json.call_count,1)
        self.assertTrue((self.workspace.root/'public_examples'/'rates.sqlite').exists())

    def test_stale_cached_result_retains_report_but_pauses_schedule(self):
        self.tasks.search({'consent':True,'query':query().payload()});self.wait()
        self.configure();self.now[0]+=DAY;self.transport.json.side_effect=FetchError('http_503')
        self.schedule.tick();task=self.finish()
        self.assertTrue(task['stale']);self.assertTrue(self.workspace.report(task['report_id']))
        self.assertEqual(self.schedule.state()['code'],'result_attention')
        self.assertTrue(self.schedule.state()['history'][0]['stale'])

    def test_clock_rollback_during_committed_result_stays_paused(self):
        self.configure();self.now[0]+=DAY;self.schedule.tick();task=self.wait()
        self.now[0]-=1;self.schedule.tick()
        state=self.schedule.state();self.assertEqual(state['code'],'clock_rollback')
        self.assertEqual(state['status'],'paused');self.assertIsNone(state['next_due'])
        self.assertEqual(state['history'][0]['report_id'],task['report_id'])

    def test_shutdown_during_due_decision_does_not_dispatch(self):
        self.configure();self.now[0]+=DAY;self.schedule.close();self.schedule.tick()
        self.transport.json.assert_not_called();self.assertEqual(self.schedule.state()['status'],'scheduled')

    def test_confirmed_route_is_frozen_if_preferences_change_before_worker_runs(self):
        self.configure();self.now[0]+=DAY
        approved=self.workspace.network_policy();changed=replace(approved,encrypted_dns=True)
        real_run=self.tasks._run
        def held(*args):
            self.entered.set();self.release.wait(15);real_run(*args)
        # Exercise the default transport branch without making a real request.
        self.client._default_transport=True
        with patch.object(self.tasks,'_run',side_effect=held):
            self.schedule.tick();self.assertTrue(self.entered.wait(5))
            with patch.object(self.workspace,'network_policy',return_value=changed):
                self.release.set();self.finish()
                self.assertEqual(self.client.client.network_policy.fingerprint,approved.fingerprint)
                self.now[0]+=DAY;self.schedule.tick()
        self.assertEqual(self.schedule.state()['code'],'conditions_changed')
        self.assertEqual(self.transport.json.call_count,1)

    def test_clock_pause_is_not_rewritten_on_every_poll(self):
        self.configure();self.now[0]-=10;self.schedule.tick()
        revision=self.schedule.state()['revision']
        for _ in range(3):self.schedule.tick()
        self.assertEqual(self.schedule.state()['revision'],revision)

    def test_uncertain_dispatch_before_task_id_is_never_replayed(self):
        self.configure();value=self.schedule._read()
        value.update(status='dispatching',code='dispatching',attempt_id='a'*32)
        self.schedule._write(value)
        restarted=PublicSchedule(self.workspace,self.tasks,clock=lambda:self.now[0])
        restarted.recover();self.now[0]+=2*DAY;restarted.tick()
        self.assertEqual(restarted.state()['code'],'interrupted');self.transport.json.assert_not_called()

    def test_restart_reconciles_known_complete_report_without_replaying(self):
        self.configure();self.now[0]+=DAY;self.schedule.tick();task=self.wait()
        restarted_tasks=PublicTasks(self.workspace,hybrid_client=self.client);self.addCleanup(restarted_tasks.close)
        restarted=PublicSchedule(self.workspace,restarted_tasks,clock=lambda:self.now[0])
        restarted.recover();restarted.tick()
        state=restarted.state();self.assertEqual(state['status'],'scheduled')
        self.assertEqual(state['history'][0]['report_id'],task['report_id'])
        self.assertEqual(self.transport.json.call_count,1)

    def test_replaced_completed_task_uses_original_receipt_and_preserves_manual_work(self):
        self.configure();self.now[0]+=DAY;self.schedule.tick();original=self.wait()
        manual=self.tasks.search({'consent':True,'query':query(query='Engineer').payload()})
        self.schedule.tick();self.assertEqual(self.schedule.state()['status'],'scheduled')
        self.assertEqual(self.schedule.state()['history'][0]['report_id'],original['report_id'])
        self.assertEqual(self.tasks._state['id'],manual['id']);self.assertFalse(self.tasks._cancel.is_set());self.wait()

    def test_missing_outcome_still_pauses_without_cancelling_manual_work(self):
        self.configure();self.now[0]+=DAY;self.schedule.tick();self.wait()
        manual=self.tasks.search({'consent':True,'query':query(query='Engineer').payload()})
        (self.tasks.root/'outcomes-v1.sqlite').unlink()
        self.schedule.tick();self.assertEqual(self.schedule.state()['code'],'interrupted')
        self.assertEqual(self.tasks._state['id'],manual['id']);self.assertFalse(self.tasks._cancel.is_set());self.wait()

    def test_receipt_write_failure_refuses_new_query_and_preserves_original_record_report_and_budget(self):
        self.configure();self.now[0]+=DAY;self.schedule.tick();original=self.wait()
        before=self.tasks.path.read_bytes()
        report=self.workspace.root/'reports'/original['report_id']/'requirements.csv';csv=report.read_bytes()
        with patch('vibe_job_radar.public_tasks.PublicOutcomes.remember',side_effect=InputError('fixture failure')):
            with self.assertRaises(InputError):self.tasks.search({'consent':True,'query':query(query='Engineer').payload()})
        self.assertEqual(self.tasks.path.read_bytes(),before);self.assertEqual(report.read_bytes(),csv)
        self.assertEqual(self.transport.json.call_count,1);self.assertIsNone(self.tasks._lease)
        self.schedule.tick();self.assertEqual(self.schedule.state()['status'],'scheduled')

    def test_manual_resume_is_a_different_attempt_and_disabling_plan_does_not_cancel_it(self):
        self.configure();self.now[0]+=DAY;self.transport.json.side_effect=self.hold
        self.schedule.tick();self.assertTrue(self.entered.wait(5))
        original=self.tasks.snapshot();self.tasks.cancel({'id':original['id']})
        self.release.set();self.assertEqual(self.wait()['status'],'cancelled')
        self.entered.clear();self.release.clear();original_import=self.tasks._import
        def held(*args):
            self.entered.set();self.release.wait(15);return original_import(*args)
        with patch.object(self.tasks,'_import',side_effect=held):
            self.tasks.resume({'id':original['id'],'consent':True});self.assertTrue(self.entered.wait(5))
            self.schedule.disable({'revision':self.schedule.state()['revision']})
            self.assertFalse(self.tasks._cancel.is_set())
            self.release.set();manual=self.wait();self.schedule.tick()
        self.assertEqual((manual['id'],manual['attempt'],manual['status']),(original['id'],2,'completed'))
        state=self.schedule.state();self.assertEqual(state['status'],'disabled')
        self.assertEqual(state['history'][0]['status'],'cancelled');self.assertEqual(state['history'][0]['report_id'],'')
        self.assertTrue(self.workspace.report(manual['report_id']));self.assertEqual(self.transport.json.call_count,1)

    def test_another_process_replaces_completed_slot_and_restart_finds_original_report(self):
        self.now[0]-=DAY;self.configure();self.now[0]+=DAY;self.schedule.tick();original=self.wait()
        report=self.workspace.root/'reports'/original['report_id']/'requirements.csv';before=report.read_bytes()
        code="""import json,sys
from unittest.mock import Mock,patch
from test_local_public import query
from vibe_job_radar.workspace import Workspace
from vibe_job_radar.local_public import LocalPublicDataClient
from vibe_job_radar.public_tasks import PublicTasks
wire=Mock();wire.json.side_effect=AssertionError('cached result expected')
with patch('urllib.request.getproxies',return_value={}):
 tasks=PublicTasks(Workspace(sys.argv[1]),hybrid_client=LocalPublicDataClient(Workspace(sys.argv[1]),transport=wire))
 try:
  tasks.search({'consent':True,'query':query(query='Engineer').payload()})
  tasks._thread.join(15);state=tasks.snapshot()
  assert state['status']=='completed' and wire.json.call_count==0
  print(json.dumps({'id':state['id'],'report_id':state['report_id']}))
 finally:tasks.close()
"""
        checkout=Path(__file__).resolve().parents[1]
        env={**os.environ,'PYTHONPATH':os.pathsep.join([str(checkout/'src'),str(checkout/'tests')])}
        child=subprocess.run([sys.executable,'-c',code,str(self.workspace.root)],capture_output=True,text=True,
            env=env,timeout=25,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        self.assertEqual(child.returncode,0,child.stderr);manual=json.loads(child.stdout)
        self.assertNotEqual(manual['id'],original['id'])
        restarted_tasks=PublicTasks(self.workspace,hybrid_client=self.client);self.addCleanup(restarted_tasks.close)
        restarted=PublicSchedule(self.workspace,restarted_tasks,clock=lambda:self.now[0])
        restarted.recover();state=restarted.state()
        self.assertEqual(state['status'],'scheduled');self.assertEqual(state['history'][0]['report_id'],original['report_id'])
        self.assertEqual(report.read_bytes(),before);self.assertTrue(self.workspace.report(manual['report_id']))
        self.assertEqual(self.transport.json.call_count,1)

    def test_storage_failure_after_submit_stops_owner_without_retry(self):
        self.configure();self.now[0]+=DAY;original=self.schedule._write
        def broken(value,**kw):
            if value['status']=='running':raise InputError('artificial write failure')
            return original(value,**kw)
        with patch.object(self.schedule,'_write',side_effect=broken):
            self.schedule.start();self.schedule._thread.join(10)
        self.assertFalse(self.schedule._thread.is_alive());self.wait()
        self.assertEqual(self.schedule.state()['code'],'storage_error')
        self.assertEqual(self.schedule._read()['status'],'dispatching')
        with self.assertRaises(InputError):self.configure()
        self.assertEqual(self.transport.json.call_count,1)

    def test_duplicate_keys_bad_schema_and_unsafe_time_fail_closed(self):
        self.configure();good=json.dumps(self.schedule._read())
        for raw in ('{"schema_version":1,"schema_version":1}',good.replace('"schema_version": 1','"schema_version": 99'),good.replace('"last_seen":','"unknown":')):
            with closing(sqlite3.connect(self.schedule.path)) as db:
                db.execute('UPDATE schedule SET payload=?',(raw,));db.commit()
            with self.subTest(raw=raw[:60]),self.assertRaises(InputError):self.schedule.tick()
        self.transport.json.assert_not_called()

    def test_another_process_cannot_dispatch_while_owner_lease_is_held(self):
        self.configure();self.now[0]+=DAY
        # Real second Python process acquires the same OS owner file.
        owner=self.schedule.root/'owner';owner.mkdir()
        code="from pathlib import Path;from vibe_job_radar.collection import writer_lock;import sys\nwith writer_lock(Path(sys.argv[1])):\n print('locked',flush=True);sys.stdin.readline()"
        # The test runner adds src to its own sys.path, not the child's.
        # Explicitly use this checkout, independent of shell PYTHONPATH/install.
        child_env={**os.environ,'PYTHONPATH':str(Path(__file__).resolve().parents[1]/'src')}
        child=subprocess.Popen([sys.executable,'-u','-c',code,str(owner)],stdin=subprocess.PIPE,stdout=subprocess.PIPE,text=True,env=child_env)
        self.addCleanup(lambda:child.poll() is None and child.kill())
        try:
            self.assertEqual(child.stdout.readline().strip(),'locked')
            self.schedule.start()
            deadline=time.monotonic()+5
            while not self.schedule._error and time.monotonic()<deadline:time.sleep(.02)
            self.assertEqual(self.schedule.state()['code'],'owner_busy');self.transport.json.assert_not_called()
            child.communicate('\n',timeout=5);self.schedule._wake.set()
            deadline=time.monotonic()+10
            while self.tasks._thread is None and time.monotonic()<deadline:time.sleep(.02)
            self.wait();self.schedule.close()
            self.assertEqual(self.transport.json.call_count,1)
        finally:
            if child.poll() is None:child.communicate('\n',timeout=5)


class ScheduleHTTPTests(unittest.TestCase):
    setUp=http_fixtures.HTTPTests.setUp
    tearDown=http_fixtures.HTTPTests.tearDown
    call=http_fixtures.HTTPTests.call

    def test_authenticated_local_save_state_stop_without_upstream(self):
        data={'query':query().payload(),'consent':True,'revision':0}
        base='/api/public/schedule/'
        self.assertEqual(self.call(base+'state',authorized=False)[0],403)
        self.assertEqual(self.call(base+'configure',data,headers={'Origin':'https://bad.test'})[0],403)
        self.assertFalse(self.server.public_schedule.path.exists())
        code,_,raw=self.call(base+'configure',data);self.assertEqual(code,200,raw)
        value=json.loads(raw);self.assertEqual(value['status'],'scheduled')
        self.assertEqual(self.call(base+'configure',data)[0],400)
        self.assertEqual(self.call(base+'disable',{'revision':value['revision']})[0],200)
        self.assertIsNone(self.server.public_tasks._thread)
        self.assertIn(b'public-schedule.js',self.call('/')[2])
        self.assertNotIn(b'innerHTML',self.call('/public-schedule.js')[2])
