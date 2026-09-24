"""Explicit trust in the current user's configured PAC URL, never WPAD discovery.

Configuration reads are local. A session lazily downloads once in an owned,
bounded process; neither URLs nor downloaded text enter persisted state/logs.
This bootstrap transport is separate from public job-target validation.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import http.client
import ipaddress
import json
import multiprocessing
import os
import re
import socket
import ssl
import threading
import time
from urllib.parse import urlsplit

from .loopback_proxy import LocalProxyError
from .pac import PacSnapshot, canonical_target, validate_script
from .pac_native import MAX_SCRIPT
from .tls_context import create_client_context

CONSENT = 'workspace-windows-configured-pac-url-v1'
DEADLINE = 8.0
_SLOTS = threading.BoundedSemaphore(2)
MESSAGES = {
    'system_pac_unavailable': '当前系统无法读取Windows当前用户的PAC配置；未改为直连。',
    'system_pac_missing': 'Windows当前连接没有明确配置PAC地址；此功能不执行WPAD自动发现。',
    'system_pac_source_invalid': '系统PAC地址不受支持：仅接受无账号、无片段的HTTP(S)地址；不会使用其他地址。',
    'system_pac_changed': 'Windows的PAC来源已改变，请刷新、核对来源并重新确认；已停止联网。',
    'system_pac_address_rejected': 'PAC来源解析含不受支持的地址；仅允许公网、本机、RFC1918或IPv6唯一本地地址。',
    'system_pac_http_rejected': 'PAC来源拒绝请求、重定向或要求认证；未跟随、提交凭据或更换来源。',
    'system_pac_content_invalid': 'PAC响应必须为不超过64KiB的完整UTF-8脚本；不接受压缩或异常响应。',
    'system_pac_tls_failed': 'PAC来源证书或TLS校验失败；未关闭验证或改用其他路线。',
    'system_pac_timeout': '读取PAC来源超过总时限，已终止工作进程；本会话不会再次下载。',
    'system_pac_failed': '读取PAC来源未完成；本会话不会反复下载，也不会切换直连。',
}
DISCLOSURE = ('仅使用Windows当前用户已配置的PAC来源，不修改系统设置、不发现WPAD。'
    '确认表示信任该地址现在及后续提供的脚本：每个新会话读取一次，会话内冻结，失败不重试。'
    '地址改变须重新确认。仅显示来源站点，路径和参数不写入记录；读取为直接HTTP(S)，不提交账号、Cookie或自动认证，不跟随跳转。'
    '脚本由Windows执行，DNS函数可能解析域名；只接收HTTPS域名根地址，不含职位路径或关键词。'
    '仅接受DIRECT、匿名本机HTTP及明确SOCKS5，完整检查后只采用第一项，失败不切换路线。'
    '手工导入模式可固定脚本版本。保存与查看状态不会下载或执行脚本。')


@dataclass(frozen=True)
class Source:
    url: str = field(repr=False)
    config_id: str = field(init=False)
    origin: str = field(init=False)

    def __post_init__(self):
        try:
            raw = self.url
            if (not isinstance(raw, str) or not 1 <= len(raw) <= 4096
                    or any(not 33 <= ord(c) <= 126 for c in raw) or '\\' in raw or '#' in raw):
                raise ValueError()
            parts = urlsplit(raw)
            if (parts.scheme not in {'http', 'https'} or not parts.hostname or parts.username is not None
                    or parts.password is not None or '%' in parts.netloc):
                raise ValueError()
            host, normalized = canonical_target(parts.hostname)
            port = parts.port if parts.port is not None else (443 if parts.scheme == 'https' else 80)
            if not 1 <= port <= 65535 or parts.netloc.endswith(':'): raise ValueError()
            origin = parts.scheme + '://' + normalized[8:-1]
            if parts.port is not None: origin += ':' + str(port)
            object.__setattr__(self, 'origin', origin)
            object.__setattr__(self, 'config_id', hashlib.sha256(raw.encode('ascii')).hexdigest())
        except (ValueError, UnicodeError, LocalProxyError):
            raise LocalProxyError('system_pac_source_invalid') from None


def _configured_url():
    """Official read-only current-user API. Free every allocated native string."""
    if os.name != 'nt': raise LocalProxyError('system_pac_unavailable')
    import ctypes
    from ctypes import wintypes as w
    class Config(ctypes.Structure):
        _fields_ = [('detect', w.BOOL), ('url', ctypes.c_void_p),
                    ('proxy', ctypes.c_void_p), ('bypass', ctypes.c_void_p)]
    api = ctypes.WinDLL('winhttp', use_last_error=True)
    read = api.WinHttpGetIEProxyConfigForCurrentUser
    read.argtypes, read.restype = [ctypes.POINTER(Config)], w.BOOL
    free = ctypes.WinDLL('kernel32', use_last_error=True).GlobalFree
    free.argtypes, free.restype = [ctypes.c_void_p], ctypes.c_void_p
    value = Config()
    try:
        if not read(ctypes.byref(value)): raise LocalProxyError('system_pac_unavailable')
        if not value.url: raise LocalProxyError('system_pac_missing')
        chars = ctypes.cast(value.url, ctypes.POINTER(ctypes.c_wchar))
        result = []
        for i in range(4097):
            if chars[i] == '\x00': return ''.join(result)
            result.append(chars[i])
        raise LocalProxyError('system_pac_source_invalid')
    finally:
        for pointer in (value.url, value.proxy, value.bypass):
            if pointer: free(pointer)


def current_source():
    try: return Source(_configured_url())
    except LocalProxyError: raise
    except Exception: raise LocalProxyError('system_pac_unavailable') from None


def preview():
    try:
        source = current_source()
        return {'configured': True, 'config_id': source.config_id, 'origin': source.origin,
                'code': 'system_pac_configured', 'message': '只读取了当前用户配置；尚未下载或执行。'}
    except LocalProxyError as exc:
        return {'configured': False, 'config_id': '', 'origin': '', 'code': exc.code,
                'message': MESSAGES.get(exc.code, MESSAGES['system_pac_unavailable'])}


def _source_address(value):
    address = ipaddress.ip_address(value)
    private = (any(address in ipaddress.ip_network(n) for n in ('10.0.0.0/8','172.16.0.0/12','192.168.0.0/16'))
               if address.version == 4 else address in ipaddress.ip_network('fc00::/7'))
    return (not address.is_multicast and not address.is_unspecified and not address.is_link_local
            and not getattr(address, 'ipv4_mapped', None)
            and (address.is_loopback or private or address.is_global))


def _download(url):
    # Exactly one chosen numeric peer, no proxy recursion, redirects, cookie jar,
    # authentication or secondary address attempt. The parent bounds DNS/TLS/read
    # together, including slow headers/body and Windows certificate retrieval.
    source = Source(url)
    parts = urlsplit(source.url)
    host = canonical_target(parts.hostname)[0]
    port = parts.port or (443 if parts.scheme == 'https' else 80)
    answers = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    if (not answers or len(answers) > 64
            or any(a[0] not in (socket.AF_INET, socket.AF_INET6) or not _source_address(a[4][0]) for a in answers)):
        raise LocalProxyError('system_pac_address_rejected')
    family, kind, proto, _, address = answers[0]
    context = create_client_context() if parts.scheme == 'https' else None
    conn = http.client.HTTPConnection(host, port, timeout=DEADLINE)
    sock = socket.socket(family, kind, proto)
    try:
        sock.settimeout(DEADLINE)
        sock.connect(address)
        if context: sock = context.wrap_socket(sock, server_hostname=host)
        conn.sock = sock
        path = (parts.path or '/') + ('?' + parts.query if parts.query else '')
        conn.request('GET', path, headers={'User-Agent':'VibeJobRadar-PAC', 'Accept-Encoding':'identity', 'Connection':'close'})
        response = conn.getresponse()
        if response.status != 200: raise LocalProxyError('system_pac_http_rejected')
        lengths = response.headers.get_all('Content-Length', [])
        encoding = response.headers.get_all('Content-Encoding', [])
        transfer = response.headers.get_all('Transfer-Encoding', [])
        if (len(lengths) > 1 or (lengths and (not re.fullmatch('[0-9]{1,6}', lengths[0]) or not 1 <= int(lengths[0]) <= MAX_SCRIPT))
                or (encoding and encoding != ['identity']) or (transfer and transfer != ['chunked']) or (transfer and lengths)):
            raise LocalProxyError('system_pac_content_invalid')
        body = response.read(MAX_SCRIPT + 1)
        if len(body) > MAX_SCRIPT or (lengths and len(body) != int(lengths[0])):
            raise LocalProxyError('system_pac_content_invalid')
        # UTF-8 BOM is accepted; no guessed legacy encodings.
        script = body.decode('utf-8-sig')
        validate_script(script)
        return script
    finally:
        conn.close()
        sock.close()


def _worker(sender, url):
    try: value = {'script': _download(url)}
    except LocalProxyError as exc: value = {'error': exc.code if exc.code in MESSAGES else 'system_pac_failed'}
    except ssl.SSLError: value = {'error': 'system_pac_tls_failed'}
    except (UnicodeError, ValueError, http.client.HTTPException): value = {'error': 'system_pac_content_invalid'}
    except TimeoutError: value = {'error': 'system_pac_timeout'}
    except Exception: value = {'error': 'system_pac_failed'}
    try: sender.send_bytes(json.dumps(value, ensure_ascii=False).encode('utf-8'))
    finally: sender.close()


def fetch(source, permitted):
    if not _SLOTS.acquire(blocking=False): raise LocalProxyError('pac_busy')
    receiver = sender = process = None
    end = time.monotonic() + DEADLINE
    try:
        if not permitted(): raise LocalProxyError('pac_revoked')
        context = multiprocessing.get_context('spawn')
        receiver, sender = context.Pipe(duplex=False)
        process = context.Process(target=_worker, args=(sender, source.url), daemon=True)
        process.start(); sender.close(); sender = None
        while time.monotonic() < end:
            if not permitted(): raise LocalProxyError('pac_revoked')
            if receiver.poll(min(.05, max(0, end-time.monotonic()))):
                value = json.loads(receiver.recv_bytes(MAX_SCRIPT * 6 + 64).decode('utf-8'))
                if not permitted(): raise LocalProxyError('pac_revoked')
                if time.monotonic() >= end: raise LocalProxyError('system_pac_timeout')
                if isinstance(value, dict) and set(value) == {'error'} and value['error'] in MESSAGES:
                    raise LocalProxyError(value['error'])
                if not isinstance(value, dict) or set(value) != {'script'}: raise ValueError()
                validate_script(value['script'])
                return value['script']
            if not process.is_alive(): raise LocalProxyError('system_pac_failed')
        raise LocalProxyError('system_pac_timeout')
    except LocalProxyError: raise
    except Exception: raise LocalProxyError('system_pac_failed') from None
    finally:
        try:
            if process is not None:
                if process.pid is not None:
                    process.join(.1)
                    if process.is_alive(): process.terminate(); process.join(1)
                    if process.is_alive(): process.kill(); process.join(1)
                process.close()
        except (OSError, ValueError):
            raise LocalProxyError('system_pac_failed') from None
        finally:
            if receiver is not None: receiver.close()
            if sender is not None: sender.close()
            _SLOTS.release()


@dataclass(frozen=True)
class SystemPacSnapshot:
    source: Source = field(repr=False)
    permission: object = field(repr=False, compare=False)
    _loaded: list = field(default_factory=list, init=False, repr=False, compare=False)
    _lock: object = field(default_factory=threading.Lock, init=False, repr=False, compare=False)
    _stop: object = field(default_factory=threading.Event, init=False, repr=False, compare=False)
    _cancellations: list = field(default_factory=list, init=False, repr=False, compare=False)

    @property
    def binding(self): return self.source.config_id

    def bind_cancellation(self, event): self._cancellations.append(event)

    def close(self): self._stop.set()

    def permitted(self):
        try:
            return (not self._stop.is_set() and not any(e.is_set() for e in self._cancellations)
                    and self.permission() is True and current_source().config_id == self.binding)
        except Exception: return False

    def ensure_active(self):
        if not self.permitted(): raise LocalProxyError('pac_revoked')

    def for_host(self, host):
        self.ensure_active()
        canonical_target(host)  # Invalid callers cannot trigger a download.
        end = time.monotonic() + DEADLINE
        while not self._lock.acquire(timeout=.05):
            self.ensure_active()
            if time.monotonic() >= end: raise LocalProxyError('pac_busy')
        try:
            self.ensure_active()
            if not self._loaded:
                try: value = PacSnapshot(fetch(self.source, self.permitted), self.permitted)
                except LocalProxyError as exc:
                    value = exc.code if exc.code in MESSAGES or exc.code in {'pac_revoked','pac_busy'} else 'system_pac_failed'
                self._loaded.append(value)
            self.ensure_active()
            value = self._loaded[0]
            if isinstance(value, str): raise LocalProxyError(value)
        finally: self._lock.release()
        return value.for_host(host)
