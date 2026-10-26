"""One isolated CI diagnosis; never release qualification or a successful retry."""
import json
import os
from pathlib import Path
import sys
import time
from unittest.mock import patch

BASE = 'ecf075c0d4ff1fcfc7171e5923ca78a2cd381633'
BRANCH = 'codex/native-proxy-investigation-0203'
MAX_BACKENDS = 8
MAX_SNAPSHOTS = 48


def main():
    if not (os.name == 'nt' and os.environ.get('CI') == 'true'
            and os.environ.get('GITHUB_ACTIONS') == 'true'
            and os.environ.get('GITHUB_REPOSITORY') == 'saksim/vibe_job_radar'
            and os.environ.get('GITHUB_EVENT_NAME') == 'push'
            and os.environ.get('GITHUB_REF') == 'refs/heads/' + BRANCH
            and os.environ.get('GITHUB_RUN_ATTEMPT') == '1'
            and sys.argv[1:] == ['--controlled', '--channel', 'msedge']):
        raise SystemExit('Requires the single isolated GitHub Windows Edge headless diagnostic run.')
    import run_native_proxy_auth as acceptance
    from vibe_job_radar.guided import native_browser
    from native_guard_evidence import install_guard_observer, unavailable
    from native_proxy_evidence import STAGES, CODES, ERROR_TYPES

    out = Path(__file__).resolve().parents[1] / 'browser-acceptance' / 'native-proxy-investigation'
    out.mkdir(parents=True, exist_ok=True)
    original_factory = native_browser.NativeTunnel
    original_open = acceptance.NativeBackend.open
    original_checkpoint = acceptance.update_proxy_evidence
    guards, snapshots = [], []
    state = {'stage': 'unknown', 'capture_failed': False, 'overflow': False}
    started = time.monotonic()
    outcome = 'not_completed'
    suite_calls = 0

    def fixed_code(value):
        return value if isinstance(value, str) and value in CODES | {''} else 'unknown'

    def observed_factory(*args, **kwargs):
        guard = original_factory(*args, **kwargs)
        # Original constructor has bound/listened and started its thread;
        # _launch_options has not returned, so this browser has not launched.
        try:
            if len(guards) >= MAX_BACKENDS:
                state.update(capture_failed=True, overflow=True)
            else:
                probe = install_guard_observer(guard)
                guards.append((guard, probe, state['stage']))
                if probe is None:
                    state['capture_failed'] = True
        except Exception:
            state['capture_failed'] = True
        return guard

    def capture(guard, phase, *, fixture=None, error=None):
        try:
            match = next(((i, probe, stage) for i, (owner, probe, stage) in enumerate(guards, 1)
                          if owner is guard), None)
            if match is None or len(snapshots) >= MAX_SNAPSHOTS:
                state['capture_failed'] = True
                state['overflow'] |= len(snapshots) >= MAX_SNAPSHOTS
                return
            number, probe, scenario = match
            facts = probe.snapshot() if probe is not None else unavailable()
            facts = dict(facts, interval='after_guard_construction_before_browser_launch')
            if not (facts.get('available') is True and facts.get('complete') is True
                    and facts.get('overflow') is False):
                state['capture_failed'] = True
            counts = {}
            if fixture is not None:
                for name in ('dials', 'proxy_peers', 'greetings', 'authentication', 'connects'):
                    value = getattr(fixture, name, None)
                    counts[name] = len(value) if isinstance(value, (list, tuple, set)) else None
            snapshots.append({'backend': number, 'scenario': scenario, 'phase': phase,
                'elapsed_seconds': round(time.monotonic() - started, 3), 'guard_transport': facts,
                'guard_error_code': fixed_code(getattr(guard, 'last_error', '')),
                'error_type': (type(error).__name__ if type(error).__name__ in ERROR_TYPES else 'other')
                              if error is not None else None,
                'fixture_counts': counts})
        except Exception:
            state['capture_failed'] = True

    def observed_open(backend, *args, **kwargs):
        capture(backend.tunnel, 'before_open')
        try:
            value = original_open(backend, *args, **kwargs)
        except BaseException as exc:
            capture(backend.tunnel, 'after_open_error', error=exc)
            raise
        capture(backend.tunnel, 'after_open_return')
        return value

    def observed_checkpoint(result, stage, **kwargs):
        value = original_checkpoint(result, stage, **kwargs)
        try:
            if stage in STAGES:
                state['stage'] = stage
            backend = kwargs.get('backend')
            if backend is not None:
                capture(backend.tunnel, 'original_pre_close_checkpoint',
                        fixture=kwargs.get('fixture'), error=kwargs.get('error'))
        except Exception:
            state['capture_failed'] = True
        return value

    try:
        with patch.object(native_browser, 'NativeTunnel', observed_factory), \
             patch.object(acceptance.NativeBackend, 'open', observed_open), \
             patch.object(acceptance, 'update_proxy_evidence', observed_checkpoint):
            suite_calls += 1
            acceptance.main()  # Exactly one original suite call; no assertion/timeout changes.
        outcome = 'passed_inconclusive'
    except BaseException:
        outcome = 'original_suite_failed'
        raise
    finally:
        original_error = sys.exc_info()[0] is not None
        for guard, _, _ in guards:
            capture(guard, 'after_original_suite_cleanup')
        result = {'diagnostic_only': True, 'qualifies_release': False, 'source_base': BASE,
            'original_suite_calls': suite_calls, 'outcome': outcome,
            'capture_complete': not state['capture_failed'], 'overflow': state['overflow'],
            'backend_count': len(guards), 'snapshots': snapshots,
            'limits': {'backends': MAX_BACKENDS, 'snapshots': MAX_SNAPSHOTS},
            'interpretation': 'A pass is inconclusive. Missing/incomplete observations are unknown. '
                              'Accepted connections may include background traffic, not necessarily the failed request.'}
        try:
            (out / 'guard-investigation.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
        except Exception:
            if not original_error:
                raise RuntimeError('native_proxy_investigation_evidence_unavailable') from None
        if not original_error and state['capture_failed']:
            raise RuntimeError('native_proxy_investigation_capture_incomplete')


if __name__ == '__main__':
    main()
