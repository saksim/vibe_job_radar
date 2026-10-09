"""Bounded metadata from an already-failed TLS operation; never another probe.

No exception strings, peer certificates, paths, query names, headers or secrets.
The original TLS verification, route and retry decisions stay authoritative.
"""
from __future__ import annotations

import os
import re
import ssl

from .utils import utc_now
from .tls_context import TLSConfigurationError

VERIFY_REASONS = {
    2: 'issuer_unavailable', 9: 'not_yet_valid', 10: 'expired',
    18: 'self_signed_leaf', 19: 'self_signed_chain', 20: 'issuer_unavailable',
    21: 'chain_unverifiable', 23: 'revoked', 27: 'untrusted',
    62: 'hostname_mismatch', 64: 'ip_address_mismatch',
}
WINDOWS_VERIFY_REASONS = {
    0x800B0101: 'expired_or_not_yet_valid', 0x800B0109: 'untrusted_root',
    0x800B010A: 'issuer_unavailable', 0x800B010C: 'revoked',
    0x800B010F: 'hostname_mismatch', 0x800B0110: 'wrong_usage',
    0x80096004: 'signature_invalid', 0x80092013: 'revocation_unavailable',
}
HINTS = {
    'configuration': 'Windows 原生证书验证组件不可用或已更新。请重新启动工作台；未重试其他验证器或放宽校验。',
    'certificate_verification': '证书验证未通过。按 verify_code 核对时间、证书主机名及可信链；不要关闭校验或自动信任收到的证书。',
    'peer_closed': 'TLS 连接在完整握手或响应前结束，尚不能认定为根证书缺失。请核对当前允许的网络路线及其中间设备，不要靠重装浏览器修复。',
    'protocol_mismatch': '收到的协议数据不符合 TLS。请核对现有路线或代理协议是否与实际服务一致，不能据此断言某个软件有问题。',
    'tls_protocol': 'TLS 协商或读取失败。请根据 ssl_reason 核对当前允许的路线和 TLS 环境；不能把所有此类错误当作证书缺失。',
}


def _symbol(value):
    return value if isinstance(value, str) and re.fullmatch(r'[A-Z0-9_]{1,96}', value) else None


def failure_details(exc: ssl.SSLError, *, phase: str, connection=None,
                    bootstrap=(), policy_id='') -> dict:
    """Extract only allowlisted fields; no str(exc) or additional network calls."""
    if not isinstance(exc, ssl.SSLError):
        raise TypeError('TLS exception required')
    reason = _symbol(getattr(exc, 'reason', None))
    if isinstance(exc, TLSConfigurationError):
        category, error_type = 'configuration', 'TLSConfigurationError'
    elif isinstance(exc, ssl.SSLCertVerificationError):
        category, error_type = 'certificate_verification', 'SSLCertVerificationError'
    elif isinstance(exc, (ssl.SSLEOFError, ssl.SSLZeroReturnError)):
        category, error_type = 'peer_closed', ('SSLEOFError' if isinstance(exc, ssl.SSLEOFError) else 'SSLZeroReturnError')
    elif reason in {'UNEXPECTED_EOF_WHILE_READING'}:
        category, error_type = 'peer_closed', 'SSLError'
    elif reason in {'WRONG_VERSION_NUMBER', 'UNKNOWN_PROTOCOL', 'HTTP_REQUEST', 'HTTPS_PROXY_REQUEST'}:
        category, error_type = 'protocol_mismatch', 'SSLError'
    else:
        category, error_type = 'tls_protocol', 'SSLError'
    phases = {'tls_context', 'tls_handshake', 'request_setup', 'dns_request', 'response_headers', 'response_body'}
    result = {
        'schema_version': 1, 'observed_at': utc_now(), 'category': category,
        'error_type': error_type, 'phase': phase if phase in phases else 'unknown',
        'endpoint_host': 'cloudflare-dns.com', 'endpoint_port': 443,
        'policy_id': policy_id, 'reused_failure': False,
        'cause_confirmed': False, 'next_action': HINTS[category],
        'tls_handshake_completed': phase in {'request_setup', 'dns_request', 'response_headers', 'response_body'},
        'dns_request_attempted': phase in {'dns_request', 'response_headers', 'response_body'},
        'ssl_cert_file_configured': bool(os.environ.get('SSL_CERT_FILE')),
        'ssl_cert_dir_configured': bool(os.environ.get('SSL_CERT_DIR')),
    }
    if reason:
        result['ssl_reason'] = reason
    library = _symbol(getattr(exc, 'library', None))
    if library:
        result['ssl_library'] = library
    code = getattr(exc, 'verify_code', None)
    if isinstance(exc, ssl.SSLCertVerificationError) and type(code) is int and -(2**31) <= code < 2**32:
        code = code & 0xffffffff
        result['verify_code'] = code
        native_code = code >= 0x80000000
        result['verify_code_namespace'] = 'windows_chain_policy' if native_code else 'openssl_x509'
        if native_code:
            result['verify_code_hex'] = f'0x{code:08X}'
        # Never copy verify_message: it may contain a hostname or arbitrary text.
        reasons = WINDOWS_VERIFY_REASONS if native_code else VERIFY_REASONS
        result['verification_reason'] = reasons.get(code, 'other_certificate_error')
        if result['verification_reason'] == 'issuer_unavailable':
            result['next_action'] = ('证书验证未通过：无法将证书链连接到当前信任的签发者；这不是证书数量为零，也不能直接判定证书过期。'
                                     'Windows 可使用原生系统验证组件完成构链；仍失败时由管理员核对缺失的中间链或可信根，不自动信任网站证书。')
    if isinstance(ssl.OPENSSL_VERSION, str) and len(ssl.OPENSSL_VERSION) < 150:
        result['openssl_version'] = ssl.OPENSSL_VERSION
    attempts = getattr(connection, 'connection_attempts', None)
    if isinstance(attempts, list):
        safe = []
        for attempt in attempts[:4]:
            if (isinstance(attempt, dict) and attempt.get('ip') in bootstrap
                    and attempt.get('phase') in {'tcp', 'proxy_connect', 'tls'}
                    and attempt.get('outcome') in {'pending', 'connected', 'proxy_failed', 'tls_rejected', 'connect_failed'}):
                safe.append({k: attempt[k] for k in ('ip', 'phase', 'outcome')})
        result['connection_attempts'] = safe
    context = getattr(connection, '_context', None)
    if isinstance(context, ssl.SSLContext):
        result['tls_policy'] = {'check_hostname': context.check_hostname,
                                'verify_mode': context.verify_mode.name,
                                'verify_flags': int(context.verify_flags)}
        try:
            result['trust_store_counts'] = context.cert_store_stats()
        except NotImplementedError:
            # Native stores are not OpenSSL's enumerable snapshot; unknown != 0.
            result['trust_store_counts'] = None
            result['trust_store_counts_scope'] = 'native_store_not_enumerated'
        engine = getattr(context, 'radar_tls_engine', 'openssl_default')
        if engine in {'openssl_default', 'windows_cryptoapi'}:
            result['trust_engine'] = engine
            result['certificate_retrieval'] = 'windows_managed' if engine == 'windows_cryptoapi' else 'not_app_managed'
    return result
