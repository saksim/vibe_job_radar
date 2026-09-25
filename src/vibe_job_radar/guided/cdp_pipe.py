"""Private process-pipe transport; no listener, endpoint discovery or logs."""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import queue
import sys
import threading

from .contracts import CrawlError


def node_executable():
    # Use the same installed distribution as the existing Playwright driver.
    # This locates its bundled executable, without patching or calling private
    # SDK APIs. Preserve an explicitly configured Node executable selection.
    configured = os.environ.get('PLAYWRIGHT_NODEJS_PATH')
    if configured:
        path = Path(configured)
    else:
        spec = importlib.util.find_spec('playwright')
        if spec is None or not spec.origin:
            raise CrawlError('playwright_missing')
        path = Path(spec.origin).parent / 'driver' / ('node.exe' if sys.platform == 'win32' else 'node')
    if not path.is_file():
        raise CrawlError('playwright_driver_failed')
    return str(path)


class PipeTransport:
    MAX_MESSAGE = 8_000_000

    def __init__(self, process):
        self.process = process
        self.messages = queue.Queue(maxsize=16)
        self.outgoing = queue.Queue(maxsize=32)
        self.stopping = threading.Event()
        self.failed = threading.Event()
        self.reader = threading.Thread(target=self._read, daemon=True, name='native-cdp-read')
        self.writer = threading.Thread(target=self._write, daemon=True, name='native-cdp-write')
        self.reader.start()
        self.writer.start()

    def _read(self):
        buffered = bytearray()
        try:
            while not self.stopping.is_set():
                chunk = self.process.stdout.read(16384)
                if not chunk:
                    break
                buffered.extend(chunk)
                while b'\n' in buffered:
                    end = buffered.index(b'\n')
                    if end > self.MAX_MESSAGE:
                        raise ValueError()
                    packet = bytes(buffered[:end]).decode('utf-8')
                    del buffered[:end + 1]
                    while not self.stopping.is_set():
                        try:
                            self.messages.put(packet, timeout=.05)
                            break
                        except queue.Full:
                            continue
                if len(buffered) > self.MAX_MESSAGE:
                    raise ValueError()
        except Exception:
            pass  # No exception text: buffers can contain forms or cookies.
        finally:
            self.failed.set()

    def _write(self):
        try:
            while not self.stopping.is_set():
                try:
                    packet = self.outgoing.get(timeout=.05)
                except queue.Empty:
                    continue
                remaining = memoryview(packet)
                while remaining and not self.stopping.is_set():
                    size = self.process.stdin.write(remaining)
                    if not size:
                        raise OSError()
                    remaining = remaining[size:]
                self.process.stdin.flush()
        except Exception:
            self.failed.set()

    def send(self, value):
        if self.failed.is_set() or self.stopping.is_set():
            raise CrawlError('browser_closed')
        packet = value.encode('utf-8')
        if len(packet) > self.MAX_MESSAGE or b'\n' in packet or b'\0' in packet:
            raise CrawlError('native_protocol_error')
        try:
            self.outgoing.put_nowait(packet + b'\n')
        except queue.Full:
            raise CrawlError('native_observation_limit') from None

    def recv(self, *, timeout):
        try:
            return self.messages.get(timeout=max(0, timeout))
        except queue.Empty:
            if self.failed.is_set() or self.stopping.is_set():
                raise CrawlError('browser_closed') from None
            raise TimeoutError() from None

    def close(self):
        self.stopping.set()
        # Browser.close normally finishes before this point. Closing stdin
        # also asks the bridge to terminate its own browser if startup failed.
        try:
            self.process.stdin.close()
        except OSError:
            pass
        self.writer.join(timeout=1)
        self.reader.join(timeout=1)
