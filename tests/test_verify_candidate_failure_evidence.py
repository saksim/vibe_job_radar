"""Keep current bounded runner evidence before its qualification tempdir expires."""
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]


def script(name):
    spec = importlib.util.spec_from_file_location('failure_evidence_' + name, ROOT/'scripts'/f'{name}.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class CandidateFailureEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root/'src/vibe_job_radar').mkdir(parents=True)
        (self.root/'scripts').mkdir()
        (self.root/'src/vibe_job_radar/_version.py').write_text('__version__ = "0.2.1"\n', encoding='utf-8')
        (self.root/'pyproject.toml').write_text('version = "0.2.1"\n', encoding='utf-8')
        self.module = script('verify_candidate')
        self.module.ROOT = self.root
        self.out = self.root/'out'
        self.report = {'tests_run': 1, 'success': False, 'failures': 1, 'errors': 0, 'skipped': 0}
        self.failures = self.out/'unit-tests.failures.jsonl'

    def current_record(self):
        # Exercise the actual producer's schema, including its bounded stacks.
        runner = script('run_tests')
        stream = io.StringIO()
        result = runner.FailureResult(io.StringIO(), True, 1, failure_stream=stream)
        result.started = time.monotonic()
        result._failure_evidence(SimpleNamespace(id=lambda: 'test_fixture.Case.test_failure'), 'failure')
        return stream.getvalue().encode('utf-8')

    def verify(self, raw, *, summary=True, timeout=False, success=False):
        self.unit_paths = []
        def run(args, **kwargs):
            if '--report' in args:
                self.assertEqual(kwargs['timeout'], 1200)
                unit = Path(args[args.index('--report') + 1])
                self.unit_paths.append(unit)
                if summary:
                    report = dict(self.report, success=success, failures=0 if success else 1)
                    unit.write_text(json.dumps(report), encoding='utf-8')
                if raw is not None:
                    unit.with_suffix('.failures.jsonl').write_bytes(raw)
                if timeout:
                    raise subprocess.TimeoutExpired('PRIVATE_COMMAND', kwargs['timeout'])
            return subprocess.CompletedProcess(args, 0 if success else 1, b'')
        with patch.object(self.module.platform, 'platform', return_value='fixture'), \
                patch.object(self.module.subprocess, 'run', side_effect=run):
            return self.module.verify(self.out)

    def test_current_failure_records_survive_temporary_directory_cleanup(self):
        raw = self.current_record()
        result = self.verify(raw)
        self.assertFalse(result['success'])
        self.assertEqual(result['tests'], self.report)
        self.assertTrue(self.failures.is_file())
        self.assertEqual(result.get('unit_failure_evidence'), 'retained')
        self.assertEqual(self.failures.read_bytes(), raw)
        self.assertFalse(self.unit_paths[0].parent.exists())

    def test_timeout_preserves_current_failure_record_without_unit_summary(self):
        raw = self.current_record()
        result = self.verify(raw, summary=False, timeout=True)
        self.assertEqual(result['tests'], {})
        self.assertEqual(result['error'], 'local_check_timeout')
        self.assertEqual(result['timeout_seconds'], 1200)
        self.assertTrue(self.failures.is_file())
        self.assertEqual(result.get('unit_failure_evidence'), 'retained')
        self.assertEqual(self.failures.read_bytes(), raw)
        self.assertNotIn('PRIVATE_', json.dumps(result))

    def test_missing_or_empty_current_file_cannot_reuse_previous_failures(self):
        for raw in (None, b''):
            with self.subTest(current=raw):
                self.out.mkdir(exist_ok=True)
                self.failures.write_bytes(b'PRIVATE_OLD_RUN\n')
                result = self.verify(raw, success=True)
                self.assertTrue(result['success'])
                self.assertEqual(result.get('unit_failure_evidence'), 'missing' if raw is None else 'empty')
                if raw is None:
                    self.assertFalse(self.failures.exists())
                else:
                    self.assertEqual(self.failures.read_bytes(), b'')

    def test_invalid_or_oversize_records_are_not_exported_or_change_test_result(self):
        valid = {'event': 'failure', 'test': 'test_fixture.Case.test_failure',
                 'elapsed_seconds': 1, 'capture_status': 'unavailable'}
        values = [(b'{PRIVATE_BROKEN', 'invalid'), (b'\xff', 'invalid'), (b'x' * 513, 'too_large'),
                  (json.dumps({**valid, 'message': 'PRIVATE_EXCEPTION'}).encode(), 'invalid'),
                  (json.dumps({**valid, 'elapsed_seconds': float('nan')}).encode(), 'invalid'),
                  (b'{"event":"PRIVATE_DUPLICATE",' + json.dumps(valid).encode()[1:], 'invalid')]
        for raw, status in values:
            with self.subTest(size=len(raw)), \
                    patch.object(self.module, 'MAX_FAILURE_EVIDENCE_BYTES', 512, create=True):
                result = self.verify(raw, success=True)
                self.assertTrue(result['success'])
                self.assertFalse(self.failures.exists())
                self.assertEqual(result.get('unit_failure_evidence'), status)
                self.assertNotIn('PRIVATE_', json.dumps(result))

    def test_diagnostic_errors_do_not_replace_original_timeout_or_summary(self):
        class PRIVATE_ERROR(OSError):
            pass
        for timeout in (False, True):
            for raw, status in ((b'{PRIVATE_BROKEN', 'invalid'), (self.current_record(), 'unavailable')):
                with self.subTest(timeout=timeout, status=status), \
                        patch.object(self.module, 'atomic_text', side_effect=PRIVATE_ERROR('PRIVATE_PATH'), create=True):
                    result = self.verify(raw, timeout=timeout)
                self.assertFalse(result['success'])
                if timeout:
                    self.assertEqual(result['error'], 'local_check_timeout')
                self.assertEqual(result.get('unit_failure_evidence'), status)
                self.assertEqual(result['tests'], self.report)
                self.assertEqual(json.loads((self.out/'unit-tests.json').read_text(encoding='utf-8')), self.report)
                self.assertFalse(self.failures.exists())
                self.assertNotIn('PRIVATE_', json.dumps(result))


if __name__ == '__main__':
    unittest.main()
