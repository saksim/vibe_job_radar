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


# Protocol enum values only. failedParameter can contain a publisher value and
# is deliberately never recorded. See the official Network.loadingFailed schema.
_CORS_ERRORS = frozenset('''DisallowedByMode InvalidResponse WildcardOriginNotAllowed
MissingAllowOriginHeader MultipleAllowOriginValues InvalidAllowOriginValue
AllowOriginMismatch InvalidAllowCredentials CorsDisabledScheme PreflightInvalidStatus
PreflightDisallowedRedirect PreflightWildcardOriginNotAllowed PreflightMissingAllowOriginHeader
PreflightMultipleAllowOriginValues PreflightInvalidAllowOriginValue PreflightAllowOriginMismatch
PreflightInvalidAllowCredentials PreflightMissingAllowExternal PreflightInvalidAllowExternal
InvalidAllowMethodsPreflightResponse InvalidAllowHeadersPreflightResponse
MethodDisallowedByPreflightResponse HeaderDisallowedByPreflightResponse RedirectContainsCredentials
InsecureLocalNetwork InvalidLocalNetworkAccess NoCorsRedirectModeNotFollow
LocalNetworkAccessPermissionDenied'''.split())
_BLOCKED_REASONS = frozenset('''other csp mixed-content origin inspector integrity
subresource-filter content-type coep-frame-resource-needs-coep-header
coop-sandboxed-iframe-cannot-navigate-to-coop-page corp-not-same-origin
corp-not-same-origin-after-defaulted-to-same-origin-by-coep
corp-not-same-origin-after-defaulted-to-same-origin-by-dip
corp-not-same-origin-after-defaulted-to-same-origin-by-coep-and-dip
corp-not-same-site sri-message-signature-mismatch'''.split())


def failure_policy_metadata(data):
    def category(value, allowed):
        return None if value is None else value if isinstance(value, str) and value in allowed else 'unclassified'
    cors = data.get('corsErrorStatus')
    return dict(
        blocked_reason=category(data.get('blockedReason'), _BLOCKED_REASONS),
        cors_error=category(cors.get('corsError') if isinstance(cors, dict) else None, _CORS_ERRORS))


class SearchObserver(ObservedBackend):
    def _response_paused(self, session, event):
        key = (session, event.get('networkId', event['requestId']))
        record = self._requests.get(key)
        if record and record.get('role') == 'business':
            headers = event.get('responseHeaders', [])
            allowed = [h['value'] for h in headers if h['name'].lower() == 'access-control-allow-origin']
            origin = urlsplit(self.adapter.search_base)
            expected = origin.scheme + '://' + origin.netloc
            phase = dict(
                method=event['request']['method'] if event['request']['method'] in {'GET','POST','OPTIONS'} else 'other',
                status=event.get('responseStatusCode'), error_stage='responseErrorReason' in event,
                phrase=event.get('responseStatusText') if event.get('responseStatusText') in {'OK','Connection Established','Connection','No Content'} else 'other',
                header_count=len(headers),
                json_type=any(h['name'].lower() == 'content-type' and h['value'].split(';')[0] == 'application/json' for h in headers),
                request_matches=event['request']['url'] == record['url'],
                allow_origin_count=len(allowed), allow_origin_matches=allowed == [expected],
                allow_credentials=any(h['name'].lower() == 'access-control-allow-credentials' and h['value'] == 'true' for h in headers))
            phases = record.setdefault('_probe_response_policy', [])
            if len(phases) < 4:
                phases.append(phase)
        return super()._response_paused(session, event)

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
                    halted_before=self._halted, response_policy=record.get('_probe_response_policy'),
                    **failure_policy_metadata(data)))
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
