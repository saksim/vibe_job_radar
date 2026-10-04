"""Bounded, private-data-free failure facts for local native CI probes only.

These observations never change a request, retry, timeout or stop decision.
Record before the controller consumes the event and before cleanup removes
connection state. A broken observer must not replace the original outcome.
"""
from __future__ import annotations

from vibe_job_radar.guided.native_errors import native_failure_code
from vibe_job_radar.guided.native_policy import liepin_bootstrap
from native_proxy_evidence import CODES

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
ROLES = frozenset({'document', 'business', 'asset', 'robots', 'login'})
RESOURCE_TYPES = frozenset({'Document', 'Stylesheet', 'Image', 'Media', 'Font',
    'Script', 'TextTrack', 'XHR', 'Fetch', 'Prefetch', 'EventSource',
    'WebSocket', 'Manifest', 'SignedExchange', 'Ping', 'CSPViolationReport',
    'Preflight', 'Other'})
OPERATIONS = frozenset({'robots', 'search', 'detail', 'asset', 'page', 'jobs'} |
    {rule.key for rule in liepin_bootstrap().rules})
BLOCKED_REASONS = frozenset({'other', 'csp', 'mixed-content', 'origin',
    'inspector', 'subresource-filter', 'content-type',
    'coep-frame-resource-needs-coep-header',
    'coop-sandboxed-iframe-cannot-navigate-to-coop-page',
    'corp-not-same-origin', 'corp-not-same-origin-after-defaulted-to-same-origin-by-coep',
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
    'PrivateNetworkAccessPermissionDenied', 'LocalNetworkAccessPermissionDenied'})
STOP_CODES = CODES | {'http_401', 'http_403', 'http_429', 'native_protocol_error',
    'native_operation_unreviewed', 'native_policy_changed', 'robots_denied',
    'native_business_response_invalid', 'native_observation_limit',
    'native_unaccounted_response', 'native_surface_unsupported',
    'liepin_search_query_mismatch', 'request_too_large', 'response_too_large',
    'redirect_requires_attention', 'site_stopped', 'page_not_ready'}


def fixed(value, allowed):
    return value if isinstance(value, str) and value in allowed else 'unknown'


def number(value):
    return value if type(value) is int and value >= 0 else None


def facts(backend):
    guard = getattr(backend, 'tunnel', None)
    worker = getattr(guard, 'thread', None)
    sockets = getattr(guard, '_sockets', None)
    counts = getattr(backend, 'native_counts', {})
    return {
        'guard_present': guard is not None,
        'guard_thread_alive': bool(worker and worker.is_alive()),
        'guard_closed': bool(getattr(guard, '_closed', False)),
        'guard_connections': number(getattr(guard, 'connections', None)),
        'guard_listener_backlog': number(getattr(getattr(guard, 'server', None), 'request_queue_size', None)),
        'guard_active_sockets': len(sockets) if isinstance(sockets, set) else None,
        'guard_error_code': fixed(getattr(guard, 'last_error', ''), STOP_CODES | {''}),
        'backend_error_code': fixed(getattr(backend, 'error', None), STOP_CODES | {''}),
        'backend_closing': bool(getattr(backend, '_closing', False)),
        'backend_halted': bool(getattr(backend, '_halted', False)),
        'cancelled': backend.cancelled.is_set(),
        'owned_sessions': len(backend._sessions),
        'pending_commands': len(backend._pending),
        'active_requests': len(backend._requests),
        'native_counts': {key: number(counts.get(key)) for key in sorted(ROLES | {'responses', 'blocked'})},
    }


def loading_failure(backend, event, message):
    try:
        if message.get('method') != 'Network.loadingFailed':
            return
        data = message.get('params', {})
        session = event.get('sessionId')
        record = backend._requests.get((session, data.get('requestId')))
        request = record or {}
        raw = data.get('errorText', '')
        token = raw.removeprefix('net::') if isinstance(raw, str) else ''
        context = request.get('context', {})
        sequence = context.get('sequence')
        cors = data.get('corsErrorStatus')
        row = {
            'index': backend.probe.get('loading_failure_count', 0) + 1,
            'browser_error': fixed(token, NET_ERRORS),
            'browser_failure_code': native_failure_code(raw) if isinstance(raw, str) else '',
            'resource_type': fixed(data.get('type'), RESOURCE_TYPES),
            'canceled': data.get('canceled') if type(data.get('canceled')) is bool else None,
            'blocked_reason': fixed(data.get('blockedReason'), BLOCKED_REASONS),
            'cors_error': fixed(cors.get('corsError'), CORS_ERRORS) if isinstance(cors, dict) else None,
            'owned_session': session in backend._sessions,
            'request_observed': record is not None,
            'role': fixed(request.get('role'), ROLES),
            'operation': fixed(request.get('operation'), OPERATIONS),
            'epoch_is_current': request.get('epoch') == backend._epoch if record else None,
            'sequence_is_current': (backend.__dict__.get('_latest_business', {}).get(request.get('operation')) == sequence) if sequence is not None else None,
            'response_status': request.get('status') if type(request.get('status')) is int and 100 <= request['status'] <= 599 else None,
            'json_response': request.get('json') is True,
            'connection_facts': facts(backend),
        }
        rows = backend.probe.setdefault('loading_failures', [])
        rows.append(row)
        del rows[:-32]
        backend.probe['loading_failure_count'] = row['index']
    except Exception:
        # Auxiliary diagnostics cannot interfere with the controller event.
        pass


def fatal_failure(backend, code):
    try:
        if 'first_fatal' in backend.probe:
            return
        rows = backend.probe.get('loading_failures', [])
        backend.probe['first_fatal'] = {
            'code': fixed(code, STOP_CODES),
            'connection_facts': facts(backend),
            'latest_loading_failure': rows[-1] if rows else None,
        }
    except Exception:
        pass
