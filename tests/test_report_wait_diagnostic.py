"""Wait evidence includes only bounded frames from the waiting worker's own report pool."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import threading
from types import SimpleNamespace
import unittest
from guided_wait_diagnostic import wait_diagnostic


class ReportWaitDiagnosticTests(unittest.TestCase):
    def test_own_writer_stacks_are_captured_bounded_without_other_pool_or_locals(self):
        owner=threading.current_thread(); release=threading.Event(); entered=threading.Barrier(7)
        def own_writer():
            private_marker='DO_NOT_EXPORT_INPUT_OR_PATH'
            entered.wait(timeout=10); release.wait(timeout=10)
        def unrelated_writer():
            entered.wait(timeout=10); release.wait(timeout=10)
        with ThreadPoolExecutor(max_workers=5,thread_name_prefix=f'radar-report-{owner.ident}') as own, \
             ThreadPoolExecutor(max_workers=1,thread_name_prefix='radar-report-other') as other:
            tasks=[own.submit(own_writer) for _ in range(5)]+[other.submit(unrelated_writer)]
            try:
                entered.wait(timeout=10)
                result=wait_diagnostic(SimpleNamespace(_thread=owner),{'busy':True,'jobs':[],'active':None})
                stacks=result['report_writer_stacks']
                self.assertEqual(len(stacks),4)
                self.assertTrue(all(any(f['function']=='own_writer' for f in stack) for stack in stacks))
                for stack in stacks:
                    self.assertLessEqual(len(stack),24)
                    for frame in stack:
                        self.assertEqual(set(frame),{'file','line','function'})
                        self.assertEqual(frame['file'],Path(frame['file']).name)
                self.assertNotIn('DO_NOT_EXPORT',json.dumps(result))
                self.assertNotIn('unrelated_writer',json.dumps(stacks))
            finally:
                release.set()
            for task in tasks:task.result()

    def test_unstarted_worker_does_not_borrow_another_workers_report_frames(self):
        worker=threading.Thread(target=lambda:None)
        result=wait_diagnostic(SimpleNamespace(_thread=worker),{'busy':True,'jobs':[],'active':None})
        self.assertEqual(result['report_writer_stacks'],[])
        self.assertEqual(result['worker_stack'],[])
