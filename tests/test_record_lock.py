"""Fair local admission supplements rather than replaces the real OS lock."""
from contextlib import contextmanager
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from vibe_job_radar.record_lock import _Gate,_gate,record_lock
from vibe_job_radar.collection import writer_lock
from vibe_job_radar.workspace import InputError


class RecordLockTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup);self.root=Path(tmp.name)

    def test_waiting_observer_enters_before_writer_reacquires(self):
        gate=_gate(self.root);order=[];errors=[]
        def observer():
            try:
                with record_lock(self.root):order.append('observer')
            except BaseException as exc:errors.append(exc)
        with record_lock(self.root):
            worker=threading.Thread(target=observer);worker.start()
            with gate.condition:self.assertTrue(gate.condition.wait_for(lambda:len(gate.tickets)==2,timeout=2))
        for _ in range(10):
            with record_lock(self.root):order.append('writer')
        worker.join(2);self.assertFalse(worker.is_alive());self.assertEqual(errors,[])
        self.assertEqual(order[0],'observer');self.assertEqual(order.count('writer'),10)

    def test_timeout_removes_waiter_and_later_owner_can_enter(self):
        gate=_Gate();errors=[]
        def expired():
            try:
                with gate.turn(.03):self.fail('must not enter another owner')
            except InputError:errors.append('timeout')
        with gate.turn(1):
            worker=threading.Thread(target=expired);worker.start();worker.join(1)
            self.assertFalse(worker.is_alive());self.assertEqual(errors,['timeout']);self.assertEqual(len(gate.tickets),1)
        with gate.turn(1):pass
        self.assertEqual(len(gate.tickets),0)

    def test_exception_releases_queue_and_real_file_lock(self):
        with self.assertRaisesRegex(RuntimeError,'artificial'):
            with record_lock(self.root):raise RuntimeError('artificial')
        with writer_lock(self.root):pass
        with record_lock(self.root):pass

    def test_same_canonical_path_shares_gate_and_other_workspaces_do_not_wait(self):
        gate=_gate(self.root);self.assertIs(_gate(self.root/'.'),gate)
        other=self.root/'other';other.mkdir();done=threading.Event();errors=[]
        def independent():
            try:
                with record_lock(other):done.set()
            except BaseException as exc:errors.append(exc)
        with record_lock(self.root):
            worker=threading.Thread(target=independent);worker.start();self.assertTrue(done.wait(1))
        worker.join(1);self.assertEqual(errors,[])

    def test_real_os_contention_stays_bounded_and_does_not_discard_the_lock(self):
        with writer_lock(self.root):
            with self.assertRaises(InputError):
                with record_lock(self.root,timeout=.03):self.fail('OS owner still holds the lock')
        with record_lock(self.root):pass

    def test_invalid_path_error_is_not_retried(self):
        @contextmanager
        def denied(root):raise InputError('artificial path denial');yield
        with patch('vibe_job_radar.record_lock.writer_lock',side_effect=denied) as lock:
            with self.assertRaisesRegex(InputError,'artificial path denial'):
                with record_lock(self.root):pass
            self.assertEqual(lock.call_count,1)
