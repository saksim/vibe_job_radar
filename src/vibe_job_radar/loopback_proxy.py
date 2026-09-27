"""Explicit local HTTP CONNECT for an already validated public destination.

This deliberately narrow transport does NOT implement the previously proposed
Fake-IP/remote-DNS mode. Final destination DNS and all public-IP validation stay
unchanged. NetworkPolicy selects loopback HTTP from explicit application overrides
or supported static settings; optional credentials require an explicit override.
No PAC, LAN probing,
DNS replacement, private target exceptions or fallback to direct connections.
"""
from __future__ import annotations

import http.client
import ipaddress
import math
import os
import socket
import time
from dataclasses import dataclass, field, replace
from urllib.parse import urlsplit
from .proxy_credentials import ProxyCredentials, configured as credentials_configured


class LocalProxyError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class LoopbackProxy:
    host: str
    port: int
    credentials: ProxyCredentials | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        try:
            if self.credentials is not None and not isinstance(self.credentials, ProxyCredentials):
                raise ValueError('invalid credential object')
            address = ipaddress.ip_address(self.host)
            if (not address.is_loopback or '%' in self.host or type(self.port) is not int
                    or not 1 <= self.port <= 65535):
                raise ValueError('unsupported local endpoint')
        except (ValueError, TypeError) as exc:
            raise LocalProxyError('local_proxy_configuration_invalid') from exc

    @classmethod
    def from_environment(cls) -> LoopbackProxy | None:
        raw = os.environ.get('VIBE_RADAR_HTTP_PROXY', '')
        if not raw.strip():
            if credentials_configured():
                raise LocalProxyError('local_proxy_credentials_require_explicit')
            return None
        return cls.from_url(raw).with_environment_credentials()

    def with_environment_credentials(self):
        try:
            return replace(self, credentials=ProxyCredentials.from_environment())
        except ValueError:
            raise LocalProxyError('local_proxy_credentials_invalid') from None

    @classmethod
    def from_url(cls, raw: str) -> LoopbackProxy:
        try:
            if not isinstance(raw, str):
                raise ValueError('invalid setting')
            if len(raw) > 2048 or any(ord(c) < 32 for c in raw):
                raise ValueError('invalid setting')
            parsed = urlsplit(raw.strip())
            host = '127.0.0.1' if parsed.hostname == 'localhost' else parsed.hostname
            address = ipaddress.ip_address(host or '')
            port = parsed.port
            if (parsed.scheme != 'http' or not address.is_loopback or port is None
                    or not 1 <= port <= 65535 or parsed.username is not None
                    or parsed.password is not None or parsed.query or parsed.fragment
                    or parsed.path not in ('', '/') or any(ord(c) < 32 for c in raw)):
                raise ValueError('unsupported proxy configuration')
            return cls(str(address), port)
        except (ValueError, TypeError) as exc:
            # Never return raw configuration or arbitrary exception text.
            raise LocalProxyError('local_proxy_configuration_invalid') from exc

    def open_tunnel(self, public_ip: str, timeout: float, source_address=None) -> socket.socket:
        try:
            address = ipaddress.ip_address(public_ip)
        except (ValueError, TypeError) as exc:
            raise LocalProxyError('non_public_address') from exc
        if not address.is_global or '%' in public_ip:
            raise LocalProxyError('non_public_address')
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError('a finite positive connection timeout is required')
        deadline = time.monotonic() + timeout
        proxy = http.client.HTTPConnection(self.host, self.port, timeout=timeout,
                                            source_address=source_address)
        proxy.set_debuglevel(0)  # Inherited stdlib debug output must not print secrets.
        # CONNECT only the verified numeric IP, never ask the proxy to resolve a
        # hostname differently. Original-host TLS is performed by the caller.
        authority = '[' + str(address) + ']' if address.version == 6 else str(address)
        headers = {'Host': authority + ':443'}
        if self.credentials is not None:
            headers['Proxy-Authorization'] = self.credentials.basic_header()
        proxy.set_tunnel(authority, 443, headers=headers)
        try:
            proxy.connect()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError('local proxy timeout')
            sock = proxy.sock
            if sock is None:
                raise OSError('proxy did not establish a tunnel')
            sock.settimeout(remaining)
            proxy.sock = None  # Transfer sole socket ownership to the TLS caller.
            return sock
        except (OSError, http.client.HTTPException) as exc:
            proxy.close()
            # A selected proxy failure stops; it must never change to direct.
            if self.credentials is not None and str(exc).startswith('Tunnel connection failed: 407 '):
                raise LocalProxyError('local_proxy_auth_failed') from None
            raise LocalProxyError('local_proxy_connection_failed') from None
        finally:
            headers.clear()
            proxy._tunnel_headers.clear()
