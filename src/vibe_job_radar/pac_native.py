"""Bounded Windows WinHTTP PAC evaluation; returned proxy metadata is never used.

WinHTTP normalizes/drops unsupported PAC directives. Encode the original return
into an inert result hostname before that normalization, then decode it locally.
Native PAC helpers may resolve DNS. Only explicitly trusted scripts are accepted.
"""
from __future__ import annotations

import json
import multiprocessing
import os
import re
import secrets
import threading
import time

from .loopback_proxy import LocalProxyError

MAX_SCRIPT = 65536
MAX_RESULT = 96
DEADLINE = 8.0
_SLOTS = threading.BoundedSemaphore(2)
ERRORS = {'pac_unavailable', 'pac_invalid_script', 'pac_invalid_result',
          'pac_timeout', 'pac_busy', 'pac_revoked', 'pac_failed', 'pac_cache_limit'}


def available() -> bool:
    if os.name != 'nt':
        return False
    try:
        import ctypes
        api = ctypes.WinDLL('winhttp', use_last_error=True)
        return all(hasattr(api, name) for name in ('WinHttpGetProxyForUrlEx',
            'WinHttpCreateProxyResolver', 'WinHttpGetProxyResult', 'WinHttpFreeProxyResult'))
    except (OSError, AttributeError):
        return False


def wrap_script(source: str, nonce: str) -> str:
    # The nonce binds the one native result to this invocation. This is a data
    # envelope, not a security boundary against the explicitly trusted script.
    return ('var __radar_callback=(function(){var FindProxyForURL;\n' + source +
        '\nvar select=FindProxyForURL;if(typeof select!=="function")throw new Error("entry");'
        'return function(url,host){var raw=select(url,host);'
        'if(typeof raw!=="string"||raw.length<1||raw.length>96)throw new Error("length");'
        'var encoded="";for(var i=0;i<raw.length;i++){var code=raw.charCodeAt(i);'
        'if(code<32||code>126)throw new Error("char");var hex=code.toString(16);'
        'encoded+=(hex.length===1?"0":"")+hex;}'
        'var labels=[];for(var j=0;j<encoded.length;j+=48)labels.push(encoded.slice(j,j+48));'
        'return "PROXY vjr' + nonce + '."+labels.join(".")+".invalid:9";};})();'
        'function FindProxyForURL(url,host){return __radar_callback(url,host);}')


def decode_result(host, nonce):
    if not isinstance(host, str) or len(host) > 253:
        raise LocalProxyError('pac_invalid_result')
    match = re.fullmatch('vjr' + re.escape(nonce) + r'\.([0-9a-f.]+)\.invalid', host)
    if not match:
        raise LocalProxyError('pac_invalid_result')
    labels = match[1].split('.')
    if (not 1 <= len(labels) <= 4 or any(len(p) != 48 for p in labels[:-1])
            or not 2 <= len(labels[-1]) <= 48 or len(labels[-1]) % 2):
        raise LocalProxyError('pac_invalid_result')
    try:
        raw = bytes.fromhex(''.join(labels)).decode('ascii')
    except (ValueError, UnicodeError):
        raise LocalProxyError('pac_invalid_result') from None
    if not 1 <= len(raw) <= MAX_RESULT or any(not 32 <= ord(c) <= 126 for c in raw):
        raise LocalProxyError('pac_invalid_result')
    return raw


def _native(source, url):
    """Runs only in an owned short-lived worker, never on a request thread."""
    import ctypes
    from ctypes import wintypes as w
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class Auto(ctypes.Structure):
        _fields_ = [('flags',w.DWORD),('detect',w.DWORD),('url',w.LPCWSTR),
                    ('reserved',ctypes.c_void_p),('reserved2',w.DWORD),('auth',w.BOOL)]
    class Entry(ctypes.Structure):
        _fields_ = [('proxy',w.BOOL),('bypass',w.BOOL),('scheme',ctypes.c_int),
                    ('host',ctypes.c_void_p),('port',w.WORD)]
    class Result(ctypes.Structure):
        _fields_ = [('count',w.DWORD),('entries',ctypes.POINTER(Entry))]

    api = ctypes.WinDLL('winhttp', use_last_error=True)
    callback_type = ctypes.WINFUNCTYPE(None,w.HANDLE,ctypes.c_size_t,w.DWORD,ctypes.c_void_p,w.DWORD)
    signatures = {
        'WinHttpOpen': ([w.LPCWSTR,w.DWORD,w.LPCWSTR,w.LPCWSTR,w.DWORD],w.HANDLE),
        'WinHttpSetStatusCallback': ([w.HANDLE,callback_type,w.DWORD,ctypes.c_size_t],ctypes.c_void_p),
        'WinHttpCreateProxyResolver': ([w.HANDLE,ctypes.POINTER(w.HANDLE)],w.DWORD),
        'WinHttpGetProxyForUrlEx': ([w.HANDLE,w.LPCWSTR,ctypes.POINTER(Auto),ctypes.c_size_t],w.DWORD),
        'WinHttpGetProxyResult': ([w.HANDLE,ctypes.POINTER(Result)],w.DWORD),
        'WinHttpFreeProxyResult': ([ctypes.POINTER(Result)],None),
        'WinHttpCloseHandle': ([w.HANDLE],w.BOOL),
    }
    for name, (args, result_type) in signatures.items():
        function = getattr(api, name); function.argtypes = args; function.restype = result_type
    nonce = secrets.token_hex(8)
    body = wrap_script(source, nonce).encode('utf-8')
    path = '/' + secrets.token_hex(16) + '.pac'

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_GET(self):
            if self.path != path or self.headers.get('Host') != authority:
                self.send_error(404); return
            self.send_response(200)
            self.send_header('Content-Type', 'application/x-ns-proxy-autoconfig; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.end_headers(); self.wfile.write(body)

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.daemon_threads = True
    authority = f'127.0.0.1:{server.server_port}'
    thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval':.05}, daemon=True)
    thread.start()
    done = threading.Event(); closed = threading.Event(); failed = threading.Event()
    session_closed = threading.Event()
    resolver = w.HANDLE(); result = Result()
    @callback_type
    def callback(handle, context, status, info, length):
        if status == 0x01000000: done.set()  # GETPROXYFORURL_COMPLETE
        elif status == 0x00200000: failed.set(); done.set()  # REQUEST_ERROR
        elif status == 0x00000800:
            if handle == resolver.value: closed.set()
            elif handle == session: session_closed.set()
    session = None
    try:
        session = api.WinHttpOpen('VibeJobRadar-Local-PAC', 1, None, None, 0x10000000)
        if not session: raise LocalProxyError('pac_unavailable')
        if api.WinHttpSetStatusCallback(session, callback, 0x01200800, 0) == ctypes.c_void_p(-1).value:
            raise LocalProxyError('pac_unavailable')
        if api.WinHttpCreateProxyResolver(session, ctypes.byref(resolver)):
            raise LocalProxyError('pac_unavailable')
        # CONFIG_URL only: no WPAD, no system PAC, no automatic credentials.
        options = Auto(2, 0, 'http://' + authority + path, None, 0, False)
        if api.WinHttpGetProxyForUrlEx(resolver, url, ctypes.byref(options), 0) != 997:
            raise LocalProxyError('pac_invalid_script')
        if not done.wait(5): raise LocalProxyError('pac_timeout')
        if failed.is_set(): raise LocalProxyError('pac_invalid_script')
        if api.WinHttpGetProxyResult(resolver, ctypes.byref(result)):
            raise LocalProxyError('pac_failed')
        if result.count != 1 or not result.entries:
            raise LocalProxyError('pac_invalid_result')
        item = result.entries[0]
        if not item.proxy or item.bypass or item.scheme != 1 or item.port != 9 or not item.host:
            raise LocalProxyError('pac_invalid_result')
        # Native library owns this null-terminated hostname. No DNS/connect call
        # receives it; only the original return string leaves the worker.
        return decode_result(ctypes.wstring_at(item.host), nonce)
    finally:
        api.WinHttpFreeProxyResult(ctypes.byref(result))
        if resolver.value: api.WinHttpCloseHandle(resolver); closed.wait(1)
        if session: api.WinHttpCloseHandle(session); session_closed.wait(1)
        server.shutdown(); server.server_close(); thread.join(1)


def _worker(sender, source, url):
    try:
        value = {'raw': _native(source, url)}
    except LocalProxyError as exc:
        value = {'error': exc.code if exc.code in ERRORS else 'pac_failed'}
    except Exception:
        value = {'error': 'pac_failed'}
    try:
        sender.send_bytes(json.dumps(value, ensure_ascii=True).encode('ascii'))
    finally:
        sender.close()


def evaluate(source: str, url: str, permitted) -> str:
    if not available(): raise LocalProxyError('pac_unavailable')
    if not _SLOTS.acquire(blocking=False): raise LocalProxyError('pac_busy')
    receiver = sender = process = None
    try:
        if not permitted(): raise LocalProxyError('pac_revoked')
        context = multiprocessing.get_context('spawn')
        receiver, sender = context.Pipe(duplex=False)
        process = context.Process(target=_worker, args=(sender, source, url), daemon=True)
        process.start(); sender.close(); sender = None
        end = time.monotonic() + DEADLINE
        while time.monotonic() < end:
            if not permitted(): raise LocalProxyError('pac_revoked')
            if receiver.poll(min(.1, max(0, end-time.monotonic()))):
                value = json.loads(receiver.recv_bytes(4096).decode('ascii'))
                if not isinstance(value, dict): raise ValueError()
                if set(value) == {'error'} and value['error'] in ERRORS:
                    raise LocalProxyError(value['error'])
                if set(value) != {'raw'} or not isinstance(value['raw'], str): raise ValueError()
                if not permitted(): raise LocalProxyError('pac_revoked')
                return value['raw']
            if not process.is_alive(): raise LocalProxyError('pac_failed')
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
                    if process.is_alive(): process.terminate(); process.join(1)
                    if process.is_alive(): process.kill(); process.join(1)
                process.close()
        except (OSError, ValueError):
            raise LocalProxyError('pac_failed') from None
        finally:
            if receiver is not None: receiver.close()
            if sender is not None: sender.close()
            _SLOTS.release()
