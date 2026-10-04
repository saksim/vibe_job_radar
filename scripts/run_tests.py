"""Run all tests without installing the package; optionally save machine-readable evidence."""
from __future__ import annotations
import argparse
import builtins
from contextlib import ExitStack, contextmanager
import faulthandler
from functools import partial
import io
import json
import platform
import math
import sys
import time
import threading
import unittest
from unittest.mock import patch
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
LEGACY_DEFERRED_RESULTS = sys.version_info[:2] == (3, 10)


def failure_threads():
    """Bounded code locations only; never thread names, source, locals or values."""
    current = threading.get_ident()
    snapshots = sys._current_frames()
    identities = ([current] if current in snapshots else []) + sorted(
        identity for identity in snapshots if identity != current)
    threads = []
    for identity in identities[:16]:
        frame = snapshots[identity]
        stack = []
        while frame is not None and len(stack) < 24:
            stack.append({'file': Path(frame.f_code.co_filename).name,
                          'line': frame.f_lineno, 'function': frame.f_code.co_name})
            frame = frame.f_back
        threads.append({'current': identity == current, 'stack': stack,
                        'frames_truncated': frame is not None})
    return {'threads': threads, 'threads_omitted': max(0, len(identities) - 16)}

@contextmanager
def capture_before_cleanup():
    # Python 3.10 queues outcomes until after cleanup. Observe the exception
    # at the existing outcome boundary, then let unittest handle it unchanged.
    original = unittest.case._Outcome.testPartExecutor

    def observed(outcome, test, *args, **kwargs):
        context = original(outcome, test, *args, **kwargs)

        class Observed:
            def __enter__(self):
                return context.__enter__()

            def __exit__(self, typ, exc, tb):
                callback = getattr(outcome.result, '_capture_once', None)
                if (exc is not None and callback is not None and not outcome.expecting_failure
                        and not isinstance(exc, (KeyboardInterrupt, unittest.SkipTest,
                                                 unittest.case._ShouldStop))):
                    subtest = isinstance(test, unittest.case._SubTest)
                    parent = test.test_case if subtest else test
                    kind = 'failure' if isinstance(exc, parent.failureException) else 'error'
                    callback(parent, ('subtest_' if subtest else '') + kind, exc)
                # Delegate the original traceback unchanged. An extra generator
                # yield becomes a user frame that unittest can truncate before
                # the actual assertion on Python 3.12.
                return context.__exit__(typ, exc, tb)

        return Observed()

    unittest.case._Outcome.testPartExecutor = observed
    try:
        yield
    finally:
        unittest.case._Outcome.testPartExecutor = original

class FailureResult(unittest.TextTestResult):
    """Save failure-time evidence before tearDown/addCleanup can release workers."""
    def __init__(self, *args, failure_stream=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.failure_stream = failure_stream
        self.started = 0
        self._captured = []

    def startTest(self, test):
        super().startTest(test)
        self.started = time.monotonic()
        self._captured.clear()
        self._capture_context = capture_before_cleanup()
        self._capture_context.__enter__()

    def stopTest(self, test):
        try:
            super().stopTest(test)
        finally:
            self._capture_context.__exit__(None, None, None)
            # Do not retain exceptions/tracebacks or fixture locals after a test.
            self._captured.clear()

    def _capture_once(self, test, kind, exc):
        if any(exc is previous for previous in self._captured):
            return
        self._captured.append(exc)
        try:
            self._failure_evidence(test, kind)
        except Exception:
            # Tests can replace clocks/serializers as well as frame capture.
            # Evidence must not stop unittest from recording the original
            # failure and invoking its normal cleanup.
            try:
                self.stream.write('FAILURE_EVIDENCE unavailable; original result retained\n')
                self.stream.flush()
            except Exception:
                pass

    def _failure_evidence(self, test, kind):
        try:
            locations = failure_threads()
        except Exception:
            locations = {'capture_status': 'unavailable'}
        record = {'event': kind, 'test': test.id(),
                  'elapsed_seconds': round(time.monotonic() - self.started, 3),
                  **locations}
        line = json.dumps(record, ensure_ascii=False) + '\n'
        # The normal buffered log is a fallback if the optional file fails.
        self.stream.write('FAILURE_EVIDENCE ' + line)
        self.stream.flush()
        if self.failure_stream is not None:
            try:
                self.failure_stream.write(line)
                self.failure_stream.flush()
            except Exception:
                self.stream.write('FAILURE_EVIDENCE file unavailable; original result retained\n')
                self.stream.flush()

    def addFailure(self, test, err):
        super().addFailure(test, err)
        self._capture_once(test, 'failure', err[1])

    def addError(self, test, err):
        super().addError(test, err)
        self._capture_once(test, 'error', err[1])

    def addSubTest(self, test, subtest, err):
        super().addSubTest(test, subtest, err)
        if err is not None:
            # The parent ID excludes arbitrary subtest parameter values.
            self._capture_once(test, 'subtest_failure' if
                               issubclass(err[0], test.failureException) else 'subtest_error', err[1])


class ProgressResult(FailureResult):
    """Capture a failed or stalled test without inputs, locals or exception text."""
    def __init__(self,*args,progress,**kwargs):
        super().__init__(*args,**kwargs);self.progress=progress;self.started=0
        self.failure_dumped = False
    def startTest(self,test):
        super().startTest(test);self.started=time.monotonic()
        self.failure_dumped = False
        self.test_cleanup = ExitStack()
        if LEGACY_DEFERRED_RESULTS:
            # Python 3.10 feeds outcome.errors to the result only after cleanup.
            # Observe that stored outcome before the original cleanup, without
            # changing it, replacing TestCase.run, or intercepting exceptions.
            for name in ('_callTearDown', 'doCleanups'):
                original = getattr(test, name)
                def observed(*args, _original=original, **kwargs):
                    self._observe_deferred_outcome(test)
                    return _original(*args, **kwargs)
                self.test_cleanup.enter_context(patch.object(test, name, observed))
        self.progress.write('START '+test.id()+'\n');self.progress.flush()
        faulthandler.dump_traceback_later(120,file=self.progress)

    def _observe_deferred_outcome(self, test):
        try:
            errors = getattr(getattr(test, '_outcome', None), 'errors', ())
            for _, err in errors:
                if err is not None:
                    self._failure_stack('PENDING_FAILURE', test, err)
                    break
        except Exception:
            pass

    def _failure_location(self, err):
        # Only static source paths/line numbers and canonical built-in types.
        # Exception messages, source lines, locals and subtest inputs stay out.
        name = getattr(err[0], '__name__', '')
        exception = name if getattr(builtins, name, None) is err[0] else 'Exception'
        frames, external, count = [], 0, 0
        current = err[2]
        root = ROOT.resolve()
        while current is not None and count < 256:
            count += 1
            try:
                path = Path(current.tb_frame.f_code.co_filename).resolve().relative_to(root)
                if path.parts[0] not in {'src', 'scripts', 'tests'} or path.suffix != '.py':
                    raise ValueError('not a project source frame')
                frames.append({'file': path.as_posix(), 'line': current.tb_lineno})
            except (ValueError, OSError):
                external += 1
            current = current.tb_next
        row = dict(exception=exception, frames=frames[-24:], external_frames=external,
                   omitted_source_frames=max(0, len(frames)-24), traceback_truncated=current is not None)
        self.progress.write('FAILURE_LOCATION '+json.dumps(row, ensure_ascii=True)+'\n')
        self.progress.flush()

    def _failure_stack(self, kind, test, err):
        # The result or legacy outcome is already recorded. Before cleanup can
        # release a still-working thread; repeated subtest failures stay bounded.
        if self.failure_dumped:
            return
        self.failure_dumped = True
        try:
            self._failure_location(err)
        except Exception:
            # A missing location must not suppress the independent live stack.
            try:
                self.progress.write('FAILURE_LOCATION_UNAVAILABLE\n')
                self.progress.flush()
            except Exception:
                pass
        try:
            self.progress.write(f'{kind} {test.id()}\n')
            self.progress.flush()
            faulthandler.dump_traceback(file=self.progress, all_threads=True)
        except Exception:
            # Diagnostics must not turn an original assertion into another error.
            try:
                self.progress.write('STACK_UNAVAILABLE\n')
                self.progress.flush()
            except Exception:
                pass

    def addFailure(self, test, err):
        super().addFailure(test, err)
        self._failure_stack('FAILURE', test, err)

    def addError(self, test, err):
        super().addError(test, err)
        self._failure_stack('ERROR', test, err)

    def addSubTest(self, test, subtest, err):
        super().addSubTest(test, subtest, err)
        if err is not None:
            # A subtest ID can contain parameter values; use only its parent ID.
            self._failure_stack('SUBTEST_FAILURE', test, err)

    def stopTest(self,test):
        try:
            faulthandler.cancel_dump_traceback_later()
            self.progress.write(f'END {test.id()} {time.monotonic()-self.started:.3f}s\n');self.progress.flush()
        finally:
            self.test_cleanup.close()
            super().stopTest(test)


def _suite_diagnostic_error(progress):
    try:
        progress.write("SUITE_BUDGET_STACK_UNAVAILABLE\n")
        progress.flush()
    except Exception:
        pass


@contextmanager
def suite_budget_snapshot(progress, after_seconds):
    """One best-effort checkpoint, independent of per-test alarm resets."""
    stopped = None
    # perf_counter is monotonic and has finer resolution than GetTickCount64
    # on supported older Windows/Python combinations.
    # Tests may temporarily patch these module attributes after discovery.
    # Keep this suite observer independent of the code being exercised.
    clock = time.perf_counter
    dump_traceback = faulthandler.dump_traceback
    serialize = json.dumps
    started = clock()
    deadline = started + after_seconds

    def capture():
        try:
            while True:
                if stopped.wait(max(0, deadline - clock())):
                    return
                if clock() >= deadline:
                    break
            marker = {"target_seconds": after_seconds,
                      "elapsed_seconds": round(clock() - started, 6),
                      "clock": "perf_counter",
                      "scope": "budget_checkpoint_not_timeout_or_root_cause"}
            progress.write("SUITE_BUDGET_SNAPSHOT " + serialize(marker) + "\n")
            progress.flush()
            dump_traceback(file=progress, all_threads=True)
        except Exception:
            _suite_diagnostic_error(progress)

    worker = None
    running = False
    try:
        try:
            stopped = threading.Event()
            worker = threading.Thread(target=capture, name="radar-suite-budget-snapshot", daemon=True)
            worker.start()
            running = True
        except Exception:
            _suite_diagnostic_error(progress)
        yield
    finally:
        if stopped is not None:
            stopped.set()
        if running:
            # Finish any in-flight dump before the progress file can be closed.
            worker.join()


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--report", type=Path)
    p.add_argument("--progress",action='store_true',help='Retain test IDs/timings, failure code locations and live thread stacks at the first failure/error or after 120s; requires --report')
    p.add_argument("--suite-snapshot-after", type=float,
                   help="One thread checkpoint after this many suite seconds; requires --progress/--report, does not change timeouts")
    args = p.parse_args()
    if args.progress and args.report is None:p.error('--progress requires --report')
    if args.suite_snapshot_after is not None:
        if not args.progress or args.report is None:
            p.error("--suite-snapshot-after requires --progress and --report")
        if not math.isfinite(args.suite_snapshot_after) or args.suite_snapshot_after <= 0:
            p.error("--suite-snapshot-after must be finite and positive")
    stream = io.StringIO()
    with ExitStack() as cleanup:
        failure_stream = None
        if args.report:
            args.report.parent.mkdir(parents=True, exist_ok=True)
            failure_stream = cleanup.enter_context(
                args.report.with_suffix('.failures.jsonl').open('w', encoding='utf-8'))
        resultclass = partial(FailureResult, failure_stream=failure_stream)
        if args.progress:
            args.report.parent.mkdir(parents=True,exist_ok=True)
            progress=cleanup.enter_context(args.report.with_suffix('.progress.log').open('w',encoding='utf-8'))
            cleanup.callback(faulthandler.cancel_dump_traceback_later)
            resultclass=partial(ProgressResult,progress=progress,failure_stream=failure_stream)
            if args.suite_snapshot_after is not None:
                cleanup.enter_context(suite_budget_snapshot(progress, args.suite_snapshot_after))
        # Discovery can also consume the suite budget; include it in this clock.
        suite = unittest.defaultTestLoader.discover(str(ROOT / "tests"))
        result = unittest.TextTestRunner(stream=stream, verbosity=2,resultclass=resultclass).run(suite)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps({"created_at": datetime.now(timezone.utc).isoformat(),
                              "python": sys.version, "platform": platform.platform(),
                              "tests_run": result.testsRun, "failures": len(result.failures),
                              "errors": len(result.errors), "skipped": len(result.skipped),
                              "success": result.wasSuccessful(), "live_network_calls": 0,
                              "note": "Network provider paths use mocked responses; not a live integration certification."},
                              ensure_ascii=False, indent=2), encoding="utf-8")
        args.report.with_suffix(".log").write_text(stream.getvalue(), encoding="utf-8")
    # Save UTF-8 evidence before console output: a Windows legacy code page
    # must not erase the original failure or prevent the JSON report entirely.
    encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
    console = stream.getvalue().encode(encoding, errors="backslashreplace").decode(encoding)
    print(console)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
