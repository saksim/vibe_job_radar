"""Check the current native collector on a fresh blank page, with no allowed route."""
from __future__ import annotations

import json
from pathlib import Path
import re
import subprocess
import tempfile
import threading

from ..network_policy import NetworkPolicy, use_policy
from ..runtime import description
from .adapters import DOMAdapter
from .browser_health import BrowserStartupError, HEALTH_MESSAGES, environment_report
from .browser_choice import validate_choice
from .native_browser import NativeBackend
from .native_policy import NativeContract
from .contracts import CrawlError
from .native_startup_diagnostic import startup_failure_facts
from .rate import RateLedger


def cleanup_snapshot(browser, tunnel, *, minimal=False):
    """Fixed completion facts; unavailable or mistyped observations stay unknown."""
    def negate(value):
        return not value if type(value) is bool else None

    def process_exited():
        code = browser.process.poll()
        return code is not None if code is None or type(code) is int else None

    checks = {
        'browser_disconnected': lambda: negate(browser.is_connected()),
        'tunnel_closed': lambda: tunnel._closed,
        'tunnel_thread_stopped': lambda: negate(tunnel.thread.is_alive()),
    }
    if minimal or getattr(browser, 'minimal_events', False) is True:
        checks.update({
            'profile_removed': lambda: negate(browser.profile.exists()),
            'profile_cleanup_ok': lambda: negate(browser.cleanup_failed),
            'bridge_exited': process_exited,
        })
    result = {}
    for name, observe in checks.items():
        try:
            value = observe()
            result[name] = value if type(value) is bool else None
        except Exception:
            result[name] = None
    return result


def check_native_browser(*, channel=None, headless=True):
    channel = validate_choice(channel or 'bundled')
    if channel == 'bundled':
        channel = None
    result = dict(success=False, code='native_check_failed', stage='launch',
        browser_channel=channel or 'bundled', browser_version='', launch_tested=False, process_exit_hex='',
        runtime=description()['kind'], minimal_controller=False,
        blank_page_check=False, request_guard_check=False, external_connections=None,
        cleanup_verified=False, live_sites_certified=False,
        controller=('minimal_cdp' if channel == 'chrome' else 'playwright_public_cdp'),
        cleanup={'attempted':False, 'close_returned':False, 'close_error_type':'',
                 **cleanup_snapshot(None, None, minimal=channel == 'chrome')})
    backend = browser = tunnel = None
    try:
        # The contract has no request rule. A different synthetic hostname is
        # rejected by both controller and tunnel, before target DNS or connect.
        host = 'native-check.invalid'
        adapter = DOMAdapter('native_runtime_check', 'Native component check', (host,),
            'https://' + host + '/', 'key', r'/never-a-job', 'https://' + host + '/', (),
            native_contract=NativeContract('native_runtime_check', (host,), ()))
        with tempfile.TemporaryDirectory(prefix='vibe-native-check-') as tmp:
            try:
                with use_policy(NetworkPolicy()):
                    backend = NativeBackend(adapter, RateLedger(Path(tmp)/'rate.sqlite'),
                        threading.Event(), headless=headless, **({'channel':channel} if channel else {}))
                browser, tunnel = backend.browser, backend.tunnel
                result['launch_tested'] = backend.startup_report.get('launch_tested') is True
                version = browser.version
                if isinstance(version, str) and re.fullmatch(r'[0-9]+(?:\.[0-9]+){1,4}', version):
                    result['browser_version'] = version
                result['stage'] = 'blank_page'
                result['minimal_controller'] = getattr(browser, 'minimal_events', False) is True
                result['controller'] = 'minimal_cdp' if result['minimal_controller'] else 'playwright_public_cdp'
                if result['minimal_controller'] != (channel == 'chrome'):
                    raise RuntimeError('unexpected native control mode')
                observed = backend.page.evaluate('() => ({url: location.href, value: 21 * 2})')
                result['blank_page_check'] = (observed == {'url':'about:blank', 'value':42}
                    and backend.page in backend._bound_pages and len(backend.context.pages) == 1)
                if not result['blank_page_check'] or not result['browser_version']:
                    raise RuntimeError('native component did not initialize')
                result['stage'] = 'request_guard'
                observed = backend.page.evaluate("""async () => {
                    try { await fetch('https://rejected-native-check.invalid/check'); return 'unexpected_success'; }
                    catch { return 'blocked'; }
                }""")
                counts = backend.native_counts
                result['request_guard_check'] = (observed == 'blocked'
                    and backend.error == 'resource_domain_blocked' and backend._halted
                    and counts['blocked'] >= 1
                    and all(counts[k] == 0 for k in ('document','business','asset','robots','login','responses'))
                    and not backend._requests and not backend._observations)
                result['external_connections'] = tunnel.connections
                if not result['request_guard_check'] or result['external_connections'] != 0:
                    raise RuntimeError('native request guard did not refuse locally')
                result['stage'] = 'cleanup'
            finally:
                if backend is not None:
                    result['cleanup']['attempted'] = True
                    try:
                        backend.close()
                        result['cleanup']['close_returned'] = True
                    except Exception as exc:
                        known = (PermissionError, subprocess.TimeoutExpired, OSError, RuntimeError)
                        result['cleanup']['close_error_type'] = next(
                            (kind.__name__ for kind in known if isinstance(exc, kind)), 'other')
                        raise
                    finally:
                        cleanup = result['cleanup']
                        cleanup.update(cleanup_snapshot(browser, tunnel, minimal=channel == 'chrome'))
                        result['cleanup_verified'] = (cleanup['close_error_type'] == ''
                            and all(value is True for key, value in cleanup.items() if key != 'close_error_type'))
            if not result['cleanup_verified']:
                raise RuntimeError('native cleanup could not be confirmed')
        result.update(success=True, code='native_component_ready', stage='passed')
    except BrowserStartupError as exc:
        # The backend has already preserved the cause while closing itself.
        # Export only fixed facts, before the component summary loses them.
        cause = exc.__cause__
        result['startup_failure'] = startup_failure_facts({**exc.report,
            'cause_code': cause.code if isinstance(cause, CrawlError) else ''})
        code = exc.report.get('code')
        if code in HEALTH_MESSAGES:
            result['code'] = code
        result['launch_tested'] = exc.report.get('launch_tested') is True
        exit_hex = exc.report.get('process_exit_hex', '')
        if isinstance(exit_hex, str) and re.fullmatch(r'0x[0-9A-F]{8}', exit_hex):
            result['process_exit_hex'] = exit_hex
    except Exception:
        pass
    return result


def probe_native_browser(*, channel=None):
    result = check_native_browser(channel=channel, headless=False)
    ready = result['success']
    return {**environment_report(), 'network_backend':'native', 'mode':'headed',
        'browser_channel':channel or 'bundled', 'browser_version':result['browser_version'],
        'launch_tested':result['launch_tested'], 'process_exit_hex':result['process_exit_hex'], 'ready':ready, 'stage':result['stage'],
        'code':'browser_ready' if ready else result['code'],
        'message':('已通过原生实验的空白页、请求拦截和退出检查；默认浏览器桥须独立检查。'
                   if ready else HEALTH_MESSAGES.get(result['code'], '原生实验启动或退出检查未通过；未更换采集浏览器。')),
        'native_component':result}


def run_cli(*, channel=None):
    result = check_native_browser(channel=channel)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result['success'] else 2
