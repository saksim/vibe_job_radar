"""Current-user Windows protection for one workspace's local proxy credentials."""
from __future__ import annotations

import base64
import hashlib
import json
import os

from .loopback_proxy import LocalProxyError
from .proxy_credentials import ProxyCredentials

MODES = {'http_auth': 'http', 'socks5_auth': 'socks5'}
CONSENT = 'workspace-loopback-current-user-dpapi-v1'
FIELD = 'proxy_auth'
MESSAGES = {
    'workspace_proxy_auth_unavailable': '当前系统不支持Windows本机加密代理凭据。已停止此入口；可在原Windows账号中重新配置，或明确选择其他连接方式。',
    'workspace_proxy_auth_unreadable': '无法解密此工作区的代理凭据。请核对Windows账号、工作区位置和代理入口，再重新输入；不会匿名连接或改走直连。',
    'workspace_proxy_auth_missing': '此代理的已保存凭据已清除；请重新输入，或明确选择其他连接方式。新连接已停止，原任务和数据保留。',
    'workspace_proxy_auth_save_failed': 'Windows未能加密代理凭据，原网络配置保留；没有写入明文或进行连接测试。',
}
DISCLOSURE = ('只使用你信任的本机HTTP Basic或SOCKS5用户名密码代理，不填写招聘账号密码。'
    '凭据以当前Windows账号加密，绑定本工作区位置和代理入口；重启后可继续使用，不在页面回显。'
    '迁移工作区或更换Windows账号、入口时需要重新输入。代理握手本身仍是本机协议，'
    '不保证其他同账号程序无法读取；不承诺清除所有内存副本。保存不联网。')


def available():
    return os.name == 'nt'


def validate_envelope(value):
    if value is None:  # Cleared credentials stop the selected route.
        return
    if (not isinstance(value, dict) or set(value) != {'version', 'protection', 'payload'}
            or type(value['version']) is not int or value['version'] != 1
            or value['protection'] != 'dpapi-current-user'
            or not isinstance(value['payload'], str) or not 1 <= len(value['payload']) <= 3072):
        raise ValueError('invalid protected proxy envelope')
    raw = base64.b64decode(value['payload'], validate=True)
    if not raw or base64.b64encode(raw).decode('ascii') != value['payload']:
        raise ValueError('invalid protected proxy envelope')


def _context(root, mode, endpoint):
    value = [CONSENT, os.path.normcase(str(root.resolve())), mode, endpoint]
    return hashlib.sha256(json.dumps(value, ensure_ascii=True, separators=(',', ':')).encode()).digest()


def _protect(data, entropy, *, decrypt=False):
    # The existing wrapper uses current-user DPAPI without UI or a stored key.
    from .guided.saved_session import _dpapi
    return _dpapi(data, entropy, decrypt=decrypt)


def seal(root, mode, endpoint, username, password):
    if not available():
        raise LocalProxyError('workspace_proxy_auth_unavailable')
    try:
        ProxyCredentials(username, password)
    except ValueError:
        raise LocalProxyError('local_proxy_credentials_invalid') from None
    try:
        raw = json.dumps({'username': username, 'password': password}, separators=(',', ':')).encode('ascii')
        encrypted = _protect(raw, _context(root, mode, endpoint))
        envelope = {'version': 1, 'protection': 'dpapi-current-user',
                    'payload': base64.b64encode(encrypted).decode('ascii')}
        validate_envelope(envelope)
        return envelope
    except Exception:
        raise LocalProxyError('workspace_proxy_auth_save_failed') from None


def restore(root, mode, endpoint, envelope):
    if envelope is None:
        raise LocalProxyError('workspace_proxy_auth_missing')
    if not available():
        raise LocalProxyError('workspace_proxy_auth_unavailable')
    try:
        validate_envelope(envelope)
        raw = _protect(base64.b64decode(envelope['payload'], validate=True),
                       _context(root, mode, endpoint), decrypt=True)
        if len(raw) > 2048:
            raise ValueError('invalid protected payload')
        from .html_parser import _unique_object
        value = json.loads(raw.decode('ascii'), object_pairs_hook=_unique_object)
        if not isinstance(value, dict) or set(value) != {'username', 'password'}:
            raise ValueError('invalid protected payload')
        return ProxyCredentials(value['username'], value['password'])
    except Exception:
        raise LocalProxyError('workspace_proxy_auth_unreadable') from None
