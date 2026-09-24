"""Finite persisted queries, real original tasks/reports and artificial source."""
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
from unittest.mock import Mock,patch

from test_local_public import payload,query
from vibe_job_radar.local_public import LocalPublicDataClient,SCOPE
from vibe_job_radar.network import FetchError
from vibe_job_radar.guided.rate import RateLimit
from vibe_job_radar.public_queue import PublicQueue,DAY,GAP,CAPACITY
from vibe_job_radar.public_tasks import PublicTasks
from vibe_job_radar.public_outcomes import PublicOutcomes
from vibe_job_radar.workspace import Workspace,InputError
import test_workbench as http_fixtures


class QueueTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup)
        self.workspace=Workspace(tmp.name);self.now=[time.time()]
        env=patch.dict(os.environ,{k:v for k,v in os.environ.items() if not k.startswith('VIBE_RADAR_')},clear=True)
        env.start();self.addCleanup(env.stop)
        proxy=patch('urllib.request.getproxies',return_value={});proxy.start();self.addCleanup(proxy.stop)
        self.wire=Mock();self.wire.json.return_value=payload()
        self.client=LocalPublicDataClient(self.workspace,transport=self.wire,clock=lambda:self.now[0])
        self.tasks=PublicTasks(self.workspace,hybrid_client=self.client);self.addCleanup(self.tasks.close)
        self.queue=PublicQueue(self.workspace,self.tasks,clock=lambda:self.now[0]);self.addCleanup(self.queue.close)
        self.entered,self.release=threading.Event(),threading.Event();self.addCleanup(self.release.set)

    def enqueue(self,word='Architect',**overrides):
        return self.queue.enqueue({'revision':self.queue.state()['revision'],'consent':True,
            'query':query(query=word).payload(),**overrides})

    def wait(self):
        with self.tasks._lock:worker=self.tasks._thread
        worker.join(15);self.assertFalse(worker.is_alive());return self.tasks.snapshot()

    def finish(self):
        result=self.wait();self.queue.tick();return result

    def hold(self,url):
        self.entered.set()
        if not self.release.wait(15):raise AssertionError('fixture not released')
        return payload()

    def test_empty_queue_and_enqueue_are_offline_and_first_execution_is_explicit(self):
        self.assertEqual(self.queue.state()['status'],'idle');self.assertFalse(self.queue.root.exists())
        self.queue.start();self.queue.close();self.assertFalse(self.queue.root.exists());self.queue._stop.clear()
        self.enqueue();self.assertIsNone(self.tasks._thread);self.wire.json.assert_not_called()
        self.queue.tick();task=self.finish();state=self.queue.state()
        self.assertEqual((task['status'],state['status']),('completed','idle'))
        self.assertEqual(state['history'][0]['report_id'],task['report_id'])

    def test_fifo_minimum_gap_and_original_cache_reports(self):
        first=self.enqueue()['items'][0]['id'];second=self.enqueue('Engineer')['items'][1]['id']
        self.queue.tick();one=self.finish();self.assertEqual(self.queue.state()['status'],'queued')
        self.queue.tick();self.assertEqual(self.tasks.snapshot()['id'],one['id'])
        self.now[0]+=GAP;self.queue.tick();two=self.finish()
        state=self.queue.state();self.assertEqual(state['status'],'idle')
        self.assertEqual([row['id'] for row in state['history']],[first,second])
        self.assertNotEqual(one['report_id'],two['report_id']);self.assertEqual(self.wire.json.call_count,1)
        for task in (one,two):self.assertTrue(self.workspace.report(task['report_id']))

    def test_capacity_consent_cursor_limit_and_stale_revision_reject_without_requests(self):
        for override in ({'consent':False},{'revision':True},{'extra':1},
                         {'query':{**query().payload(),'cursor':'opaque'}},{'query':{**query().payload(),'limit':21}}):
            with self.subTest(override=override),self.assertRaises(InputError):self.enqueue(**override)
        self.assertFalse(self.queue.path.exists())
        for index in range(CAPACITY):self.enqueue('Architect '+str(index))
        before=self.queue.path.read_bytes()
        with self.assertRaises(InputError):self.enqueue()
        state=self.queue.state()
        with self.assertRaises(InputError):self.queue.remove({'revision':0,'id':state['items'][0]['id']})
        self.assertEqual(self.queue.path.read_bytes(),before);self.wire.json.assert_not_called()
        self.queue.remove({'revision':state['revision'],'id':state['items'][0]['id']})
        self.assertEqual(len(self.queue.state()['items']),CAPACITY-1)

    def test_busy_manual_task_is_not_overwritten_or_cancelled(self):
        self.wire.json.side_effect=self.hold
        manual=self.tasks.search({'consent':True,'query':query().payload()});self.assertTrue(self.entered.wait(5))
        self.enqueue('Engineer');self.queue.tick()
        self.assertEqual(self.tasks.snapshot()['id'],manual['id']);self.assertFalse(self.tasks._cancel.is_set())
        self.assertEqual(self.queue.state()['items'][0]['phase'],'pending')
        self.release.set();previous=self.wait();self.queue.tick();self.finish()
        self.assertTrue(self.workspace.report(previous['report_id']));self.assertEqual(self.wire.json.call_count,1)

    def test_pause_cancels_only_original_active_task_and_never_replays_it_on_resume(self):
        self.wire.json.side_effect=self.hold;self.enqueue();self.enqueue('Engineer');self.queue.tick()
        self.assertTrue(self.entered.wait(5));state=self.queue.state()
        with self.assertRaises(InputError):self.queue.remove({'revision':state['revision'],'id':state['items'][0]['id']})
        self.queue.pause({'revision':state['revision']})
        with self.assertRaises(InputError):self.queue.resume({'revision':self.queue.state()['revision'],'consent':True})
        self.release.set();self.assertEqual(self.finish()['status'],'cancelled')
        state=self.queue.state();self.assertEqual(state['status'],'paused');self.assertEqual(len(state['items']),1)
        self.assertEqual(state['history'][0]['status'],'cancelled');self.now[0]+=GAP
        self.queue.resume({'revision':state['revision'],'consent':True});self.queue.tick();self.finish()
        self.assertEqual(self.queue.state()['status'],'idle');self.assertEqual(len(self.queue.state()['history']),2)

    def test_observer_pause_is_seen_by_owner_without_cross_process_task_cancellation(self):
        self.wire.json.side_effect=self.hold;self.enqueue();self.queue.tick();self.assertTrue(self.entered.wait(5))
        other_tasks=PublicTasks(self.workspace,hybrid_client=self.client);self.addCleanup(other_tasks.close)
        other=PublicQueue(self.workspace,other_tasks,clock=lambda:self.now[0])
        other.pause({'revision':other.state()['revision']});self.assertFalse(self.tasks._cancel.is_set())
        self.queue.tick();self.assertTrue(self.tasks._cancel.is_set());self.release.set();self.finish()
        self.assertEqual(self.queue.state()['status'],'paused')

    def test_completion_receipt_recovers_original_result_after_manual_replacement(self):
        self.enqueue();self.enqueue('Engineer');self.queue.tick();original=self.wait()
        report=self.workspace.root/'reports'/original['report_id']/'requirements.csv';before=report.read_bytes()
        self.tasks.search({'consent':True,'query':query(query='Engineer').payload()});manual=self.wait()
        restarted=PublicQueue(self.workspace,self.tasks,clock=lambda:self.now[0]);restarted.recover()
        state=restarted.state();self.assertEqual(state['status'],'queued')
        self.assertEqual(state['history'][0]['report_id'],original['report_id'])
        self.assertEqual(self.tasks.snapshot()['id'],manual['id']);self.assertEqual(report.read_bytes(),before)
        self.assertEqual(self.wire.json.call_count,1)

    def test_unknown_dispatch_is_recorded_without_replay_and_other_entries_stay_paused(self):
        self.enqueue();self.enqueue('Engineer')
        with self.queue._edit():
            state=self.queue._read();state['items'][0]['phase']='dispatching'
            state.update(status='dispatching',code='dispatching');self.queue._write(state)
        restarted=PublicQueue(self.workspace,self.tasks,clock=lambda:self.now[0]);restarted.recover();restarted.tick()
        state=restarted.state();self.assertEqual(state['status'],'paused');self.assertEqual(len(state['items']),1)
        self.assertEqual(state['history'][0]['status'],'interrupted');self.assertEqual(state['history'][0]['task_id'],'')
        self.assertIsNone(self.tasks._thread);self.wire.json.assert_not_called()

    def test_expired_permission_never_fetches_and_resuming_only_renews_unstarted_items(self):
        self.enqueue();self.enqueue('Engineer');self.now[0]+=DAY;self.queue.tick()
        state=self.queue.state();self.assertEqual(state['code'],'expired');self.assertEqual(len(state['items']),1)
        self.assertEqual(state['history'][0]['status'],'expired');self.wire.json.assert_not_called()
        self.queue.resume({'revision':state['revision'],'consent':True});state=self.queue.state()
        self.assertEqual(state['items'][0]['expires_at'],self.now[0]+DAY)
        self.now[0]+=GAP;self.queue.tick();self.finish();self.assertEqual(self.wire.json.call_count,1)

    def test_changed_conditions_and_clock_rollback_pause_without_dispatch(self):
        self.enqueue();original=self.queue._conditions
        def changed(value):
            query,binding,policy=original(value);return query,'f'*64,policy
        with patch.object(self.queue,'_conditions',side_effect=changed):self.queue.tick()
        self.assertEqual(self.queue.state()['code'],'conditions_changed');self.wire.json.assert_not_called()
        self.queue.resume({'revision':self.queue.state()['revision'],'consent':True})
        self.now[0]-=1;self.queue.tick();self.assertEqual(self.queue.state()['code'],'clock_rollback')
        self.wire.json.assert_not_called()

    def test_failed_dispatch_preserves_unknown_marker_and_cancels_only_just_started_task(self):
        self.enqueue();original=self.queue._write
        def fail_after_start(value):
            if value['status']=='running':
                # Dispatch still holds the task lock: the new worker cannot
                # perform its first save/request until this transaction ends.
                raise InputError('fixture write failure')
            return original(value)
        with patch.object(self.queue,'_write',side_effect=fail_after_start),self.assertRaises(InputError):self.queue.tick()
        self.assertTrue(self.tasks._cancel.is_set());self.wait()
        self.assertEqual(self.queue.state()['status'],'dispatching')
        self.queue.recover();self.assertEqual(self.queue.state()['code'],'interrupted')
        self.wire.json.assert_not_called()

    def test_corrupt_future_duplicate_or_oversized_record_is_not_reset(self):
        self.enqueue();valid=self.queue._read()
        invalid=[{**valid,'schema_version':2},{**valid,'revision':True},{**valid,'next_due':float('nan')},
                 {**valid,'last_seen':10**400},{**valid,'code':'running'},
                 {**valid,'items':[valid['items'][0]]*6},{**valid,'items':[valid['items'][0]]*2},
                 {**valid,'items':[{**valid['items'][0],'expires_at':self.now[0]+DAY+1}]}]
        for value in invalid:
            with closing(sqlite3.connect(self.queue.path)) as conn:
                conn.execute('UPDATE queue SET payload=?',(json.dumps(value),));conn.commit()
            before=self.queue.path.read_bytes()
            with self.subTest(value=value),self.assertRaises(InputError):self.queue.tick()
            self.assertEqual(self.queue.path.read_bytes(),before)
        self.queue.path.write_bytes(b'x'*(1024*1024+1))
        with self.assertRaises(InputError):self.queue.state()
        self.wire.json.assert_not_called()

    def test_failure_and_original_rate_ledger_pause_without_refund_or_automatic_retry(self):
        self.wire.json.side_effect=FetchError('http_403');self.enqueue();self.enqueue('Engineer')
        self.queue.tick();self.assertEqual(self.finish()['status'],'failed')
        state=self.queue.state();self.assertEqual(state['code'],'result_attention');self.assertEqual(len(state['items']),1)
        with self.assertRaises(RateLimit):self.client.ledger.reserve(SCOPE,'request')
        before=self.client.ledger.path.read_bytes();self.now[0]+=GAP;self.queue.tick()
        self.assertEqual(self.client.ledger.path.read_bytes(),before);self.assertEqual(self.wire.json.call_count,1)

    def test_manual_activity_uses_same_quota_and_queue_does_not_bypass_it(self):
        self.client._prepare();self.client.ledger.reserve(SCOPE,'request');before=self.client.ledger.path.read_bytes()
        self.enqueue();self.enqueue('Engineer');self.queue.tick();self.assertEqual(self.finish()['status'],'failed')
        self.assertEqual(self.queue.state()['code'],'result_attention');self.wire.json.assert_not_called()
        self.assertEqual(self.client.ledger.path.read_bytes(),before)

    def test_stale_cache_keeps_original_report_but_pauses_remaining_queries(self):
        self.client.search(query(),consent=True);self.now[0]+=601
        self.wire.json.side_effect=FetchError('http_503');self.enqueue();self.enqueue('Engineer')
        self.queue.tick();task=self.finish();state=self.queue.state()
        self.assertEqual((task['status'],state['code']),('completed','result_attention'))
        self.assertTrue(state['history'][0]['stale']);self.assertTrue(self.workspace.report(task['report_id']))
        self.now[0]+=GAP;self.queue.tick();self.assertEqual(self.wire.json.call_count,2)

    def test_pause_does_not_cancel_same_id_manually_resumed_attempt(self):
        self.wire.json.side_effect=self.hold;self.enqueue();self.enqueue('Engineer');self.queue.tick()
        self.assertTrue(self.entered.wait(5));original=self.tasks.snapshot();self.tasks.cancel({'id':original['id']})
        self.release.set();self.assertEqual(self.wait()['status'],'cancelled')
        self.entered.clear();self.release.clear();original_import=self.tasks._import
        def held(*args):
            self.entered.set()
            if not self.release.wait(15):raise AssertionError('manual attempt fixture not released')
            return original_import(*args)
        with patch.object(self.tasks,'_import',side_effect=held):
            self.tasks.resume({'id':original['id'],'consent':True});self.assertTrue(self.entered.wait(5))
            self.queue.pause({'revision':self.queue.state()['revision']});self.queue.tick()
            self.assertFalse(self.tasks._cancel.is_set());self.release.set();manual=self.wait()
        state=self.queue.state();self.assertEqual(state['status'],'paused')
        self.assertEqual(state['history'][0]['status'],'cancelled');self.assertEqual(state['history'][0]['report_id'],'')
        self.assertEqual((manual['id'],manual['attempt'],manual['status']),(original['id'],2,'completed'))
        self.assertTrue(self.workspace.report(manual['report_id']));self.assertEqual(self.wire.json.call_count,1)

    def test_missing_receipt_pauses_and_corrupt_receipt_is_preserved_without_guessing(self):
        self.enqueue();self.enqueue('Engineer');self.queue.tick();original=self.wait()
        self.tasks.search({'consent':True,'query':query(query='Engineer').payload()});self.wait()
        receipt=PublicOutcomes(self.tasks.root).path;before=receipt.read_bytes()
        receipt.write_bytes(b'INVALID PRIVATE RECORD')
        with self.assertRaises(InputError):self.queue.recover()
        self.assertEqual(receipt.read_bytes(),b'INVALID PRIVATE RECORD')
        receipt.write_bytes(before)
        with closing(sqlite3.connect(receipt)) as db:
            db.execute('DELETE FROM outcomes');db.commit()
        self.queue.recover();state=self.queue.state()
        self.assertEqual(state['code'],'interrupted');self.assertEqual(state['history'][0]['report_id'],'')
        self.assertTrue(self.workspace.report(original['report_id']));self.assertEqual(self.wire.json.call_count,1)

    def test_recovery_after_clock_rollback_keeps_report_and_requires_reconfirmation(self):
        self.enqueue();self.enqueue('Engineer');self.queue.tick();original=self.wait();confirmed=self.now[0]
        self.now[0]-=2
        restarted=PublicQueue(self.workspace,self.tasks,clock=lambda:self.now[0]);restarted.recover()
        state=restarted.state();self.assertEqual(state['code'],'clock_rollback')
        self.assertEqual(state['history'][0]['report_id'],original['report_id']);self.assertEqual(state['last_seen'],confirmed)
        before=restarted.path.read_bytes();restarted.tick();restarted.tick()
        self.assertEqual(restarted.path.read_bytes(),before);self.assertEqual(self.wire.json.call_count,1)

    def test_history_is_bounded_and_old_reports_stay_on_disk(self):
        reports=[]
        for _ in range(22):
            self.enqueue();self.queue.tick();reports.append(self.finish()['report_id']);self.now[0]+=GAP
        state=self.queue.state();self.assertEqual(len(state['history']),20)
        self.assertEqual([row['report_id'] for row in state['history']],reports[-20:])
        self.assertTrue(all(self.workspace.report(ident) for ident in reports));self.assertEqual(self.wire.json.call_count,2)

    def test_duplicate_keys_and_sql_version_are_not_repaired_by_execution(self):
        self.enqueue();valid=json.dumps(self.queue._read())
        with closing(sqlite3.connect(self.queue.path)) as db:
            db.execute('UPDATE queue SET payload=?',(valid[:-1]+',"revision":1}',));db.commit()
        with self.assertRaises(InputError):self.queue.state()
        with closing(sqlite3.connect(self.queue.path)) as db:
            db.execute('UPDATE queue SET payload=?',(valid,));db.execute('PRAGMA user_version=99');db.commit()
        before=self.queue.path.read_bytes()
        with self.assertRaises(InputError):self.queue.tick()
        self.assertEqual(self.queue.path.read_bytes(),before);self.wire.json.assert_not_called()

    def test_actual_second_process_runs_pending_items_once_and_retains_order_across_restart(self):
        self.enqueue();self.enqueue('Engineer')
        code="""import json,sys
from pathlib import Path
from unittest.mock import Mock,patch
from test_local_public import payload
from vibe_job_radar.workspace import Workspace
from vibe_job_radar.local_public import LocalPublicDataClient
from vibe_job_radar.public_tasks import PublicTasks
from vibe_job_radar.public_queue import PublicQueue
workspace=Workspace(sys.argv[1]);now=float(sys.argv[2]);wire=Mock()
if sys.argv[3]=='first':wire.json.return_value=payload()
else:wire.json.side_effect=AssertionError('cached second task expected')
with patch('urllib.request.getproxies',return_value={}):
 tasks=PublicTasks(workspace,hybrid_client=LocalPublicDataClient(workspace,transport=wire,clock=lambda:now))
 queue=PublicQueue(workspace,tasks,clock=lambda:now)
 try:
  queue.recover();queue.tick();tasks._thread.join(15)
  assert tasks.snapshot()['status']=='completed'
  queue.tick();print(json.dumps({'history':len(queue.state()['history']),'requests':wire.json.call_count}))
 finally:queue.close();tasks.close()
"""
        checkout=Path(__file__).resolve().parents[1]
        env={**os.environ,'PYTHONPATH':os.pathsep.join([str(checkout/'src'),str(checkout/'tests')])}
        for index in range(2):
            child=subprocess.run([sys.executable,'-c',code,str(self.workspace.root),str(self.now[0]+GAP*index),
                'first' if index==0 else 'second'],env=env,capture_output=True,text=True,timeout=25,
                creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            self.assertEqual(child.returncode,0,child.stderr)
            self.assertEqual(json.loads(child.stdout),{'history':index+1,'requests':1 if index==0 else 0})
        state=self.queue.state();self.assertEqual(state['status'],'idle');self.assertEqual(len(state['history']),2)
        self.assertTrue(all(self.workspace.report(row['report_id']) for row in state['history']))


class QueueHTTPTests(unittest.TestCase):
    def setUp(self):
        http_fixtures.HTTPTests.setUp(self)
        self.addCleanup(http_fixtures.HTTPTests.tearDown,self)
        # Keep real HTTP and control locks, but do not dispatch in this API test.
        deadline=time.monotonic()+5
        while not self.server.public_queue.is_running() and time.monotonic()<deadline:time.sleep(.01)
        self.assertTrue(self.server.public_queue.is_running())
        self.server.public_queue.close()
        self.wire=Mock();self.wire.json.side_effect=AssertionError('API-only test must not fetch')
        self.server.public_tasks.hybrid.transport=self.wire
        proxy=patch('urllib.request.getproxies',return_value={});proxy.start();self.addCleanup(proxy.stop)

    call=http_fixtures.HTTPTests.call

    def test_auth_origin_revision_actions_and_packaged_asset(self):
        base='/api/public/queue/';data={'revision':0,'consent':True,'query':query().payload()}
        self.assertEqual(self.call(base+'state',authorized=False)[0],403)
        for action,body in (('enqueue',data),('pause',{'revision':0}),('remove',{'revision':0,'id':'a'*32}),
                            ('resume',{'revision':0,'consent':True})):
            self.assertEqual(self.call(base+action,body,authorized=False)[0],403)
            self.assertEqual(self.call(base+action,body,headers={'Origin':'https://bad.test'})[0],403)
        self.assertFalse(self.server.public_queue.path.exists())
        code,_,raw=self.call(base+'enqueue',data);self.assertEqual(code,200,raw);state=json.loads(raw)
        self.assertEqual(self.call(base+'enqueue',data)[0],400)
        code,_,raw=self.call(base+'pause',{'revision':state['revision']});self.assertEqual(code,200);state=json.loads(raw)
        self.assertEqual(state['status'],'paused')
        code,_,raw=self.call(base+'resume',{'revision':state['revision'],'consent':True});self.assertEqual(code,200);state=json.loads(raw)
        code,_,raw=self.call(base+'remove',{'revision':state['revision'],'id':state['items'][0]['id']})
        self.assertEqual(code,200);self.assertEqual(json.loads(raw)['status'],'idle')
        self.assertIsNone(self.server.public_tasks._thread)
        self.wire.json.assert_not_called()
        self.assertIn(b'public-queue.js',self.call('/')[2]);self.assertNotIn(b'innerHTML',self.call('/public-queue.js')[2])


if __name__=='__main__':unittest.main()
