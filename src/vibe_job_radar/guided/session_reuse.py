"""Transfer one explicitly selected live browser session, never its secrets.

This is in-process continuity between queries, not persistent authentication.
The owner worker calls it before opening a new search. It performs no navigation,
Cookie extraction, storage export, login, quota reset, or backend construction.
"""
from __future__ import annotations

from .contracts import CrawlError
from ..network_policy import current_policy


def reuse_current_session(service, state):
    """Move an idle compatible backend to a new query after explicit opt-in."""
    if not state.get('reuse_current_session') or state['id'] in service._backends:
        return False
    # Never graft a different query's current page onto a resumed capture or
    # partially collected job. Its normal reconstruction path remains unchanged.
    if state.get('phase') != 'search' or state.get('cards') or state.get('pages_seen'):
        return False
    with service._lock:
        if not service._backends:
            return False  # First task still starts a normal fresh browser.
        if len(service._backends) != 1:
            raise CrawlError('session_reuse_unavailable')
        previous_id, backend = next(iter(service._backends.items()))
        previous = service._load(previous_id)
        if (previous.get('platform') != state['platform']
                or previous.get('backend', 'bridge') != state.get('backend', 'bridge')
                or previous.get('human_login', False) != state.get('human_login', False)
                or bool(previous.get('persist_session')) != bool(state.get('persist_session'))):
            raise CrawlError('session_reuse_incompatible')
        if (previous.get('status') not in {'ready', 'completed'}
                or getattr(backend, 'error', None) not in (None, 'paused')
                or getattr(backend, 'wait_error', None) is not None
                or getattr(backend, 'auth_mode', False)):
            raise CrawlError('session_reuse_unavailable')
        health = getattr(backend, 'startup_report', {})
        wire = getattr(backend, 'wire', None)
        policy = getattr(wire, 'network_policy', None)
        if (health.get('browser_channel') != service._selected_browser
                or policy is None or policy.fingerprint != current_policy().fingerprint
                or backend.adapter is not service.registry.get(state['platform'])):
            raise CrawlError('session_reuse_incompatible')
        if not callable(getattr(backend, 'alive', None)) or not backend.alive():
            raise CrawlError('session_reuse_unavailable')
        # Preserve original results/selection. Only browser ownership moves;
        # the new search will reset the native observation epoch normally.
        service._login_return.disarm(previous_id)
        service._save(previous, session_transferred_to=state['id'],
                      login_continuation='off')
        service._backends.pop(previous_id)
        service._backends[state['id']] = backend
        counts = getattr(backend, 'native_counts', None)
        if isinstance(counts, dict):
            backend.native_counts = {key: 0 for key in counts}
        wire.progress = lambda code, seconds: service._save(state, code, wait_seconds=seconds)
        service._save(state, session_reused=True, session_source_task=previous_id,
                      authentication='reused_session_unverified')
        return True
