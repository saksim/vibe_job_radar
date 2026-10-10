"""Reduce ephemeral Chromium NetLog to bounded connection facts in CI only.

Only fixed event names, numeric identifiers/errors and equality classifications
leave the private directory. No raw NetLog, URL, address, header or payload is
an artifact. This observer does not retry requests or change proxy decisions.
"""
from __future__ import annotations

from collections import Counter, deque
import json
import math
from pathlib import Path
import shutil
import tempfile
from urllib.parse import urlsplit

from native_wait_diagnostics import require_ci

MAX_BYTES = 20 * 1024 * 1024
MAX_EVENTS = 4096
EVENTS = frozenset({
    'URL_REQUEST_START_JOB', 'REQUEST_ALIVE', 'HTTP_STREAM_REQUEST',
    'HTTP_STREAM_JOB', 'HTTP_STREAM_JOB_BOUND_TO_REQUEST',
    'HTTP_STREAM_REQUEST_BOUND_TO_JOB', 'HTTP_STREAM_JOB_CONTROLLER',
    'HTTP_STREAM_JOB_CONTROLLER_BOUND',
    'SOCKET_POOL', 'SOCKET_POOL_BOUND_TO_CONNECT_JOB',
    'SOCKET_POOL_BOUND_TO_SOCKET', 'SOCKET_POOL_CONNECT_JOB_CREATED',
    'SOCKET_POOL_CONNECT_JOB_CONNECT', 'CONNECT_JOB', 'CONNECT_JOB_SET_SOCKET',
    'CONNECT_JOB_TIMED_OUT', 'TRANSPORT_CONNECT_JOB_CONNECT',
    'HTTP_PROXY_CONNECT_JOB_CONNECT', 'HTTP_PROXY_CONNECT_JOB_TUNNEL_CONNECT',
    'TCP_CONNECT', 'TCP_CONNECT_ATTEMPT', 'SOCKET_ALIVE',
    'SOCKET_READ_ERROR', 'SOCKET_WRITE_ERROR', 'SOCKET_CLOSED',
    'PROXY_RESOLUTION_SERVICE', 'PROXY_RESOLUTION_SERVICE_RESOLVED_PROXY_LIST',
    'PROXY_LIST_FALLBACK', 'HTTP_TRANSACTION_TUNNEL_SEND_REQUEST',
    'HTTP_TRANSACTION_TUNNEL_READ_HEADERS', 'HTTP_TRANSACTION_RESTART_AFTER_ERROR',
})
SOURCES = frozenset({
    'NONE', 'URL_REQUEST', 'CONNECT_JOB', 'SOCKET', 'HTTP_STREAM_JOB',
    'HTTP_STREAM_REQUEST', 'HTTP_STREAM_JOB_CONTROLLER', 'PROXY_CLIENT_SOCKET',
    'TRANSPORT_CONNECT_JOB', 'HTTP_PROXY_CONNECT_JOB',
})
PHASES = frozenset({'PHASE_NONE', 'PHASE_BEGIN', 'PHASE_END'})
METHODS = frozenset({'GET', 'HEAD', 'OPTIONS', 'POST'})


def integer(value, low=0, high=2**53 - 1):
    return value if type(value) is int and low <= value <= high else None


def unavailable(reason):
    return {'available': False, 'complete': False, 'reason': reason}


def _mapping(constants, key, allowed):
    raw = constants.get(key, {})
    if not isinstance(raw, dict):
        raise ValueError('invalid constants')
    return {value: name for name, value in raw.items()
            if name in allowed and integer(value) is not None}


def reduce_netlog(data, endpoint, hosts, *, limit=MAX_EVENTS):
    if not isinstance(data, dict) or not isinstance(data.get('constants'), dict):
        raise ValueError('invalid NetLog')
    events = data.get('events')
    if not isinstance(events, list) or len(events) > 200_000:
        raise ValueError('invalid events')
    if type(limit) is not int or not 1 <= limit <= MAX_EVENTS:
        raise ValueError('invalid limit')
    constants = data['constants']
    names = _mapping(constants, 'logEventTypes', EVENTS)
    sources = _mapping(constants, 'logSourceType', SOURCES)
    phases = _mapping(constants, 'logEventPhase', PHASES)
    if not names or not phases or not sources:
        raise ValueError('unknown schema')
    proxy = urlsplit(endpoint)
    owned_address = proxy.hostname + ':' + str(proxy.port)
    rows = deque(maxlen=limit)
    counts = Counter()
    seen = malformed = 0
    first_time = None
    for event in events:
        if not isinstance(event, dict):
            malformed += 1
            continue
        params = event.get('params', {})
        source = event.get('source', {})
        if not isinstance(params, dict) or not isinstance(source, dict):
            malformed += 1
            continue
        code = integer(params.get('net_error'), -(2**31), 0)
        event_type = integer(event.get('type'))
        name = names.get(event_type, 'unknown')
        if name == 'unknown' and (code is None or code == 0):
            continue
        phase = phases.get(integer(event.get('phase')), 'unknown')
        source_type = sources.get(integer(source.get('type')), 'unknown')
        source_id = integer(source.get('id'))
        dependency = params.get('source_dependency', {})
        if not isinstance(dependency, dict):
            dependency = {}
        dependent_id = integer(dependency.get('id'))
        raw_time = event.get('time')
        try:
            clock = float(raw_time) if type(raw_time) in (str, int, float) else float('nan')
            if not math.isfinite(clock) or not 0 <= clock <= 1e15:
                raise ValueError('invalid time')
            if first_time is None:
                first_time = clock
            elapsed = round(clock - first_time, 3)
        except (ValueError, OverflowError):
            elapsed = None
        addresses = [params[key] for key in ('address', 'remote_address', 'endpoint')
                     if isinstance(params.get(key), str)]
        url = params.get('url')
        fixture = None
        if isinstance(url, str):
            try:
                fixture = urlsplit(url).hostname in hosts
            except ValueError:
                fixture = False
        row = {'event': name, 'phase': phase, 'source': source_id,
               'source_type': source_type, 'elapsed_ms': elapsed,
               'net_error': code, 'os_error': integer(params.get('os_error'), -(2**31), 2**31 - 1),
               'dependency': dependent_id,
               'dependency_type': sources.get(integer(dependency.get('type')), 'unknown') if dependent_id is not None else None,
               'owned_proxy_endpoint': owned_address in addresses if addresses else None,
               'fixture_host': fixture,
               'method': params.get('method') if isinstance(params.get('method'), str) and params['method'] in METHODS else None}
        rows.append(row)
        counts[name] += 1
        seen += 1
    # The browser's bounded ring may already have lost early events. Do not
    # advertise complete capture even when our own reducer did not truncate.
    return {'available': True, 'complete': False, 'capture': 'bounded_browser_log',
            'events_read': len(events), 'selected': seen, 'dropped': max(0, seen-limit),
            'malformed': malformed, 'counts': dict(counts), 'events': list(rows)}


def read_netlog(path, endpoint, hosts):
    try:
        with path.open('rb') as stream:
            raw = stream.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            return unavailable('size_limit')
        return reduce_netlog(json.loads(raw), endpoint, hosts)
    except (OSError, ValueError, TypeError, KeyError, OverflowError, RecursionError):
        return unavailable('missing_or_invalid')


class NativeNetLog:
    def __init__(self, root, endpoint, hosts):
        require_ci()
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.directory = Path(tempfile.mkdtemp(prefix='owned-', dir=self.root)).resolve()
        self.path = self.directory / 'netlog.json'
        self.endpoint, self.hosts = endpoint, frozenset(hosts)
        self.result = None

    def options(self, options):
        args = options.get('args', [])
        if any(arg.startswith('--log-net-log') or arg.startswith('--net-log-') for arg in args):
            raise ValueError('existing logging options')
        return {**options, 'args': [*args, '--log-net-log=' + str(self.path),
            '--net-log-capture-mode=Default', '--net-log-max-size-mb=16']}

    def finish(self):
        if self.result is not None and self.result.get('private_cleanup') == 'removed':
            return self.result
        if self.result is None:
            try:
                self.result = read_netlog(self.path, self.endpoint, self.hosts)
            except Exception:
                self.result = unavailable('observer_error')
        self.result['cleanup_attempts'] = self.result.get('cleanup_attempts', 0) + 1
        # Only our newly created directory, never a browser/user profile.
        try:
            resolved = self.directory.resolve()
            if resolved.parent != self.root or self.directory.is_symlink():
                raise ValueError('unexpected directory')
            shutil.rmtree(resolved)
            self.result['private_cleanup'] = 'removed'
        except (OSError, ValueError):
            self.result['private_cleanup'] = 'pending'
        return self.result
