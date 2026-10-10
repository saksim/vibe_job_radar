"""Opt-in Windows DHCP/DNS PAC discovery; no script download or evaluation.

The URL is private IPC data. Only callers holding explicit discovery consent may
invoke discover(); state/description and Windows configuration reads never do so.
"""
from __future__ import annotations

import json
import multiprocessing
import os
import threading
import time

from .loopback_proxy import LocalProxyError
from .system_pac import Source

DEADLINE = 8.0
MAX_URL = 4096
_SLOTS = threading.BoundedSemaphore(2)
MESSAGES = {
    'wpad_unavailable': '当前系统不支持Windows WPAD发现；原设置保留。',
    'wpad_not_found': 'Windows未发现PAC来源；原设置保留，没有改用其他路线。',
    'wpad_source_invalid': '发现的PAC来源不符合HTTP(S)地址要求；未下载或执行。',
    'wpad_timeout': 'WPAD发现超过时限，已停止自有工作进程；没有反复探测。',
    'wpad_failed': 'WPAD发现未正常完成；原设置保留，没有改用其他路线。',
    'wpad_source_changed': '新会话发现的PAC来源与已确认来源不同；请重新发现、核对并确认。',
    'wpad_candidate_expired': '本页的来源预览已过期或设置已改变，请重新发现并核对。',
}


def available():
    return os.name == 'nt'


def _source(url):
    try:
        return Source(url)
    except LocalProxyError:
        raise LocalProxyError('wpad_source_invalid') from None


def _native_url():
    """Call the official discovery-only API and release its native allocation."""
    if not available():
        raise LocalProxyError('wpad_unavailable')
    import ctypes
    from ctypes import wintypes as w
    try:
        api = ctypes.WinDLL('winhttp', use_last_error=True)
        detect = api.WinHttpDetectAutoProxyConfigUrl
        free = ctypes.WinDLL('kernel32', use_last_error=True).GlobalFree
    except (AttributeError, OSError):
        raise LocalProxyError('wpad_unavailable') from None
    detect.argtypes = [w.DWORD, ctypes.POINTER(ctypes.c_void_p)]
    detect.restype = w.BOOL
    free.argtypes, free.restype = [ctypes.c_void_p], ctypes.c_void_p
    value = ctypes.c_void_p()
    try:
        # DHCP first, then DNS_A if DHCP does not provide a source. No registry
        # changes, manual gateway guesses, port scans, or custom DNS fallbacks.
        if not detect(3, ctypes.byref(value)):
            code = 'wpad_not_found' if ctypes.get_last_error() == 12180 else 'wpad_failed'
            raise LocalProxyError(code)
        if not value.value:
            raise LocalProxyError('wpad_source_invalid')
        chars = ctypes.cast(value, ctypes.POINTER(ctypes.c_wchar))
        result = []
        for index in range(MAX_URL + 1):
            if chars[index] == '\x00':
                return ''.join(result)
            result.append(chars[index])
        raise LocalProxyError('wpad_source_invalid')
    finally:
        if value.value:
            free(value)


def _worker(sender):
    try:
        source = _source(_native_url())
        value = {'url': source.url}
    except LocalProxyError as exc:
        value = {'error': exc.code if exc.code in MESSAGES else 'wpad_failed'}
    except Exception:
        value = {'error': 'wpad_failed'}
    try:
        sender.send_bytes(json.dumps(value, ensure_ascii=True).encode('ascii'))
    finally:
        sender.close()


def _permitted(callback):
    try:
        return callback() is True
    except Exception:
        return False


def discover(permitted):
    """One consented bounded attempt; synchronous OS startup/cleanup may add time."""
    if not available():
        raise LocalProxyError('wpad_unavailable')
    if not _SLOTS.acquire(blocking=False):
        raise LocalProxyError('pac_busy')
    receiver = sender = process = None
    end = time.monotonic() + DEADLINE
    try:
        if not _permitted(permitted):
            raise LocalProxyError('pac_revoked')
        context = multiprocessing.get_context('spawn')
        receiver, sender = context.Pipe(duplex=False)
        process = context.Process(target=_worker, args=(sender,), daemon=True)
        if time.monotonic() >= end:
            raise LocalProxyError('wpad_timeout')
        process.start()
        sender.close()
        sender = None
        while time.monotonic() < end:
            if not _permitted(permitted):
                raise LocalProxyError('pac_revoked')
            if receiver.poll(min(.05, max(0, end - time.monotonic()))):
                value = json.loads(receiver.recv_bytes(MAX_URL * 6 + 64).decode('ascii'))
                if not isinstance(value, dict):
                    raise ValueError()
                if set(value) == {'error'} and value['error'] in MESSAGES:
                    raise LocalProxyError(value['error'])
                if set(value) != {'url'}:
                    raise ValueError()
                source = _source(value['url'])
                if not _permitted(permitted):
                    raise LocalProxyError('pac_revoked')
                if time.monotonic() >= end:
                    raise LocalProxyError('wpad_timeout')
                return source
            if not process.is_alive():
                raise LocalProxyError('wpad_failed')
        raise LocalProxyError('wpad_timeout')
    except LocalProxyError:
        raise
    except Exception:
        raise LocalProxyError('wpad_failed') from None
    finally:
        cleanup_failed = False
        try:
            if process is not None:
                if process.pid is not None:
                    process.join(.1)
                    if process.is_alive():
                        process.terminate()
                        process.join(1)
                    if process.is_alive():
                        process.kill()
                        process.join(1)
                process.close()
        except (OSError, ValueError):
            cleanup_failed = True
        finally:
            for pipe in (receiver, sender):
                if pipe is not None:
                    try:
                        pipe.close()
                    except (OSError, ValueError):
                        cleanup_failed = True
            _SLOTS.release()
        if cleanup_failed:
            raise LocalProxyError('wpad_failed') from None
