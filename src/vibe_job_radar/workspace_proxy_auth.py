"""Opt-in OS protection for one workspace's local proxy credentials."""
from __future__ import annotations

import base64
import hashlib
import json
import os

from .loopback_proxy import LocalProxyError
from .proxy_credentials import ProxyCredentials
from . import system_credentials as store

MODES = {'http_auth': 'http', 'http_digest_auth': 'http', 'http_ntlm_auth': 'http', 'socks5_auth': 'socks5'}
NTLM_CONSENT = 'workspace-loopback-explicit-ntlmv2-current-user-dpapi-v1'
CONSENT = 'workspace-loopback-current-user-dpapi-v1'
FIELD = 'proxy_auth'
MESSAGES = {
    **store.MESSAGES,
    'workspace_proxy_auth_unavailable': '当前系统无法使用此加密方式（Windows DPAPI／Linux Secret Service／macOS Keychain）。请核对原系统账号和组件，或明确选择其他连接方式。',
    'workspace_proxy_auth_unreadable': '无法解密此工作区的代理凭据。请核对系统账号、工作区位置和代理入口，再重新输入；不会匿名连接或改走直连。',
    'workspace_proxy_auth_missing': '此代理的已保存凭据已清除；请重新输入，或明确选择其他连接方式。新连接已停止，原任务和数据保留。',
    'workspace_proxy_auth_save_failed': '系统未能加密代理凭据，原网络配置保留；没有写入明文或进行连接测试。',
}
DISCLOSURE = ('只使用你信任的本机HTTP Basic、HTTP Digest、NTLMv2或SOCKS5用户名密码代理，并明确选择认证方式，不填写招聘账号密码。'
    'Windows使用当前账号DPAPI；Linux/macOS使用默认系统密钥库保护的工作区密钥加密，绑定工作区位置和代理入口。重启后可继续使用，不在页面回显。'
    '迁移工作区或更换系统账号、入口时需要重新输入。密钥库锁定或不可用时新连接停止；请在系统中解锁或授权程序，应用不会自动弹窗或改存明文。清除凭据保留工作区密钥，旧备份需自行管理。代理握手本身仍是本机协议，'
    '不保证其他同账号程序无法读取；不承诺清除所有内存副本。保存不联网。'
    'NTLMv2只使用本次输入的代理账号（可填域\\用户名），不自动使用Windows登录身份，不协商Kerberos或NTLMv1。')


def consent_version(mode, *, system=False):
    if system:
        return ('workspace-loopback-explicit-ntlmv2-system-keyring-v1' if mode == 'http_ntlm_auth'
                else 'workspace-loopback-system-keyring-v1')
    return NTLM_CONSENT if mode == 'http_ntlm_auth' else CONSENT


def schema_version(mode, *, system=False):
    return 12 if system else 11 if mode == 'http_ntlm_auth' else 8


def storage_kind():
    return 'dpapi-current-user' if os.name == 'nt' else store.SCHEME


def available():
    return os.name == 'nt' or store.available()


def validate_envelope(value):
    if value is None:  # Cleared credentials stop the selected route.
        return
    if (not isinstance(value, dict) or set(value) != {'version', 'protection', 'payload'}
            or type(value['version']) is not int
            or (value['version'], value['protection']) not in ((1, 'dpapi-current-user'), (2, store.SCHEME))
            or not isinstance(value['payload'], str) or not 1 <= len(value['payload']) <= 3072):
        raise ValueError('invalid protected proxy envelope')
    raw = base64.b64decode(value['payload'], validate=True)
    if not raw or base64.b64encode(raw).decode('ascii') != value['payload']:
        raise ValueError('invalid protected proxy envelope')


def _context(root, mode, endpoint):
    value = [consent_version(mode), os.path.normcase(str(root.resolve())), mode, endpoint]
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
        if storage_kind() == store.SCHEME:
            envelope = {'version': 2, 'protection': store.SCHEME,
                        'payload': store.seal(root, 'proxy', [consent_version(mode, system=True), mode, endpoint], raw)}
        else:
            encrypted = _protect(raw, _context(root, mode, endpoint))
            envelope = {'version': 1, 'protection': 'dpapi-current-user',
                        'payload': base64.b64encode(encrypted).decode('ascii')}
        validate_envelope(envelope)
        return envelope
    except store.CredentialStoreError as error:
        raise LocalProxyError(error.code) from None
    except Exception:
        raise LocalProxyError('workspace_proxy_auth_save_failed') from None


def restore(root, mode, endpoint, envelope):
    if envelope is None:
        raise LocalProxyError('workspace_proxy_auth_missing')
    if not available():
        raise LocalProxyError('workspace_proxy_auth_unavailable')
    try:
        validate_envelope(envelope)
        if envelope['protection'] != storage_kind():
            raise LocalProxyError('workspace_proxy_auth_unavailable')
        if envelope['version'] == 2:
            raw = store.restore(root, 'proxy', [consent_version(mode, system=True), mode, endpoint], envelope['payload'])
        else:
            raw = _protect(base64.b64decode(envelope['payload'], validate=True),
                           _context(root, mode, endpoint), decrypt=True)
        if len(raw) > 2048:
            raise ValueError('invalid protected payload')
        from .html_parser import _unique_object
        value = json.loads(raw.decode('ascii'), object_pairs_hook=_unique_object)
        if not isinstance(value, dict) or set(value) != {'username', 'password'}:
            raise ValueError('invalid protected payload')
        return ProxyCredentials(value['username'], value['password'])
    except LocalProxyError:
        raise
    except store.CredentialStoreError as error:
        raise LocalProxyError(error.code) from None
    except Exception:
        raise LocalProxyError('workspace_proxy_auth_unreadable') from None
