"""Fixed, bounded facts for the artificial native proxy acceptance only."""
from vibe_job_radar.guided.native_errors import native_failure_code

STAGES = {'http-normal', 'http-rejected', 'socks5-normal', 'socks5-rejected'}
ACTIONS = {'create', 'search', 'detail', 'rejected_search', 'verify', 'close', 'setup_or_cleanup'}
CODES = {'paused', 'network_error', 'dns_error', 'robots_unavailable',
         'tls_verification_failed', 'tls_handshake_failed', 'native_proxy_auth_failed',
         'native_administrator_blocked', 'local_proxy_connection_failed',
         'local_proxy_auth_failed', 'local_proxy_configuration_invalid',
         'local_proxy_configuration_conflict', 'local_proxy_credentials_invalid',
         'local_proxy_credentials_require_explicit', 'local_socks_configuration_invalid',
         'local_socks_truncated_reply', 'local_socks_protocol_error',
         'local_socks_auth_unsupported', 'local_socks_request_rejected',
         'local_socks_timeout', 'local_socks_connection_failed'}
ERROR_TYPES = {'CrawlError', 'BrowserStartupError', 'Error', 'AssertionError',
               'TimeoutError', 'RuntimeError', 'ValueError', 'OSError'}


def _code(value):
    if value is None:
        return ''
    return value if isinstance(value, str) and (value in CODES or value == '') else 'unknown'


def _count(owner, attribute):
    value = getattr(owner, attribute, None)
    return len(value) if isinstance(value, (list, tuple, set)) else None


def connection_facts(backend, fixture, source, source_start):
    guard = getattr(backend, 'tunnel', None)
    worker = getattr(guard, 'thread', None)
    total = _count(source, 'requests')
    connections = getattr(guard, 'connections', None)
    return {
        'guard_present': guard is not None,
        'guard_thread_alive': bool(worker and worker.is_alive()),
        'guard_closed': bool(getattr(guard, '_closed', False)) if guard else None,
        'guard_connections': connections if type(connections) is int else None,
        'guard_active_sockets': _count(guard, '_sockets'),
        'guard_error_code': _code(getattr(guard, 'last_error', '')),
        'backend_error_code': _code(getattr(backend, 'error', '')),
        'upstream_greetings': _count(fixture, 'greetings'),
        'upstream_authentications': _count(fixture, 'authentication'),
        'upstream_connects': _count(fixture, 'connects'),
        'source_requests': total,
        'stage_source_requests': max(0, total - source_start) if total is not None else None,
    }


def update_proxy_evidence(result, stage, *, source=None, fixture=None, backend=None,
                          source_start=0, error=None, action='verify'):
    if stage not in STAGES | {'finished', 'failed'} or action not in ACTIONS:
        raise ValueError('unsupported native proxy evidence field')
    result['stage'] = stage
    result['source_requests'] = _count(source, 'requests') or 0
    if stage in STAGES:
        result['last_active_stage'] = stage
    active = result.get('last_active_stage')
    if backend is not None and active in STAGES:
        result.setdefault('connection_facts', {})[active] = connection_facts(
            backend, fixture, source, source_start)
    if error is not None and 'failure' not in result:
        # Snapshot before backend.close; later final checkpoints must not erase it.
        facts = connection_facts(backend, fixture, source, source_start)
        if backend is None and active in result.get('connection_facts', {}):
            facts = dict(result['connection_facts'][active])
        cause = error
        browser_code = ''
        for _ in range(3):
            if cause is None:
                break
            browser_code = native_failure_code(cause) or browser_code
            cause = cause.__cause__ or cause.__context__
        result['failure'] = {
            'stage': active if active in STAGES else 'setup_or_cleanup',
            'action': action,
            'error_type': type(error).__name__ if type(error).__name__ in ERROR_TYPES else 'other',
            'error_code': _code(getattr(error, 'code', '')),
            'browser_error_code': browser_code,
            'connection_facts': facts,
        }
