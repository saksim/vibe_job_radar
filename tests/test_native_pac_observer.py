"""Passive PAC observation must preserve the original controller error path."""
from collections import Counter
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
with patch.object(sys, "path", [str(ROOT / "scripts"), str(ROOT / "tests"), *sys.path]):
    import run_native_pac_browser as pac
    from run_native_auth_probe import ObservedBackend


class NativePacObserverTests(unittest.TestCase):
    def test_shared_send_keeps_auth_counts_callback_and_original_exception(self):
        for fails in (False, True):
            with self.subTest(fails=fails):
                backend = object.__new__(ObservedBackend)
                backend.probe = {'sent': Counter(), 'acknowledged': Counter()}
                backend._handshake_trace = pac.HandshakeTrace()
                params = {'private_fixture': 'not-to-be-recorded'}
                received, calls = [], []
                payload, token = object(), object()
                original_error = RuntimeError('private original operation failure')

                def send(owner, session, method, actual_params, callback):
                    calls.append((owner, session, method, actual_params))
                    if fails:
                        raise original_error
                    callback(payload)
                    return token

                with patch.object(pac.NativeBackend, '_send', send):
                    if fails:
                        with self.assertRaises(RuntimeError) as caught:
                            backend._send('owned', 'Fetch.enable', params, received.append)
                        self.assertIs(caught.exception, original_error)
                    else:
                        self.assertIs(backend._send('owned', 'Fetch.enable', params, received.append), token)
                self.assertEqual(calls, [(backend, 'owned', 'Fetch.enable', params)])
                self.assertEqual(received, [] if fails else [payload])
                self.assertEqual(backend.probe['sent']['Fetch.enable'], 1)
                self.assertEqual(backend.probe['acknowledged']['Fetch.enable'], 0 if fails else 1)
                facts = pac.handshake_facts(backend)
                self.assertEqual([row['event'] for row in facts['events']],
                                 ['Fetch.enable.sent'] if fails else ['Fetch.enable.sent', 'Fetch.enable.acknowledged'])
                self.assertNotIn('private', json.dumps(facts))
                self.assertNotIn('not-to-be-recorded', json.dumps(facts))

    def test_observer_decode_error_still_reaches_original_handler(self):
        for original_decode_fails in (False, True):
            with self.subTest(original_decode_fails=original_decode_fails):
                # Use the actual observed class and inherited received/fatal
                # methods without starting a browser, tunnel, CA or network.
                backend = object.__new__(pac.ObservedPacBackend)
                backend._closing = False
                backend._sessions = {'owned': 'target'}
                received = []
                backend._pending = {41: ('owned', received.append)}
                backend._handshake_trace = pac.HandshakeTrace()
                backend.error, backend.wait_error = '', None
                backend._halted, backend._cdp = False, None
                event = {'sessionId': 'owned', 'message': 'unused injected protocol input'}
                payload = {'accepted': True}
                original_result = (RecursionError('private protocol contents')
                                   if original_decode_fails else {'id': 41, 'result': payload})
                with patch.object(pac.json, 'loads', side_effect=[
                        RecursionError('private observer contents'), original_result]) as decode:
                    backend._received(event)
                self.assertEqual(decode.call_count, 2)
                self.assertEqual(received, [] if original_decode_fails else [payload])
                self.assertEqual(backend.error, 'native_protocol_error' if original_decode_fails else '')
                self.assertEqual(backend._halted, original_decode_fails)
