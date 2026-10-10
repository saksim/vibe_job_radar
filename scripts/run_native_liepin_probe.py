"""Passive metadata for the unchanged local search acceptance assertions.

Reuses the existing auth-probe observer. It does not alter target acceptance,
requests, CSP, credentials or fixture outcomes; exceptions still fail the run.
"""
from __future__ import annotations

import json
from urllib.parse import urlsplit
from unittest.mock import patch

import run_native_liepin_search as acceptance
from run_native_auth_probe import ObservedBackend, PROBES
from native_netlog_evidence import NativeNetLog, unavailable
from native_wait_diagnostics import Stages, capture, observed_backend, observed_wait, observe_io, require_ci


# Keep only our CI captures for a final cleanup pass after all browser owners
# have finished; raw files remain outside the artifact directory.
CONNECTION_LOGS = []


def finish_client_logs(entries):
    for probe, observer in entries:
        try:
            probe['client_connection_log'] = observer.finish()
        except Exception:
            probe['client_connection_log'] = unavailable('observer_error')


class SearchObserver(ObservedBackend):
    def _launch_options(self, options):
        configured = super()._launch_options(options)
        try:
            self._connection_log = NativeNetLog(
                acceptance.ROOT / '.verify' / 'native-netlog-private',
                self.tunnel.endpoint, self.contract.hosts)
            CONNECTION_LOGS.append((self.probe, self._connection_log))
            return self._connection_log.options(configured)
        except Exception:
            self.probe['client_connection_log'] = unavailable('setup_failed')
            return configured

    def close(self):
        try:
            return super().close()
        finally:
            observer = self.__dict__.get('_connection_log')
            if observer is not None:
                # Browser shutdown flushes the private log. Preserve any close
                # exception; auxiliary evidence never alters the test outcome.
                try:
                    self.probe['client_connection_log'] = observer.finish()
                except Exception:
                    self.probe['client_connection_log'] = unavailable('observer_error')

    def _received(self, event):
        message = json.loads(event['message'])
        if message.get('method') == 'Target.attachedToTarget':
            info = message.get('params', {}).get('targetInfo', {})
            items = self.probe.setdefault('child_attachments', [])
            if len(items) < 16:
                url = urlsplit(info.get('url', ''))
                items.append({'kind': str(info.get('type', ''))[:40],
                    'known_browser_ui': self._browser_chrome_ui(info),
                    'has_opener': bool(info.get('openerId')),
                    'blank': info.get('url', '') in ('', 'about:blank'),
                    'internal_host': url.hostname if url.scheme in {'chrome', 'devtools'} else None})
        return super()._received(event)


def main():
    require_ci()
    stages = Stages()
    out = acceptance.ROOT / 'browser-acceptance' / 'native'
    try:
        with capture(stages, out, 'search-wait'), observe_io(stages), \
                patch.object(acceptance, 'NativeBackend', observed_backend(SearchObserver, stages)), \
                patch.object(acceptance, 'wait', observed_wait(acceptance.wait, stages, out, 'search-wait')):
            acceptance.main()
    finally:
        finish_client_logs(CONNECTION_LOGS)
        out = acceptance.ROOT / 'browser-acceptance' / 'native'
        out.mkdir(parents=True, exist_ok=True)
        (out / 'search-probe.json').write_text(json.dumps({
            'scope': 'Passive metadata only; unchanged artificial-source tests and production request decisions.',
            'backends': PROBES,
        }, indent=2), encoding='utf-8')


if __name__ == '__main__':
    main()
