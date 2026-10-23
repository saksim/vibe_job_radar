"""Only controlled private pipes and loopback listeners; no browser/site."""
import io
import threading
import unittest
from unittest.mock import Mock, patch
from types import SimpleNamespace

from vibe_job_radar.guided.cdp_pipe import PipeTransport
from vibe_job_radar.guided.native_tunnel import NativeTunnel
from vibe_job_radar.network_policy import NetworkPolicy


class PipeStartupFailureTests(unittest.TestCase):
    def test_reader_allocation_failure_closes_control_input(self):
        process = SimpleNamespace(stdin=io.BytesIO(), stdout=io.BytesIO())
        failure = RuntimeError('controlled reader allocation failure')
        try:
            with patch('vibe_job_radar.guided.cdp_pipe.threading.Thread', side_effect=failure):
                with self.assertRaises(RuntimeError) as caught:
                    PipeTransport(process)
            self.assertIs(caught.exception, failure)
            self.assertTrue(process.stdin.closed)
        finally:
            process.stdin.close()

    def test_writer_start_failure_stops_and_joins_started_reader(self):
        release = threading.Event()
        entered = threading.Event()
        class Input(io.BytesIO):
            def close(self):
                release.set()
                super().close()
        class Output:
            def read(self, _size):
                entered.set()
                release.wait(2)
                return b''
        process = SimpleNamespace(stdin=Input(), stdout=Output())
        original_start = threading.Thread.start
        threads = []
        failure = RuntimeError('controlled writer startup failure')
        def start(thread):
            threads.append(thread)
            if thread.name == 'native-cdp-write':
                raise failure
            return original_start(thread)
        try:
            with patch('threading.Thread.start', start):
                with self.assertRaises(RuntimeError) as caught:
                    PipeTransport(process)
            self.assertIs(caught.exception, failure)
            self.assertTrue(entered.wait(1))
            self.assertEqual((process.stdin.closed, threads[0].is_alive()), (True, False))
        finally:
            process.stdin.close()
            for thread in threads:
                if thread.ident is not None:
                    thread.join(timeout=2)

    def test_cleanup_error_cannot_replace_writer_start_failure(self):
        process = SimpleNamespace(stdin=Mock(), stdout=io.BytesIO())
        process.stdin.close.side_effect = OSError('controlled pipe close failure')
        failure = RuntimeError('controlled writer startup failure')
        reader, writer = Mock(), Mock()
        writer.start.side_effect = failure
        with patch('vibe_job_radar.guided.cdp_pipe.threading.Thread', side_effect=[reader, writer]):
            with self.assertRaises(RuntimeError) as caught:
                PipeTransport(process)
        self.assertIs(caught.exception, failure)
        process.stdin.close.assert_called_once()
        reader.join.assert_called_once_with(timeout=1)
        writer.join.assert_not_called()


class TunnelShutdownFailureTests(unittest.TestCase):
    def test_shutdown_error_does_not_leave_listener_bound(self):
        guard = NativeTunnel(('jobs.fixture.test',), NetworkPolicy(), threading.Event())
        real_shutdown = guard.server.shutdown
        original = OSError('controlled shutdown error after loop stopped')
        def shutdown():
            real_shutdown()
            raise original
        try:
            with patch.object(guard.server, 'shutdown', side_effect=shutdown):
                with self.assertRaises(OSError) as caught:
                    guard.close()
            self.assertIs(caught.exception, original)
            self.assertEqual(guard.server.socket.fileno(), -1)
            self.assertFalse(guard.thread.is_alive())
            guard.close()
        finally:
            real_shutdown()
            guard.server.server_close()
            guard.thread.join(timeout=2)

    def test_server_close_error_does_not_skip_join(self):
        guard = NativeTunnel(('jobs.fixture.test',), NetworkPolicy(), threading.Event())
        real_close = guard.server.server_close
        original = OSError('controlled server close failure')
        def close():
            real_close()
            raise original
        try:
            with patch.object(guard.server, 'server_close', side_effect=close), \
                    patch.object(guard.thread, 'join', wraps=guard.thread.join) as join:
                with self.assertRaises(OSError) as caught:
                    guard.close()
                self.assertIs(caught.exception, original)
                join.assert_called_once_with(timeout=2)
            self.assertEqual(guard.server.socket.fileno(), -1)
        finally:
            guard.server.shutdown()
            real_close()
            guard.thread.join(timeout=2)


if __name__ == '__main__':
    unittest.main()
