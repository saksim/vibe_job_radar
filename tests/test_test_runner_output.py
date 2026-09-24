"""The runner must preserve failures even with Windows legacy console output."""
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch


def runner_module():
    path = Path(__file__).resolve().parents[1] / 'scripts/run_tests.py'
    spec = importlib.util.spec_from_file_location('runner_output_fixture', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class RunnerOutputTests(unittest.TestCase):
    def test_failure_stacks_capture_live_worker_before_cleanup(self):
        runner = runner_module()
        original_dump = runner.faulthandler.dump_traceback
        for kind in ('failure', 'error', 'subtest_failure', 'subtest_error'):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as tmp:
                released = threading.Event()
                waiting = threading.Event()
                cleaned = []
                observed = []

                def awaiting_failure_cleanup():
                    waiting.set()
                    released.wait()

                class ArtificialCase(unittest.TestCase):
                    def runTest(self):
                        worker = threading.Thread(target=awaiting_failure_cleanup)
                        worker.start()
                        def cleanup():
                            released.set()
                            worker.join()
                            cleaned.append(True)
                        self.addCleanup(cleanup)
                        waiting.wait()
                        if kind.startswith('subtest'):
                            with self.subTest(private_input='fixture-private-subtest'):
                                if kind.endswith('failure'):
                                    self.fail('fixture-private-assertion')
                                raise ValueError('fixture-private-error')
                        elif kind == 'failure':
                            self.fail('fixture-private-assertion')
                        else:
                            raise ValueError('fixture-private-error')

                def capture(**kwargs):
                    observed.append(not cleaned and not released.is_set())
                    original_dump(**kwargs)

                with (Path(tmp) / 'progress.log').open('w', encoding='utf-8') as progress, \
                        patch.object(runner.faulthandler, 'dump_traceback', side_effect=capture), \
                        patch.object(runner.faulthandler, 'dump_traceback_later') as delayed, \
                        patch.object(runner.faulthandler, 'cancel_dump_traceback_later'):
                    result = runner.ProgressResult(unittest.runner._WritelnDecorator(io.StringIO()), True, 2,
                                                   progress=progress)
                    ArtificialCase().run(result)
                self.assertEqual(observed, [True])
                self.assertEqual(cleaned, [True])
                expected = (1, 0) if kind.endswith('failure') else (0, 1)
                self.assertEqual((len(result.failures), len(result.errors)), expected)
                self.assertFalse(result.wasSuccessful())
                self.assertEqual(delayed.call_args.args[0], 120)
                text = (Path(tmp) / 'progress.log').read_text(encoding='utf-8')
                self.assertIn('awaiting_failure_cleanup', text)
                self.assertNotIn('fixture-private-', text)

    def test_diagnostic_error_keeps_original_failure_and_exit_code(self):
        runner = runner_module()
        def original_failure():
            raise AssertionError('original artificial assertion')
        suite = unittest.TestSuite([unittest.FunctionTestCase(original_failure)])
        with tempfile.TemporaryDirectory() as tmp:
            report = Path(tmp) / 'result.json'
            with patch.object(sys, 'argv', ['run_tests.py', '--report', str(report), '--progress']), \
                    patch.object(sys, 'stdout', io.StringIO()), \
                    patch.object(unittest.defaultTestLoader, 'discover', return_value=suite), \
                    patch.object(runner.faulthandler, 'dump_traceback', side_effect=OSError('private diagnostic error')):
                code = runner.main()
            result = json.loads(report.read_text(encoding='utf-8'))
            self.assertEqual((code, result['failures'], result['errors']), (1, 1, 0))
            self.assertFalse(result['success'])
            self.assertIn('original artificial assertion', report.with_suffix('.log').read_text(encoding='utf-8'))
            text = report.with_suffix('.progress.log').read_text(encoding='utf-8')
            self.assertIn('STACK_UNAVAILABLE', text)
            self.assertNotIn('private diagnostic error', text)

    def test_only_first_unexpected_failure_per_test_is_dumped(self):
        runner = runner_module()
        class ArtificialSubtests(unittest.TestCase):
            def runTest(self):
                for number in range(2):
                    with self.subTest(number=number):
                        self.fail('artificial subtest')
        class Expected(unittest.TestCase):
            @unittest.expectedFailure
            def runTest(self):
                self.fail('expected artificial failure')
        suite = unittest.TestSuite([ArtificialSubtests(), Expected(), unittest.FunctionTestCase(lambda: None)])
        with tempfile.TemporaryFile(mode='w', encoding='utf-8') as progress, \
                patch.object(runner.faulthandler, 'dump_traceback') as dump, \
                patch.object(runner.faulthandler, 'dump_traceback_later'), \
                patch.object(runner.faulthandler, 'cancel_dump_traceback_later'):
            result = runner.ProgressResult(unittest.runner._WritelnDecorator(io.StringIO()), True, 2,
                                           progress=progress)
            suite.run(result)
            self.assertEqual((result.testsRun, len(result.failures), len(result.expectedFailures)), (3, 2, 1))
            self.assertEqual(dump.call_count, 1)
            self.assertTrue(dump.call_args.kwargs['all_threads'])

    def test_progress_preserves_test_identity_and_failure_without_changing_result(self):
        runner=runner_module()
        def artificial_failure():raise AssertionError('fixture failure')
        suite=unittest.TestSuite([unittest.FunctionTestCase(artificial_failure)])
        with tempfile.TemporaryDirectory() as tmp:
            report=Path(tmp)/'result.json'
            with patch.object(sys,'argv',['run_tests.py','--report',str(report),'--progress']),\
                 patch.object(sys,'stdout',io.StringIO()),\
                 patch.object(unittest.defaultTestLoader,'discover',return_value=suite):
                self.assertEqual(runner.main(),1)
            progress=report.with_suffix('.progress.log').read_text(encoding='utf-8')
            self.assertIn('START artificial_failure',progress);self.assertIn('END artificial_failure',progress)
            self.assertEqual(json.loads(report.read_text(encoding='utf-8'))['failures'],1)

    def test_legacy_console_keeps_failure_exit_and_utf8_evidence(self):
        runner = runner_module()
        def fail():
            raise AssertionError('中文错误：仅人工测试')
        suite = unittest.TestSuite([unittest.FunctionTestCase(fail)])
        buffer = io.BytesIO()
        console = io.TextIOWrapper(buffer, encoding='cp1252', errors='strict', write_through=True)
        with tempfile.TemporaryDirectory() as tmp:
            report = Path(tmp) / 'report.json'
            with patch.object(sys, 'argv', ['run_tests.py', '--report', str(report)]), \
                    patch.object(sys, 'stdout', console), \
                    patch.object(unittest.defaultTestLoader, 'discover', return_value=suite):
                code = runner.main()
            result = json.loads(report.read_text(encoding='utf-8'))
            self.assertEqual(code, 1)
            self.assertFalse(result['success'])
            self.assertEqual(result['failures'], 1)
            self.assertIn('中文错误', report.with_suffix('.log').read_text(encoding='utf-8'))
            self.assertIn(b'FAILED', buffer.getvalue())
            self.assertIn(b'\\u4e2d', buffer.getvalue())
        console.close()

    def test_string_console_without_encoding_keeps_success(self):
        runner = runner_module()
        suite = unittest.TestSuite([unittest.FunctionTestCase(lambda: None)])
        with patch.object(sys, 'argv', ['run_tests.py']), patch.object(sys, 'stdout', io.StringIO()), \
                patch.object(unittest.defaultTestLoader, 'discover', return_value=suite):
            self.assertEqual(runner.main(), 0)

    def test_broken_output_does_not_destroy_report(self):
        runner = runner_module()
        class BrokenOutput:
            encoding = 'utf-8'
            def write(self, value):
                raise BrokenPipeError('synthetic closed console')
        suite = unittest.TestSuite([unittest.FunctionTestCase(lambda: None)])
        with tempfile.TemporaryDirectory() as tmp:
            report = Path(tmp) / 'report.json'
            with patch.object(sys, 'argv', ['run_tests.py', '--report', str(report)]), \
                    patch.object(sys, 'stdout', BrokenOutput()), \
                    patch.object(unittest.defaultTestLoader, 'discover', return_value=suite):
                with self.assertRaises(BrokenPipeError):
                    runner.main()
            self.assertTrue(json.loads(report.read_text(encoding='utf-8'))['success'])
            self.assertTrue(report.with_suffix('.log').is_file())
