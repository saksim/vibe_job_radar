"""The runner must preserve failures even with Windows legacy console output."""
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch


def runner_module():
    path = Path(__file__).resolve().parents[1] / 'scripts/run_tests.py'
    spec = importlib.util.spec_from_file_location('runner_output_fixture', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class RunnerOutputTests(unittest.TestCase):
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
