"""Run all tests without installing the package; optionally save machine-readable evidence."""
from __future__ import annotations
import argparse
from contextlib import ExitStack, contextmanager
import faulthandler
from functools import partial
import io
import json
import platform
import sys
import time
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


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
        self._failure_evidence(test, kind)

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
    """Identify a stalled test without dumping inputs, locals or process secrets."""
    def __init__(self,*args,progress,**kwargs):
        super().__init__(*args,**kwargs);self.progress=progress;self.started=0
    def startTest(self,test):
        super().startTest(test);self.started=time.monotonic()
        self.progress.write('START '+test.id()+'\n');self.progress.flush()
        faulthandler.dump_traceback_later(120,file=self.progress)
    def stopTest(self,test):
        faulthandler.cancel_dump_traceback_later()
        self.progress.write(f'END {test.id()} {time.monotonic()-self.started:.3f}s\n');self.progress.flush()
        super().stopTest(test)


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--report", type=Path)
    p.add_argument("--progress",action='store_true',help='Retain test IDs/timings and a stack trace if one test exceeds 120s; requires --report')
    args = p.parse_args()
    if args.progress and args.report is None:p.error('--progress requires --report')
    stream = io.StringIO()
    suite = unittest.defaultTestLoader.discover(str(ROOT / "tests"))
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
