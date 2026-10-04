"""Immutable consented PAC snapshots and strict anonymous loopback routes."""
from __future__ import annotations

import hashlib
import ipaddress
import re
import threading
from dataclasses import dataclass, field

from .loopback_proxy import LocalProxyError, LoopbackProxy
from .loopback_socks import LoopbackSocks5
from . import pac_native

CONSENT = 'workspace-trusted-domain-pac-v1'
MESSAGES = {
    'pac_unavailable': '当前系统没有可用的Windows PAC接口；已停止，未改为直连。',
    'pac_invalid_script': 'PAC脚本执行失败或返回值超出限制，请核对脚本；未改为其他路线。',
    'pac_invalid_result': 'PAC结果不受支持。仅接受DIRECT、匿名本机PROXY或明确SOCKS5；不猜协议、不忽略无效项。',
    'pac_timeout': 'PAC求值超时，已终止本次工作进程；没有发出目标请求。',
    'pac_busy': 'PAC求值正在忙，请稍后再试；没有改用其他路线。',
    'pac_revoked': 'PAC许可或文件已改变。请停止并重新打开采集会话，未进行新的选路连接。',
    'pac_failed': 'PAC工作进程未正常完成；已停止本次动作并保留数据。',
    'pac_file_invalid': 'PAC副本缺失或校验失败，已停止联网；可选择自动模式撤销许可，再核对原文件。',
    'pac_cache_limit': '此会话的PAC域名数量已达到上限，请停止并重新打开会话。',
}


def validate_script(source):
    if not isinstance(source, str): raise ValueError('invalid PAC text')
    try:
        raw = source.encode('utf-8')
    except UnicodeError:
        raise ValueError('invalid PAC encoding') from None
    if not source.strip() or len(raw) > pac_native.MAX_SCRIPT or '\x00' in source:
        raise ValueError('invalid PAC size or content')
    return raw


def parse_result(raw):
    if (not isinstance(raw, str) or not 1 <= len(raw) <= pac_native.MAX_RESULT
            or any(not 32 <= ord(c) <= 126 for c in raw)):
        raise LocalProxyError('pac_invalid_result')
    parts = raw.split(';')
    if not 1 <= len(parts) <= 4: raise LocalProxyError('pac_invalid_result')
    routes = []
    for part in parts:
        part = part.strip()
        if part == 'DIRECT':
            routes.append(None); continue
        match = re.fullmatch(r'(PROXY|SOCKS5) +((?:[a-zA-Z0-9.:-]+|\[[a-fA-F0-9:]+\]):[0-9]{1,5})', part)
        if not match: raise LocalProxyError('pac_invalid_result')
        parser, scheme = (LoopbackProxy, 'http') if match[1] == 'PROXY' else (LoopbackSocks5, 'socks5')
        try:
            routes.append(parser.from_url(scheme + '://' + match[2]))
        except LocalProxyError:
            raise LocalProxyError('pac_invalid_result') from None
    # Validate every directive, but never advance to a fallback after a failure.
    return routes[0]


def canonical_target(host):
    if not isinstance(host, str) or not host or len(host) > 253 or '%' in host:
        raise LocalProxyError('pac_invalid_result')
    try:
        address = ipaddress.ip_address(host)
        host = str(address)
        authority = '[' + host + ']' if address.version == 6 else host
    except ValueError:
        try:
            host = host.rstrip('.').encode('idna').decode('ascii').lower()
        except UnicodeError:
            raise LocalProxyError('pac_invalid_result') from None
        if (len(host) > 253 or not host or any(not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', p)
                                             for p in host.split('.'))):
            raise LocalProxyError('pac_invalid_result')
        authority = host
    return host, 'https://' + authority + '/'


@dataclass(frozen=True)
class PacSnapshot:
    source: str = field(repr=False)
    permission: object = field(repr=False, compare=False)
    sha256: str = field(init=False)
    _cache: dict = field(default_factory=dict, init=False, repr=False, compare=False)
    _lock: object = field(default_factory=threading.Lock, init=False, repr=False, compare=False)

    def __post_init__(self):
        object.__setattr__(self, 'sha256', hashlib.sha256(validate_script(self.source)).hexdigest())

    def permitted(self):
        try: return self.permission() is True
        except Exception: return False

    def ensure_active(self):
        if not self.permitted(): raise LocalProxyError('pac_revoked')

    def for_host(self, host):
        self.ensure_active()
        host, url = canonical_target(host)
        if not self._lock.acquire(timeout=pac_native.DEADLINE): raise LocalProxyError('pac_busy')
        try:
            self.ensure_active()
            if host not in self._cache:
                if len(self._cache) >= 128: raise LocalProxyError('pac_cache_limit')
                try:
                    raw = pac_native.evaluate(self.source, url, self.permitted)
                    self._cache[host] = parse_result(raw)
                except LocalProxyError as exc:
                    # Keep failures too: invalid scripts do not retry on every
                    # asset. A fresh explicit session creates a fresh snapshot.
                    self._cache[host] = exc.code if exc.code in MESSAGES else 'pac_failed'
            self.ensure_active()
            selected = self._cache[host]
            if isinstance(selected, str): raise LocalProxyError(selected)
            return selected
        finally:
            self._lock.release()
