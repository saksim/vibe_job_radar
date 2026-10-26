"""Candidate command failures retain bounded facts without replacing the failure."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import build_container_candidate as candidate


class CandidateCommandFailureTests(unittest.TestCase):
    def test_build_failure_preserves_boundary_and_redacts_diagnostics(self):
        for message, signals in (("SECRET_HOST unknown flag: --private", ["unknown_flag_message"]),
                                 ("SECRET_TOKEN an unfamiliar error", [])):
            with self.subTest(signals=signals), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                version = root / "src/vibe_job_radar/_version.py"
                version.parent.mkdir(parents=True)
                version.write_text('__version__ = "0.20.2"\n', encoding="utf-8")
                out = root / "candidate"
                result = subprocess.CompletedProcess(["docker"], 125, "SECRET_STDOUT", message)
                with patch.object(candidate, "ROOT", root), \
                     patch.object(candidate.platform, "machine", return_value="x86_64"), \
                     patch.object(candidate, "git_source", return_value=("a" * 40, "b" * 40, "sha256:" + "c" * 64)), \
                     patch.dict(os.environ, {"RADAR_SOURCE_SHA": "a" * 40, "GITHUB_RUN_ID": "123", "GITHUB_RUN_ATTEMPT": "1"}), \
                     patch.object(candidate.subprocess, "run", return_value=result) as run, \
                     patch.object(candidate, "archive_config") as archive, \
                     patch.object(candidate, "smoke") as smoke:
                    with self.assertRaisesRegex(ValueError, "^Command failed: docker$"):
                        candidate.build(out, "amd64", "example/repo", "python:3.12-slim@sha256:" + "d" * 64)
                run.assert_called_once()
                self.assertEqual(run.call_args.args[0][:2], ["docker", "build"])
                self.assertEqual(run.call_args.kwargs["timeout"], 600)
                archive.assert_not_called()
                smoke.assert_not_called()
                raw = (out / "failure.json").read_text(encoding="utf-8")
                proof = json.loads(raw)
                self.assertNotIn("SECRET", raw)
                self.assertNotIn("example/repo", raw)
                self.assertEqual(proof["source_revision"], "a" * 40)
                self.assertEqual(proof["candidate_attempt"], 1)
                self.assertEqual(proof["stage"], "build")
                self.assertEqual(proof["outcome"], "nonzero_exit")
                self.assertEqual(proof["exit_code"], 125)
                self.assertEqual(proof["root_cause"], "unknown")
                self.assertTrue(proof["diagnostic_only"])
                self.assertEqual(proof["stderr"]["observed_signals"], signals)
                self.assertEqual(proof["stderr"]["length"], len(message))
                self.assertEqual(sorted(p.name for p in out.iterdir()), ["failure.json"])

    def test_timeout_and_spawn_errors_survive_diagnostic_write_failure(self):
        for kind in ("timeout", "spawn_error"):
            for write_fails in (False, True):
                with self.subTest(kind=kind, write_fails=write_fails), tempfile.TemporaryDirectory() as folder:
                    out = Path(folder)
                    error = (subprocess.TimeoutExpired(["docker", "SECRET_ARG"], 600,
                                                       output=b"SECRET_OUTPUT i/o timeout", stderr=None)
                             if kind == "timeout" else FileNotFoundError("SECRET_PATH"))
                    with patch.object(candidate.subprocess, "run", side_effect=error), \
                         patch("builtins.print", side_effect=BrokenPipeError("SECRET_PRINT")) as output:
                        if write_fails:
                            with patch.object(candidate, "save", side_effect=OSError("SECRET_WRITE")):
                                with self.assertRaises(type(error)) as raised:
                                    candidate._candidate_command(out, {}, "inspect", ["docker", "SECRET_ARG"])
                        else:
                            with self.assertRaises(type(error)) as raised:
                                candidate._candidate_command(out, {}, "inspect", ["docker", "SECRET_ARG"])
                    self.assertIs(raised.exception, error)
                    output.assert_not_called()
                    if write_fails:
                        self.assertFalse((out / "failure.json").exists())
                    else:
                        raw = (out / "failure.json").read_text(encoding="utf-8")
                        proof = json.loads(raw)
                        self.assertNotIn("SECRET", raw)
                        self.assertEqual(proof["outcome"], kind)
                        self.assertIsNone(proof["exit_code"])
                        self.assertFalse(proof["stderr"]["available"])
                        self.assertIsNone(proof["stderr"]["length"])
                        self.assertFalse(proof["stderr"]["scan_complete"])
                        self.assertEqual(proof["stdout"]["observed_signals"],
                                         ["network_timeout_message"] if kind == "timeout" else [])

    def test_success_is_unchanged_and_signal_scan_is_bounded(self):
        with tempfile.TemporaryDirectory() as folder:
            out = Path(folder)
            result = subprocess.CompletedProcess(["docker"], 0, "original output", "")
            for timeout in (600, 120):
                with patch.object(candidate.subprocess, "run", return_value=result) as run:
                    self.assertEqual(candidate._candidate_command(out, {}, "save", ["docker", "save"],
                                                                 cwd=out, timeout=timeout), "original output")
                self.assertEqual(run.call_args.kwargs, {"cwd": out, "env": None, "input": None,
                    "capture_output": True, "text": True, "encoding": "utf-8", "timeout": timeout})
            self.assertEqual(list(out.iterdir()), [])
        text = "SECRET unknown flag" + "x" * candidate._OUTPUT_SCAN_LIMIT
        summary = candidate._output_summary(text)
        self.assertEqual(summary["length"], len(text))
        self.assertFalse(summary["scan_complete"])
        self.assertEqual(summary["observed_signals"], [])
        byte_summary = candidate._output_summary(b"SECRET x509: unknown authority")
        self.assertEqual(byte_summary["unit"], "bytes")
        self.assertEqual(byte_summary["observed_signals"], ["certificate_message"])
        self.assertNotIn("SECRET", json.dumps([summary, byte_summary]))


if __name__ == "__main__":
    unittest.main()
