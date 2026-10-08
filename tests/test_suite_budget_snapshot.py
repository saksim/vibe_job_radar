"""Short artificial budgets validate diagnostics, never production timeout changes."""
from contextlib import contextmanager
import importlib.util
import io
import json
import re
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from test_test_runner_output import runner_module

ROOT = Path(__file__).resolve().parents[1]


@contextmanager
def owned_snapshot_threads(runner):
    """Observe this context's thread objects without counting an outer runner."""
    thread_type = threading.Thread
    created = []
    def create(*args, **kwargs):
        thread = thread_type(*args, **kwargs)
        created.append(thread)
        return thread
    with patch.object(runner.threading, "Thread", side_effect=create):
        yield created


class SuiteBudgetTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def invoke(self, runner, suite, *, seconds=.02, discovery=None):
        report = self.root / "result.json"
        args = ["run_tests.py", "--report", str(report), "--progress"]
        if seconds is not None:
            args += ["--suite-snapshot-after", str(seconds)]
        with patch.object(sys, "argv", args), patch.object(sys, "stdout", io.StringIO()), \
                patch.object(unittest.defaultTestLoader, "discover", side_effect=discovery,
                             return_value=suite):
            code = runner.main()
        return code, json.loads(report.read_text(encoding="utf-8")), report.with_suffix(".progress.log").read_text(encoding="utf-8")

    def test_many_short_tests_trigger_one_suite_snapshot_without_changing_per_test_alarm(self):
        runner = runner_module()
        suite = unittest.TestSuite([unittest.FunctionTestCase(lambda: time.sleep(.004)) for _ in range(30)])
        with patch.object(runner.faulthandler, "dump_traceback_later") as per_test, \
                patch.object(runner.faulthandler, "cancel_dump_traceback_later"):
            code, result, progress = self.invoke(runner, suite)
        self.assertEqual((code, result["tests_run"], result["failures"], result["errors"]), (0, 30, 0, 0))
        markers = [json.loads(line.split(" ", 1)[1]) for line in progress.splitlines()
                   if line.startswith("SUITE_BUDGET_SNAPSHOT ")]
        self.assertEqual(len(markers), 1)
        self.assertEqual(markers[0]["target_seconds"], .02)
        self.assertGreaterEqual(markers[0]["elapsed_seconds"], .02)
        self.assertEqual(markers[0]["scope"], "budget_checkpoint_not_timeout_or_root_cause")
        self.assertEqual(per_test.call_count, 30)
        self.assertTrue(all(call.args[0] == 120 for call in per_test.call_args_list))
        self.assertIn("Thread", progress)

    def test_coarse_monotonic_clock_cannot_make_suite_elapsed_earlier_than_target(self):
        runner = runner_module()
        captured = threading.Event()
        path = self.root / "precise.log"
        with path.open("w", encoding="utf-8") as progress, \
                patch.object(runner.time, "monotonic", return_value=0), \
                patch.object(runner.faulthandler, "dump_traceback", side_effect=lambda **kw: captured.set()):
            with runner.suite_budget_snapshot(progress, .02):
                self.assertTrue(captured.wait(5))
        marker = json.loads(path.read_text(encoding="utf-8").split(" ", 1)[1])
        self.assertEqual(marker["clock"], "perf_counter")
        self.assertGreaterEqual(marker["elapsed_seconds"], marker["target_seconds"])

    def test_later_test_patches_cannot_intercept_suite_diagnostics(self):
        runner = runner_module()
        captured, patched = threading.Event(), threading.Event()
        real_clock = time.perf_counter
        def initial_clock():
            if threading.current_thread().name == "radar-suite-budget-snapshot":
                patched.wait(5)
            return real_clock()
        with (
            (self.root / "isolated.log").open("w", encoding="utf-8") as progress,
            patch.object(runner.time, "perf_counter", side_effect=initial_clock),
            patch.object(runner.faulthandler, "dump_traceback", side_effect=lambda **kw: captured.set()),
        ):
            with runner.suite_budget_snapshot(progress, .02):
                with (
                    patch.object(runner.time, "perf_counter", return_value=-1) as later_clock,
                    patch.object(runner.faulthandler, "dump_traceback") as later_dump,
                    patch.object(runner.json, "dumps", side_effect=AssertionError("PRIVATE_LATER_MOCK")) as later_json,
                ):
                    patched.set()
                    self.assertTrue(captured.wait(5), "suite diagnostic was intercepted by later test patches")
                    later_clock.assert_not_called()
                    later_dump.assert_not_called()
                    later_json.assert_not_called()
        text = (self.root / "isolated.log").read_text(encoding="utf-8")
        self.assertIn("SUITE_BUDGET_SNAPSHOT ", text)
        self.assertNotIn("PRIVATE_LATER_MOCK", text)

    def test_discovery_is_included_before_first_test_starts(self):
        runner = runner_module()
        observed = threading.Event()
        suite = unittest.TestSuite([unittest.FunctionTestCase(lambda: None)])
        def discovering(_):
            self.assertTrue(observed.wait(5), "diagnostics never reached discovery")
            return suite
        with patch.object(runner.faulthandler, "dump_traceback", side_effect=lambda **kw: observed.set()):
            code, result, progress = self.invoke(runner, suite, discovery=discovering)
        self.assertEqual((code, result["tests_run"]), (0, 1))
        self.assertLess(progress.index("SUITE_BUDGET_SNAPSHOT "), progress.index("START "))

    def test_early_exit_cancels_thread_before_progress_file_closes(self):
        runner = runner_module()
        path = self.root / "progress.log"
        with path.open("w", encoding="utf-8") as progress, \
                patch.object(runner.faulthandler, "dump_traceback") as capture, \
                owned_snapshot_threads(runner) as created:
            with runner.suite_budget_snapshot(progress, 30):
                pass
            self.assertEqual(len(created), 1)
            self.assertFalse(created[0].is_alive())
            self.assertNotIn(created[0], threading.enumerate())
            capture.assert_not_called()
        self.assertEqual(path.read_text(encoding="utf-8"), "")

    def test_inflight_snapshot_is_joined_before_file_close(self):
        runner = runner_module()
        entered, released = threading.Event(), threading.Event()
        observed = []
        with (self.root / "progress.log").open("w", encoding="utf-8") as progress:
            def capture(**kwargs):
                entered.set()
                released.wait(5)
                observed.append(progress.closed)
            def release_after_body():
                self.assertTrue(entered.wait(5))
                released.set()
            helper = threading.Thread(target=release_after_body)
            helper.start()
            try:
                with patch.object(runner.faulthandler, "dump_traceback", side_effect=capture), \
                        owned_snapshot_threads(runner) as created:
                    with runner.suite_budget_snapshot(progress, .001):
                        self.assertTrue(entered.wait(5))
                self.assertEqual(observed, [False])
            finally:
                released.set()
                helper.join()
        self.assertEqual(len(created), 1)
        self.assertFalse(created[0].is_alive())
        self.assertNotIn(created[0], threading.enumerate())

    def test_cleanup_in_nested_run_keeps_outer_diagnostic_active(self):
        runner = runner_module()
        with (self.root / "outer.log").open("w", encoding="utf-8") as progress, \
                owned_snapshot_threads(runner) as created:
            with runner.suite_budget_snapshot(progress, 30):
                self.assertEqual(len(created), 1)
                outer = created[0]
                result = unittest.TestResult()
                unittest.TestSuite([
                    SuiteBudgetTests("test_early_exit_cancels_thread_before_progress_file_closes"),
                    SuiteBudgetTests("test_inflight_snapshot_is_joined_before_file_close"),
                ]).run(result)
                self.assertEqual(result.testsRun, 2)
                self.assertEqual(result.failures, [])
                self.assertEqual(result.errors, [])
                self.assertTrue(outer.is_alive())
            self.assertFalse(outer.is_alive())
            self.assertNotIn(outer, threading.enumerate())

    def test_snapshot_error_preserves_original_failure_and_excludes_diagnostic_message(self):
        runner = runner_module()
        attempted = threading.Event()
        def fail():
            self.assertTrue(attempted.wait(5))
            raise AssertionError("original artificial failure")
        def broken_capture(**kwargs):
            attempted.set()
            raise OSError("PRIVATE_DIAGNOSTIC_MESSAGE")
        suite = unittest.TestSuite([unittest.FunctionTestCase(fail)])
        with patch.object(runner.faulthandler, "dump_traceback", side_effect=broken_capture):
            code, result, progress = self.invoke(runner, suite)
        self.assertEqual((code, result["failures"], result["errors"]), (1, 1, 0))
        self.assertIn("SUITE_BUDGET_STACK_UNAVAILABLE", progress)
        self.assertNotIn("PRIVATE_DIAGNOSTIC_MESSAGE", progress)
        self.assertIn("original artificial failure", (self.root / "result.log").read_text(encoding="utf-8"))

    def test_default_does_not_start_suite_thread(self):
        runner = runner_module()
        with patch.object(runner.threading, "Thread", side_effect=AssertionError("must stay disabled")):
            code, result, progress = self.invoke(runner, unittest.TestSuite(), seconds=None)
        self.assertEqual((code, result["tests_run"]), (0, 0))
        self.assertNotIn("SUITE_BUDGET_", progress)

    def test_thread_construction_or_start_failure_does_not_change_success(self):
        runner = runner_module()
        for where in ("construction", "start"):
            with self.subTest(where=where):
                target = patch.object(runner.threading, "Thread", side_effect=RuntimeError("PRIVATE_CONSTRUCTION")) \
                    if where == "construction" else patch.object(runner.threading.Thread, "start", side_effect=RuntimeError("PRIVATE_START"))
                suite = unittest.TestSuite([unittest.FunctionTestCase(lambda: None)])
                with target:
                    code, result, progress = self.invoke(runner, suite)
                self.assertEqual((code, result["tests_run"], result["errors"]), (0, 1, 0))
                self.assertIn("SUITE_BUDGET_STACK_UNAVAILABLE", progress)
                self.assertNotIn("PRIVATE_", progress)

    def test_invalid_cli_budget_cannot_create_output(self):
        runner = runner_module()
        report = self.root / "invalid.json"
        sets = [
            ["--suite-snapshot-after", "1"],
            ["--report", str(report), "--suite-snapshot-after", "1"],
            ["--progress", "--suite-snapshot-after", "1"],
        ]
        sets += [["--report", str(report), "--progress", "--suite-snapshot-after", n]
                 for n in ("0", "-1", "inf", "-inf", "nan")]
        for args in sets:
            with self.subTest(args=args), patch.object(sys, "argv", ["run_tests.py", *args]), \
                    patch.object(sys, "stderr", io.StringIO()):
                with self.assertRaises(SystemExit) as caught:
                    runner.main()
                self.assertEqual(caught.exception.code, 2)
                self.assertFalse(report.exists())

    def test_verifier_uses_reviewed_1200_180_timeouts_and_keeps_570_snapshot(self):
        path = ROOT / "scripts/verify_candidate.py"
        spec = importlib.util.spec_from_file_location("budget_verifier_fixture", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        fixture = self.root / "source"
        (fixture / "src/vibe_job_radar").mkdir(parents=True)
        (fixture / "scripts").mkdir()
        (fixture / "src/vibe_job_radar/_version.py").write_text('__version__ = "0.2.1"\n', encoding="utf-8")
        (fixture / "pyproject.toml").write_text('version = "0.2.1"\n', encoding="utf-8")
        calls = []
        def run(args, **kwargs):
            calls.append((args, kwargs))
            if "--report" in args:
                Path(args[args.index("--report") + 1]).write_text(json.dumps(
                    dict(tests_run=3, success=True, failures=0, errors=0, skipped=0)), encoding="utf-8")
            return subprocess.CompletedProcess(args, 0, b"fixture")
        with patch.object(module, "ROOT", fixture), patch.object(module.platform, "platform", return_value="fixture"), \
                patch.object(module.subprocess, "run", side_effect=run):
            result = module.verify(self.root / "out")
        self.assertTrue(result["success"])
        self.assertEqual([kwargs["timeout"] for _, kwargs in calls], [1200, 180, 180, 180])
        self.assertEqual(calls[0][0][-2:], ["--suite-snapshot-after", "570"])
        self.assertTrue(all("--suite-snapshot-after" not in args for args, _ in calls[1:]))
        self.assertEqual(result["steps"][0]["suite_snapshot_after_seconds"], 570)

        # The enclosing job must keep its existing setup/build/upload allowance.
        # These are workflow budgets, not individual product-operation deadlines.
        full_qualification_seconds = sum(kwargs["timeout"] for _, kwargs in calls)
        for filename, job, reserve in [
            ("browser.yml", "chromium-user-journey", 6 * 60),
            ("windows-portable.yml", "build-and-run-executable", 11 * 60),
        ]:
            with self.subTest(workflow=filename):
                workflow = (ROOT / ".github/workflows" / filename).read_text(encoding="utf-8")
                section = workflow.split(f"  {job}:\n", 1)[1]
                section = re.split(r"(?m)^  (?=\S)", section, maxsplit=1)[0]
                minutes = re.search(r"(?m)^    timeout-minutes: (\d+)$", section)
                self.assertIsNotNone(minutes)
                self.assertGreaterEqual(int(minutes[1]) * 60,
                                        full_qualification_seconds + reserve)

    def test_full_suite_budgets_preserve_steps_and_outer_cleanup_allowance(self):
        workflows=ROOT / ".github/workflows"
        for filename,job in [('tests.yml','test'),('windows-native-tls.yml','native-chain-and-repair')]:
            with self.subTest(workflow=filename):
                source=(workflows / filename).read_text(encoding='utf8')
                section=source.split(f'  {job}:\n',1)[1]
                self.assertIn('    timeout-minutes: 25\n',section)
                unit_step=re.search(r'(?m)^      - (?:name:.*\n|.*\n)*?        timeout-minutes: 20\n        run: python scripts/run_tests.py [^\n]+',section)
                self.assertIsNotNone(unit_step)
                self.assertIn('if: always()',section)
        native=(workflows / 'native-browser.yml').read_text(encoding='utf8')
        for job in ['native-http','native-chrome']:
            section=native.split(f'  {job}:\n',1)[1]
            section=re.split(r'(?m)^  (?=\S)',section,maxsplit=1)[0]
            self.assertIn('    timeout-minutes: 12\n',section)
        steps=re.findall(r'(?m)^      - name: ([\s\S]+?)(?=^      - |\Z)',native)
        search_steps=[s for s in steps if 'run: ' in s and 'scripts/run_native_liepin_probe.py --controlled' in s]
        self.assertEqual(len(search_steps),3)
        for step in search_steps:
            self.assertIn('        timeout-minutes: 5\n',step)
        self.assertEqual(native.count('        timeout-minutes: 3\n'),10)

    def test_real_child_retains_checkpoint_before_forced_termination_without_success_report(self):
        code = r'''
import importlib.util,sys,threading,unittest
from unittest.mock import patch
from pathlib import Path
path, report = map(Path,sys.argv[1:3])
spec=importlib.util.spec_from_file_location("real_budget_fixture",path)
runner=importlib.util.module_from_spec(spec);spec.loader.exec_module(runner)
def awaiting_parent_termination():
    private_value="ARTIFICIAL_PRIVATE_VALUE_NOT_FOR_DIAGNOSTICS"
    threading.Event().wait(30)
suite=unittest.TestSuite([unittest.FunctionTestCase(lambda:None) for _ in range(3)]
                        +[unittest.FunctionTestCase(awaiting_parent_termination)])
args=["run_tests.py","--report",str(report),"--progress","--suite-snapshot-after","0.05"]
with patch.object(sys,"argv",args),patch.object(unittest.defaultTestLoader,"discover",return_value=suite):
    raise SystemExit(runner.main())
'''
        child_file = self.root / "child.py"
        child_file.write_text(code, encoding="utf-8")
        report = self.root / "child-result.json"
        progress = report.with_suffix(".progress.log")
        process = subprocess.Popen([sys.executable, str(child_file), str(ROOT / "scripts/run_tests.py"), str(report)],
                                   stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        captured = ""
        deadline = time.monotonic() + 15
        try:
            while time.monotonic() < deadline and process.poll() is None:
                if progress.exists():
                    captured = progress.read_text(encoding="utf-8")
                    if "SUITE_BUDGET_SNAPSHOT " in captured and "Thread" in captured:
                        break
                time.sleep(.01)
            self.assertIn("SUITE_BUDGET_SNAPSHOT ", captured)
            self.assertIn("Thread", captured)
            self.assertNotIn("ARTIFICIAL_PRIVATE_VALUE_NOT_FOR_DIAGNOSTICS", captured)
            self.assertFalse(report.exists())
        finally:
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=5)
        self.assertNotEqual(process.returncode, 0)
        self.assertFalse(report.exists())
        self.assertIn("budget_checkpoint_not_timeout_or_root_cause", progress.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
