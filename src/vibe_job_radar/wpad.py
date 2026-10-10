"""Explicit workspace WPAD preview and frozen, consented session snapshots."""
from __future__ import annotations

import secrets
import threading
import time
from dataclasses import dataclass, field

from . import system_pac, wpad_native
from .loopback_proxy import LocalProxyError
from .pac import PacSnapshot, canonical_target, MESSAGES as PAC_MESSAGES

CONSENT = 'workspace-windows-wpad-confirmed-source-v1'
PREVIEW_TTL = 300
DISCLOSURE = (
    '发现会通过Windows向当前网络查询DHCP/DNS中的PAC地址，不下载脚本。'
    '网络提供的地址不代表可信；请先核对来源站点，再单独同意信任此完整地址现在和后续提供的脚本。'
    '路径和参数隐藏，不写入偏好文件。新会话会重新发现，完整地址必须与已确认地址一致。'
    '首次使用才下载一次脚本并在本会话冻结；不持续监测网络变化，换网后请停止并重开会话。'
    'Windows会执行脚本，其原生DNS函数可能解析域名；仅向脚本提供HTTPS根地址和域名。'
    '仅接受DIRECT、匿名本机HTTP或SOCKS5，失败不换路线；不支持远程或认证代理。'
)
MESSAGES = {
    **wpad_native.MESSAGES,
    'wpad_not_discovered': '尚未发现来源；打开本页不会探测网络。',
    'wpad_discovering': '正在向当前网络发现PAC地址；尚未下载或执行脚本。',
    'wpad_source_ready': '来源预览有效5分钟；请核对站点后单独确认信任。',
    'wpad_cancelled': '已取消发现并清除来源预览，原设置保留。',
}


class Discovery:
    """One owned attempt and one expiring candidate per Workspace instance."""
    def __init__(self):
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._cancel = threading.Event()
        self._thread = None
        self._candidate = None
        self._observed = None
        self._code = 'wpad_not_discovered'

    def matches(self, config_id):
        with self._lock:
            return not self._stop.is_set() and self._observed in (None, config_id)

    def _valid(self, revision):
        if self._candidate is not None:
            created, saved_revision, token, source = self._candidate
            if revision != saved_revision or time.monotonic() - created >= PREVIEW_TTL:
                self._candidate = None
                self._code = 'wpad_candidate_expired'
        return self._candidate

    def state(self, revision):
        with self._lock:
            candidate = self._valid(revision)
            result = {'available': wpad_native.available(), 'discovery_running':
                      self._thread is not None and self._thread.is_alive(),
                      'code': self._code, 'message': MESSAGES.get(self._code,
                          PAC_MESSAGES.get(self._code, MESSAGES['wpad_failed']))}
            if candidate is not None:
                _, _, token, source = candidate
                result.update(candidate=token, config_id=source.config_id, origin=source.origin)
            return result

    def start(self, revision, permission):
        with self._lock:
            if self._stop.is_set():
                raise LocalProxyError('pac_revoked')
            if self._thread is not None and self._thread.is_alive():
                raise LocalProxyError('pac_busy')
            self._cancel = threading.Event()
            self._candidate = None
            self._code = 'wpad_discovering'
            self._thread = threading.Thread(target=self._run, args=(revision, permission, self._cancel),
                                            name='radar-wpad-discovery', daemon=True)
            try:
                self._thread.start()
            except Exception:
                self._thread = None
                self._code = 'wpad_failed'
                raise LocalProxyError('wpad_failed') from None

    def _run(self, revision, permission, cancel):
        def permitted():
            return not self._stop.is_set() and not cancel.is_set() and permission() is True
        try:
            source = wpad_native.discover(permitted)
            if not permitted():
                raise LocalProxyError('pac_revoked')
            with self._lock:
                if not permitted():
                    raise LocalProxyError('pac_revoked')
                self._candidate = time.monotonic(), revision, secrets.token_hex(16), source
                self._observed = source.config_id
                self._code = 'wpad_source_ready'
        except Exception as exc:
            with self._lock:
                self._candidate = None
                self._code = ('wpad_cancelled' if cancel.is_set() or self._stop.is_set() else
                              exc.code if isinstance(exc, LocalProxyError) and
                              exc.code in {*MESSAGES, *PAC_MESSAGES} else 'wpad_failed')

    def confirm(self, revision, token, save):
        with self._lock:
            candidate = self._valid(revision)
            if self._stop.is_set() or candidate is None or not secrets.compare_digest(candidate[2], token):
                raise LocalProxyError('wpad_candidate_expired')
            # The caller holds the workspace writer lock. A failed write leaves
            # the candidate and original grant intact; no network occurs here.
            save(candidate[3].config_id)
            self._candidate = None
            self._code = 'wpad_not_discovered'

    def cancel(self):
        with self._lock:
            self._cancel.set()
            self._candidate = None
            self._code = 'wpad_cancelled'

    def close(self):
        self._stop.set()
        self.cancel()
        with self._lock:
            thread = self._thread
        if thread is not None:
            thread.join(3)


@dataclass(frozen=True)
class WpadSnapshot:
    config_id: str
    permission: object = field(repr=False, compare=False)
    _loaded: list = field(default_factory=list, init=False, repr=False, compare=False)
    _lock: object = field(default_factory=threading.Lock, init=False, repr=False, compare=False)
    _stop: object = field(default_factory=threading.Event, init=False, repr=False, compare=False)
    _cancellations: list = field(default_factory=list, init=False, repr=False, compare=False)
    kind = 'wpad'

    @property
    def binding(self):
        return self.config_id

    def bind_cancellation(self, event):
        self._cancellations.append(event)

    def close(self):
        self._stop.set()

    def permitted(self):
        try:
            return (not self._stop.is_set() and not any(e.is_set() for e in self._cancellations)
                    and self.permission() is True)
        except Exception:
            return False

    def ensure_active(self):
        if not self.permitted():
            raise LocalProxyError('pac_revoked')

    def for_host(self, host):
        self.ensure_active()
        canonical_target(host)
        # A peer can own both sequential phases. Its discovery and download
        # keep their original deadlines; contending hosts wait for both.
        end = time.monotonic() + wpad_native.DEADLINE + system_pac.DEADLINE
        while not self._lock.acquire(timeout=.05):
            self.ensure_active()
            if time.monotonic() >= end:
                raise LocalProxyError('pac_busy')
        try:
            self.ensure_active()
            if not self._loaded:
                try:
                    source = wpad_native.discover(self.permitted)
                    self.ensure_active()
                    if source.config_id != self.binding:
                        raise LocalProxyError('wpad_source_changed')
                    value = PacSnapshot(system_pac.fetch(source, self.permitted), self.permitted)
                except LocalProxyError as exc:
                    value = exc.code if exc.code in {*MESSAGES, *system_pac.MESSAGES, *PAC_MESSAGES} else 'wpad_failed'
                except Exception:
                    value = 'wpad_failed'
                self._loaded.append(value)
            self.ensure_active()
            value = self._loaded[0]
            if isinstance(value, str):
                raise LocalProxyError(value)
        finally:
            self._lock.release()
        return value.for_host(host)
