"""Opt-in, bounded metadata observations. Never a source of access decisions.

A recorder is bound explicitly to a backend/transport: Playwright callbacks need
not inherit ContextVars. No headers, bodies, exception text or raw URLs are kept.
"""
from __future__ import annotations

from collections import deque
from contextlib import contextmanager
from functools import wraps
import copy
import hashlib
import importlib.metadata
import math
from pathlib import Path
import platform
import re
import threading
import time
from urllib.parse import parse_qsl, urlsplit
import uuid

PLANNING_BASELINE = '108941f2e1bb64c032170aa2d178aa5a38e3b58d'
STAGES = frozenset({'task', 'browser_session', 'listing', 'list_parse', 'collection',
    'detail_navigation', 'detail_identity', 'detail_parse', 'persist', 'report', 'network_policy',
    'route', 'http_request', 'robots', 'navigation', 'login', 'pagination', 'unknown'})
RESOURCES = frozenset({'document', 'stylesheet', 'script', 'image', 'media', 'font',
    'xhr', 'fetch', 'websocket', 'other', 'unknown'})
CODES = frozenset('''operation_error network_error dns_error non_public_address
    workspace_proxy_environment_conflict
    pac_unavailable pac_invalid_script pac_invalid_result pac_timeout pac_busy
    pac_revoked pac_failed pac_cache_limit pac_file_invalid
    read_transient_failure read_retry_wait read_retry_exhausted read_retry_unavailable
    read_retry_state_invalid read_retry_after_invalid
    tls_verification_failed tls_handshake_failed http_401 http_403 http_429
    robots_denied robots_unavailable resource_domain_blocked write_not_allowed native_optional_request_blocked
    method_blocked redirect_requires_attention login_origin_changed invalid_url
    wrong_platform credential_url not_job_url not_job_list manual_required
    login_form_changed login_credentials_rejected login_password_submitted job_unavailable invalid_page_observation
    structure_changed invalid_job_data response_too_large request_too_large
    unexpected_compression request_headers_invalid request_headers_conflict remote_server_error site_stopped
    page_not_ready browser_closed browser_missing playwright_missing
    playwright_incompatible browser_executable_missing browser_launch_failed
    browser_choice_invalid paused rate_wait publisher_wait cooldown hourly_limit
    daily_limit list_page_limit login_rate_limited rate_storage_error clock_rollback
    publisher_policy_invalid automatic_resume_unavailable encrypted_dns_consent_required encrypted_dns_tls_failed
    encrypted_dns_unavailable encrypted_dns_invalid_response encrypted_dns_disabled
    encrypted_dns_non_public_answer encrypted_dns_route_failed encrypted_dns_timeout
    encrypted_dns_http_rejected encrypted_dns_refused encrypted_dns_name_not_found
    encrypted_dns_empty_answer encrypted_dns_expired_answer encrypted_dns_cooldown
    encrypted_dns_budget encrypted_dns_clock_rollback
    local_proxy_configuration_conflict local_proxy_configuration_invalid
    local_proxy_credentials_invalid local_proxy_credentials_require_explicit local_proxy_auth_failed
    local_proxy_connection_failed local_socks_configuration_invalid
    vm_proxy_configuration_invalid vm_proxy_explicit_required vm_proxy_connection_failed vm_proxy_timeout
    local_socks_auth_unsupported local_socks_protocol_error local_socks_timeout
    local_socks_connection_failed local_socks_request_rejected local_socks_truncated_reply
    robots_response_html robots_http_unavailable robots_encoding_invalid robots_file_absent
    robots_rules_observed robots_extensions_observed robots_no_rules_observed
    robots_inspection_truncated no_cards no_records native_administrator_blocked native_contract_unavailable
    native_contract_invalid native_operation_unreviewed native_surface_unsupported
    native_protocol_error native_proxy_auth_failed native_policy_changed native_observation_limit native_page_cleared
    native_unaccounted_response native_business_response_invalid checkpoint_incompatible checkpoint_records_missing batch_identity_unsupported search_scope_changed'''.split())
WAITS = frozenset({'paused', 'rate_wait', 'publisher_wait', 'cooldown', 'http_429',
    'hourly_limit', 'daily_limit', 'login_rate_limited'})
LOCAL_POLICIES = {
    'resource_domain_blocked': 'resource_domains', 'write_not_allowed': 'post_auth_only',
    'method_blocked': 'http_methods', 'robots_denied': 'robots_rules',
    'robots_unavailable': 'robots_availability', 'non_public_address': 'public_address',
    'invalid_url': 'url_validation', 'wrong_platform': 'platform_domains',
    'credential_url': 'credential_query', 'login_origin_changed': 'login_domains',
    'redirect_requires_attention': 'redirect_policy', 'unexpected_compression': 'identity_encoding',
    'response_too_large': 'response_size', 'request_too_large': 'request_size',
    'request_headers_invalid': 'header_validation', 'request_headers_conflict': 'header_validation',
    'paused': 'cancellation', 'native_operation_unreviewed': 'native_site_contract',
    'native_optional_request_blocked': 'native_optional_dependency',
    'native_surface_unsupported': 'native_surface_policy', 'native_policy_changed': 'workspace_policy',
    'native_observation_limit': 'native_response_limit',
}
# Only fixed path components/query NAMES survive; unknown segments and names
# might themselves contain a credential. Values/fragments/userinfo never survive.
PATH_PARTS = frozenset({'robots.txt', 'web', 'geek', 'job', 'job_detail', 'user',
    'zhaopin', 'pc', 'search', 'jobdetail', 'login', 'api', 'v1', 'v2', 'jobs'})
QUERY_NAMES = frozenset({'q', 'query', 'keyword', 'key', 'page', 'pageSize',
    'currentPage', 'jobId', 'city', 'industry', 'offset', 'limit'})


def safe_target(url):
    """Deliberately lossy, bounded representation; no hashed credential values."""
    result = {'host': '', 'path_template': '', 'query_names': []}
    if not isinstance(url, str) or len(url) > 8192:
        return result
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or '').lower()
        if parsed.scheme not in {'http', 'https'} or not re.fullmatch(r'[a-z0-9.-]{1,253}', host):
            return result
        result['host'] = host
        parts = parsed.path.split('/')[1:17]
        result['path_template'] = '/' + '/'.join(p if p in PATH_PARTS else ':segment' for p in parts)
        if len(parsed.path.split('/')) > 17:
            result['path_template'] += '/:truncated'
        pairs = parse_qsl(parsed.query, keep_blank_values=True, max_num_fields=64)
        result['query_names'] = sorted({k if k in QUERY_NAMES else ':redacted' for k, _ in pairs})
    except (ValueError, UnicodeError):
        pass
    return result


def runtime_metadata():
    """Local code identity, not an assertion of remote CI or live-site success."""
    from .._version import __version__
    try:
        version = importlib.metadata.version('playwright')
    except importlib.metadata.PackageNotFoundError:
        version = None
    hashes = {}
    for name in ('diagnostic_trace.py', 'browser.py', 'transport.py', 'service.py',
                 'native_browser.py', 'native_policy.py', 'native_tunnel.py'):
        try:
            hashes[name] = hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest()
        except OSError:
            hashes[name] = None
    return {'planning_baseline': PLANNING_BASELINE, 'package_version': __version__,
            'python': platform.python_version(), 'system': platform.system(),
            'playwright': version if version and re.fullmatch(r'[0-9.a-z+-]{1,40}', version) else None,
            'code_sha256': hashes, 'live_verification': 'not_established_by_diagnostics'}


class DiagnosticTrace:
    """One ephemeral recording session per task; snapshots contain safe scalars."""
    def __init__(self, task_id, site, *, adapter_version='unknown', browser='unknown',
                 max_events=200, clock=time.time):
        if not isinstance(task_id, str) or not re.fullmatch(r'[a-f0-9]{32}', task_id):
            raise ValueError('invalid diagnostic task id')
        if type(max_events) is not int or not 8 <= max_events <= 500:
            raise ValueError('invalid diagnostic capacity')
        self.task_id, self.session_id = task_id, uuid.uuid4().hex
        self.site = site if isinstance(site, str) and re.fullmatch(r'[a-z0-9_]{2,32}', site) else 'unknown'
        self.adapter_version = adapter_version if isinstance(adapter_version, str) and re.fullmatch(r'[0-9.]{1,20}', adapter_version) else 'unknown'
        self.browser = browser if browser in {'bundled', 'msedge'} else 'unknown'
        self.browser_version = None
        self.runtime = runtime_metadata()
        self.events = deque(maxlen=max_events)
        self._lock, self._clock = threading.RLock(), clock
        self._sequence = self.dropped = self.observer_errors = 0
        self._frames = []
        self.first_failure = self.first_content_candidate = None
        self.enabled = True

    def _emit(self, frame, outcome):
        if not self.enabled:
            return
        timestamp = self._clock()
        if not math.isfinite(timestamp):
            raise ValueError('invalid diagnostic timestamp')
        self._sequence += 1
        event = {**frame, 'seq': self._sequence, 'time': round(timestamp, 3), 'outcome': outcome}
        if len(self.events) == self.events.maxlen:
            self.dropped += 1
        self.events.append(event)
        if outcome == 'failure':
            if self.first_failure is None:
                self.first_failure = copy.deepcopy(event)
            if event['impact'] != 'optional' and self.first_content_candidate is None:
                self.first_content_candidate = copy.deepcopy(event)

    def begin(self, stage, actor, *, url='', resource='unknown', method='', impact='unknown', operation='', entity=''):
        with self._lock:
            operation = operation if operation in {'search', 'login', 'login_password', 'capture', 'more', 'collect', 'pause_idle', 'close', 'resume'} else ''
            entity = entity if isinstance(entity, str) and re.fullmatch(r'[a-f0-9]{24}', entity) else ''
            parent = self._frames[-1] if self._frames else {}
            frame = {'operation': operation or parent.get('operation', ''), 'entity': entity or parent.get('entity', ''), 'stage': stage if stage in STAGES else 'unknown',
                     'actor': actor if actor in {'service', 'browser', 'transport'} else 'unknown',
                     **safe_target(url), 'resource': resource if resource in RESOURCES else 'unknown',
                     'method': method if method in {'GET', 'HEAD', 'POST', 'OPTIONS', 'PUT', 'PATCH', 'DELETE'} else '',
                     'impact': impact if impact in {'required_by_backend', 'optional', 'unknown'} else 'unknown',
                     'span_id': self._sequence + 1,
                     'parent_span_id': self._frames[-1]['span_id'] if self._frames else None,
                     'parent_stage': self._frames[-1]['stage'] if self._frames else None,
                     'status': None, 'code': '', 'local_block': None, 'policy': ''}
            self._frames.append(frame)
            try:
                self._emit(frame, 'started')
            except Exception:
                self._frames.pop()
                raise
            return frame

    def mark(self, *, code=None, status=None):
        with self._lock:
            if not self._frames:
                return
            frame = self._frames[-1]
            if type(status) is int and 100 <= status <= 599:
                frame['status'] = status
            if code is not None:
                code = code if isinstance(code, str) and code in CODES else 'operation_error'
                frame['code'] = code
                if code in {'http_401', 'http_403', 'http_429'}:
                    frame['status'] = int(code[-3:])
                frame['local_block'] = True if code in LOCAL_POLICIES else False if code in {'http_401', 'http_403', 'http_429'} else None
                frame['policy'] = LOCAL_POLICIES.get(code, 'shared_quota' if code in WAITS else '')

    def note(self, code):
        with self._lock:
            if self._frames:
                frame = {**self._frames[-1], 'code': code if code in CODES else 'operation_error'}
                self._emit(frame, 'observation')

    def finish(self, frame, error=None):
        with self._lock:
            if error is not None:
                self.mark(code=getattr(error, 'code', 'operation_error'))
            code = frame['code']
            outcome = 'waiting' if code in WAITS else 'failure' if code else 'completed'
            try:
                self._emit(frame, outcome)
            finally:
                if self._frames and self._frames[-1] is frame:
                    self._frames.pop()

    def set_browser_version(self, value):
        with self._lock:
            self.browser_version = value if isinstance(value, str) and re.fullmatch(r'[0-9.]{1,40}', value) else None

    def disable(self):
        with self._lock:
            self.enabled = False
            self.events.clear()
            self.first_failure = self.first_content_candidate = None

    def snapshot(self):
        with self._lock:
            return copy.deepcopy({'schema_version': 1, 'trace_id': self.task_id,
                'recording_id': self.session_id, 'enabled': self.enabled,
                'platform': self.site, 'adapter_version': self.adapter_version, 'browser': self.browser,
                'browser_version': self.browser_version,
                'runtime': self.runtime, 'events': list(self.events), 'dropped_events': self.dropped,
                'observer_errors': self.observer_errors, 'first_failure': self.first_failure,
                'first_content_candidate': self.first_content_candidate,
                'scope': 'Opt-in in-memory metadata only; not a HAR, root-cause proof or live-site certification. '
                         'Required means the existing backend classification, not proven business relevance. '
                         'Recordings are lost on service exit; download after local preview to retain evidence.'})


def notify(trace, entrypoint, **kwargs):
    """Observer failures cannot change a request, quota, exception or result."""
    if type(trace) is not DiagnosticTrace:
        return None
    try:
        return getattr(trace, entrypoint)(**kwargs)
    except Exception:
        trace.observer_errors += 1
        return None


@contextmanager
def observe(trace, stage, actor='service', **metadata):
    frame = notify(trace, 'begin', stage=stage, actor=actor, **metadata)
    try:
        yield
    except BaseException as exc:
        if frame is not None:
            notify(trace, 'finish', frame=frame, error=exc)
        raise
    else:
        if frame is not None:
            notify(trace, 'finish', frame=frame)


def traced(stage, actor, *, state_index=None, url=False, route=False):
    """Instrument existing methods without changing their arguments or decisions."""
    def decorate(function):
        @wraps(function)
        def wrapped(self, *args, **kwargs):
            trace, metadata = None, {}
            try:
                trace = (self._trace_for(args[state_index] if len(args) > state_index else kwargs['state'])
                         if state_index is not None else getattr(self, '_diagnostics', None))
                if stage == 'task':
                    metadata['operation'] = args[0] if args else kwargs.get('action', '')
                if url:
                    metadata['url'] = args[0] if args else kwargs.get('url', '')
                    if stage == 'http_request':
                        metadata['method'] = args[1] if len(args) > 1 else kwargs.get('method', 'GET')
                    metadata['impact'] = 'required_by_backend' if kwargs.get('required', True) else 'optional'
                if route:
                    request = args[0].request
                    kind = request.resource_type
                    metadata = {'url': request.url, 'resource': kind, 'method': request.method,
                                'impact': 'required_by_backend' if kind in {'document', 'xhr', 'fetch'} else 'optional'}
            except Exception:
                trace = None
            effective_stage = 'login' if stage == 'navigation' and kwargs.get('authentication') else stage
            with observe(trace, effective_stage, actor, **metadata):
                return function(self, *args, **kwargs)
        return wrapped
    return decorate


def observe_robots(trace, result):
    """Passive categorization of the ALREADY fetched response; no policy changes."""
    if type(trace) is not DiagnosticTrace:
        return
    try:
        if result.status in {404, 410}:
            code = 'robots_file_absent'
        elif result.status != 200:
            code = 'robots_http_unavailable'
        elif 'html' in result.headers.get('content-type', '').lower():
            code = 'robots_response_html'
        elif len(result.body) > 524288:
            code = 'robots_inspection_truncated'
        else:
            try:
                text = result.body.decode('utf-8-sig')
            except UnicodeError:
                code = 'robots_encoding_invalid'
            else:
                rules = [line.split('#', 1)[0].strip() for line in text.splitlines()]
                if not any(re.match(r'(?i)user-agent\s*:', line) for line in rules):
                    code = 'robots_no_rules_observed'
                elif any(re.match(r'(?i)(?:allow|disallow)\s*:.*[\*$]', line) for line in rules):
                    code = 'robots_extensions_observed'
                else:
                    code = 'robots_rules_observed'
        notify(trace, 'note', code=code)
    except Exception:
        trace.observer_errors += 1
