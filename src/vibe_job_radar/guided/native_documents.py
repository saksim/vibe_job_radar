"""Additive document sandbox for native CORS collection contexts.

Target-created callbacks cannot reliably prevent the first popup request. A
header-delivered sandbox is enforced before a document runs, without allowing
auxiliary browsing contexts. Existing publisher policies remain separate and
are all still enforced. The native-fetched document is delivered with identical
decoded bytes and an extra policy; no upstream request is repeated. CORS
responses stay native. This helper grants no request or authentication.
"""
from __future__ import annotations

import base64

from .contracts import CrawlError

# Preserve scripts, the original origin and same-tab normal forms. Do not grant
# popups, popup escape, downloads, top navigation or other unsupported surfaces.
DOCUMENT_SANDBOX = "sandbox allow-scripts allow-same-origin allow-forms; frame-src 'none'"
ROBOTS_SANDBOX = "sandbox; default-src 'none'; base-uri 'none'; form-action 'none'"


def document_response_params(event: dict, *, enabled: bool, robots: bool = False) -> dict:
    """Build continueResponse parameters, with one extra restriction if needed.

    Called only after the normal status, redirect, size and accounting checks.
    Keep header order/case/duplicates, including every original CSP/Set-Cookie.
    Appending an independent enforcing CSP cannot relax a publisher policy.
    """
    params = {'requestId': event['requestId']}
    status = event.get('responseStatusCode', 0)
    if (not (enabled or robots) or event.get('resourceType') != 'Document'
            or 'responseErrorReason' in event or status < 200 or 300 <= status < 400):
        return params
    params.update(responseCode=status,
                  responsePhrase=event.get('responseStatusText', ''),
                  responseHeaders=[dict(h) for h in event.get('responseHeaders', [])] + [
                      {'name': 'Content-Security-Policy',
                       'value': ROBOTS_SANDBOX if robots else DOCUMENT_SANDBOX}])
    return params


def continue_document_response(backend, session: str, event: dict, *, robots: bool = False) -> None:
    """Deliver a bounded native-fetched document with its original policies.

    Chromium's header-only continuation can retain old parsed policy metadata.
    Supplying the unchanged decoded body makes the browser parse all headers
    together. Other responses keep the normal native continuation.
    """
    params = document_response_params(event, enabled=getattr(backend, '_native_cors', False), robots=robots)
    if 'responseHeaders' not in params or event['responseStatusCode'] in {204, 205}:
        backend._send(session, 'Fetch.continueResponse', {'requestId': event['requestId']})
        return

    def deliver(result):
        if backend._closing or session not in backend._sessions:
            return  # The target was retired; never send through a stale handle.
        if backend.cancelled.is_set():
            raise CrawlError('paused')
        if not getattr(backend, 'policy_check', lambda: True)():
            raise CrawlError('native_policy_changed')
        if backend._halted:
            raise backend.wait_error or CrawlError(backend.error or 'site_stopped')
        encoded = result.get('body')
        if not isinstance(encoded, str) or len(encoded) > 6_700_000:
            raise CrawlError('response_too_large')
        try:
            raw = (base64.b64decode(encoded, validate=True) if result.get('base64Encoded')
                   else encoded.encode('utf-8'))
        except (ValueError, UnicodeError):
            raise CrawlError('native_protocol_error') from None
        if len(raw) > 5_000_000:
            raise CrawlError('response_too_large')
        # Fetch returns decoded bytes. Keep duplicate publisher CSP/Set-Cookie
        # headers, but do not falsely label decoded delivery as compressed or
        # chunked. This is a local representation change, never a new request.
        headers = [h for h in params['responseHeaders'] if h['name'].lower() not in {
            'content-encoding', 'content-length', 'transfer-encoding'}]
        headers.append({'name': 'Content-Length', 'value': str(len(raw))})
        backend._send(session, 'Fetch.fulfillRequest', {**params,
            'responseHeaders': headers, 'body': base64.b64encode(raw).decode('ascii')})

    backend._send(session, 'Fetch.getResponseBody', {'requestId': event['requestId']}, deliver)
