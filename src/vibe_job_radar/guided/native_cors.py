"""Abort-only CDP attachment for unsupported surfaces in CORS contexts.

Do not use Playwright request routing in those contexts: its driver can answer
OPTIONS itself. Quarantined targets never receive credentials or a debugger
resume; every paused HTTP request is failed by the already-halted controller.
"""
from __future__ import annotations

from .contracts import CrawlError


def quarantine_target(backend, target):
    """Called only after cancellation and rejection have been recorded."""
    if not backend.cancelled.is_set() or not backend._halted:
        raise CrawlError('native_protocol_error')
    try:
        result = backend._cdp.send('Target.attachToTarget', {
            'targetId': target, 'flatten': False})
        session = result['sessionId']
        if not isinstance(session, str) or not session:
            raise ValueError('invalid quarantine attachment')
        backend._sessions[session] = target
        backend._send(session, 'Fetch.enable', {
            'patterns': [{'urlPattern': '*', 'requestStage': 'Request'}],
            'handleAuthRequests': True})
    except Exception:
        # Keep the target on the owner's close queue. Never resume a partially
        # configured page, disclose the protocol text, or fall back to routing.
        backend.cancelled.set()
        raise CrawlError('native_protocol_error') from None
