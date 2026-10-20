"""Diagnostic ordering and privacy without modifying browser decisions."""
import json
import sys
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
with patch.object(sys, "path", [str(ROOT / "scripts"), *sys.path]):
    import native_handshake_evidence as handshake
    from native_guard_evidence import GuardEvidence


class NativeHandshakeEvidenceTests(unittest.TestCase):
    def test_shared_trace_keeps_fixed_handshake_order_and_exception(self):
        trace = handshake.HandshakeTrace()
        guard = GuardEvidence(trace)
        secret = "PRIVATE_HEADER_OR_URL"
        error = RuntimeError(secret)
        trace.record("Fetch.enable.acknowledged")
        reply = guard.observed(lambda sock, status: None, "reply")
        reply(secret, 407)
        trace.record("Fetch.authRequired.event")
        trace.record("Fetch.continueWithAuth.sent")
        operation = guard.observed(lambda: (_ for _ in ()).throw(error), "open")
        with self.assertRaises(RuntimeError) as caught:
            operation()
        self.assertIs(caught.exception, error)
        trace.record(secret)
        snapshot = handshake.handshake_facts(SimpleNamespace(_handshake_trace=trace))
        self.assertTrue(snapshot["complete"])
        self.assertEqual([row["event"] for row in snapshot["events"]], [
            "Fetch.enable.acknowledged", "guard.reply.407.started", "guard.reply.407.returned",
            "Fetch.authRequired.event", "Fetch.continueWithAuth.sent",
            "guard.open.started", "guard.open.raised"])
        self.assertNotIn(secret, json.dumps(snapshot))
        self.assertEqual(sorted(row["elapsed_ms"] for row in snapshot["events"]),
                         [row["elapsed_ms"] for row in snapshot["events"]])
        trace.record("Network.loadingFailed.event")
        self.assertEqual(len(snapshot["events"]), 7)

    def test_bounds_missing_and_locked_trace_never_become_complete_zero(self):
        self.assertFalse(handshake.handshake_facts(SimpleNamespace())["available"])
        trace = handshake.HandshakeTrace()
        with patch.object(handshake, "LIMIT", 2):
            for _ in range(3):
                trace.record("Fetch.enable.sent")
        value = trace.snapshot()
        self.assertEqual(len(value["events"]), 2)
        self.assertFalse(value["complete"])
        self.assertTrue(value["overflow"])
        trace = handshake.HandshakeTrace()
        trace._lock.acquire()
        finished = threading.Event()
        def worker():
            trace.record("Fetch.enable.sent")
            finished.set()
        thread = threading.Thread(target=worker)
        try:
            thread.start()
            self.assertTrue(finished.wait(1))
            self.assertFalse(trace.snapshot()["available"])
        finally:
            trace._lock.release()
            thread.join(1)
        self.assertFalse(trace.snapshot()["complete"])
        self.assertEqual(trace.snapshot()["events"], [])
