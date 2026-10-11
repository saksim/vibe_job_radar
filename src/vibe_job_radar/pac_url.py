"""Explicit full-HTTPS-URL PAC and immutable per-request route bindings."""
from __future__ import annotations
import hashlib
import json
import threading
from dataclasses import dataclass, field
from urllib.parse import urlsplit
from .loopback_proxy import LocalProxyError
from .loopback_socks import LoopbackSocks5
from .pac import MESSAGES as DOMAIN_MESSAGES, canonical_target, parse_result, validate_script
from . import pac_url_worker

CONSENT = 'workspace-trusted-full-https-url-pac-v1'
MESSAGES = {
    **DOMAIN_MESSAGES,
    'pac_url_unavailable': '完整网址PAC组件未安装或版本不兼容；已停止，未改为直连。',
    'pac_url_invalid_target': '完整网址PAC只接受规范的HTTPS地址（最多8192字符），不能包含账号、密码或片段。',
    'pac_url_required': '此PAC需要完整HTTPS网址；当前入口只有域名，已停止选路。',
    'pac_url_native_unsupported': '完整网址PAC不支持原生浏览器通道。请显式改用请求桥或其他网络模式；本次未打开浏览器。',
    'pac_url_cache_limit': '此会话已使用128个不同网址的PAC路线，请停止并重新打开会话。',
}
DISCLOSURE = ('只导入你信任的本地PAC文件（UTF-8，最多64KiB）。独立进程执行脚本，'
    '脚本会收到完整HTTPS路径和查询参数，可能包括关键词或网址中的凭据；不传Cookie、请求头、正文或个人材料。'
    '仅提供五个字符串助手函数，不提供网络、文件、DNS或本机地址接口。'
    '只适用于HTTP采集和浏览器请求桥；原生浏览器通道不支持此模式。'
    '仅接受DIRECT、匿名本机PROXY和明确SOCKS5；只采用第一项，失败不换路线。')

def canonical_url(url):
    try:
        if (not isinstance(url, str) or not 1 <= len(url) <= 8192
                or any(ord(c) < 33 or ord(c) > 126 for c in url) or '\\' in url):
            raise ValueError()
        parts = urlsplit(url)
        if (parts.scheme != 'https' or not parts.hostname or parts.username is not None
                or parts.password is not None or '#' in url or parts.port not in (None, 443)):
            raise ValueError()
        host, root = canonical_target(parts.hostname)
        target = parts.path or '/'
        if not target.startswith('/'):
            raise ValueError()
        if parts.query:
            target += '?' + parts.query
        return host, root[:-1] + target
    except (ValueError, UnicodeError, LocalProxyError):
        raise LocalProxyError('pac_url_invalid_target') from None

@dataclass(frozen=True)
class UrlPacRoute:
    snapshot: object = field(repr=False, compare=False)
    host: str = field(repr=False)
    target_id: str = field(repr=False)
    selected: object = field(repr=False)
    kind = 'full_url_pac'
    requires_url = False

    @property
    def sha256(self):
        return self.snapshot.sha256

    @property
    def binding(self):
        # DNS answers depend on the selected route, not the URL path. Preserve
        # reuse for the same host/route and isolate a different protocol/endpoint.
        selected = self.selected
        route = ('SOCKS5' if isinstance(selected, LoopbackSocks5) else
                 'PROXY' if selected else 'DIRECT')
        value = (self.host, route, selected.host if selected else None,
                 selected.port if selected else None)
        return self.snapshot.binding + ':' + hashlib.sha256(
            json.dumps(value, separators=(',', ':')).encode('ascii')).hexdigest()

    def ensure_active(self):
        self.snapshot.ensure_active()

    def for_host(self, host):
        self.ensure_active()
        if canonical_target(host)[0] != self.host:
            raise LocalProxyError('pac_url_required')
        return self.selected

    def for_url(self, url):
        return self.snapshot.for_url(url)

    def bind_cancellation(self, event):
        self.snapshot.bind_cancellation(event)

    def close(self):
        self.snapshot.close()

@dataclass(frozen=True)
class UrlPacSnapshot:
    source: str = field(repr=False)
    permission: object = field(repr=False, compare=False)
    sha256: str = field(init=False)
    _cache: dict = field(default_factory=dict, init=False, repr=False, compare=False)
    _lock: object = field(default_factory=threading.Lock, init=False, repr=False, compare=False)
    _events: list = field(default_factory=list, init=False, repr=False, compare=False)
    _event_lock: object = field(default_factory=threading.Lock, init=False, repr=False, compare=False)
    _closed: object = field(default_factory=threading.Event, init=False, repr=False, compare=False)
    kind = 'full_url_pac'
    requires_url = True

    def __post_init__(self):
        object.__setattr__(self, 'sha256', hashlib.sha256(validate_script(self.source)).hexdigest())

    @property
    def binding(self):
        return 'full-url-v1:' + self.sha256

    def bind_cancellation(self, event):
        with self._event_lock:
            if event not in self._events:
                self._events.append(event)

    def close(self):
        self._closed.set()

    def permitted(self):
        try:
            with self._event_lock:
                cancelled = any(event.is_set() for event in self._events)
            return not self._closed.is_set() and not cancelled and self.permission() is True
        except Exception:
            return False

    def ensure_active(self):
        if not self.permitted():
            raise LocalProxyError('pac_revoked')

    def for_host(self, host):
        self.ensure_active()
        raise LocalProxyError('pac_url_required')

    def for_url(self, url):
        self.ensure_active()
        host, target = canonical_url(url)
        key = hashlib.sha256(target.encode('ascii')).hexdigest()
        if not self._lock.acquire(timeout=pac_url_worker.DEADLINE):
            raise LocalProxyError('pac_busy')
        try:
            self.ensure_active()
            if key not in self._cache:
                if len(self._cache) >= 128:
                    raise LocalProxyError('pac_url_cache_limit')
                try:
                    raw = pac_url_worker.evaluate(self.source, target, host, self.permitted)
                    self._cache[key] = parse_result(raw)
                except LocalProxyError as exc:
                    self._cache[key] = exc.code if exc.code in MESSAGES else 'pac_failed'
            self.ensure_active()
            selected = self._cache[key]
            if isinstance(selected, str):
                raise LocalProxyError(selected)
            # Retain only a one-way URL identifier after selecting the route.
            return UrlPacRoute(self, host, key, selected)
        finally:
            self._lock.release()
