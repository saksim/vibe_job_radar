"""Classify browser transport failures without retaining arbitrary error text."""
from __future__ import annotations

import re


# Shared by opt-in production diagnostics and existing developer probes.
# Fixed CDP values only; unknown/future labels never copy arbitrary text.
NET_ERRORS = frozenset({
    'ERR_FAILED', 'ERR_ABORTED', 'ERR_EMPTY_RESPONSE', 'ERR_CONNECTION_CLOSED',
    'ERR_CONNECTION_RESET', 'ERR_CONNECTION_REFUSED', 'ERR_CONNECTION_ABORTED',
    'ERR_CONNECTION_TIMED_OUT', 'ERR_TIMED_OUT', 'ERR_NETWORK_CHANGED',
    'ERR_INTERNET_DISCONNECTED', 'ERR_PROXY_CONNECTION_FAILED',
    'ERR_TUNNEL_CONNECTION_FAILED', 'ERR_PROXY_AUTH_REQUESTED',
    'ERR_INVALID_AUTH_CREDENTIALS', 'ERR_NAME_NOT_RESOLVED',
    'ERR_BLOCKED_BY_CLIENT', 'ERR_BLOCKED_BY_RESPONSE',
    'ERR_BLOCKED_BY_ADMINISTRATOR', 'ERR_CERT_AUTHORITY_INVALID',
    'ERR_CERT_COMMON_NAME_INVALID', 'ERR_CERT_DATE_INVALID',
    'ERR_SSL_PROTOCOL_ERROR', 'ERR_HTTP2_PROTOCOL_ERROR',
    'ERR_CONTENT_LENGTH_MISMATCH', 'ERR_INCOMPLETE_CHUNKED_ENCODING',
})
BLOCKED_REASONS = frozenset({'other', 'csp', 'mixed-content', 'origin',
    'inspector', 'integrity', 'subresource-filter', 'content-type',
    'coep-frame-resource-needs-coep-header',
    'coop-sandboxed-iframe-cannot-navigate-to-coop-page',
    'corp-not-same-origin', 'corp-not-same-origin-after-defaulted-to-same-origin-by-coep',
    'corp-not-same-origin-after-defaulted-to-same-origin-by-dip',
    'corp-not-same-origin-after-defaulted-to-same-origin-by-coep-and-dip',
    'corp-not-same-site', 'sri-message-signature-mismatch'})
CORS_ERRORS = frozenset({'DisallowedByMode', 'InvalidResponse', 'WildcardOriginNotAllowed',
    'MissingAllowOriginHeader', 'MultipleAllowOriginValues', 'InvalidAllowOriginValue',
    'AllowOriginMismatch', 'InvalidAllowCredentials', 'CorsDisabledScheme',
    'PreflightInvalidStatus', 'PreflightDisallowedRedirect',
    'PreflightWildcardOriginNotAllowed', 'PreflightMissingAllowOriginHeader',
    'PreflightMultipleAllowOriginValues', 'PreflightInvalidAllowOriginValue',
    'PreflightAllowOriginMismatch', 'PreflightInvalidAllowCredentials',
    'PreflightMissingAllowExternal', 'PreflightInvalidAllowExternal',
    'InvalidAllowMethodsPreflightResponse', 'InvalidAllowHeadersPreflightResponse',
    'MethodDisallowedByPreflightResponse', 'HeaderDisallowedByPreflightResponse',
    'RedirectContainsCredentials', 'InsecurePrivateNetwork', 'InvalidPrivateNetworkAccess',
    'UnexpectedPrivateNetworkAccess', 'PreflightMissingAllowPrivateNetwork',
    'PreflightInvalidAllowPrivateNetwork', 'PreflightMissingPrivateNetworkAccessId',
    'PreflightMissingPrivateNetworkAccessName', 'PrivateNetworkAccessPermissionUnavailable',
    'PrivateNetworkAccessPermissionDenied', 'LocalNetworkAccessPermissionDenied', 'InsecureLocalNetwork',
    'InvalidLocalNetworkAccess', 'NoCorsRedirectModeNotFollow'})


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


def loading_failure_details(event):
    """Finite CDP loading failure fields; never include failedParameter."""
    def fixed(value, allowed):
        return value if isinstance(value, str) and value in allowed else 'unknown'
    raw = event.get('errorText')
    token = raw.removeprefix('net::') if isinstance(raw, str) else ''
    cors = event.get('corsErrorStatus')
    return {
        'browser_error': fixed(token, NET_ERRORS),
        'browser_failure_code': native_failure_code(raw) if isinstance(raw, str) else '',
        'canceled': event.get('canceled') if type(event.get('canceled')) is bool else None,
        'blocked_reason': fixed(event.get('blockedReason'), BLOCKED_REASONS),
        'cors_error': fixed(cors.get('corsError'), CORS_ERRORS) if isinstance(cors, dict) else None,
    }
