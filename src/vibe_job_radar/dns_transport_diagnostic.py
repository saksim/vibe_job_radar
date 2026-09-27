"""Allowlisted facts from an already-failed DoH transport operation.

No exception text, queried name, headers, payload, new probe, or route change.
The phase names identify operation entry, not the cause of an interruption.
"""
from __future__ import annotations

import http.client
import re
import ssl

from .utils import utc_now, parse_time

KIND = 'encrypted_dns_transport_failure'
PHASES = {
    'tls_context': '解析连接准备',
    'connection_setup': '解析服务连接建立',
    'dns_request': '解析请求发送',
    'response_headers': '解析响应头读取',
    'response_body': '解析响应体读取',
    'unknown': '解析服务通信',
}
ERROR_TYPES = (
    (http.client.RemoteDisconnected, 'RemoteDisconnected', 'connection_interrupted'),
    (http.client.IncompleteRead, 'IncompleteRead', 'incomplete_response'),
    (http.client.BadStatusLine, 'BadStatusLine', 'http_protocol'),
    (TimeoutError, 'TimeoutError', 'timeout'),
    (ConnectionRefusedError, 'ConnectionRefusedError', 'connection_refused'),
    (ConnectionResetError, 'ConnectionResetError', 'connection_interrupted'),
    (ConnectionAbortedError, 'ConnectionAbortedError', 'connection_interrupted'),
    (BrokenPipeError, 'BrokenPipeError', 'connection_interrupted'),
    (http.client.HTTPException, 'HTTPException', 'http_protocol'),
    (OSError, 'OSError', 'os_error'),
)
CATEGORIES = {name: category for _, name, category in ERROR_TYPES}
# Metadata may name only the reviewed resolver's fixed bootstrap addresses.
BOOTSTRAP = frozenset({'1.1.1.1', '1.0.0.1', '2606:4700:4700::1111', '2606:4700:4700::1001'})


def _attempts(value):
    result = []
    if type(value) is list:
        for row in value[:4]:
            if (type(row) is dict
                    and all(type(row.get(key)) is str for key in ('ip', 'phase', 'outcome'))
                    and row['ip'] in BOOTSTRAP
                    and row.get('phase') in {'tcp', 'proxy_connect', 'tls'}
                    and row.get('outcome') in {'pending', 'connected', 'proxy_failed', 'tls_rejected', 'connect_failed'}):
                result.append({key: row[key] for key in ('ip', 'phase', 'outcome')})
    return result


def public_details(value):
    """Rebuild the public shape; never copy an arbitrary diagnostic dictionary."""
    if (type(value) is not dict or value.get('kind') != KIND
            or type(value.get('schema_version')) is not int or value['schema_version'] != 1):
        return None
    phase, error_type = value.get('phase'), value.get('error_type')
    if (not isinstance(phase, str) or phase not in PHASES
            or not isinstance(error_type, str) or error_type not in CATEGORIES
            or value.get('category') != CATEGORIES[error_type]):
        return None
    observed = value.get('observed_at')
    if (not isinstance(observed, str) or len(observed) > 40
            or not re.fullmatch(r'[0-9T:+.Z-]+', observed)):
        return None
    try:
        parse_time(observed)
    except (ValueError, TypeError, OverflowError):
        return None
    policy_id = value.get('policy_id')
    policy_id = policy_id if isinstance(policy_id, str) and re.fullmatch('[a-f0-9]{16}', policy_id) else ''
    result = {
        'schema_version': 1, 'kind': KIND, 'observed_at': observed,
        'phase': phase, 'category': CATEGORIES[error_type], 'error_type': error_type,
        'endpoint_host': 'cloudflare-dns.com', 'endpoint_port': 443,
        'policy_id': policy_id, 'reused_failure': value.get('reused_failure') is True,
        'cause_confirmed': False,
        'tls_handshake_completed': phase in {'dns_request', 'response_headers', 'response_body'},
        'dns_request_attempted': phase in {'dns_request', 'response_headers', 'response_body'},
        'source_request_started': False,
        'connection_attempts': _attempts(value.get('connection_attempts')),
        'next_action': (PHASES[phase] + '阶段中断，依赖这次解析的页面请求尚未发出。'
                        '这些信息不能单独确定网络设备或服务端原因；核对当前网络后再确认。'),
    }
    for key in ('errno', 'winerror'):
        number = value.get(key)
        if type(number) is int and -(2**31) <= number < 2**32:
            result[key] = number
    if type(value.get('matches_current_policy')) is bool:
        result['matches_current_policy'] = value['matches_current_policy']
    return result


def failure_details(exc, *, phase, connection=None, policy_id=''):
    """Classify a non-TLS failure without exception messages or further I/O."""
    if isinstance(exc, ssl.SSLError) or not isinstance(exc, (OSError, http.client.HTTPException)):
        raise TypeError('non-TLS transport exception required')
    _, name, category = next(item for item in ERROR_TYPES if isinstance(exc, item[0]))
    # PinnedHTTPSConnection.connect includes TCP, proxy and TLS setup. A socket
    # error there cannot by itself establish that a TLS handshake was attempted.
    phase = 'connection_setup' if phase == 'tls_handshake' else phase
    value = {
        'schema_version': 1, 'kind': KIND, 'observed_at': utc_now(),
        'phase': phase if type(phase) is str and phase in PHASES else 'unknown', 'category': category,
        'error_type': name, 'policy_id': policy_id, 'reused_failure': False,
        'connection_attempts': getattr(connection, 'connection_attempts', None),
    }
    for key in ('errno', 'winerror'):
        value[key] = getattr(exc, key, None)
    return public_details(value)
