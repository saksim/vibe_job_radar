"""Owned human-verification frames for the explicit installed-browser login.

Only a frame attached to our current login page, whose exact document request
has passed the reviewed contract, may acquire a child CDP session. Popups,
workers, unrelated frames and normal collection keep the existing refusal.
"""
import json
from urllib.parse import urlsplit

from .contracts import CrawlError


EVENTS = ('Page.frameNavigated', 'Page.navigatedWithinDocument', 'Page.frameAttached',
          'Page.frameDetached', 'Fetch.requestPaused', 'Fetch.authRequired',
          'Network.dataReceived', 'Network.loadingFinished', 'Network.loadingFailed',
          'Target.attachedToTarget', 'Target.detachedFromTarget')


def enabled(backend):
    return (getattr(backend, 'human_login', False) is True and backend.auth_mode
            and getattr(backend, '_direct_cdp', False) is True)


class LoginFrames:
    def __init__(self, backend):
        self.backend = backend
        self.parents = {}  # (owned main session, frame) -> parent frame
        self.approved = set()
        self.owners = {}   # child session -> owned main session
        self.protocol_sessions = {}

    def owner(self, session):
        return self.owners.get(session, session)

    def current(self, owner):
        b = self.backend
        return (enabled(b) and b._bound_pages.get(b.page) == owner
                and owner in b._sessions and b.page and not b.page.is_closed())

    def attached_frame(self, session, data):
        owner = self.owner(session)
        if not self.current(owner):
            return
        frame, parent = data.get('frameId'), data.get('parentFrameId')
        # Only one reviewed widget level; nested/unknown browsing surfaces do
        # not gain access by sharing a browser context or hostname.
        if (isinstance(frame, str) and parent == self.backend._sessions.get(owner)
                and len(self.parents) < 8):
            self.parents[owner, frame] = parent

    def request(self, session, event, rule):
        b = self.backend
        owner, frame = self.owner(session), event.get('frameId')
        child = session in self.owners or bool(frame and frame != b._sessions.get(session))
        widget = rule.key.startswith('captcha_')
        if not child:
            if rule.key == 'captcha_frame':
                raise CrawlError('login_origin_changed')
            return
        if (not self.current(owner) or not widget or (owner, frame) not in self.parents):
            raise CrawlError('native_surface_unsupported')
        if event.get('resourceType') == 'Document':
            if rule.key != 'captcha_frame':
                raise CrawlError('native_surface_unsupported')
            self.approved.add((owner, frame))
        elif (owner, frame) not in self.approved:
            raise CrawlError('native_surface_unsupported')

    def attach_target(self, parent_session, data):
        b = self.backend
        owner = self.owner(parent_session)
        info = data['targetInfo']
        target, protocol = info.get('targetId'), data.get('sessionId')
        if (not self.current(owner) or info.get('type') != 'iframe'
                or info.get('openerId') or (owner, target) not in self.approved
                or not isinstance(protocol, str) or len(b._sessions) >= 8
                or info.get('browserContextId', b._context_id) != b._context_id):
            return False
        # Frame ownership comes from Page.frameAttached + the permitted
        # Document request, not from a target's untrusted URL alone.
        session = 'login-frame:' + target
        if session in b._sessions:
            return False
        client = b.browser.connection.session(protocol)
        self.owners[session] = owner
        self.protocol_sessions[protocol] = session
        b._page_sessions[session] = client
        for method in EVENTS:
            client.on(method, lambda event, name=method: b._received({
                'sessionId': session, 'message': json.dumps({'method': name, 'params': event})}))
        b._install_target(session, info)
        return True

    def detached_frame(self, session, data):
        # A renderer swap retains the same frame and its reviewed document.
        if data.get('reason') == 'swap':
            return
        owner, frame = self.owner(session), data.get('frameId')
        self.parents.pop((owner, frame), None)
        self.approved.discard((owner, frame))
        child = 'login-frame:' + str(frame)
        if child in self.owners:
            self.backend._detached({'sessionId': child})

    def detached_target(self, data):
        child = self.protocol_sessions.get(data.get('sessionId'))
        if child:
            self.backend._detached({'sessionId': child})

    def retire(self, session):
        for child, owner in tuple(self.owners.items()):
            if owner == session:
                self.backend._detached({'sessionId': child})
        self.owners.pop(session, None)
        self.protocol_sessions = {p:s for p,s in self.protocol_sessions.items() if s != session}
        self.parents = {k:v for k,v in self.parents.items() if k[0] != session}
        self.approved = {k for k in self.approved if k[0] != session}

    def clear(self):
        self.parents.clear(); self.approved.clear()
        self.owners.clear(); self.protocol_sessions.clear()


def document_frame_source(backend, session, event):
    """An additive CSP permits only the reviewed widget on a login document."""
    if not enabled(backend) or session in backend._login_frames.owners:
        return None
    p = urlsplit(event.get('request', {}).get('url', ''))
    if (p.scheme == 'https' and p.hostname in {'www.liepin.com', 'c.liepin.com'}
            and p.port in (None, 443) and not p.username and not p.password):
        return 'https://turing.captcha.qcloud.com/template/'
    return None
