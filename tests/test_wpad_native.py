"""WPAD ABI/IPC tests use synthetic sources; never discover on the host LAN."""
import ctypes
import json
import multiprocessing
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from vibe_job_radar import wpad_native
from vibe_job_radar.loopback_proxy import LocalProxyError

URL = 'https://pac.example/private.pac?token=SYNTHETIC'


def fixture_worker(sender):
    # A real spawned process, with only the OS discovery source substituted.
    with patch.object(wpad_native, '_native_url', return_value=URL):
        wpad_native._worker(sender)


class WpadNativeTests(unittest.TestCase):
    def test_native_abi_uses_dhcp_and_dns_and_frees_all_returned_allocations(self):
        for success, text, expected in (
                (True, URL, None), (True, '', None),
                (True, 'x'*4097, 'wpad_source_invalid'),
                (False, URL, 'wpad_not_found')):
            with self.subTest(success=success, length=len(text)):
                buffer = ctypes.create_unicode_buffer(text)
                detect, free = MagicMock(), MagicMock()
                def call(flags, output):
                    self.assertEqual(flags, 3)
                    ctypes.cast(output, ctypes.POINTER(ctypes.c_void_p))[0] = ctypes.addressof(buffer)
                    return success
                detect.side_effect = call
                def library(name, **kwargs):
                    return SimpleNamespace(WinHttpDetectAutoProxyConfigUrl=detect) if name == 'winhttp' else SimpleNamespace(GlobalFree=free)
                with patch.object(wpad_native, 'available', return_value=True), \
                     patch.object(ctypes, 'WinDLL', side_effect=library, create=True), \
                     patch.object(ctypes, 'get_last_error', return_value=12180, create=True):
                    if expected:
                        with self.assertRaisesRegex(LocalProxyError, expected): wpad_native._native_url()
                    else:
                        self.assertEqual(wpad_native._native_url(), text)
                self.assertEqual(detect.argtypes, [ctypes.wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p)])
                self.assertEqual(free.call_args.args[0].value, ctypes.addressof(buffer))
                free.assert_called_once()

    def test_missing_native_api_or_null_pointer_is_fixed_error(self):
        with patch.object(wpad_native, 'available', return_value=False):
            with self.assertRaisesRegex(LocalProxyError, 'wpad_unavailable'): wpad_native._native_url()
        dll = SimpleNamespace(WinHttpDetectAutoProxyConfigUrl=MagicMock(return_value=True),
                              GlobalFree=MagicMock())
        with patch.object(wpad_native, 'available', return_value=True), \
             patch.object(ctypes, 'WinDLL', return_value=dll, create=True):
            with self.assertRaisesRegex(LocalProxyError, 'wpad_source_invalid'): wpad_native._native_url()
            dll.GlobalFree.assert_not_called()

    def test_worker_does_not_leak_native_errors_or_accept_untrusted_url_forms(self):
        for value in ('file:///private.pac', 'https://user:password@pac.example/', 'http://host/path#private', 'x'*4097):
            sender = MagicMock()
            with patch.object(wpad_native, '_native_url', return_value=value):
                wpad_native._worker(sender)
            self.assertEqual(json.loads(sender.send_bytes.call_args.args[0]), {'error':'wpad_source_invalid'})
            sender.close.assert_called_once()
        sender = MagicMock()
        with patch.object(wpad_native, '_native_url', side_effect=RuntimeError('PRIVATE-SOURCE')):
            wpad_native._worker(sender)
        self.assertNotIn(b'PRIVATE', sender.send_bytes.call_args.args[0])

    def test_real_spawn_and_bytes_pipe_without_calling_host_discovery(self):
        context = multiprocessing.get_context('spawn')
        def process(**kwargs):
            self.assertIs(kwargs.pop('target'), wpad_native._worker)
            return context.Process(target=fixture_worker, **kwargs)
        proxy = SimpleNamespace(Pipe=context.Pipe, Process=process)
        before = {p.pid for p in multiprocessing.active_children()}
        with patch.object(wpad_native, 'available', return_value=True), \
             patch.object(wpad_native.multiprocessing, 'get_context', return_value=proxy):
            self.assertEqual(wpad_native.discover(lambda: True).url, URL)
        self.assertEqual({p.pid for p in multiprocessing.active_children()}, before)

    def test_original_deadline_permission_malformed_ipc_and_cleanup(self):
        cases = [
            ('valid', 1., 1., 1., True, json.dumps({'url':URL}).encode(), None),
            ('startup', 8., 0., 0., True, b'', 'wpad_timeout'),
            ('receive', 1., 1., 6., True, json.dumps({'url':URL}).encode(), 'wpad_timeout'),
            ('revoked', 0., 0., 0., False, json.dumps({'url':URL}).encode(), 'pac_revoked'),
            ('malformed', 0., 0., 0., True, b'[]', 'wpad_failed'),
            ('unknown_error', 0., 0., 0., True, b'{"error":"PRIVATE"}', 'wpad_failed'),
            ('refused', 0., 0., 0., True, b'{"error":"wpad_not_found"}', 'wpad_not_found'),
            ('bad_url', 0., 0., 0., True, b'{"url":"file:///secret"}', 'wpad_source_invalid'),
        ]
        for name, start, poll, receive, allowed, raw, error in cases:
            with self.subTest(name=name):
                elapsed = [0.]
                context, receiver, sender = MagicMock(), MagicMock(), MagicMock()
                context.Pipe.return_value = receiver, sender
                process = context.Process.return_value
                process.pid = 123
                process.is_alive.return_value = True
                def advance(seconds): elapsed[0] += seconds
                process.start.side_effect = lambda: advance(start)
                receiver.poll.side_effect = lambda timeout: (advance(poll), True)[1]
                receiver.recv_bytes.side_effect = lambda limit: (advance(receive), raw)[1]
                clock = SimpleNamespace(monotonic=lambda: elapsed[0])
                with patch.object(wpad_native, 'available', return_value=True), \
                     patch.object(wpad_native, 'time', clock), \
                     patch.object(wpad_native.multiprocessing, 'get_context', return_value=context):
                    if error:
                        with self.assertRaisesRegex(LocalProxyError, error): wpad_native.discover(lambda: allowed)
                    else:
                        self.assertEqual(wpad_native.discover(lambda: allowed).url, URL)
                if process.start.called:
                    process.close.assert_called_once()
                    receiver.close.assert_called_once()
                    process.terminate.assert_called_once()
                self.assertTrue(wpad_native._SLOTS.acquire(blocking=False))
                self.assertTrue(wpad_native._SLOTS.acquire(blocking=False))
                wpad_native._SLOTS.release(); wpad_native._SLOTS.release()

    def test_no_permission_no_worker_and_slot_overload_no_new_worker(self):
        with patch.object(wpad_native, 'available', return_value=True), \
             patch.object(wpad_native.multiprocessing, 'get_context') as context:
            with self.assertRaisesRegex(LocalProxyError, 'pac_revoked'): wpad_native.discover(lambda: 1)
            context.assert_not_called()
            wpad_native._SLOTS.acquire(); wpad_native._SLOTS.acquire()
            try:
                with self.assertRaisesRegex(LocalProxyError, 'pac_busy'): wpad_native.discover(lambda: True)
            finally:
                wpad_native._SLOTS.release(); wpad_native._SLOTS.release()
            context.assert_not_called()


if __name__ == '__main__': unittest.main()
