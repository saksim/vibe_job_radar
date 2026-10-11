"""Optional PAC worker without host I/O bindings; only trusted scripts."""
from __future__ import annotations
import importlib.metadata
import importlib.util
import json
import multiprocessing
import threading
import time
from .loopback_proxy import LocalProxyError
from .pac_native import DEADLINE, MAX_RESULT
from .pac_calendar import CALENDAR_HELPERS

ENGINE_VERSION = '0.17.0.1'
MEMORY_LIMIT = 16 * 1024 * 1024
STACK_LIMIT = 512 * 1024
CPU_LIMIT = 4.0
_SLOTS = threading.BoundedSemaphore(2)
ERRORS = frozenset({'pac_url_unavailable', 'pac_invalid_script', 'pac_invalid_result',
                   'pac_timeout', 'pac_busy', 'pac_revoked', 'pac_failed'})

# String helpers only. No callbacks into Python or host I/O are supplied.
HELPERS = r"""
function isPlainHostName(host) { return host.indexOf('.') === -1; }
function dnsDomainIs(host, domain) {
    return host.slice(host.length - domain.length) === domain;
}
function localHostOrDomainIs(host, hostdom) {
    return host === hostdom || (isPlainHostName(host) &&
        hostdom.slice(0, host.length + 1) === host + '.');
}
function dnsDomainLevels(host) { return host.split('.').length - 1; }
function shExpMatch(text, pattern) {
    var expression = '^';
    for (var i = 0; i < pattern.length; i++) {
        var c = pattern.charAt(i);
        if (c === '*') expression += '[\\s\\S]*';
        else if (c === '?') expression += '[\\s\\S]';
        else expression += '\\^$.*+?()[]{}|'.indexOf(c) >= 0 ? '\\' + c : c;
    }
    return new RegExp(expression + '$').test(text);
}
"""

def available():
    # Status reads must not import the extension or execute a script.
    try:
        return (importlib.metadata.version('quickjs-ng') == ENGINE_VERSION
                and importlib.util.find_spec('quickjs') is not None
                and importlib.util.find_spec('_quickjs') is not None)
    except (ImportError, OSError, ValueError):
        return False

def _evaluate(source, url, host):
    if not available():
        raise LocalProxyError('pac_url_unavailable')
    import quickjs
    context = quickjs.Context()
    context.set_memory_limit(MEMORY_LIMIT)
    context.set_max_stack_size(STACK_LIMIT)
    context.set_time_limit(CPU_LIMIT)
    try:
        context.eval(HELPERS + '\n' + CALENDAR_HELPERS)
        context.eval(source)
        callback = context.get('FindProxyForURL')
        raw = callback(url, host)
    except (quickjs.JSException, TypeError):
        # Engine errors can contain private script or URL values.
        raise LocalProxyError('pac_invalid_script') from None
    if (not isinstance(raw, str) or not 1 <= len(raw) <= MAX_RESULT
            or any(not 32 <= ord(c) <= 126 for c in raw)):
        raise LocalProxyError('pac_invalid_result')
    return raw

def _worker(sender, source, url, host):
    try:
        value = {'raw': _evaluate(source, url, host)}
    except LocalProxyError as exc:
        value = {'error': exc.code if exc.code in ERRORS else 'pac_failed'}
    except Exception:
        value = {'error': 'pac_failed'}
    try:
        sender.send_bytes(json.dumps(value, ensure_ascii=True).encode('ascii'))
    finally:
        sender.close()

def evaluate(source, url, host, permitted):
    if not available():
        raise LocalProxyError('pac_url_unavailable')
    if not _SLOTS.acquire(blocking=False):
        raise LocalProxyError('pac_busy')
    receiver = sender = process = None
    try:
        end = time.monotonic() + DEADLINE
        if not permitted():
            raise LocalProxyError('pac_revoked')
        context = multiprocessing.get_context('spawn')
        receiver, sender = context.Pipe(duplex=False)
        process = context.Process(target=_worker, args=(sender, source, url, host), daemon=True)
        process.start()
        sender.close()
        sender = None
        while time.monotonic() < end:
            if not permitted():
                raise LocalProxyError('pac_revoked')
            if receiver.poll(min(.1, max(0, end-time.monotonic()))):
                value = json.loads(receiver.recv_bytes(4096).decode('ascii'))
                if not isinstance(value, dict):
                    raise ValueError()
                if set(value) == {'error'} and value['error'] in ERRORS:
                    raise LocalProxyError(value['error'])
                if set(value) != {'raw'} or not isinstance(value['raw'], str):
                    raise ValueError()
                if not permitted():
                    raise LocalProxyError('pac_revoked')
                if time.monotonic() >= end:
                    raise LocalProxyError('pac_timeout')
                return value['raw']
            if not process.is_alive():
                raise LocalProxyError('pac_failed')
        raise LocalProxyError('pac_timeout')
    except LocalProxyError:
        raise
    except Exception:
        raise LocalProxyError('pac_failed') from None
    finally:
        try:
            if process is not None:
                if process.pid is not None:
                    process.join(.2)
                    if process.is_alive():
                        process.terminate()
                        process.join(1)
                    if process.is_alive():
                        process.kill()
                        process.join(1)
                process.close()
        except (OSError, ValueError):
            raise LocalProxyError('pac_failed') from None
        finally:
            if receiver is not None:
                receiver.close()
            if sender is not None:
                sender.close()
            _SLOTS.release()
