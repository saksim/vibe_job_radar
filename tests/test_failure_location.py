"""Failure artifacts retain code locations without exception inputs or locals."""
import io
import json
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from test_test_runner_output import runner_module


class FailureLocationTests(unittest.TestCase):
    def run_case(self, case):
        runner = runner_module()
        progress = io.StringIO()
        with patch.object(runner.faulthandler, 'dump_traceback'), \
                patch.object(runner.faulthandler, 'dump_traceback_later'), \
                patch.object(runner.faulthandler, 'cancel_dump_traceback_later'):
            result = runner.ProgressResult(unittest.runner._WritelnDecorator(io.StringIO()),
                                           True, 2, progress=progress)
            case.run(result)
        locations = [json.loads(line.removeprefix('FAILURE_LOCATION '))
                     for line in progress.getvalue().splitlines()
                     if line.startswith('FAILURE_LOCATION ')]
        self.assertEqual(len(locations), 1)
        return result, locations[0], progress.getvalue()

    def test_long_assertion_keeps_exact_raise_line_without_message(self):
        expected = {}
        class Artificial(unittest.TestCase):
            def runTest(self):
                secret = 'ARTIFICIAL-PRIVATE-ASSERTION-' * 1000
                expected['line'] = sys._getframe().f_lineno + 1
                raise AssertionError(secret)
        result, location, text = self.run_case(Artificial())
        self.assertEqual((result.testsRun, len(result.failures), len(result.errors)), (1, 1, 0))
        self.assertEqual(location['exception'], 'AssertionError')
        self.assertEqual(location['frames'][-1], dict(file='tests/test_failure_location.py', line=expected['line']))
        self.assertNotIn('ARTIFICIAL-PRIVATE-ASSERTION', text)
        self.assertNotIn('secret', text)

    def test_subtests_keep_first_location_and_all_original_failures(self):
        class Artificial(unittest.TestCase):
            def runTest(self):
                for number in range(2):
                    with self.subTest(private='ARTIFICIAL-PRIVATE-PARAMETER', number=number):
                        self.fail('ARTIFICIAL-PRIVATE-MESSAGE')
        result, location, text = self.run_case(Artificial())
        self.assertEqual(len(result.failures), 2)
        self.assertTrue(location['frames'])
        self.assertNotIn('ARTIFICIAL-PRIVATE', text)

    def test_legacy_deferred_outcome_keeps_traceback_before_cleanup(self):
        runner = runner_module()
        try:
            raise ValueError('ARTIFICIAL-PRIVATE-LEGACY')
        except ValueError:
            original = sys.exc_info()
        case = unittest.FunctionTestCase(lambda: None)
        errors = [(case, None), (case, original)]
        case._outcome = SimpleNamespace(errors=errors)
        progress = io.StringIO()
        with patch.object(runner, 'LEGACY_DEFERRED_RESULTS', True), \
                patch.object(runner.faulthandler, 'dump_traceback'), \
                patch.object(runner.faulthandler, 'dump_traceback_later'), \
                patch.object(runner.faulthandler, 'cancel_dump_traceback_later'):
            result = runner.ProgressResult(unittest.runner._WritelnDecorator(io.StringIO()), True, 2,
                                           progress=progress)
            result.startTest(case)
            try:
                case._callTearDown()
                result.addError(case, original)
            finally:
                result.stopTest(case)
        locations = [json.loads(line.removeprefix('FAILURE_LOCATION '))
                     for line in progress.getvalue().splitlines() if line.startswith('FAILURE_LOCATION ')]
        self.assertEqual(len(locations), 1)
        self.assertEqual(locations[0]['exception'], 'ValueError')
        self.assertEqual(locations[0]['frames'][-1]['file'], 'tests/test_failure_location.py')
        self.assertIs(case._outcome.errors, errors)
        self.assertEqual(len(result.errors), 1)
        self.assertNotIn('ARTIFICIAL-PRIVATE', progress.getvalue())

    def test_external_path_and_custom_exception_name_are_not_exported(self):
        namespace = {}
        exec(compile("def fail():\n    raise private_type('ARTIFICIAL-PRIVATE-MESSAGE')\n",
                     '/ARTIFICIAL-PRIVATE-LOCATION/credentials.py', 'exec'), namespace)
        namespace['private_type'] = type('ARTIFICIAL_PRIVATE_EXCEPTION', (Exception,), {})
        result, location, text = self.run_case(unittest.FunctionTestCase(namespace['fail']))
        self.assertEqual(len(result.errors), 1)
        self.assertEqual(location['exception'], 'Exception')
        self.assertGreater(location['external_frames'], 0)
        self.assertNotIn('ARTIFICIAL', text)

    def test_location_output_error_keeps_live_stack_and_original_failure(self):
        runner = runner_module()
        class FailingProgress(io.StringIO):
            def write(self, value):
                if value.startswith('FAILURE_LOCATION '):
                    raise OSError('ARTIFICIAL-PRIVATE-WRITE-ERROR')
                return super().write(value)
        def fail():
            raise AssertionError('ARTIFICIAL-PRIVATE-ASSERTION')
        progress = FailingProgress()
        with patch.object(runner.faulthandler, 'dump_traceback') as dump, \
                patch.object(runner.faulthandler, 'dump_traceback_later'), \
                patch.object(runner.faulthandler, 'cancel_dump_traceback_later'):
            result = runner.ProgressResult(unittest.runner._WritelnDecorator(io.StringIO()), True, 2,
                                           progress=progress)
            unittest.FunctionTestCase(fail).run(result)
        self.assertEqual(len(result.failures), 1)
        self.assertEqual(dump.call_count, 1)
        self.assertIn('FAILURE_LOCATION_UNAVAILABLE', progress.getvalue())
        self.assertNotIn('ARTIFICIAL-PRIVATE', progress.getvalue())

    def test_deep_traceback_keeps_bounded_recent_frames(self):
        def recurse(depth):
            if depth:
                return recurse(depth - 1)
            raise RuntimeError('ARTIFICIAL-PRIVATE-DEEP-FAILURE')
        for depth in (80, 400):
            with self.subTest(depth=depth):
                result, location, text = self.run_case(unittest.FunctionTestCase(lambda: recurse(depth)))
                self.assertEqual(len(result.errors), 1)
                self.assertEqual(location['exception'], 'RuntimeError')
                self.assertEqual(len(location['frames']), 24)
                self.assertGreater(location['omitted_source_frames'], 0)
                self.assertEqual(location['traceback_truncated'], depth > 256)
                self.assertNotIn('ARTIFICIAL-PRIVATE', text)
