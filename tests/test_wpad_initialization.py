"""Concurrent hosts share WPAD initialization; do not time out mid-download."""
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from vibe_job_radar import wpad, wpad_native, system_pac, pac_native
from vibe_job_radar.loopback_proxy import LocalProxyError


class WpadInitializationTests(unittest.TestCase):
    def exercise(self, outcome):
        clock = [0.]
        allowed = [True]
        discovery_entered = threading.Event()
        discovery_release = threading.Event()
        download_entered = threading.Event()
        download_release = threading.Event()
        waiter_contended = threading.Event()
        waiter_finished = threading.Event()
        values = {}
        native_lock = threading.Lock()
        class ObservedLock:
            def acquire(self, *, timeout):
                acquired = native_lock.acquire(timeout=timeout)
                if not acquired: waiter_contended.set()
                return acquired
            def release(self): native_lock.release()
        source = system_pac.Source('https://fixture.example/wpad.dat')
        policy = wpad.WpadSnapshot(source.config_id, lambda:allowed[0])
        object.__setattr__(policy, '_lock', ObservedLock())
        def discover(permitted):
            discovery_entered.set()
            if not discovery_release.wait(3): raise AssertionError('fixture discovery release missing')
            return source
        def fetch(selected, permitted):
            self.assertEqual(selected, source)
            download_entered.set()
            if not download_release.wait(3): raise AssertionError('fixture download release missing')
            return 'function FindProxyForURL(url,host){return "DIRECT";}'
        def request(name, done=None):
            try: values[name] = policy.for_host(name+'.example')
            except Exception as exc: values[name] = exc.code if isinstance(exc,LocalProxyError) else type(exc).__name__
            finally:
                if done is not None: done.set()
        first = threading.Thread(target=request, args=('first',))
        second = threading.Thread(target=request, args=('second', waiter_finished))
        with patch.object(wpad, 'time', SimpleNamespace(monotonic=lambda:clock[0])), \
             patch.object(wpad_native, 'discover', side_effect=discover) as discovery, \
             patch.object(system_pac, 'fetch', side_effect=fetch) as download, \
             patch.object(pac_native, 'evaluate', return_value='DIRECT') as evaluate:
            try:
                first.start(); self.assertTrue(discovery_entered.wait(2))
                second.start(); self.assertTrue(waiter_contended.wait(2))
                clock[0] = 6.
                discovery_release.set(); self.assertTrue(download_entered.wait(2))
                clock[0] = 9. if outcome == 'success' else 17.
                if outcome == 'revoke': allowed[0] = False
                if outcome == 'success':
                    self.assertFalse(waiter_finished.wait(.2), 'waiter stopped before shared discovery + download completed')
                    clock[0] = 12.
                else:
                    self.assertTrue(waiter_finished.wait(2))
                    self.assertEqual(values['second'], 'pac_revoked' if outcome == 'revoke' else 'pac_busy')
                download_release.set()
                first.join(2); second.join(2)
                self.assertFalse(first.is_alive() or second.is_alive())
                if outcome == 'success':
                    self.assertEqual(values, {'first':None, 'second':None})
                    self.assertEqual(evaluate.call_count, 2)
                discovery.assert_called_once(); download.assert_called_once()
            finally:
                discovery_release.set(); download_release.set()
                if first.ident is not None: first.join(3)
                if second.ident is not None: second.join(3)

    def test_waiter_survives_discovery_then_download_exceeding_one_phase_budget(self):
        self.exercise('success')

    def test_combined_initialization_wait_remains_finite(self):
        self.exercise('timeout')

    def test_waiter_can_be_revoked_during_the_shared_initialization(self):
        self.exercise('revoke')


if __name__ == '__main__': unittest.main()
