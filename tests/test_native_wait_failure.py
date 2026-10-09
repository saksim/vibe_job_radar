"""Capture an actual failed wait before its caller closes a blocked worker."""
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
import native_wait_diagnostics as probe
import native_service_wait
import run_native_liepin_probe as search_probe

SECRET = 'fixture-private-value-not-for-evidence'


class Clock:
    def __init__(self):
        self.now = 0.0
    def monotonic(self):
        return self.now
    def sleep(self, seconds):
        self.now = round(self.now + seconds, 8)


class BusyService:
    _busy = True
    def __init__(self):
        self._lock = threading.Lock()
    def state(self):
        return {'busy': True, 'jobs': []}


class NativeWaitFailureTests(unittest.TestCase):
    def test_actual_deadline_records_blocked_worker_before_callers_cleanup(self):
        stages = probe.Stages()
        entered, release = threading.Event(), threading.Event()
        errors = []
        ident = stages.backend()
        def hold_original_call():
            entered.set()
            if not release.wait(5):
                raise AssertionError('test release missing')
        def work():
            token = stages.enter(ident, 'open')
            try:
                stages.io_timings.observe('rate.reserve', hold_original_call)()
            except BaseException as error:
                errors.append(error)
            finally:
                stages.leave(ident, token)
        worker = threading.Thread(target=work, name='owned-blocked-test-worker')
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            clock = Clock()
            observed = probe.observed_wait(native_service_wait.wait_for_guided_job, stages, out, 'search')
            worker.start()
            self.assertTrue(entered.wait(2))
            try:
                with patch.object(native_service_wait, 'time', clock):
                    with self.assertRaisesRegex(TimeoutError, '^native search did not finish$'):
                        observed(BusyService())
                self.assertEqual(clock.now, 45.05)
                saved = json.loads((out/'search-first-wait-failure.json').read_text(encoding='utf8'))
                self.assertEqual(saved['phase'], 'wait_timeout_before_caller_cleanup')
                self.assertEqual(saved['progress']['pending'], {'1': ['open']})
                self.assertEqual(saved['progress']['io_timings']['pending'][0]['operation'], 'rate.reserve')
                self.assertEqual(saved['progress']['closed_backends'], 0)
                self.assertIn('hold_original_call', (out/'search-first-wait-failure-threads.log').read_text(encoding='utf8'))
            finally:
                release.set()
                worker.join(2)
                stages.closed(ident)
            self.assertFalse(worker.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual(stages.snapshot()['pending'], {})
            self.assertEqual(json.loads((out/'search-first-wait-failure.json').read_text(encoding='utf8')), saved)
            self.assertTrue(all(SECRET not in p.read_text(encoding='utf8') for p in out.iterdir()))

    def test_success_preserves_arguments_result_and_writes_no_files(self):
        stages = probe.Stages()
        calls, marker, value = [], object(), {'secret': SECRET}
        def original(*args, **kwargs):
            calls.append((args, kwargs))
            return marker
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            observed = probe.observed_wait(original, stages, out, 'search')
            self.assertIs(observed(value, password=SECRET), marker)
            self.assertIs(calls[0][0][0], value)
            self.assertEqual(calls, [((value,), {'password': SECRET})])
            self.assertEqual(list(out.iterdir()), [])
            self.assertEqual(stages.result['wait_failure_observer']['attempts'], 0)

    def test_same_timeout_is_reraised_and_first_snapshot_cannot_be_replaced(self):
        stages = probe.Stages()
        failure = TimeoutError(SECRET)
        calls = []
        def original(value):
            calls.append(value)
            raise failure
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            observed = probe.observed_wait(original, stages, out, 'search')
            for value in [1, 2]:
                with self.assertRaises(TimeoutError) as caught:
                    observed(value)
                self.assertIs(caught.exception, failure)
                if value == 1:
                    first = (out/'search-first-wait-failure.json').read_bytes()
            self.assertEqual(calls, [1, 2])
            self.assertEqual((out/'search-first-wait-failure.json').read_bytes(), first)
            self.assertEqual(len(list(out.iterdir())), 2)
            state = stages.result['wait_failure_observer']
            self.assertEqual(state, {'installed': True, 'attempts': 1, 'snapshot_written': True,
                                     'stack_written': True, 'writer_error_types': []})
            self.assertNotIn(SECRET, first.decode())

    def test_other_original_error_is_not_relabelled_as_timeout(self):
        stages = probe.Stages()
        failure = ValueError(SECRET)
        def original():
            raise failure
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            with self.assertRaises(ValueError) as caught:
                probe.observed_wait(original, stages, out, 'search')()
            self.assertIs(caught.exception, failure)
            self.assertEqual(list(out.iterdir()), [])
            self.assertEqual(stages.result['wait_failure_observer']['attempts'], 0)

    def test_snapshot_fault_preserves_the_original_timeout(self):
        stages = probe.Stages()
        failure = TimeoutError(SECRET)
        def original():
            raise failure
        with tempfile.TemporaryDirectory() as tmp, patch.object(stages, 'snapshot', side_effect=RuntimeError(SECRET)):
            with self.assertRaises(TimeoutError) as caught:
                probe.observed_wait(original, stages, Path(tmp), 'search')()
            self.assertIs(caught.exception, failure)
            state = stages.result['wait_failure_observer']
            self.assertEqual(state['writer_error_types'], ['RuntimeError'])
            self.assertFalse(state['snapshot_written'])
            self.assertNotIn(SECRET, json.dumps(state))

    def test_output_fault_preserves_timeout_and_cannot_claim_saved(self):
        stages = probe.Stages()
        failure = TimeoutError(SECRET)
        def original():
            raise failure
        with tempfile.TemporaryDirectory() as tmp:
            observed = probe.observed_wait(original, stages, Path(tmp), 'search')
            with patch.object(Path, 'open', side_effect=OSError(SECRET)):
                with self.assertRaises(TimeoutError) as caught:
                    observed()
            self.assertIs(caught.exception, failure)
            state = stages.result['wait_failure_observer']
            self.assertEqual(state['attempts'], 1)
            self.assertEqual(state['writer_error_types'], ['OSError'])
            self.assertFalse(state['snapshot_written'])
            self.assertFalse(state['stack_written'])
            self.assertNotIn(SECRET, json.dumps(state))

    def test_real_probe_binds_original_wait_and_records_before_acceptance_cleanup(self):
        failure = TimeoutError(SECRET)
        calls = []
        def original(service):
            calls.append(service)
            raise failure
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            out = root/'browser-acceptance/native'
            def acceptance():
                try:
                    search_probe.acceptance.wait('artificial-service')
                finally:
                    self.assertTrue((out/'search-wait-first-wait-failure.json').is_file())
            with patch.object(search_probe, 'require_ci'), \
                    patch.object(search_probe.acceptance, 'ROOT', root), \
                    patch.object(search_probe.acceptance, 'wait', original), \
                    patch.object(search_probe.acceptance, 'main', acceptance), \
                    patch.object(search_probe, 'PROBES', []):
                with self.assertRaises(TimeoutError) as caught:
                    search_probe.main()
                self.assertIs(caught.exception, failure)
                self.assertIs(search_probe.acceptance.wait, original)
            self.assertEqual(calls, ['artificial-service'])
            result = json.loads((out/'search-wait-progress.json').read_text(encoding='utf8'))
            self.assertFalse(result['success'])
            self.assertEqual(result['wait_failure_observer']['attempts'], 1)
            self.assertTrue(result['wait_failure_observer']['snapshot_written'])
            self.assertTrue(all(SECRET not in p.read_text(encoding='utf8') for p in out.iterdir()))


if __name__ == '__main__':
    unittest.main()
