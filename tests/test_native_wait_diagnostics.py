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
            self.assertEqual(stages.snapshot()['pending'], {})
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

    def test_periodic_stack_dump_runs_on_owned_python_watcher_without_c_timer(self):
        stages=probe.Stages();observed=threading.Event();threads=[]
        original=probe.faulthandler.dump_traceback
        def dump(*args,**kwargs):
            threads.append(threading.current_thread().name)
            original(*args,**kwargs);observed.set()
        with tempfile.TemporaryDirectory() as tmp,patch.object(probe.faulthandler,'dump_traceback',dump), \
                patch.object(probe.faulthandler,'dump_traceback_later') as timer, \
                patch.object(probe.faulthandler,'cancel_dump_traceback_later') as cancel:
            out=Path(tmp)
            with probe.capture(stages,out,'periodic',interval=.01,stack_interval=.01):
                self.assertTrue(observed.wait(2))
            timer.assert_not_called();cancel.assert_not_called()
            self.assertTrue(threads);self.assertEqual(set(threads),{'native-wait-evidence'})
            self.assertIn('watch',(out/'periodic-threads.log').read_text(encoding='utf-8'))
            result=json.loads((out/'periodic-progress.json').read_text(encoding='utf-8'))
            self.assertTrue(result['success']);self.assertTrue(result['watchdog_stopped'])
            self.assertNotIn(SECRET,json.dumps(result))

    def test_failed_periodic_stack_dump_cannot_claim_success(self):
        failed=threading.Event()
        def dump(*args,**kwargs):failed.set();raise RuntimeError(SECRET)
        with tempfile.TemporaryDirectory() as tmp,patch.object(probe.faulthandler,'dump_traceback',dump):
            out=Path(tmp)
            with self.assertRaisesRegex(RuntimeError,'evidence incomplete'):
                with probe.capture(probe.Stages(),out,'periodic',interval=.01,stack_interval=.01):
                    self.assertTrue(failed.wait(2))
            result=json.loads((out/'periodic-progress.json').read_text(encoding='utf-8'))
            self.assertFalse(result['success']);self.assertEqual(result['writer_error_types'],['RuntimeError'])
            self.assertNotIn(SECRET,json.dumps(result))

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


class NativeWaitLifetimeTests(unittest.TestCase):
    def test_forty_closed_instances_leave_capacity_for_later_failure_evidence(self):
        stages = probe.Stages(); Backend = probe.observed_backend(FakeBackend, stages)
        marker = object()
        for _ in range(40):
            backend = Backend(lambda *args: marker)
            self.assertIs(backend.open({'password': SECRET}), marker)
            backend.close()
        report = stages.snapshot()
        self.assertEqual(report['dropped'], 0)
        self.assertEqual(report['pending'], {})
        self.assertEqual(report['allocated_backends'], 40)
        self.assertEqual(report['closed_backends'], 40)
        self.assertEqual(report['calls']['initialize'], 40)
        self.assertEqual(report['calls']['open'], 40)
        self.assertEqual(report['calls']['close'], 40)
        self.assertEqual(len(report['history']), 128)
        self.assertNotIn(SECRET, json.dumps(report))

    def test_sixteen_simultaneous_limit_retained_and_new_id_never_reused(self):
        stages = probe.Stages(); Backend = probe.observed_backend(FakeBackend, stages)
        backends = [Backend(None) for _ in range(16)]
        overflow = Backend(None)
        self.assertIsNone(overflow._wait_probe_id)
        self.assertEqual(len(stages.snapshot()['pending']), 16)
        self.assertGreater(stages.snapshot()['dropped'], 0)
        backends[0].close()
        latest = Backend(None)
        self.assertEqual(latest._wait_probe_id, 17)
        self.assertEqual(len(stages.snapshot()['pending']), 16)
        self.assertNotIn('1', stages.snapshot()['pending'])
        self.assertEqual(stages.snapshot()['closed_backends'], 1)
        for backend in backends[1:] + [latest, overflow]: backend.close()
        self.assertEqual(stages.snapshot()['pending'], {})

    def test_late_closed_calls_preserve_return_and_exception_without_aliasing(self):
        stages = probe.Stages(); Backend = probe.observed_backend(FakeBackend, stages)
        marker = object(); old = Backend(marker); old.close(); current = Backend(None)
        self.assertIs(old.snapshot(), marker)
        failure = ValueError(SECRET)
        def fail(*args): raise failure
        old.received = fail
        with self.assertRaises(ValueError) as caught: old.open({})
        self.assertIs(caught.exception, failure)
        old.close()
        report = stages.snapshot()
        self.assertEqual(report['pending'], {'2': []})
        self.assertEqual(report['closed_backends'], 1)
        self.assertEqual(report['dropped'], 0)
        self.assertGreater(report['after_close_calls'], 0)
        self.assertNotIn(SECRET, json.dumps(report))
        current.close()

    def test_constructor_cleanup_retires_after_outer_initialize_finishes(self):
        stages = probe.Stages(); failure = RuntimeError(SECRET)
        class FailedConstructor(FakeBackend):
            def __init__(self):
                self.close()
                raise failure
        with self.assertRaises(RuntimeError) as caught:
            probe.observed_backend(FailedConstructor, stages)()
        self.assertIs(caught.exception, failure)
        report = stages.snapshot()
        self.assertEqual(report['pending'], {})
        self.assertEqual(report['closed_backends'], 1)
        self.assertEqual(report['dropped'], 0)
        self.assertEqual(report['calls']['initialize'], 1)
        self.assertEqual(report['calls']['close'], 1)

    def test_close_keeps_inflight_worker_visible_until_its_original_return(self):
        stages = probe.Stages(); entered = threading.Event(); release = threading.Event()
        marker = object(); results = []
        def blocked(*args):
            entered.set()
            if not release.wait(5): raise AssertionError('worker was not released')
            return marker
        backend = probe.observed_backend(FakeBackend, stages)(blocked)
        worker = threading.Thread(target=lambda: results.append(backend.open({})))
        try:
            worker.start(); self.assertTrue(entered.wait(2)); backend.close()
            self.assertEqual(stages.snapshot()['pending'], {'1': ['open', 'cdp.Fetch.getResponseBody']})
            self.assertEqual(stages.snapshot()['closed_backends'], 0)
        finally:
            release.set(); worker.join(timeout=2)
        self.assertFalse(worker.is_alive()); self.assertEqual(results, [marker])
        self.assertEqual(stages.snapshot()['pending'], {})
        self.assertEqual(stages.snapshot()['closed_backends'], 1)
        self.assertEqual(stages.snapshot()['dropped'], 0)

    def test_failed_close_keeps_slot_and_original_exception(self):
        stages = probe.Stages(); failure = OSError(SECRET)
        class FailedClose(FakeBackend):
            def close(self): raise failure
        backend = probe.observed_backend(FailedClose, stages)(None)
        with self.assertRaises(OSError) as caught: backend.close()
        self.assertIs(caught.exception, failure)
        report = stages.snapshot()
        self.assertEqual(report['pending'], {'1': []})
        self.assertEqual(report['closed_backends'], 0)
        self.assertEqual(report['dropped'], 0)
        self.assertNotIn(SECRET, json.dumps(report))

    def test_dropped_evidence_refuses_success_without_overwriting_original_error(self):
        for original_error in [None, ValueError(SECRET)]:
            with self.subTest(original_failure=original_error is not None), tempfile.TemporaryDirectory() as temp:
                stages = probe.Stages()
                for _ in range(17): stages.backend()
                expected = RuntimeError if original_error is None else ValueError
                with self.assertRaises(expected) as caught:
                    with probe.capture(stages, Path(temp), 'dropped'):
                        if original_error is not None: raise original_error
                if original_error is not None: self.assertIs(caught.exception, original_error)
                else: self.assertIn('evidence incomplete', str(caught.exception))
                result = json.loads((Path(temp)/'dropped-progress.json').read_text(encoding='utf-8'))
                self.assertFalse(result['success']); self.assertGreater(result['dropped'], 0)
                self.assertTrue(result['watchdog_stopped'])
                self.assertNotIn(SECRET, json.dumps(result))


class NativeWaitCompletionTests(unittest.TestCase):
    def test_inflight_close_fails_at_capture_boundary_and_keeps_worker_stack(self):
        stages = probe.Stages(); entered = threading.Event(); release = threading.Event()
        class BlockingClose(FakeBackend):
            def close(self):
                entered.set()
                if not release.wait(5):
                    raise AssertionError('test close was not released')
        backend = probe.observed_backend(BlockingClose, stages)(None)
        worker = threading.Thread(target=backend.close)
        try:
            with tempfile.TemporaryDirectory() as temp:
                out = Path(temp)
                with self.assertRaisesRegex(RuntimeError, 'evidence incomplete'):
                    with probe.capture(stages, out, 'closing'):
                        worker.start(); self.assertTrue(entered.wait(2))
                self.assertTrue(worker.is_alive())
                result = json.loads((out/'closing-progress.json').read_text(encoding='utf-8'))
                self.assertFalse(result['success'])
                self.assertEqual(result['pending'], {'1': ['close']})
                self.assertEqual((result['allocated_backends'], result['closed_backends']), (1, 0))
                self.assertTrue(result['watchdog_stopped'])
                self.assertIn('close', (out/'closing-threads.log').read_text(encoding='utf-8'))
                self.assertTrue(all(SECRET not in p.read_text(encoding='utf-8') for p in out.iterdir()))
                release.set(); worker.join(timeout=2)
                self.assertFalse(worker.is_alive())
                self.assertEqual(stages.snapshot()['pending'], {})
                # Later cleanup must not turn the frozen first result into success.
                self.assertEqual(json.loads((out/'closing-progress.json').read_text(encoding='utf-8')), result)
        finally:
            release.set(); worker.join(timeout=2)

    def test_unclosed_idle_backend_fails_without_replacing_original_error(self):
        for original in (None, ValueError(SECRET)):
            with self.subTest(original_failure=original is not None), tempfile.TemporaryDirectory() as temp:
                stages = probe.Stages()
                backend = probe.observed_backend(FakeBackend, stages)(None)
                expected = RuntimeError if original is None else ValueError
                try:
                    with self.assertRaises(expected) as caught:
                        with probe.capture(stages, Path(temp), 'idle'):
                            if original is not None:
                                raise original
                    if original is not None:
                        self.assertIs(caught.exception, original)
                    result = json.loads((Path(temp)/'idle-progress.json').read_text(encoding='utf-8'))
                    self.assertFalse(result['success'])
                    self.assertEqual(result['pending'], {'1': []})
                    self.assertEqual(result['closed_backends'], 0)
                    self.assertTrue((Path(temp)/'idle-threads.log').stat().st_size)
                    self.assertTrue(all(SECRET not in p.read_text(encoding='utf-8') for p in Path(temp).iterdir()))
                finally:
                    backend.close()

    def test_closed_backends_complete_without_extra_stack_dump_or_wait(self):
        stages = probe.Stages()
        with tempfile.TemporaryDirectory() as temp, patch.object(probe.faulthandler, 'dump_traceback') as dump:
            with probe.capture(stages, Path(temp), 'closed'):
                backend = probe.observed_backend(FakeBackend, stages)(None)
                backend.close()
            result = json.loads((Path(temp)/'closed-progress.json').read_text(encoding='utf-8'))
            self.assertTrue(result['success'])
            self.assertEqual(result['pending'], {})
            self.assertEqual((result['allocated_backends'], result['closed_backends']), (1, 1))
            dump.assert_not_called()
