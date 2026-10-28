"""Bind Liepin's normal form to its own document and matching search reply.

CDP is used only to observe main-document identity. HTTP remains owned by the
existing bridge route, TLS checks, publisher rules and shared request ledger.
"""
from dataclasses import dataclass, field
import json

from .contracts import CrawlError, PageSnapshotChanged
from .native_navigation import observe_document
from .liepin_search import request_context
from .diagnostic_trace import observe, notify


@dataclass(frozen=True)
class SearchReply:
    operation: str
    context: dict = field(repr=False)
    payload: dict = field(repr=False)


class BridgeSearch:
    def __init__(self, backend):
        self.backend = backend
        self.sessions, self.documents = {}, {}
        self.blank_requests = {}
        self.sequence, self.reply = 0, None

    def bind(self, page):
        client = self.backend.context.new_cdp_session(page)
        target = client.send('Target.getTargetInfo')['targetInfo']['targetId']
        self.sessions[page] = (target, client)

        def observed(method, event):
            if self.sessions.get(page) != (target, client):
                return
            try:
                previous = self.documents.get(page)
                document = observe_document(previous, target, method, event)
                if document is not None:
                    self.documents[page] = document
                if method == 'Page.frameNavigated' and not event.get('frame', {}).get('parentId'):
                    blank_request = self.blank_requests.pop(page, None)
                    self.reset()  # Even a reload of the same URL retires its old results.
                    if (page is self.backend.page and not getattr(self.backend, '_bridge_closing', False)
                            and previous is not None and previous.url.startswith('https://')
                            and document.url == 'about:blank'):
                        # Observe and stop; never undo navigation or alter the
                        # publisher's security scripts. Initial/child blanks
                        # are not a lost collection/login document.
                        if self.backend.error is None:
                            self.backend.error = 'native_page_cleared'
                        with observe(getattr(self.backend, '_diagnostics', None), 'navigation',
                                     actor='browser', resource='document', impact='required_by_backend'):
                            reason = (blank_request[1] if blank_request is not None
                                      and blank_request[0] is previous else 'unknown')
                            notify(getattr(self.backend, '_diagnostics', None), 'mark',
                                   code='native_page_cleared', navigation_reason=reason)
            except (KeyError, TypeError, ValueError):
                self.backend.error = 'native_protocol_error'

        for method in ('Page.frameNavigated', 'Page.navigatedWithinDocument'):
            client.on(method, lambda event, method=method: observed(method, event))

        def requested(event, *, scheduled=False):
            # Passive metadata only. A requested navigation may be cancelled,
            # so it cannot stop collection until the matching document commits.
            if not isinstance(event, dict):
                return
            previous = self.documents.get(page)
            if (self.sessions.get(page) != (target, client) or page is not self.backend.page
                    or previous is None or not previous.url.startswith('https://')
                    or event.get('frameId') != previous.frame or event.get('url') != 'about:blank'
                    or not scheduled and event.get('disposition') != 'currentTab'):
                return
            from .diagnostic_trace import NAVIGATION_REASONS
            reason = event.get('reason')
            self.blank_requests[page] = (previous, reason if isinstance(reason, str)
                and reason in NAVIGATION_REASONS else 'unknown', scheduled)

        def cleared(event):
            pending = self.blank_requests.get(page)
            if (isinstance(event, dict) and pending is not None and pending[2]
                    and event.get('frameId') == pending[0].frame):
                self.blank_requests.pop(page, None)

        client.on('Page.frameRequestedNavigation', requested)
        client.on('Page.frameScheduledNavigation', lambda event: requested(event, scheduled=True))
        client.on('Page.frameClearedScheduledNavigation', cleared)
        client.send('Page.enable')
        observed('Page.frameNavigated', {'frame': client.send('Page.getFrameTree')['frameTree']['frame']})

        def closed():
            self.sessions.pop(page, None)
            self.documents.pop(page, None)
            self.blank_requests.pop(page, None)
        page.on('close', closed)

    def identity(self):
        backend, page = self.backend, self.backend.page
        backend._check_error()
        if page is None or page.is_closed():
            raise CrawlError('browser_closed')
        session, document = self.sessions.get(page), self.documents.get(page)
        if session is None or document is None or document.url != page.url:
            raise PageSnapshotChanged()
        target, client = session
        if self.documents.get(page) is not document or document.target != target:
            raise PageSnapshotChanged()
        return (target, id(page), id(client), document)

    def reset(self):
        self.reply = None

    def prepare(self, request):
        backend = self.backend
        if request.frame != backend.page.main_frame:
            raise CrawlError('native_surface_unsupported')
        identity = self.identity()
        try:
            raw = (request.post_data_buffer or b'').decode('utf-8')
        except UnicodeError:
            raise CrawlError('liepin_search_query_mismatch') from None
        context = request_context(backend.adapter, 'liepin_search', {'postData': raw}, backend.page.url)
        self.sequence += 1
        self.reply = None  # Older results are unavailable as soon as a new request starts.
        context['sequence'] = self.sequence
        return identity, context

    def complete(self, prepared, response):
        identity, context = prepared
        if self.identity() != identity or self.sequence != context['sequence']:
            raise PageSnapshotChanged()
        try:
            if len(response.body) > 1_000_000 or 'json' not in response.headers.get('content-type', '').lower():
                raise ValueError()
            payload = json.loads(response.body)
            if not isinstance(payload, dict):
                raise ValueError()
        except (ValueError, RecursionError, UnicodeError):
            raise CrawlError('native_business_response_invalid') from None
        self.reply = SearchReply('liepin_search', context, payload)

    def observations(self):
        return (self.reply,) if self.reply is not None else ()
