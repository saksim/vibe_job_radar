"""Exercise a blocked worker and evidence failure without browsers, CA or network."""
import importlib.util
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location('native_wait_probe_under_test',
    Path(__file__).resolve().parents[1] / 'scripts/native_wait_diagnostics.py')
probe = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(probe)
SECRET = 'sensitive-fixture-value-do-not-serialize'


class FakeBackend:
    def __init__(self, received):
        self.received = received
    def _configure_context(self): pass
    def _new_page(self): pass
    def _load_robots(self): pass
    def snapshot(self): return self.received
    def close(self): pass
    def open(self, value):
        return self._send(SECRET, 'Fetch.getResponseBody', value, self.received)
    def _send(self, session, method, params=None, callback=None):
        return callback(session, method, params)


class NativeWaitDiagnosticsTests(unittest.TestCase):
    def test_original_calls_callback_and_return_are_preserved(self):
        stages = probe.Stages(); marker = object(); arguments = []
        def received(*args):
            arguments.append(args)
            return marker
        backend = probe.observed_backend(FakeBackend, stages)(received)
        payload = {'password': SECRET}
        with patch.object(Path, 'write_text', side_effect=AssertionError('callback wrote to disk')):
            self.assertIs(backend.open(payload), marker)
            self.assertIs(backend.snapshot(), received)
        self.assertEqual(arguments, [(SECRET, 'Fetch.getResponseBody', payload)])
        self.assertIs(arguments[0][2], payload)
        self.assertEqual(stages.snapshot()['pending'], {'1': []})
        self.assertNotIn(SECRET, json.dumps(stages.snapshot()))
        failure = ValueError(SECRET)
        def fail(*_): raise failure
        backend.received = fail
        with self.assertRaises(ValueError) as caught: backend.open(payload)
        self.assertIs(caught.exception, failure)
        self.assertEqual(stages.snapshot()['pending'], {'1': []})

    def test_wait_checkpoint_survives_cleanup_and_excludes_secret_values(self):
        stages = probe.Stages(); entered = threading.Event(); release = threading.Event()
        def blocked(*_):
            entered.set()
            if not release.wait(5): raise AssertionError('test worker was not released')
        backend = probe.observed_backend(FakeBackend, stages)(blocked)
        worker = threading.Thread(target=lambda: backend.open({'password': SECRET}))
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp); failure = TimeoutError(SECRET)
            try:
                with self.assertRaises(TimeoutError) as caught:
                    with probe.capture(stages, out, 'wait', interval=.01):
                        worker.start(); self.assertTrue(entered.wait(2))
                        deadline = time.monotonic() + 2
                        while True:
                            file = out/'wait-checkpoints.jsonl'
                            text = file.read_text(encoding='utf-8') if file.exists() else ''
                            # Read only completed JSONL records during concurrent append.
                            lines = text.split('\n')[:-1]
                            if any('cdp.Fetch.getResponseBody' in json.loads(x)['pending']['1'] for x in lines): break
                            self.assertLess(time.monotonic(), deadline)
                            time.sleep(.005)
                        raise failure
                self.assertIs(caught.exception, failure)
            finally:
                release.set(); worker.join(timeout=2)
            backend.close()
            self.assertFalse(worker.is_alive())
            self.assertEqual(stages.snapshot()['pending'], {'1': []})
            snapshots = [json.loads(x) for x in (out/'wait-checkpoints.jsonl').read_text(encoding='utf-8').splitlines()]
            self.assertTrue(any(x['pending']['1'] == ['open', 'cdp.Fetch.getResponseBody'] for x in snapshots))
            final = json.loads((out/'wait-progress.json').read_text(encoding='utf-8'))
            self.assertFalse(final['success']); self.assertTrue(final['watchdog_stopped'])
            self.assertEqual(final['error_types'], ['TimeoutError'])
            stacks = (out/'wait-threads.log').read_text(encoding='utf-8')
            self.assertIn('blocked', stacks)
            self.assertTrue(all(SECRET not in x.read_text(encoding='utf-8') for x in out.iterdir()))

    def test_original_failure_survives_evidence_write_failure(self):
        stages = probe.Stages(); failure = ValueError(SECRET)
        original = Path.write_text; calls = []
        def write(path, *a, **kw):
            calls.append(path)
            if len(calls) > 1: raise OSError(SECRET)
            return original(path, *a, **kw)
        with tempfile.TemporaryDirectory() as tmp, patch.object(Path, 'write_text', write):
            with self.assertRaises(ValueError) as caught:
                with probe.capture(stages, Path(tmp), 'wait'):
                    raise failure
        self.assertIs(caught.exception, failure)

    def test_failed_checkpoint_writer_cannot_report_success(self):
        stages = probe.Stages(); original = Path.open; failed = threading.Event()
        def open_file(path, *a, **kw):
            if path.suffix == '.jsonl':
                failed.set(); raise OSError(SECRET)
            return original(path, *a, **kw)
        with tempfile.TemporaryDirectory() as tmp, patch.object(Path, 'open', open_file):
            out = Path(tmp)
            with self.assertRaisesRegex(RuntimeError, 'evidence incomplete'):
                with probe.capture(stages, out, 'wait'):
                    self.assertTrue(failed.wait(2))
            report = json.loads((out/'wait-progress.json').read_text(encoding='utf-8'))
            self.assertFalse(report['success'])
            self.assertEqual(report['writer_error_types'], ['OSError'])
            self.assertNotIn(SECRET, json.dumps(report))

    def test_bounds_and_unknown_command_labels(self):
        stages = probe.Stages()
        for _ in range(20):
            ident = stages.backend()
            for _ in range(40): stages.enter(ident, SECRET)
        report = stages.snapshot()
        self.assertEqual(len(report['pending']), 16)
        self.assertEqual(len(report['history']), 128)
        self.assertTrue(all(len(x) == 32 for x in report['pending'].values()))
        self.assertGreater(report['dropped'], 0)
        self.assertEqual(set(report['calls']), {'cdp.other'})
        self.assertNotIn(SECRET, json.dumps(report))

    def test_exception_chain_is_bounded_without_messages(self):
        errors = [ValueError(SECRET) for _ in range(10)]
        for a, b in zip(errors, errors[1:] + errors[:1]): a.__cause__ = b
        self.assertEqual(probe.error_chain(errors[0]), ['ValueError'] * 8)

    def test_ci_guard_requires_each_condition(self):
        for controlled, ci, actions in [(False,'true','true'), (True,'','true'), (True,'true',''), (True,'1','true')]:
            with self.subTest(controlled=controlled, ci=ci, actions=actions), \
                    patch.object(probe.sys, 'argv', ['probe'] + (['--controlled'] if controlled else [])), \
                    patch.dict(probe.os.environ, {'CI':ci,'GITHUB_ACTIONS':actions}):
                with self.assertRaises(SystemExit): probe.require_ci()
        with patch.object(probe.sys, 'argv', ['probe','--controlled']), \
                patch.dict(probe.os.environ, {'CI':'true','GITHUB_ACTIONS':'true'}):
            probe.require_ci()
