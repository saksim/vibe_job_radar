"""Explicit anonymous RFC1918 host endpoints, never automatic LAN discovery.

Only the proxy peer may be private. Inherited CONNECT implementations still
require numeric public destinations; callers still verify original-host TLS.
"""
from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from urllib.parse import urlsplit

from .loopback_proxy import LocalProxyError, LoopbackProxy
from .loopback_socks import LoopbackSocks5

_NETWORKS = tuple(ipaddress.ip_network(n) for n in ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16'))
ERROR_MESSAGES = {
    'vm_proxy_configuration_invalid': '宿主机代理只接受明确的匿名 RFC1918 IPv4 地址及端口；不能包含凭据、主机名或代理DNS。',
    'vm_proxy_explicit_required': '宿主机代理必须在当前工作区明确选择和确认，不从系统或凭据环境配置自动启用。',
    'vm_proxy_connection_failed': '无法通过指定宿主机代理建立连接。请核对已授权的监听地址、端口和来宾可达性；原任务保留，不会改用来宾本机代理或直连。',
    'vm_proxy_timeout': '指定宿主机代理未在期限内完成握手；原任务保留，不扫描其他入口或改走直连。',
}


class _ExplicitHost:
    scheme = ''

    def __post_init__(self):
        try:
            address = ipaddress.IPv4Address(self.host)
            if (str(address) != self.host or not any(address in n for n in _NETWORKS)
                    or type(self.port) is not int or not 1 <= self.port <= 65535
                    or self.credentials is not None):
                raise ValueError
        except (ValueError, TypeError):
            raise LocalProxyError('vm_proxy_configuration_invalid') from None

    @classmethod
    def from_url(cls, raw):
        try:
            if (not isinstance(raw, str) or len(raw) > 2048
                    or any(ord(c) < 33 or ord(c) == 127 for c in raw)):
                raise ValueError
            p = urlsplit(raw)
            if (p.scheme != cls.scheme or p.port is None or not p.hostname
                    or p.username is not None or p.password is not None
                    or p.path not in ('', '/') or p.query or p.fragment):
                raise ValueError
            return cls(p.hostname, p.port)
        except (ValueError, TypeError, LocalProxyError):
            raise LocalProxyError('vm_proxy_configuration_invalid') from None

    @classmethod
    def from_environment(cls):
        raise LocalProxyError('vm_proxy_explicit_required')

    def with_environment_credentials(self):
        raise LocalProxyError('vm_proxy_explicit_required')

    def open_tunnel(self, public_ip, timeout, source_address=None):
        try:
            return super().open_tunnel(public_ip, timeout, source_address)
        except LocalProxyError as exc:
            # Keep protocol/target refusals, but never call a host peer "local".
            code = {'local_proxy_connection_failed': 'vm_proxy_connection_failed',
                    'local_socks_connection_failed': 'vm_proxy_connection_failed',
                    'local_socks_timeout': 'vm_proxy_timeout'}.get(exc.code, exc.code)
            raise LocalProxyError(code) from None


@dataclass(frozen=True)
class VmHTTPProxy(_ExplicitHost, LoopbackProxy):
    scheme = 'http'


@dataclass(frozen=True)
class VmSocks5Proxy(_ExplicitHost, LoopbackSocks5):
    scheme = 'socks5'
