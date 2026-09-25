"""Passive metadata for the unchanged local search acceptance assertions.

Reuses the existing auth-probe observer. It does not alter target acceptance,
requests, CSP, credentials or fixture outcomes; exceptions still fail the run.
"""
from __future__ import annotations

import json
import re
from urllib.parse import urlsplit
from unittest.mock import patch

import run_native_liepin_search as acceptance
from run_native_auth_probe import ObservedBackend, PROBES


class SearchObserver(ObservedBackend):
    def _received(self, event):
        message = json.loads(event['message'])
        if message.get('method') == 'Network.loadingFailed':
            data = message.get('params', {})
            record = self._requests.get((event.get('sessionId'), data.get('requestId')))
            failures = self.probe.setdefault('tracked_network_failures', [])
            if record and record.get('role') != 'asset' and len(failures) < 8:
                error = data.get('errorText', '')
                operation = record.get('operation', '')
                sequence = record.get('context', {}).get('sequence')
                failures.append(dict(
                    error=error if isinstance(error,str) and re.fullmatch(r'(?:net::)?ERR_[A-Z0-9_]{1,80}',error) else 'unclassified',
                    canceled=data.get('canceled') is True,
                    role=record['role'] if record['role'] in {'document','business','robots','login'} else 'other',
                    operation=operation if re.fullmatch(r'[a-z0-9_]{1,80}',operation) else 'other',
                    response_status=record.get('status'), current_epoch=record.get('epoch') == self._epoch,
                    current_sequence=sequence == self._latest_business.get(operation),
                    halted_before=self._halted))
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
    try:
        with patch.object(acceptance, 'NativeBackend', SearchObserver):
            acceptance.main()
    finally:
        out = acceptance.ROOT / 'browser-acceptance' / 'native'
        out.mkdir(parents=True, exist_ok=True)
        (out / 'search-probe.json').write_text(json.dumps({
            'scope': 'Passive metadata only; unchanged artificial-source tests and production request decisions.',
            'backends': PROBES,
        }, indent=2), encoding='utf-8')


if __name__ == '__main__':
    main()
