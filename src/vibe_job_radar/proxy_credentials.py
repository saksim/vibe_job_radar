"""Explicit local-proxy secrets, kept out of URLs, repr and serialization."""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets

USERNAME_ENV = 'VIBE_RADAR_PROXY_USERNAME'
PASSWORD_ENV = 'VIBE_RADAR_PROXY_PASSWORD'
_BINDING_KEY = secrets.token_bytes(32)
ERROR_MESSAGES = {
    'local_proxy_credentials_invalid': '代理用户名和密码必须同时提供，并符合受支持的字符和长度；未发送凭据或目标请求。',
    'local_proxy_credentials_require_explicit': '已设置代理凭据，但没有明确的本程序HTTP或SOCKS5代理入口。未把凭据转交系统自动发现的其他代理。',
    'local_proxy_auth_failed': '所选本机代理拒绝认证或选择了不同认证方式。请核对代理凭据；没有降为匿名、重复认证或改走直连。',
}


def configured() -> bool:
    return USERNAME_ENV in os.environ or PASSWORD_ENV in os.environ


class ProxyCredentials:
    __slots__ = ('_username', '_password', '_binding')

    def __init__(self, username: str, password: str):
        # Without a charset challenge, Basic has no interoperable non-ASCII
        # encoding. The same bounded printable-ASCII contract serves RFC1929.
        if (not isinstance(username, str) or not isinstance(password, str)
                or not 1 <= len(username) <= 255 or not 1 <= len(password) <= 255
                or ':' in username
                or any(not 32 <= ord(c) <= 126 for c in username + password)):
            raise ValueError('invalid local proxy credentials')
        user, secret = username.encode('ascii'), password.encode('ascii')
        object.__setattr__(self, '_username', user)
        object.__setattr__(self, '_password', secret)
        # Changes must invalidate a live native policy. A process-local keyed
        # binding avoids exposing a stable password hash in public policy IDs.
        object.__setattr__(self, '_binding', hmac.new(_BINDING_KEY, user + b'\0' + secret,
                                                     hashlib.sha256).hexdigest())

    def __setattr__(self, name, value):
        raise AttributeError('proxy credentials are immutable')

    def __repr__(self):
        return '<ProxyCredentials configured>'

    def __reduce_ex__(self, protocol):
        raise TypeError('proxy credentials cannot be serialized')

    def __copy__(self):
        return self

    def __deepcopy__(self, memo):
        return self

    def __eq__(self, other):
        return isinstance(other, ProxyCredentials) and hmac.compare_digest(self._binding, other._binding)

    def __hash__(self):
        return hash(self._binding)

    @property
    def binding(self):
        return self._binding

    def basic_header(self):
        return 'Basic ' + base64.b64encode(self._username + b':' + self._password).decode('ascii')

    def socks_frame(self):
        return b'\x01' + bytes((len(self._username),)) + self._username + bytes((len(self._password),)) + self._password

    @classmethod
    def from_environment(cls):
        if not configured():
            return None
        return cls(os.environ.get(USERNAME_ENV), os.environ.get(PASSWORD_ENV))
