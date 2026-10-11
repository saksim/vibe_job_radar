"""Bounded, local acquisition observations with explicit unknowns.

One observation belongs to an owned backend and one service action. References
bind detail starts to that observation without changing detail_attempt_history
v1. Reports retain the original snapshot; no old attempt is backfilled.
"""
from __future__ import annotations

import copy
import re
import uuid

from .. import runtime_identity
from ..network_policy import NetworkPolicy
from ..utils import parse_time, utc_now
from .contracts import CrawlError
from . import attempt_history

KEY = 'acquisition_environment_history'
MAX_OBSERVATIONS = 200
ACTIONS = {'search', 'capture', 'more', 'collect', 'resume', 'login', 'login_password',
           'resume_returned_detail', 'resume_returned_search'}
TRANSPORTS = {'unknown', 'system_route', 'loopback_http_proxy', 'loopback_socks5_proxy',
              'vm_host_http_proxy', 'vm_host_socks5_proxy', 'pac_by_host', 'pac_by_url', 'unavailable'}
DNS = {'unknown', 'system', 'fake_ip_doh', 'public_doh'}


def new_history(*, legacy=False, checkpoint_at=None):
    return {'version': 1, 'origin': 'legacy_partial' if legacy else 'task_creation',
            'checkpoint_at': checkpoint_at or utc_now(), 'observations': [], 'attempts': []}


def _token(value, limit=100):
    return isinstance(value, str) and bool(re.fullmatch('[a-zA-Z0-9_.+-]{1,'+str(limit)+'}', value))


def adapter_version(adapter):
    value = getattr(adapter, 'version', None)
    if type(value) is int:
        value = str(value)
    return value if _token(value) else 'unknown'


def validate(state):
    if KEY not in state or state[KEY] is None:
        return None
    try:
        h = state[KEY]
        if (not isinstance(h, dict) or set(h) != {'version', 'origin', 'checkpoint_at', 'observations', 'attempts'}
                or type(h['version']) is not int or h['version'] != 1
                or h['origin'] not in {'task_creation', 'legacy_partial'}):
            raise ValueError()
        parse_time(h['checkpoint_at'])
        observations, refs = h['observations'], h['attempts']
        if (not isinstance(observations, list) or len(observations) > MAX_OBSERVATIONS
                or not isinstance(refs, list) or len(refs) > attempt_history.MAX_ATTEMPTS):
            raise ValueError()
        for i, obs in enumerate(observations, 1):
            if (not isinstance(obs, dict) or set(obs) != {'sequence', 'observed_at', 'action', 'session_id',
                    'adapter_key', 'adapter_version', 'backend', 'browser_channel', 'browser_version', 'runtime', 'network'}
                    or type(obs['sequence']) is not int or obs['sequence'] != i or obs['action'] not in ACTIONS):
                raise ValueError()
            parse_time(obs['observed_at'])
            if not isinstance(obs['session_id'], str) or not re.fullmatch('[a-f0-9]{32}', obs['session_id']):
                raise ValueError()
            if not _token(obs['adapter_key']) or not _token(obs['adapter_version']):
                raise ValueError()
            if obs['backend'] not in {'native', 'bridge', 'unknown'} or obs['browser_channel'] not in {'bundled', 'msedge', 'chrome', 'unknown'}:
                raise ValueError()
            if not _token(obs['browser_version'], 80):
                raise ValueError()
            runtime_identity.validate(obs['runtime'])
            if parse_time(obs['runtime']['started_at']) > parse_time(obs['observed_at']):
                raise ValueError()
            net = obs['network']
            if (not isinstance(net, dict) or set(net) != {'transport', 'dns', 'policy_id', 'authenticated_proxy',
                                                        'bypass_configured', 'physical_route'}
                    or net['transport'] not in TRANSPORTS or net['dns'] not in DNS
                    or net['physical_route'] != 'unknown'
                    or type(net['authenticated_proxy']) is not bool or type(net['bypass_configured']) is not bool
                    or net['policy_id'] is not None and (not isinstance(net['policy_id'], str)
                        or not re.fullmatch('[a-f0-9]{16}', net['policy_id']))):
                raise ValueError()
        prior = 0
        attempts = (state.get(attempt_history.KEY) or {}).get('attempts', [])
        for ref in refs:
            if (not isinstance(ref, dict) or set(ref) != {'attempt', 'observation'}
                    or type(ref['attempt']) is not int or not prior < ref['attempt'] <= len(attempts)
                    or ref['observation'] is not None and (type(ref['observation']) is not int
                        or not 1 <= ref['observation'] <= len(observations))):
                raise ValueError()
            if ref['observation'] is not None:
                obs = observations[ref['observation']-1]
                if parse_time(obs['observed_at']) > parse_time(attempts[ref['attempt']-1]['started_at']):
                    raise ValueError()
            prior = ref['attempt']
        return h
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
        raise CrawlError('environment_history_invalid') from None


def checkpoint(state, before, after):
    h = validate(state)
    if h is not None:
        if h['checkpoint_at'] != before:
            h['origin'] = 'legacy_partial'
        h['checkpoint_at'] = after


def snapshot(state):
    h = copy.deepcopy(validate(state))
    if h and h['checkpoint_at'] != state.get('updated_at'):
        h['origin'] = 'legacy_partial'
    return h


def _history(state):
    h = validate(state)
    if h is None:
        h = state[KEY] = new_history(legacy=True, checkpoint_at=state.get('updated_at'))
    elif h['checkpoint_at'] != state.get('updated_at'):
        h['origin'] = 'legacy_partial'
    return h


def network(policy):
    result = {'transport': 'unknown', 'dns': 'unknown', 'policy_id': None,
              'authenticated_proxy': False, 'bypass_configured': False, 'physical_route': 'unknown'}
    if type(policy) is not NetworkPolicy:
        return result
    # Read already-bound configuration. Never resolve a host, run PAC, or copy
    # _identity()/addresses/credentials/scripts/bypass values into evidence.
    result.update(transport=('unavailable' if policy.error else
        ('pac_by_url' if policy.pac_url_mode else 'pac_by_host') if policy.pac is not None
        else policy.transport_name(policy.proxy)),
        dns='public_doh' if policy.public_dns else 'fake_ip_doh' if policy.encrypted_dns else 'system',
        policy_id=policy.fingerprint, bypass_configured=bool(policy.bypass),
        authenticated_proxy=bool(policy.proxy and policy.proxy.credentials is not None))
    return result


def require_capacity(state):
    h = validate(state)
    if h is not None and len(h['observations']) >= MAX_OBSERVATIONS:
        raise CrawlError('environment_history_limit')


def observe(state, backend, adapter, action):
    from .browser import PlaywrightBackend
    from .native_browser import NativeBackend
    h = _history(state)
    if len(h['observations']) >= MAX_OBSERVATIONS:
        raise CrawlError('environment_history_limit')
    known = type(backend) in (PlaywrightBackend, NativeBackend)
    # Same live object donated to another task retains its session identity.
    session = backend.__dict__.setdefault('_acquisition_environment_id', uuid.uuid4().hex) if known else uuid.uuid4().hex
    health = backend.startup_report if known else {}
    version = getattr(backend.browser, 'version', 'unknown') if known else 'unknown'
    runtime = runtime_identity.snapshot()
    obs = {'sequence': len(h['observations'])+1, 'observed_at': utc_now(), 'action': action,
           'session_id': session, 'adapter_key': adapter.key,
           'adapter_version': adapter_version(adapter),
           'backend': 'native' if type(backend) is NativeBackend else 'bridge' if known else 'unknown',
           'browser_channel': health.get('browser_channel', 'unknown'),
           'browser_version': runtime_identity._version(version),
           'runtime': runtime,
           'network': network(getattr(getattr(backend, 'wire', None), 'network_policy', None)) if known else network(None)}
    h['observations'].append(obs)
    validate(state)
    return obs['sequence']


def bind_attempt(state, sequence, observation):
    h = _history(state)
    h['attempts'].append({'attempt': sequence, 'observation': observation})
    validate(state)


def extends(previous, current):
    if previous is None:
        return
    if (current is None or previous['origin'] == 'legacy_partial' and current['origin'] != 'legacy_partial'
            or current['observations'][:len(previous['observations'])] != previous['observations']
            or current['attempts'][:len(previous['attempts'])] != previous['attempts']):
        raise CrawlError('environment_history_invalid')


def require_report(current, audit):
    saved = validate(audit)
    extends(saved, current)
    if current and current['attempts'] and saved is None:
        raise CrawlError('environment_history_invalid')
    current_refs = {r['attempt']: r['observation'] for r in (current or {}).get('attempts', [])}
    saved_refs = {r['attempt']: r['observation'] for r in (saved or {}).get('attempts', [])}
    for attempt in (audit.get(attempt_history.KEY) or {}).get('attempts', []):
        seq = attempt['sequence']
        if seq in current_refs and (seq not in saved_refs or current_refs[seq] != saved_refs[seq]):
            raise CrawlError('environment_history_invalid')


def _compare_os(declared, runtime):
    # New CLI plans use system/release. Older platform.platform() labels are
    # retained; compare only fields that can be recovered unambiguously.
    declared = str(declared)
    if '/' in declared:
        label, release = declared.split('/', 1)
        family = label if label in {'Windows', 'Linux', 'Darwin'} else None
    else:
        label, _, suffix = declared.partition('-')
        family = {'Windows': 'Windows', 'Linux': 'Linux',
                  'Darwin': 'Darwin', 'macOS': 'Darwin'}.get(label)
        release = None
        if label in {'Windows', 'Darwin'}:
            release = suffix.split('-', 1)[0]
        elif label == 'Linux':
            # Kernel releases may themselves contain hyphens. Do not accept
            # a matching prefix as proof that different kernels are equal.
            match = re.fullmatch(r'(.+?)-(?:x86_64|AMD64|aarch64|arm64|i[3-6]86|'
                                 r'armv[5-8][a-z0-9]*|ppc64le|s390x|riscv64)(?:-.*)?', suffix)
            release = match.group(1) if match else None
    if family is None:
        return None, 'planner_os_not_comparable'
    if runtime['os'] != 'unknown' and family != runtime['os']:
        return 'os', None
    if (not release or not release[:1].isdigit() or runtime['os_release'] == 'unknown'
            or runtime['os'] == 'unknown'):
        return None, 'os_release_not_comparable'
    if release != runtime['os_release']:
        return 'os_release', None
    return None, None


def assessment(plan, history, attempts):
    """No certification: source/browser comparison and missing refs are facts."""
    observations = (history or {}).get('observations', [])
    refs = {r['attempt']: r['observation'] for r in (history or {}).get('attempts', [])}
    total = (attempts or {}).get('attempts', [])
    matched = set()
    mismatches, unknown = set(), set()
    if not history or history['origin'] != 'task_creation':
        unknown.add('runtime_environment_history_incomplete')
    if not observations:
        unknown.add('runtime_environment')
    for obs in observations:
        runtime = obs['runtime']; source = runtime['source']
        net = obs['network']
        expected_transports = {
            'direct': {'system_route'}, 'system': {'system_route'},
            'static_http': {'loopback_http_proxy', 'vm_host_http_proxy'},
            'static_socks5': {'loopback_socks5_proxy', 'vm_host_socks5_proxy'},
        }.get(plan.get('network_mode'))
        missing = (runtime['freshness'] != 'unchanged' or source['kind'] == 'unknown'
                   or obs['backend'] == 'unknown' or obs['browser_channel'] == 'unknown'
                   or plan['browser_version'] == 'unknown' or obs['browser_version'] == 'unknown'
                   or runtime['os'] == 'unknown' or runtime['os_release'] == 'unknown'
                   or runtime['python_version'] == 'unknown' or runtime['playwright_version'] == 'unknown'
                   or obs['adapter_key'] == 'unknown' or obs['adapter_version'] == 'unknown'
                   or net['transport'] in {'unknown', 'unavailable'} or net['dns'] == 'unknown'
                   or net['policy_id'] is None)
        if missing:
            unknown.add('runtime_environment')
        if expected_transports is None:
            missing = True
            unknown.add('network_mode_not_comparable')
        # Missing one observation never erases a difference in another known
        # field. Compare configured transport only, not an unobserved VPN/TUN
        # route or per-request PAC/bypass decision.
        fields = []
        os_difference, os_gap = _compare_os(plan.get('planner_os'), runtime)
        if os_difference:
            fields.append(os_difference)
        if os_gap:
            missing = True
            unknown.add(os_gap)
        expected = plan.get('source_content_sha256')
        if source['kind'] != 'unknown' and (
                source['content_sha256'] != expected if expected else source['raw_sha256'] != plan['source_sha256']):
            fields.append('source')
        if obs['backend'] != 'unknown' and obs['backend'] != plan['backend']:
            fields.append('backend')
        if obs['browser_channel'] != 'unknown' and obs['browser_channel'] != plan['browser']:
            fields.append('browser')
        if (plan['browser_version'] != 'unknown' and obs['browser_version'] != 'unknown'
                and obs['browser_version'] != plan['browser_version']):
            fields.append('browser_version')
        if ((obs['adapter_key'] != 'unknown' and obs['adapter_key'] != plan['platform'])
                or (obs['adapter_version'] != 'unknown' and obs['adapter_version'] != plan['adapter_version'])):
            fields.append('adapter')
        if expected_transports is not None and net['transport'] not in {'unknown', 'unavailable'}:
            if net['transport'] not in expected_transports:
                fields.append('network_mode')
        if fields:
            mismatches.update(fields)
        elif not missing:
            matched.add(obs['sequence'])
    bound = sum(refs.get(a['sequence']) is not None for a in total)
    matching = sum(refs.get(a['sequence']) in matched for a in total)
    if bound < len(total):
        unknown.add('detail_attempt_environment_missing')
    if mismatches:
        unknown.add('runtime_environment_differs_from_plan')
    return {'observations': len(observations), 'detail_attempts': len(total),
            'bound_detail_attempts': bound, 'matching_detail_attempts': matching,
            'mismatched_fields': sorted(mismatches), 'gaps': sorted(unknown),
            'scope': 'Local source/startup and applied browser/session configuration; physical VPN/TUN route and per-request PAC decisions remain unknown.'}
