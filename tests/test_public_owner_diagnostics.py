"""Failure-time observations must remain bounded and cannot wait for task locks."""
from concurrent.futures import ThreadPoolExecutor
import json
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from public_owner_diagnostics import require_child_ready, wait_diagnostic


class OwnerDiagnosticTests(unittest.TestCase):
    def test_locked_private_task_is_observed_without_snapshot_or_lock(self):
        lock=threading.Lock();lock.acquire()
        task=SimpleNamespace(_lock=lock,_thread=None,
            _state={'status':'running','phase':{'private':'PRIVATE_QUERY'},'message':'PRIVATE_MESSAGE',
                    'query':{'query':'PRIVATE_QUERY'},'source_url':'https://private.invalid/token'},
            snapshot=Mock(side_effect=AssertionError('must not read/lock task')))
        rows=[];observer=threading.Thread(target=lambda:rows.append(wait_diagnostic(task)))
        observer.start()
        try:
            observer.join(1)
            self.assertFalse(observer.is_alive(),'failure diagnostics acquired the task lock')
            self.assertEqual(rows[0]['task'],{'status':'running','phase':'unknown'})
            self.assertEqual(rows[0]['worker_stack'],[])
            self.assertNotIn('PRIVATE',json.dumps(rows))
            self.assertNotIn('private.invalid',json.dumps(rows))
            task.snapshot.assert_not_called()
        finally:
            lock.release();observer.join(5)

    def test_owned_report_threads_are_bounded_without_locals_or_other_pool(self):
        owner=threading.current_thread();entered=threading.Barrier(3);release=threading.Event()
        task=SimpleNamespace(_thread=owner,_state={'status':'running','phase':'saving'})
        def held():
            private_value='PRIVATE_BODY'
            entered.wait(5)
            if not release.wait(5):raise AssertionError('fixture not released')
            return private_value
        with ThreadPoolExecutor(max_workers=2,thread_name_prefix=f'radar-report-{owner.ident}') as pool:
            futures=[pool.submit(held) for _ in range(2)]
            try:
                entered.wait(5);row=wait_diagnostic(task)
                self.assertEqual(len(row['report_writers']),2)
                self.assertTrue(all(0<len(stack)<=24 for stack in row['report_writers']))
                self.assertTrue(all(set(frame)=={'file','line','function'} for stack in row['report_writers'] for frame in stack))
                self.assertNotIn('PRIVATE',json.dumps(row))
                self.assertTrue(all('/' not in frame['file'] and '\\' not in frame['file'] for stack in row['report_writers'] for frame in stack))
            finally:release.set()
            for future in futures:future.result()

    def test_child_failure_is_emitted_before_raising_and_never_becomes_ready(self):
        event=Mock();event.wait.return_value=False
        probe=Mock();probe.snapshot.side_effect=[{'calls_started':0},{'calls_started':1}]
        task=SimpleNamespace(_thread=None,_state={'status':'running','message':'PRIVATE_MESSAGE'})
        emitted=[]
        with self.assertRaisesRegex(AssertionError,'after 5s'):
            require_child_ready(event,task,probe,emitted.append)
        event.wait.assert_called_once_with(5)
        self.assertEqual(len(emitted),1)
        packet=json.loads(emitted[0]);self.assertEqual(packet['event'],'owner_wait_failed')
        self.assertEqual(packet['diagnostic']['worker_fsync'],{'before_wait':{'calls_started':0},'at_timeout':{'calls_started':1}})
        self.assertNotIn('id',packet)
        self.assertNotIn('PRIVATE',emitted[0])
        event.wait.return_value=True;probe.snapshot.side_effect=None;probe.snapshot.return_value={}
        require_child_ready(event,task,probe,emitted.append)
        self.assertEqual(len(emitted),1)
