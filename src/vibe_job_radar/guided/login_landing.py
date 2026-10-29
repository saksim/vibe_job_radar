"""One owned login-return intent; never permission to load the account homepage.

A completed HTTP reply plus a publisher navigation is not authentication proof.
It can only hand back to the original, already authorized task. The collector
still checks the target's rules, current query and complete job content.
"""
from dataclasses import dataclass, field

from .contracts import CrawlError
from .manual_login_progress import current


@dataclass(frozen=True)
class LandingTicket:
    identity: tuple = field(repr=False)
    tracker: object = field(repr=False)
    sequence: int
    epoch: int


def _ready(backend, tracker):
    return (tracker is not None and tracker.active and tracker.clock() < tracker.deadline
            and tracker.last_login_response == tracker.sequence
            and tracker.last_login_response is not None
            and getattr(backend, 'human_login', False) is True
            and getattr(backend, 'auth_mode', False) is True
            and getattr(backend, '_direct_cdp', False) is True
            and not backend.cancelled.is_set() and not backend.error
            and not backend._halted and not backend._closing
            and getattr(backend, 'policy_check', lambda: True)())


def offer(backend, session, event):
    """Recognize only the SMS/password page's exact main-frame root return."""
    request = event['request']
    tracker = current(backend)
    if (event.get('resourceType') != 'Document' or request.get('method') != 'GET'
            or request.get('url') != 'https://c.liepin.com/'
            or not _ready(backend, tracker)):
        return False
    identity = backend.document_identity()
    document = identity[-1]
    if (session != identity[0] or event.get('frameId') != document.frame
            or document.url != 'https://www.liepin.com/login'
            or document.document_url != document.url):
        return False
    if tracker.return_offered:
        return peek(backend) is not None  # Abort duplicates, never queue twice.
    backend._login_landing = LandingTicket(identity, tracker, tracker.sequence, backend._epoch)
    tracker.return_offered = True
    tracker.stage = 'return_available'
    return True


def peek(backend):
    ticket = backend.__dict__.get('_login_landing')
    if not isinstance(ticket, LandingTicket):
        return None
    tracker = current(backend)
    if (tracker is not ticket.tracker or not _ready(backend, tracker)
            or tracker.sequence != ticket.sequence or backend._epoch != ticket.epoch
            or backend.document_identity() != ticket.identity):
        return None
    return ticket


def claim(backend, ticket):
    if peek(backend) is not ticket:
        raise CrawlError('login_return_changed')
    backend._login_landing = None
    ticket.tracker.stop('returned')
