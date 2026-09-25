"""Explicit native component check in the actual process, with no allowed site route."""
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
from .browser_health import BrowserStartupError, HEALTH_MESSAGES
from .native_browser import NativeBackend
from .native_policy import NativeContract
from .rate import RateLedger


def cleanup_snapshot(controller, tunnel):
    """Fixed local completion facts, never paths, logs or exception messages."""
    checks = {
        'profile_removed': lambda: not controller.profile.exists(),
        'profile_cleanup_failed': lambda: controller.cleanup_failed,
        'bridge_exited': lambda: controller.process.poll() is not None,
        'tunnel_closed': lambda: tunnel._closed,
        'tunnel_thread_stopped': lambda: not tunnel.thread.is_alive(),
    }
    result = {}
    for name, observe in checks.items():
        try:
            value = observe()
            result[name] = value if type(value) is bool else None
        except Exception:
            result[name] = None
    return result


def check_native_browser():
    """Launch only the bundled fresh browser; never load user settings or a URL."""
    result=dict(success=False,code='native_check_failed',stage='launch',
        runtime=description()['kind'],browser_channel='bundled',browser_version='',
        minimal_controller=False,blank_page_check=False,request_guard_check=False,
        external_connections=None,cleanup_verified=False,live_sites_certified=False,
        cleanup={'attempted': False, 'close_returned': False,
                 'close_error_type': '', **cleanup_snapshot(None, None)})
    backend=controller=tunnel=None
    try:
        # No network rule permits even this artificial host. The single probe
        # uses a DIFFERENT .invalid host, rejected by both controller and tunnel
        # before target resolution, even if Chromium speculatively CONNECTs.
        host='native-check.invalid'
        adapter=DOMAdapter('native_runtime_check','Native component check',(host,),
            'https://'+host+'/', 'key', r'/never-a-job', 'https://'+host+'/', (),
            native_contract=NativeContract('native_runtime_check',(host,),()))
        with tempfile.TemporaryDirectory(prefix='vibe-native-check-') as tmp:
            try:
                with use_policy(NetworkPolicy()):
                    backend=NativeBackend(adapter,RateLedger(Path(tmp)/'rate.sqlite'),
                        threading.Event(),headless=True)
                controller,tunnel=backend.browser,backend.tunnel
                result['stage']='blank_page'
                result['minimal_controller']=controller.minimal_events is True
                version=controller.version
                if isinstance(version,str) and re.fullmatch(r'[0-9]+(?:\.[0-9]+){1,4}',version):
                    result['browser_version']=version
                observed=backend.page.evaluate('() => ({url: location.href, value: 21 * 2})')
                result['blank_page_check']=(observed=={'url':'about:blank','value':42}
                    and backend.page in backend._bound_pages and len(backend.context.pages)==1)
                if not result['minimal_controller'] or not result['blank_page_check'] or not result['browser_version']:
                    raise RuntimeError('native component did not initialize')
                result['stage']='request_guard'
                observed=backend.page.evaluate("""async () => {
                    try { await fetch('https://rejected-native-check.invalid/check'); return 'unexpected_success'; }
                    catch { return 'blocked'; }
                }""")
                counts=backend.native_counts
                result['request_guard_check']=(observed=='blocked'
                    and backend.error=='resource_domain_blocked' and backend._halted
                    and counts['blocked']>=1
                    and all(counts[key]==0 for key in ('document','business','asset','robots','login','responses'))
                    and not backend._requests and not backend._observations)
                result['external_connections']=tunnel.connections
                if not result['request_guard_check'] or result['external_connections']!=0:
                    raise RuntimeError('native request guard did not refuse locally')
                result['stage']='cleanup'
            finally:
                if backend is not None:
                    cleanup = result['cleanup']
                    cleanup['attempted'] = True
                    try:
                        backend.close()
                        cleanup['close_returned'] = True
                    except Exception as exc:
                        known = (PermissionError, subprocess.TimeoutExpired, OSError, RuntimeError)
                        cleanup['close_error_type'] = next(
                            (kind.__name__ for kind in known if isinstance(exc, kind)), 'other')
                        raise
                    finally:
                        cleanup.update(cleanup_snapshot(controller, tunnel))
                        result['cleanup_verified']=(cleanup['close_returned']
                            and cleanup['profile_removed'] is True
                            and cleanup['profile_cleanup_failed'] is False
                            and cleanup['bridge_exited'] is True
                            and cleanup['tunnel_closed'] is True
                            and cleanup['tunnel_thread_stopped'] is True)
            if not result['cleanup_verified']:
                raise RuntimeError('native component cleanup not confirmed')
        result.update(success=True,code='native_component_ready',stage='passed')
    except BrowserStartupError as exc:
        # Only fixed error identifiers cross the CLI boundary. Startup reports
        # can contain paths/logs and are deliberately not serialized here.
        code=exc.report.get('code')
        if code in HEALTH_MESSAGES:result['code']=code
    except Exception:
        pass
    return result


def run_cli():
    result=check_native_browser()
    print(json.dumps(result,ensure_ascii=False))
    return 0 if result['success'] else 2
