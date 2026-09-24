"""Run all tests without installing the package; optionally save machine-readable evidence."""
from __future__ import annotations
import argparse
from contextlib import ExitStack
import faulthandler
from functools import partial
import io
import json
import platform
import sys
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


class ProgressResult(unittest.TextTestResult):
    """Capture a failed or stalled test without inputs, locals or exception text."""
    def __init__(self,*args,progress,**kwargs):
        super().__init__(*args,**kwargs);self.progress=progress;self.started=0
        self.failure_dumped = False
    def startTest(self,test):
        super().startTest(test);self.started=time.monotonic()
        self.failure_dumped = False
        self.progress.write('START '+test.id()+'\n');self.progress.flush()
        faulthandler.dump_traceback_later(120,file=self.progress)

    def _failure_stack(self, kind, test):
        # The result is already recorded. Capture before TestCase cleanup can
        # release a still-working thread; repeated subtest failures stay bounded.
        if self.failure_dumped:
            return
        self.failure_dumped = True
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
        self._failure_stack('FAILURE', test)

    def addError(self, test, err):
        super().addError(test, err)
        self._failure_stack('ERROR', test)

    def addSubTest(self, test, subtest, err):
        super().addSubTest(test, subtest, err)
        if err is not None:
            # A subtest ID can contain parameter values; use only its parent ID.
            self._failure_stack('SUBTEST_FAILURE', test)

    def stopTest(self,test):
        faulthandler.cancel_dump_traceback_later()
        self.progress.write(f'END {test.id()} {time.monotonic()-self.started:.3f}s\n');self.progress.flush()
        super().stopTest(test)


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--report", type=Path)
    p.add_argument("--progress",action='store_true',help='Retain test IDs/timings and thread stacks at the first failure/error or after 120s; requires --report')
    args = p.parse_args()
    if args.progress and args.report is None:p.error('--progress requires --report')
    stream = io.StringIO()
    suite = unittest.defaultTestLoader.discover(str(ROOT / "tests"))
    with ExitStack() as cleanup:
        resultclass=unittest.TextTestResult
        if args.progress:
            args.report.parent.mkdir(parents=True,exist_ok=True)
            progress=cleanup.enter_context(args.report.with_suffix('.progress.log').open('w',encoding='utf-8'))
            cleanup.callback(faulthandler.cancel_dump_traceback_later)
            resultclass=partial(ProgressResult,progress=progress)
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
