"""Default failure evidence survives cleanup and does not change test outcomes."""
import io
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from test_test_runner_output import runner_module


class RunnerFailureEvidenceTests(unittest.TestCase):
    def run_fixture(self, suite, *, progress=False, runner=None):
        runner = runner or runner_module()
        with tempfile.TemporaryDirectory() as tmp:
            report = Path(tmp) / 'result.json'
            args = ['run_tests.py', '--report', str(report)] + (['--progress'] if progress else [])
            with patch.object(sys, 'argv', args), patch.object(sys, 'stdout', io.StringIO()), \
                    patch.object(unittest.defaultTestLoader, 'discover', return_value=suite):
                code = runner.main()
            evidence = report.with_suffix('.failures.jsonl')
            self.assertTrue(evidence.is_file(), 'failure evidence must exist without --progress')
            events = [json.loads(line) for line in evidence.read_text(encoding='utf-8').splitlines()]
            return code, json.loads(report.read_text(encoding='utf-8')), events, report.with_suffix('.log').read_text(encoding='utf-8')

    def blocked_fixture(self, *, setup_error=False):
        entered, release = threading.Event(), threading.Event()
        def blocked_worker():
            private_value = 'PRIVATE_LOCAL_DO_NOT_EXPORT'
            entered.set()
            release.wait(10)
            return private_value
        worker = threading.Thread(target=blocked_worker, name='PRIVATE_THREAD_NAME')
        outer = self
        class Failing(unittest.TestCase):
            def setUp(self):
                self.addCleanup(worker.join, 5)
                self.addCleanup(release.set)
                worker.start()
                outer.assertTrue(entered.wait(5))
                if setup_error:
                    raise RuntimeError('original setup error')
            def runTest(self):
                self.fail('original failure')
        return unittest.TestSuite([Failing()]), worker

    def test_default_report_keeps_waiting_worker_before_cleanup(self):
        suite, worker = self.blocked_fixture()
        code, result, events, log = self.run_fixture(suite)
        self.assertFalse(worker.is_alive())
        self.assertEqual((code, result['tests_run'], result['failures'], result['errors']), (1, 1, 1, 0))
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]['event'], 'failure')
        self.assertTrue(any(f['function'] == 'blocked_worker'
                            for thread in events[0]['threads'] for f in thread['stack']))
        self.assertNotIn('PRIVATE_', json.dumps(events))
        self.assertIn('original failure', log)

    def test_setup_error_is_captured_before_registered_cleanup(self):
        suite, worker = self.blocked_fixture(setup_error=True)
        code, result, events, log = self.run_fixture(suite)
        self.assertFalse(worker.is_alive())
        self.assertEqual((code, result['failures'], result['errors']), (1, 0, 1))
        self.assertEqual(events[0]['event'], 'error')
        self.assertTrue(any(f['function'] == 'blocked_worker'
                            for thread in events[0]['threads'] for f in thread['stack']))
        self.assertIn('original setup error', log)

    def test_subtest_parameters_are_excluded_and_both_outcomes_preserved(self):
        class Subtests(unittest.TestCase):
            def runTest(self):
                with self.subTest(value='PRIVATE_FAILURE_PARAMETER'):
                    self.fail('original subtest assertion')
                with self.subTest(value='PRIVATE_ERROR_PARAMETER'):
                    raise ValueError('original subtest error')
                with self.subTest(value='PRIVATE_PASS_PARAMETER'):
                    self.assertTrue(True)
        code, result, events, log = self.run_fixture(unittest.TestSuite([Subtests()]))
        self.assertEqual((code, result['tests_run'], result['failures'], result['errors']), (1, 1, 1, 1))
        self.assertEqual([e['event'] for e in events], ['subtest_failure', 'subtest_error'])
        self.assertNotIn('PRIVATE_', json.dumps(events))
        self.assertIn('original subtest assertion', log)
        self.assertIn('original subtest error', log)

    def test_success_skip_and_expected_failure_do_not_capture_stacks(self):
        runner = runner_module()
        class Cases(unittest.TestCase):
            def test_pass(self): pass
            @unittest.skip('fixture skip')
            def test_skip(self): pass
            @unittest.expectedFailure
            def test_expected_failure(self): self.fail('expected')
        with patch.object(runner, 'failure_threads', side_effect=AssertionError('must not capture')):
            code, result, events, _ = self.run_fixture(unittest.defaultTestLoader.loadTestsFromTestCase(Cases), runner=runner)
        self.assertEqual((code, result['tests_run'], result['failures'], result['errors'], result['skipped']), (0, 3, 0, 0, 1))
        self.assertEqual(events, [])

    def test_snapshot_failure_preserves_original_failure_and_cleanup(self):
        runner = runner_module()
        suite, worker = self.blocked_fixture()
        with patch.object(runner, 'failure_threads', side_effect=OSError('PRIVATE_CAPTURE_ERROR')):
            code, result, events, log = self.run_fixture(suite, runner=runner)
        self.assertFalse(worker.is_alive())
        self.assertEqual((code, result['failures'], result['errors']), (1, 1, 0))
        self.assertEqual(events[0]['capture_status'], 'unavailable')
        self.assertNotIn('PRIVATE_', json.dumps(events))
        self.assertIn('original failure', log)

    def test_file_write_failure_preserves_original_failure_and_buffered_evidence(self):
        runner = runner_module()
        suite, worker = self.blocked_fixture()
        stream = io.StringIO()
        writer = Mock()
        writer.write.side_effect = OSError('PRIVATE_WRITE_ERROR')
        result = unittest.TextTestRunner(stream=stream, resultclass=lambda *a, **kw:
                                        runner.FailureResult(*a, failure_stream=writer, **kw)).run(suite)
        self.assertFalse(worker.is_alive())
        self.assertEqual((len(result.failures), len(result.errors)), (1, 0))
        self.assertIn('file unavailable; original result retained', stream.getvalue())
        self.assertIn('blocked_worker', stream.getvalue())
        self.assertNotIn('PRIVATE_', stream.getvalue())

    def test_stacks_have_only_bounded_code_locations(self):
        runner = runner_module()
        def descend(depth):
            if depth:
                return descend(depth - 1)
            frame = sys._getframe()
            current = threading.get_ident()
            snapshots = {current: frame, **{current + i: frame for i in range(1, 20)}}
            with patch.object(runner.sys, '_current_frames', return_value=snapshots):
                return runner.failure_threads()
        evidence = descend(30)
        self.assertEqual(len(evidence['threads']), 16)
        self.assertEqual(evidence['threads_omitted'], 4)
        self.assertTrue(evidence['threads'][0]['current'])
        for thread in evidence['threads']:
            self.assertEqual(set(thread), {'current', 'stack', 'frames_truncated'})
            self.assertEqual(len(thread['stack']), 24)
            self.assertTrue(thread['frames_truncated'])
            for frame in thread['stack']:
                self.assertEqual(set(frame), {'file', 'line', 'function'})
                self.assertEqual(frame['file'], Path(frame['file']).name)

    def test_progress_mode_preserves_original_120_second_watchdog(self):
        runner = runner_module()
        suite, worker = self.blocked_fixture()
        with patch.object(runner.faulthandler, 'dump_traceback_later') as start, \
                patch.object(runner.faulthandler, 'cancel_dump_traceback_later') as cancel:
            code, result, events, _ = self.run_fixture(suite, progress=True, runner=runner)
        self.assertFalse(worker.is_alive())
        self.assertEqual((code, result['failures'], result['errors']), (1, 1, 0))
        self.assertEqual(len(events), 1)
        start.assert_called_once()
        self.assertEqual(start.call_args.args, (120,))
        self.assertEqual(cancel.call_count, 2)
