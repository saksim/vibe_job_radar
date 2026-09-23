"""Ephemeral ownership for a timer action; never stored with task metadata."""
from dataclasses import dataclass, field
from .rate import RateLimit
from .read_retry import TransientReadFailure


_TRANSIENT = frozenset({'rate_wait', 'publisher_wait', 'cooldown', 'http_429',
                        'hourly_limit', 'daily_limit'})


@dataclass(frozen=True)
class DeferredResume:
    backend: object = field(repr=False)
    next_allowed_at: float


def can_resume(backend):
    """Called only on the browser owner thread, never by HTTP status readers."""
    try:
        alive = getattr(backend, 'alive', None)
        if not callable(alive) or alive() is not True or getattr(backend, 'auth_mode', False):
            return False
        error = getattr(backend, 'error', None)
        waiting = getattr(backend, 'wait_error', None)
        # The worker cancels background requests during a wait. Their ordinary
        # 'paused' marker is cleared by collection_mode after the timer fires.
        # A fatal page/HTTP error must not be erased by a new automatic open().
        if error in (None, '', 'paused'):
            return waiting is None
        if error == 'read_transient_failure':
            return isinstance(waiting, TransientReadFailure) and waiting.code == error
        return (error in _TRANSIENT and isinstance(waiting, RateLimit)
                and waiting.code == error)
    except Exception:
        # Unknown browser state cannot authorize opening a replacement session.
        return False
