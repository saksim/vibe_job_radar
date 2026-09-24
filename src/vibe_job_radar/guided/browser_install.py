"""Fixed-command installer with bounded, redacted output and timeout cleanup."""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Callable

from ..tls_context import TRUSTSTORE_REQUIREMENT
from ..runtime import require_source_install
from .browser_health import PLAYWRIGHT_REQUIREMENT, VERSION_CHECK_REQUIREMENT, safe_text


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    output: str
    timed_out: bool = False
    cancelled: bool = False


def install_commands(mode='ensure') -> tuple[tuple[str, list[str]], ...]:
    """Fixed modes, never an arbitrary command/path/version from the client.

    'ensure' keeps the historical first-install behavior. 'reinstall' actually
    replaces the matching browser, without changing the shared Python SDK.
    'upgrade' is a separate explicitly confirmed update of SDK and matched build.
    """
    require_source_install()
    if mode == 'tls':
        return (('tls_component', [sys.executable, '-m', 'pip', 'install', TRUSTSTORE_REQUIREMENT]),)
    if mode not in {'ensure', 'reinstall', 'upgrade'}:
        raise ValueError('invalid browser installation mode')
    browser = [sys.executable, '-m', 'playwright', 'install']
    if mode != 'ensure':
        browser.append('--force')
    browser.append('chromium')
    if mode == 'reinstall':
        return (('browser_download', browser),)
    package = [sys.executable, '-m', 'pip', 'install']
    if mode == 'upgrade':
        package.append('--upgrade')
    package.extend([PLAYWRIGHT_REQUIREMENT, VERSION_CHECK_REQUIREMENT])
    return (('package_install', package), ('browser_download', browser))


def _stop_process(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    if os.name == 'nt':
        # Kill only this fixed installer process tree, not user browser processes.
        subprocess.run(['taskkill', '/PID', str(proc.pid), '/T', '/F'], stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=10, check=False)
    else:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    if proc.poll() is None:
        proc.kill()
    proc.wait(timeout=10)


def run_command(args: list[str], *, cancel: threading.Event, timeout: float = 360,
                progress: Callable[[str], None] = lambda _: None) -> CommandResult:
    """Stream complete lines into a bounded tail. Never store raw logs on disk."""
    tail: deque[str] = deque(maxlen=80)
    lock = threading.Lock()
    kwargs = {'creationflags': subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == 'nt' else {'start_new_session': True}
    proc = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, shell=False, **kwargs)

    def collect() -> None:
        # readline(size) caps memory; oversized lines are dropped in their entirety
        # rather than splitting a credential and leaking a trailing fragment.
        dropping = False
        while True:
            line = proc.stdout.readline(16385)
            if not line:
                return
            if len(line) > 16384 or dropping:
                dropping = not line.endswith(b'\n')
                if not dropping:
                    with lock:
                        tail.append('[oversized installer line omitted]')
                continue
            clean = safe_text(line, 2000)
            with lock:
                tail.append(clean)

    reader = threading.Thread(target=collect, daemon=True, name='radar-install-output')
    reader.start()
    deadline = time.monotonic() + timeout
    timed_out = was_cancelled = False
    try:
        while proc.poll() is None:
            with lock:
                current = ''.join(tail)[-12000:]
            progress(current)
            was_cancelled = cancel.is_set()
            timed_out = time.monotonic() >= deadline
            if was_cancelled or timed_out:
                _stop_process(proc)
                break
            cancel.wait(0.1)
        reader.join(timeout=5)
        with lock:
            output = ''.join(tail)[-12000:]
        progress(output)
        return CommandResult(proc.returncode if proc.returncode is not None else -1,
                             output, timed_out, was_cancelled)
    finally:
        if proc.poll() is None:
            _stop_process(proc)
        reader.join(timeout=2)
        proc.stdout.close()
