"""Classify browser transport failures without retaining arbitrary error text."""
from __future__ import annotations

import re


def target_closed_by_driver(error: Exception) -> bool:
    """Exact optional Playwright type; never infer closure from message text.

    The driver may terminate a popup before its public page/frame close event
    is observable. This helper only classifies an already refused route abort.
    If the optional package/type is unavailable, the original error is retained.
    """
    try:
        from playwright._impl._errors import TargetClosedError
    except ImportError:
        return False
    return isinstance(error, TargetClosedError)


def native_failure_code(error: object) -> str:
    """Return a fixed code from the browser's leading net error, or unknown.

    Parse only the first line and stop before a navigation URL/call log. A URL,
    response body or unrelated exception is not allowed to invent a TLS cause.
    This does not retry a request, change trust, or make access decisions.
    """
    text = str(error).split('\n', 1)[0].split(' at ', 1)[0][:512]
    match = re.fullmatch(r'(?:[A-Za-z]+\.[A-Za-z]+: )?(?:net::)?(ERR_[A-Z0-9_]+)', text)
    if not match:
        return ''
    code = match[1]
    if code.startswith('ERR_CERT_'):
        return 'tls_verification_failed'
    if code.startswith('ERR_SSL_'):
        return 'tls_handshake_failed'
    return {
        'ERR_INVALID_AUTH_CREDENTIALS': 'native_proxy_auth_failed',
        'ERR_PROXY_AUTH_REQUESTED': 'native_proxy_auth_failed',
        'ERR_PROXY_CONNECTION_FAILED': 'local_proxy_connection_failed',
        'ERR_TUNNEL_CONNECTION_FAILED': 'local_proxy_connection_failed',
        'ERR_NAME_NOT_RESOLVED': 'dns_error',
        'ERR_BLOCKED_BY_ADMINISTRATOR': 'native_administrator_blocked',
    }.get(code, '')


def native_transport_failure(error: object, tunnel_error: str = '') -> str:
    """A generic Chromium tunnel failure must retain our actual upstream cause.

    Certificate, browser-guard authentication and administrator errors are
    independent diagnoses; a previous tunnel error cannot overwrite those.
    Existing controller policy failures are preserved separately by _fatal.
    """
    code = native_failure_code(error)
    if not code or code == 'local_proxy_connection_failed':
        return tunnel_error or code
    return code
