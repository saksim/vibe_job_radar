"""Metadata-only wrapper around the unchanged local native acceptance suite.

No target URLs, headers, credentials or bodies are recorded. Existing redacted
component-startup reports are retained if initialization fails before tracing.
This developer-only probe changes neither authentication nor request decisions.
"""
from __future__ import annotations

from collections import Counter
import json
import sys
from pathlib import Path
from urllib.parse import urlsplit
from unittest.mock import patch

import run_native_browser_acceptance as acceptance
from vibe_job_radar.guided.native_tunnel import NativeTunnel
from native_failure_evidence import loading_failure, fatal_failure
from native_guard_evidence import install_guard_observer, guard_transport_facts

COMMANDS = frozenset({
    'Network.enable', 'Network.setUserAgentOverride', 'Network.setCacheDisabled',
    'Fetch.enable', 'Fetch.continueRequest', 'Fetch.continueResponse',
    'Fetch.continueWithAuth', 'Fetch.failRequest', 'Runtime.runIfWaitingForDebugger',
    'Target.setAutoAttach', 'Network.getResponseBody', 'Page.stopLoading',
})
EVENTS = frozenset({
    'Fetch.authRequired', 'Fetch.requestPaused', 'Network.loadingFailed',
    'Network.loadingFinished', 'Target.attachedToTarget',
})
PROBES = []
REPLIES = Counter()


class ObservedBackend(acceptance.NativeBackend):
    def __init__(self, *args, **kwargs):
        self.probe = {'sent': Counter(), 'acknowledged': Counter(), 'events': Counter(),
                      'attachments': Counter(), 'auth': Counter(), 'snapshots': []}
        PROBES.append(self.probe)
        try:
            super().__init__(*args, **kwargs)
        except Exception as exc:
            # Reuse the existing redacted component report, not raw protocol
            # messages or browser traffic. Tests use only an artificial source.
            report = getattr(exc, 'report', None)
            if isinstance(report, dict):
                self.probe['startup_diagnostic'] = report
            raise
        self._guard_evidence = install_guard_observer(self.tunnel)

    def close(self):
        try:
            return super().close()
        finally:
            # Save a copy; an observer failure must preserve the original close.
            try:
                self.probe['guard_transport'] = guard_transport_facts(self)
            except Exception:
                pass

    def _attached(self, event):
        info = event.get('targetInfo', {})
        kind = info.get('type')
        kinds = {'page', 'worker', 'iframe', 'tab', 'browser', 'service_worker',
                 'shared_worker', 'background_page', 'webview'}
        kind = kind if kind in kinds else 'other'
        self.probe['attachments'][kind] += 1
        observations = self.probe.setdefault('attachment_order', [])
        if len(observations) < 16:
            observations.append({'kind': kind,
                'application_context': self._target_in_context(info),
                'creation_active': bool(self._page_creation),
                'already_owned': info.get('targetId') in self._sessions.values(),
                'has_opener': bool(info.get('openerId')),
                'type_exact': info.get('type') if isinstance(info.get('type'), str) and len(info['type'])<40 else 'unknown',
                'known_browser_ui': self._browser_chrome_ui(info),
                'omnibox_url': info.get('url') if urlsplit(info.get('url', '')).scheme=='chrome' and urlsplit(info.get('url', '')).hostname=='omnibox-popup.top-chrome' and len(info.get('url', ''))<128 else None,
                'blank': info.get('url', '') in ('', 'about:blank'),
                'internal_scheme': urlsplit(info.get('url', '')).scheme if urlsplit(info.get('url', '')).scheme in {'chrome', 'chrome-extension', 'devtools', 'about'} else 'web_or_other',
                'internal_host': urlsplit(info.get('url', '')).hostname if urlsplit(info.get('url', '')).scheme in {'chrome', 'devtools'} else None})
        return super()._attached(event)

    def _fatal(self, code, error=None):
        fatal_failure(self, code)
        calls = self.probe.setdefault('fatal_sites', [])
        if len(calls) < 8:
            calls.append({'code': code, 'caller': sys._getframe(1).f_code.co_name})
        return super()._fatal(code, error)

    def _send(self, session, method, params=None, callback=None):
        key = method if method in COMMANDS else 'other'
        self.probe['sent'][key] += 1
        def acknowledged(result):
            self.probe['acknowledged'][key] += 1
            if callback:
                callback(result)
        return super()._send(session, method, params, acknowledged)

    def _received(self, event):
        try:
            message = json.loads(event['message'])
            method = message.get('method')
            loading_failure(self, event, message)
            if method in EVENTS:
                self.probe['events'][method] += 1
            if 'error' in message:
                self.probe['events']['command_error'] += 1
        except (KeyError, TypeError, ValueError):
            self.probe['events']['unreadable'] += 1
        return super()._received(event)

    def _authenticate(self, session, event):
        challenge = event.get('authChallenge', {})
        source = challenge.get('source')
        self.probe['auth'][source if source in {'Proxy', 'Server'} else 'unknown_source'] += 1
        try:
            origin = urlsplit(challenge.get('origin', ''))
            expected = urlsplit(self.tunnel.endpoint)
            matches = (origin.scheme, origin.hostname, origin.port) == (
                expected.scheme, expected.hostname, expected.port)
        except (AttributeError, TypeError, ValueError):
            matches = False
        self.probe['auth']['same_origin' if matches else 'different_origin'] += 1
        return super()._authenticate(session, event)

    def _load_robots(self):
        try:
            return super()._load_robots()
        finally:
            if len(self.probe['snapshots']) < 8:
                self.probe['snapshots'].append({
                    'owned_sessions': len(self._sessions),
                    'pending_commands': len(self._pending),
                    'active_requests': len(self._requests),
                    'auth_attempts': len(self._auth_attempts),
                    'native_counts': dict(self.native_counts),
                    'tunnel_connections': self.tunnel.connections if self.tunnel else 0,
                })


def main():
    reply = NativeTunnel._reply
    def counted_reply(sock, status):
        REPLIES[str(status) if status in {400, 403, 407, 502, 503} else 'other'] += 1
        return reply(sock, status)
    try:
        with patch.object(acceptance, 'NativeBackend', ObservedBackend), \
                patch.object(NativeTunnel, '_reply', staticmethod(counted_reply)):
            acceptance.main()
    finally:
        out = acceptance.ROOT / 'browser-acceptance' / 'native'
        out.mkdir(parents=True, exist_ok=True)
        (out / 'auth-probe.json').write_text(json.dumps({
            'scope': 'Controlled fixture only; passive fixed counters, no traffic decisions changed.',
            'proxy_replies': dict(REPLIES), 'backends': PROBES,
        }, indent=2), encoding='utf-8')


if __name__ == '__main__':
    main()
