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


def _active_context(backend, tracker):
    return (tracker is not None and tracker.active and tracker.clock() < tracker.deadline
            and getattr(backend, 'human_login', False) is True
            and getattr(backend, 'auth_mode', False) is True
            and getattr(backend, '_direct_cdp', False) is True
            and not backend.cancelled.is_set() and not backend.error
            and not backend._halted and not backend._closing
            and getattr(backend, 'policy_check', lambda: True)())


def _ready(backend, tracker):
    return (_active_context(backend, tracker)
            and tracker.last_login_response == tracker.sequence
            and tracker.last_login_response is not None)


def _owned_return(backend, session, event):
    request = event['request']
    tracker = current(backend)
    if (event.get('resourceType') != 'Document' or request.get('method') != 'GET'
            or request.get('url') != 'https://c.liepin.com/'
            or not _active_context(backend, tracker)):
        return None
    identity = backend.document_identity()
    document = identity[-1]
    if (session != identity[0] or event.get('frameId') != document.frame
            or document.url != 'https://www.liepin.com/login'
            or document.document_url != document.url):
        return None
    return tracker, identity


def offer(backend, session, event):
    """Recognize only the SMS/password page's exact main-frame root return."""
    owned = _owned_return(backend, session, event)
    if owned is None or not _ready(backend, owned[0]):
        return False
    tracker, identity = owned
    if tracker.return_offered:
        return peek(backend) is not None  # Abort duplicates, never queue twice.
    backend._login_landing = LandingTicket(identity, tracker, tracker.sequence, backend._epoch)
    tracker.return_offered = True
    tracker.stage = 'return_available'
    return True


def unverified_return(backend, session, event):
    """Preserve an owned login page when its return cannot be consumed.

    A newer submit or visible rejection invalidates the previous response.
    Cancel this exact navigation rather than replacing the form with a browser
    error page. It grants no ticket, account access or automatic continuation.
    """
    owned = _owned_return(backend, session, event)
    return (owned is not None and owned[0].submits > 0
            and (not _ready(backend, owned[0])
                 or (owned[0].return_offered and peek(backend) is None)))


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
    backend._departing_login = ticket
    ticket.tracker.stop('returned')


def departing_request(backend, session, event):
    """Cancel an owned old login request, without granting authentication.

    The login document survives Aborted until the new task document commits.
    During that interval its scripts/user controls may submit another request.
    It must not reach the network or poison the unrelated task navigation.
    """
    ticket = backend.__dict__.get('_departing_login')
    request = event['request']
    if (not isinstance(ticket, LandingTicket) or backend.auth_mode
            or not getattr(backend, 'human_login', False) or not backend._direct_cdp
            or backend.cancelled.is_set() or backend.error or backend._halted or backend._closing
            or not getattr(backend, 'policy_check', lambda: True)()
            or backend._epoch not in (ticket.epoch, ticket.epoch + 1)
            or current(backend) is not ticket.tracker or ticket.tracker.active
            or ticket.tracker.stage != 'returned' or ticket.tracker.sequence != ticket.sequence
            or ticket.tracker.clock() >= ticket.tracker.deadline
            or request.get('method') not in {'OPTIONS', 'POST'}
            or event.get('resourceType') not in {'XHR', 'Fetch', 'Preflight'}
            or session != ticket.identity[0] or event.get('frameId') != ticket.identity[-1].frame):
        return False
    if backend.document_identity() != ticket.identity:
        return False  # New documents/frames retain the normal failure policy.
    try:
        rule = backend.contract.match(request['url'], request['method'], event['resourceType'],
                                      authentication=True, headers=request.get('headers', {}))
        rule.validate_headers(request['method'], request.get('headers', {}))
    except CrawlError:
        return False
    return (rule.authentication and rule.role == 'login'
            and rule.host in {'api-passport.liepin.com', 'api-security-liepin.liepin.com'}
            and rule.cors_origin == 'https://www.liepin.com')
