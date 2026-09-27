"""The original wait failure must expose the worker frame, never fixture secrets."""
import ast
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch
import test_guided_merge_review as reviewed


class GuidedWaitFailureEvidenceTests(unittest.TestCase):
    def test_wait_failure_keeps_bounded_worker_evidence_without_private_values(self):
        entered = threading.Event()
        release = threading.Event()
        def blocked_worker():
            private_local = 'PRIVATE_LOCAL_DO_NOT_PRINT'
            entered.set()
            release.wait(30)
            return private_local
        worker = threading.Thread(target=blocked_worker)
        worker.start()
        try:
            self.assertTrue(entered.wait(5))
            view = {'busy': True, 'active': 'PRIVATE_TASK', 'jobs': [{
                'id': 'PRIVATE_TASK', 'status': 'running', 'phase': 'report',
                'code': 'operation_error', 'authentication': 'PRIVATE_AUTH',
                'login_continuation': 'PRIVATE_CONTINUATION',
                'keyword': 'PRIVATE_KEYWORD', 'body': 'PRIVATE_BODY',
                'url': 'https://PRIVATE_HOST.invalid/PRIVATE_PATH',
                'password': 'PRIVATE_PASSWORD', 'cookies': 'PRIVATE_COOKIE'}]}
            case = reviewed.ServiceReviewTests('test_partial_success_is_reported_before_blocking_error')
            case.service = SimpleNamespace(_thread=worker, state=lambda: view)
            # Exercise the existing15s boundary immediately; production wait,
            # shutdown, file synchronization and original assertions do not change.
            with patch('test_guided_merge_review.time.monotonic', side_effect=[0, 16]):
                with self.assertRaises(AssertionError) as caught:
                    case.wait()
            message = str(caught.exception)
            self.assertIn('worker_stack', message)
            self.assertNotIn('PRIVATE_', message)
            evidence = ast.literal_eval(message[message.index('{'):])
            self.assertTrue(evidence['busy'])
            self.assertTrue(evidence['worker_alive'])
            self.assertEqual(evidence['task']['phase'], 'report')
            self.assertEqual(evidence['task']['authentication'], 'unknown')
            self.assertTrue(any(f['function'] == 'blocked_worker' for f in evidence['worker_stack']))
            self.assertLessEqual(len(evidence['worker_stack']), 24)
            for frame in evidence['worker_stack']:
                self.assertEqual(set(frame), {'file', 'line', 'function'})
                self.assertNotIn('/', frame['file'])
                self.assertNotIn(chr(92), frame['file'])
        finally:
            release.set()
            worker.join(5)
        self.assertFalse(worker.is_alive())
