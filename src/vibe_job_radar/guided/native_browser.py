"""Opt-in Chromium native HTTP/TLS with a public-target opaque CONNECT guard.

CDP Fetch observes/authorizes each owned-page hop. Application-created blank
pages receive controls before navigation. Native documents add a restrictive
CSP before scripts can create unsupported surfaces; original
publisher security policies remain enforced.
Cross-origin requests require an exact, code-owned CORS operation contract.
Only application-owned browser targets are used. Worker/OOPIF targets are stopped
before running until their complete request accounting is separately supported.
"""
from __future__ import annotations

import base64
from collections import deque
from dataclasses import dataclass, field, replace
import json
import time
from urllib.parse import urljoin, urlsplit

from .browser import PlaywrightBackend
from .contracts import CrawlError, PageSnapshotChanged
from .diagnostic_trace import notify, observe, observe_robots, traced
from .native_policy import NativeRobots, contract_for
from .native_tunnel import NativeTunnel
from .native_errors import native_transport_failure, target_closed_by_driver
from .native_documents import continue_document_response
from .rate import RateLimit
from .transport import PinnedTransport, WireResponse
from .read_retry import TransientReadFailure, document_failure, read_attempt
from ..network import USER_AGENT


@dataclass(frozen=True)
class BusinessObservation:
    epoch: int
    operation: str
    received_at: float
    payload: dict | list = field(repr=False)
    context: dict = field(default_factory=dict, repr=False)


class NativeControl(PinnedTransport):
    """Reuse only durable pacing/policy binding. Never replay an HTTP request."""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.rules = {}

    def fetch(self, *args, **kwargs):
        raise RuntimeError('native backend cannot replay HTTP')

    def reserve_request_nowait(self, *, origin):
        if self.cancelled.is_set():
            raise CrawlError('paused')
        self.ledger.reserve(self.adapter.key, 'request', origin=origin)

    def ensure_robots(self, url):
        p = urlsplit(url); origin = 'https://' + p.netloc
        policy = self.rules.get(origin)
        if policy is None:
            raise CrawlError('robots_unavailable')
        if not policy.allowed(url):
            raise CrawlError('robots_denied')

    def install_robots(self, origin, response, content_type, body):
        observe_robots(getattr(self, '_diagnostics', None),
                       WireResponse(response, {'content-type': content_type}, body))
        rules = NativeRobots(response, content_type, body)
        self.ledger.set_publisher(self.adapter.key, origin, delay=rules.delay)
        for count, seconds in rules.windows:
            self.ledger.set_publisher(self.adapter.key, origin, delay=rules.delay,
                requests=count, seconds=seconds)
        self.rules[origin] = rules


class NativeBackend(PlaywrightBackend):
    def __init__(self, adapter, ledger, cancelled, progress=lambda *_: None, *,
                 headless=False, executable_path=None, channel=None, storage_state=None):
        self.contract = contract_for(adapter)
        self.tunnel = self._cdp = None
        self._sessions, self._pending, self._requests, self._hops = {}, {}, {}, {}
        self._command = self._epoch = 0
        self._observations = deque(maxlen=20)
        self._observed_bytes = 0
        self._latest_business = {}
        self._closing = self._halted = self._loading_robots = False
        self._robots_url = ''
        self._auth_attempts = set()
        self._adopting = set()
        self._page_sessions, self._bound_pages = {}, {}
        self._committed_pages = set()
        self._page_creation = 0
        self._rejected_targets = set()
        self._pending_rejected_targets = []
        self._rejected_pages = []
        self.native_counts = {'document':0, 'business':0, 'asset':0, 'robots':0, 'login':0,
                              'blocked':0, 'responses':0}
        super().__init__(adapter, ledger, cancelled, progress, headless=headless,
            executable_path=executable_path, channel=channel, transport_factory=NativeControl,
            storage_state=storage_state)
        self.startup_report['network_backend'] = 'native'

    def _launch_options(self, options):
        self.tunnel = NativeTunnel(self.contract.hosts, self.wire.network_policy, self.cancelled)
        return {**options, 'proxy': {'server': self.tunnel.endpoint},
                'args': [*options['args'], '--proxy-bypass-list=<-loopback>', '--block-new-web-contents']}

    def _launch_browser(self, options):
        from .cdp_browser import CDPBrowser, edge_executable
        options = dict(options)
        channel = options.pop('channel', None)
        options['executable_path'] = (edge_executable() if channel == 'msedge'
            else options.get('executable_path') or self.runtime.chromium.executable_path)
        return CDPBrowser(**options)

    def _configure_context(self):
        # Install before any page is created. Target debugger pause does not
        # alone prevent the browser's initial popup network request.
        self._native_cors = any(rule.cors_origin for rule in self.contract.rules)
        self._direct_cdp = getattr(self.browser, 'minimal_events', False) is True
        # Playwright's route layer auto-fulfills CORS OPTIONS. For reviewed
        # CORS contracts use direct CDP controls, so the publisher really
        # receives and decides preflight. Unsupported targets get an abort-only
        # Fetch guard before their deferred close, never collection controls.
        if not self._native_cors and not self._direct_cdp:
            self.context.route('**/*', self._ownership_route)
        if not self._direct_cdp:
            self.context.route_web_socket('**/*', lambda ws: ws.close())
        self.context.on('page', self._page_created)
        self._cdp = self.browser.new_browser_cdp_session()
        contexts = self._cdp.send('Target.getBrowserContexts')['browserContextIds']
        if len(contexts) != 1:
            raise CrawlError('native_protocol_error')
        self._context_id = contexts[0]
        # A context extra-header UA can be dropped on native redirects. Set
        # the actual browser-reported UA plus our token on every owned target
        # before it runs; never rotate or impersonate another browser.
        self._native_user_agent = self._cdp.send('Browser.getVersion')['userAgent'] + ' ' + USER_AGENT
        self._cdp.on('Target.attachedToTarget', self._attached)
        self._cdp.on('Target.receivedMessageFromTarget', self._received)
        self._cdp.on('Target.detachedFromTarget', self._detached)
        # Non-flattened sessions use documented Target.sendMessageToTarget.
        # The controller's pipes reach only its newly launched browser; it does
        # not attach to a daily browser or use Playwright private internals.
        self._cdp.send('Target.setAutoAttach', {'autoAttach':True,
            'waitForDebuggerOnStart':True, 'flatten':True})

    def _ownership_route(self, route):
        """Only admit requests whose main page already has our CDP controls.

        This is an early ownership gate, not a transport: the browser retains
        original headers, TLS, cookies, method and body. Site operations and
        redirects still require the independent native controller's approval.
        """
        page = None
        try:
            frame = route.request.frame
            page = frame.page
            session = self._bound_pages.get(page)
            owned = (session in self._page_sessions and frame == page.main_frame)
            allowed = (owned and not self._closing and not self._halted
                       and not self.cancelled.is_set())
        except Exception:
            allowed = owned = False
        if not allowed:
            if not owned:
                self.cancelled.set()
                self._fatal('native_surface_unsupported')
            try:
                route.abort('blockedbyclient')
            except Exception as exc:
                # Deferred rejection can close the target while abort yields
                # to Playwright. A confirmed closed page cannot send this
                # request; retain the original refusal/cancellation.
                # The driver's exact closed-target exception is also evidence
                # of termination, even before the page close event arrives.
                # Unknown errors still escape; no route is continued here.
                try:
                    closed = page is not None and page.is_closed() is True
                except Exception:
                    closed = False
                reason = ('closed_page' if closed else 'closed_target'
                          if target_closed_by_driver(exc) else 'unconfirmed')
                counts = self.__dict__.setdefault('_ownership_abort_counts', {})
                counts[reason] = counts.get(reason, 0) + 1
                if reason == 'unconfirmed':
                    raise
            return
        # No parameter overrides, fetch, response reconstruction or retries.
        route.continue_()

    def _page_created(self, page):
        # Never synchronously close a popup from its creation event. Chromium
        # may be waiting for that very event to finish window.open(), while the
        # browser Target guard is already closing the paused target. Closing
        # again here can deadlock headed Edge. No page is admitted in this path.
        if self._closing:
            return
        if not self._page_creation:
            self.cancelled.set()  # Opaque tunnel also stops, even for an unexpected target.
            self._fatal('native_surface_unsupported')
            pending = self.__dict__.setdefault('_rejected_pages', [])
            if len(pending) < 8 and page not in pending:
                pending.append(page)

    def _reject_target(self, target):
        # Do not call closeTarget inside attachedToTarget: closing during
        # window creation can deadlock the opener's synchronous browser call.
        # Our code never resumes an unsupported target. The context ownership
        # route blocks its initial HTTP independently of debugger pause state.
        rejected = self.__dict__.setdefault('_rejected_targets', set())
        self.cancelled.set()
        self._fatal('native_surface_unsupported')
        if target in rejected:
            return
        if len(rejected) >= 128:
            return
        rejected.add(target)
        self.__dict__.setdefault('_pending_rejected_targets', []).append(target)
        if self.__dict__.get('_native_cors', False):
            from .native_cors import quarantine_target
            quarantine_target(self, target)

    def _drain_rejected_pages(self):
        # Called outside target/page events. Our controller sends no resume
        # or request-continuation commands to these unowned targets.
        targets = self.__dict__.setdefault('_pending_rejected_targets', [])
        while targets:
            target = targets.pop(0)
            # Cancellation and rejection already stopped this target. Retire
            # its protocol callbacks BEFORE closeTarget yields to the driver:
            # a late quarantine-command error must not close the main job tab.
            # This only removes our bookkeeping; it never resumes a target,
            # disables interception or continues a pending network request.
            for session, owned_target in tuple(self._sessions.items()):
                if owned_target == target:
                    self._detached({'sessionId': session})
            try:
                self._cdp.send('Target.closeTarget', {'targetId': target})
            except Exception:
                self.cancelled.set()
                self._fatal('native_protocol_error')
                raise CrawlError('native_protocol_error') from None
        pending = self.__dict__.setdefault('_rejected_pages', [])
        while pending:
            page = pending.pop(0)
            if not page.is_closed():
                page.close()

    def _new_page(self):
        # Only application-requested blank tabs can become controlled surfaces.
        # No polling or access-rule fallback: configuration completes as part of
        # creating the page, before the caller can navigate it.
        self._page_creation += 1
        try:
            page = self.context.new_page()
            self._bind_page(page)
            if self.error:
                raise CrawlError(self.error)
            return page
        finally:
            self._page_creation -= 1

    def _bind_page(self, page):
        if page in self._bound_pages:
            return
        if self._closing or not self._page_creation or page.url not in ('', 'about:blank'):
            page.close()
            if not self._closing:
                self._fatal('native_surface_unsupported')
            return
        client = self.context.new_cdp_session(page)
        info = client.send('Target.getTargetInfo')['targetInfo']
        target = info['targetId']
        if info.get('openerId') or target not in self._sessions.values():
            client.detach()
            page.close()
            self._fatal('native_surface_unsupported')
            return
        # Retire the temporary target-creation attachment BEFORE configuring
        # Fetch on the owned page session. Edge does not deliver
        # interception events through the old non-flattened relay reliably.
        # Removing the old attachment first preserves page-level interception.
        for old, known in tuple(self._sessions.items()):
            if known == target:
                self._cdp.send('Target.detachFromTarget', {'sessionId':old})
                self._detached({'sessionId':old})
        session = 'page:' + target
        self._page_sessions[session] = client
        self._bound_pages[page] = session
        for method in ('Fetch.requestPaused', 'Fetch.authRequired',
                       'Network.dataReceived', 'Network.loadingFinished',
                       'Network.loadingFailed', 'Target.attachedToTarget'):
            client.on(method, lambda data, name=method: self._received({
                'sessionId':session, 'message':json.dumps({'method':name, 'params':data})}))
        page.on('close', lambda *_: self._detached({'sessionId':session}))
        page.on('framenavigated', lambda frame: self._main_navigation(page, frame))
        self._install_target(session, info)
        super()._bind_page(page)

    def _main_navigation(self, page, frame):
        """A live page leaving for blank is a stop, not an empty job result.

        Do not infer why it happened, undo the navigation or modify publisher
        scripts. Initial/scratch/child frames are not collection documents.
        """
        if (self._closing or self._loading_robots or page is not self.page
                or frame != page.main_frame or page not in self._bound_pages):
            return
        committed = self.__dict__.setdefault('_committed_pages', set())
        if frame.url.startswith('https://'):
            committed.add(page)
        elif page in committed and frame.url == 'about:blank':
            self._epoch += 1
            self._observations.clear()
            self.__dict__.get('_latest_business', {}).clear()
            self._observed_bytes = 0
            with observe(getattr(self, '_diagnostics', None), 'navigation', actor='browser',
                         resource='document', impact='required_by_backend'):
                notify(getattr(self, '_diagnostics', None), 'mark', code='native_page_cleared')
                self._fatal('native_page_cleared')

    def _send(self, session, method, params=None, callback=None):
        if self._closing:
            return
        # Public page handles are local identifiers, never relay session IDs.
        # A page can close while a synchronous CDP call yields to callbacks.
        if session.startswith('page:') and session not in self._page_sessions:
            return
        self._command += 1
        ident = self._command
        if len(self._pending) >= 512:
            raise CrawlError('native_observation_limit')
        self._pending[ident] = (session, callback)
        try:
            client = self._page_sessions.get(session)
            if client is not None:
                result = client.send(method, params or {})
                self._pending.pop(ident, None)
                if callback:
                    callback(result)
                return
            self._cdp.send('Target.sendMessageToTarget', {'sessionId':session,
                'message':json.dumps({'id':ident, 'method':method, 'params':params or {}})})
        except Exception:
            self._pending.pop(ident, None)
            # Only an explicitly retired/closed page can cancel an in-flight
            # command harmlessly. Active-session protocol errors still fail.
            if client is not None and session not in self._sessions:
                return
            raise

    @staticmethod
    def _browser_chrome_ui(info):
        # Observed in Chrome153 headed startup: this is the address-bar UI,
        # not a web page or collection popup. Never generalize to all 'other'
        # targets, extensions or chrome:// pages.
        return (info.get('type') in {'other', 'browser_ui'} and not info.get('openerId')
                and info.get('url') in {'chrome://omnibox-popup.top-chrome',
                                        'chrome://omnibox-popup.top-chrome/',
                                        'chrome://omnibox-popup.top-chrome/omnibox_popup_aim.html'})

    def _target_in_context(self, info):
        expected = getattr(self, '_context_id', None)
        return expected is None or info.get('browserContextId') == expected

    def _attached(self, event):
        if self._closing:
            return
        info, session = event['targetInfo'], event['sessionId']
        if not self._target_in_context(info):
            # Chromium can emit an initial default-context blank target. It is
            # not a page of our isolated collection context. Release only this
            # automatic debugger attachment; do not cancel the collection.
            self._cdp.send('Target.detachFromTarget', {'sessionId': session})
            return
        if self._browser_chrome_ui(info):
            # Detach our automatic debugger only. Do not initialize request
            # controls, send credentials, navigate or close browser UI.
            self._cdp.send('Target.detachFromTarget', {'sessionId': session})
            return
        if (info['type'] != 'page' or len(self._sessions) >= 8
                or info.get('openerId') or not self._page_creation
                or info.get('url', '') not in ('', 'about:blank')):
            self._reject_target(info['targetId'])
            return
        target = info['targetId']
        if session in self._sessions or target in self._adopting:
            # attachToTarget may emit its attachment event before its response.
            # The outer adoption owns initialization; a late duplicate event
            # must not detach/reconfigure the already-owned command session.
            return
        self._adopting.add(target)
        try:
            # Keep the new command attachment alive while retiring the unused
            # automatic attachment. Configure Fetch only AFTER that detach:
            # otherwise a reentrant attachment installs controls before another
            # debugger is removed, which can reset the target's interception.
            result = self._cdp.send('Target.attachToTarget', {'targetId':target,'flatten':False})
            legacy = result['sessionId']
            self._cdp.send('Target.detachFromTarget', {'sessionId':session})
            self._install_target(legacy, info)
        except Exception:
            self._fatal('native_protocol_error')
            self._cdp.send('Target.closeTarget', {'targetId':target})
        finally:
            self._adopting.discard(target)

    def _install_target(self, session, info):
        self._sessions[session] = info['targetId']
        try:
            if session not in self._page_sessions:
                # The only permitted target here is an app-created blank tab.
                # Release creation so Playwright can expose its public Page;
                # no document is navigated before _bind_page configures Fetch.
                self._send(session, 'Runtime.runIfWaitingForDebugger')
                return
            self._send(session, 'Network.enable', {'maxTotalBufferSize':5_000_000,'maxResourceBufferSize':1_000_000})
            self._send(session, 'Network.setUserAgentOverride', {'userAgent':self._native_user_agent})
            self._send(session, 'Network.setCacheDisabled', {'cacheDisabled':True})
            self._send(session, 'Fetch.enable', {'patterns':[
                {'urlPattern':'*','requestStage':'Request'},
                {'urlPattern':'*','requestStage':'Response'}], 'handleAuthRequests':True})
            self._send(session, 'Target.setAutoAttach', {'autoAttach':True,
                'waitForDebuggerOnStart':True,'flatten':True})
            self._send(session, 'Runtime.runIfWaitingForDebugger')
        except Exception:
            self._fatal('native_protocol_error')
            self._cdp.send('Target.closeTarget', {'targetId':info['targetId']})

    def _detached(self, event):
        session = event['sessionId']
        if pacer := self.__dict__.get('_request_pacer'):
            pacer.retire(session)
        self._sessions.pop(session, None)
        self._page_sessions.pop(session, None)
        for page, bound in tuple(self._bound_pages.items()):
            if bound == session:
                self._bound_pages.pop(page, None)
                self.__dict__.get('_committed_pages', set()).discard(page)
        for key in [k for k,v in self._pending.items() if v[0] == session]:
            self._pending.pop(key, None)
        for key in [k for k in self._requests if k[0] == session]:
            self._requests.pop(key, None); self._hops.pop(key, None)
        self._auth_attempts.difference_update(
            key for key in tuple(self._auth_attempts) if key[0] == session)

    def _close_owned_page(self, page):
        """Retire callbacks before closing our temporary page, not its peers.

        No Fetch.disable/continue is sent: pending requests die with the tab.
        A close failure remains fatal; it is not a reason to resume a tab whose
        controller is no longer registered.
        """
        session = self._bound_pages.get(page)
        if session is not None:
            self._detached({'sessionId': session})
        try:
            page.close()
        except Exception:
            self._fatal('native_protocol_error')
            # The retired target is no longer in _sessions; close it explicitly.
            if session is not None:
                try:
                    self._cdp.send('Target.closeTarget', {'targetId': session.removeprefix('page:')})
                except Exception:
                    self.cancelled.set()
            raise CrawlError('native_protocol_error') from None

    def _received(self, event):
        if self._closing:
            return
        try:
            session = event['sessionId']
            # Late events from a retired scratch tab must not poison the job
            # tab or send its credentials through a stale protocol handle.
            if session not in self._sessions:
                return
            message = json.loads(event['message'])
            if 'id' in message:
                entry = self._pending.pop(message['id'], None)
                if entry and entry[0] in self._sessions:
                    if 'error' in message:
                        # Never expose protocol errors containing raw URLs/bodies.
                        self._fatal('native_protocol_error')
                    elif entry[1]:
                        entry[1](message.get('result', {}))
                return
            method, data = message.get('method'), message.get('params', {})
            if method == 'Target.attachedToTarget':
                if self._browser_chrome_ui(data['targetInfo']):
                    self._send(session, 'Target.detachFromTarget', {'sessionId': data['sessionId']})
                else:
                    # Dedicated/shared workers and OOPIFs remain unsupported.
                    self._reject_target(data['targetInfo']['targetId'])
            elif method == 'Fetch.requestPaused':
                self._paused(session, data)
            elif method == 'Fetch.authRequired':
                self._authenticate(session, data)
            elif method == 'Network.dataReceived':
                record = self._requests.get((session,data['requestId']))
                if record:
                    record['size'] += data.get('dataLength',0)
                    if record['size'] > 5_000_000:
                        self._fatal('response_too_large')
                        self._send(session,'Page.stopLoading')
            elif method == 'Network.loadingFinished':
                self._finished(session, data)
            elif method == 'Network.loadingFailed':
                if pacer := self.__dict__.get('_request_pacer'):
                    pacer.retire(session, data['requestId'])
                key=(session,data['requestId']); record=self._requests.pop(key,None)
                self._hops.pop(key,None)
                if record and record['role'] != 'asset' and not self.cancelled.is_set() and not self.error:
                    err = data.get('errorText','')
                    code = native_transport_failure(err, self.tunnel.last_error) or 'network_error'
                    self._fatal(code)
        except Exception:
            self._fatal('native_protocol_error')

    def _fatal(self, code, error=None):
        if not self.error:
            self.error = code
            self.wait_error = error if isinstance(error,(RateLimit,TransientReadFailure)) else None
        self._halted = True
        if code == 'native_protocol_error' and self._cdp:
            # A failed interception command must not leave a page running with
            # an unknown policy state. Close only this application's targets.
            for target in tuple(set(self._sessions.values())):
                try:
                    self._cdp.send('Target.closeTarget', {'targetId':target})
                except Exception:
                    pass

    def _authenticate(self, session, event):
        challenge = event['authChallenge']; key=(session,event['requestId'])
        origin = urlsplit(challenge.get('origin',''))
        expected = urlsplit(self.tunnel.endpoint)
        if (challenge.get('source') == 'Proxy' and origin.hostname == expected.hostname
                and origin.port == expected.port and key not in self._auth_attempts
                and len(self._auth_attempts) < 128
                and not self.cancelled.is_set() and not self._halted):
            self._auth_attempts.add(key)
            response = {'response':'ProvideCredentials','username':self.tunnel.username,'password':self.tunnel.password}
        else:
            response = {'response':'CancelAuth'}
            self._fatal('native_proxy_auth_failed' if challenge.get('source') == 'Proxy' else 'http_401')
        self._send(session, 'Fetch.continueWithAuth', {'requestId':event['requestId'],'authChallengeResponse':response})

    def _paused(self, session, event):
        request = event['request']; url = request['url']; kind=event.get('resourceType','Other')
        response = 'responseStatusCode' in event or 'responseErrorReason' in event
        ignored = not response and self.contract.ignored_request(url, request['method'], kind)
        resource = {'XHR':'xhr','Fetch':'fetch','Document':'document','Stylesheet':'stylesheet',
                    'Script':'script','Image':'image','Font':'font','Media':'media'}.get(kind,'other')
        with observe(getattr(self,'_diagnostics',None), 'http_request' if response else 'route',
                actor='browser', url=url, method=request['method'], resource=resource,
                impact='optional' if ignored or resource in {'script','stylesheet','image','font','media'} else 'required_by_backend'):
            try:
                if self.cancelled.is_set():
                    raise CrawlError('paused')
                if not getattr(self, 'policy_check', lambda: True)():
                    raise CrawlError('native_policy_changed')
                if self._halted:
                    raise self.wait_error or CrawlError(self.error or 'site_stopped')
                if ignored:
                    notify(getattr(self,'_diagnostics',None),'mark',code='native_optional_request_blocked')
                    self.native_counts['blocked'] += 1
                    self._send(session,'Fetch.failRequest',{'requestId':event['requestId'],'errorReason':'BlockedByClient'})
                    return
                if response:
                    self._response_paused(session,event)
                else:
                    self._request_paused(session,event)
            except Exception as exc:
                code = getattr(exc,'code','native_protocol_error')
                notify(getattr(self,'_diagnostics',None),'mark',code=code)
                self.native_counts['blocked'] += 1
                # An unknown business request must not become a silent empty list.
                # Unknown optional assets are reported without poisoning the task.
                if kind in {'Document','Fetch','XHR','Preflight'} or code not in {'native_operation_unreviewed','resource_domain_blocked'}:
                    self._fatal(code,exc)
                try:
                    self._send(session,'Fetch.failRequest',{'requestId':event['requestId'],'errorReason':'BlockedByClient'})
                except Exception:
                    self._fatal('native_protocol_error')

    def _request_paused(self, session, event):
        r=event['request']; url=r['url']; kind=event.get('resourceType','Other')
        p, _ = self.contract.target(url)
        if event.get('frameId') and event['frameId'] != self._sessions.get(session):
            raise CrawlError('native_surface_unsupported')
        robots = self._loading_robots and url == self._robots_url and kind == 'Document' and r['method']=='GET'
        if robots:
            role, operation='robots','robots'
        else:
            rule=self.contract.match(url,r['method'],kind,authentication=self.auth_mode)
            rule.validate_headers(r['method'], r.get('headers', {}))
            role,operation=rule.role,rule.key
            if role != 'asset':
                self.wire.ensure_robots(url)
        if len(r.get('postData','').encode('utf-8')) > 1_000_000:
            raise CrawlError('request_too_large')
        if kind == 'Document' and not robots:
            if self._pagination_page is not None:
                self._pagination_page=None
            else:
                self.wire.reserve('page')
        key=(session,event.get('networkId',event['requestId']))
        context = {}
        bind = getattr(self.adapter, 'native_request_context', None)
        if role == 'business' and r['method'] != 'OPTIONS' and callable(bind):
            context = bind(operation, r, self.page.url)
        from .native_pacing import NativeRequestPacer, PausedRequest
        if '_request_pacer' not in self.__dict__:
            self._request_pacer = NativeRequestPacer(self)
        record = {'context': context, 'epoch':self._epoch,'operation':operation,'role':role,'size':0,
                  'url':url,'status':None, 'json':False}
        self._request_pacer.submit(PausedRequest(session, event['requestId'], key, 'https://'+p.netloc,
            record, role == 'business' and r['method'] != 'OPTIONS', not robots and rule.authentication))

    def _admit_request(self, item):
        record = item.record
        context, operation = record['context'], record['operation']
        if item.business:
            # The browser has requested newer data even while pacing holds it.
            # Never expose an older response during that new asynchronous wait.
            self._business_sequence = getattr(self, '_business_sequence', 0) + 1
            context['sequence'] = self._business_sequence
            self.__dict__.setdefault('_latest_business', {})[operation] = self._business_sequence
            self._observations = deque((o for o in self._observations if o.operation != operation), maxlen=20)

    def _continue_request(self, item):
        record = item.record
        self._requests[item.key] = record
        self.native_counts[record['role']] += 1
        self._send(item.session,'Fetch.continueRequest',{'requestId':item.request_id})

    def _response_paused(self, session, event):
        url=event['request']['url']; status=event.get('responseStatusCode',0)
        notify(getattr(self,'_diagnostics',None),'mark',status=status)
        key=(session,event.get('networkId',event['requestId'])); record=self._requests.get(key)
        if not record:
            raise CrawlError('native_unaccounted_response')
        if 'responseErrorReason' in event:
            # The already-authorized native request has failed. Preserve its
            # original browser failure instead of replacing it with our own
            # BlockedByClient. This resumes error delivery, not a new request.
            self._send(session,'Fetch.continueRequest',{'requestId':event['requestId']})
            return
        headers={h['name'].lower():h['value'] for h in event.get('responseHeaders',[])}
        if status in {401,403,429} and (record['role']!='asset' or status==429):
            delay=self.wire._retry_seconds(headers.get('retry-after',''))
            self.wire.ledger.cool(self.adapter.key,delay)
            if status==429:
                raise RateLimit(delay,'http_429',next_allowed_at=self.wire.ledger.clock()+delay)
            raise CrawlError('http_'+str(status))
        if status >= 500 and record['role']!='asset':
            deadlines=[h['value'] for h in event.get('responseHeaders',[]) if h['name'].lower()=='retry-after']
            raise document_failure(self,url,event['request']['method'],
                'document' if event.get('resourceType')=='Document' else 'other',status,
                deadlines[0] if len(deadlines)==1 else ('invalid' if deadlines else ''),
                main=bool(event.get('frameId') and event['frameId']==self._sessions.get(session)
                          and record['role']!='robots' and not self._loading_robots))
        if 300 <= status < 400:
            if event.get('resourceType') == 'Document':
                self._read_redirected = True
            if record['role']=='robots' or status==304:
                raise CrawlError('robots_unavailable' if record['role']=='robots' else 'native_unaccounted_response')
            target=urljoin(url,headers.get('location',''))
            # Same-origin redirects keep native browser cookie/method semantics.
            # Cross-origin redirects need a site-specific credential contract.
            if urlsplit(target).netloc != urlsplit(url).netloc:
                raise CrawlError('redirect_requires_attention')
            self.contract.target(target)
            self._hops[key]=self._hops.get(key,0)+1
            if self._hops[key] > 5:
                raise CrawlError('redirect_requires_attention')
        size=headers.get('content-length','')
        if size and (not size.isdigit() or int(size)>5_000_000):
            raise CrawlError('response_too_large')
        record['status']=status
        record['json']=headers.get('content-type','').split(';')[0].strip().lower()=='application/json'
        self.native_counts['responses']+=1
        # CORS documents need a fully parsed policy before scripts execute.
        # No HTTP request is repeated; actual preflight/business replies remain
        # native. See native_documents for the bounded decoded-body delivery.
        # Robots error pages are control data, never a login/content page. In
        # particular an HTML 404 must not execute scripts while being inspected.
        continue_document_response(self, session, event, robots=record['role'] == 'robots')

    def _finished(self, session, event):
        key=(session,event['requestId']); record=self._requests.pop(key,None)
        self._hops.pop(key,None)
        if (not record or record['role']!='business' or not record['json']
                or record['epoch']!=self._epoch or record['status']!=200 or self._halted):
            return
        sequence = record.get('context', {}).get('sequence')
        if sequence is not None and self.__dict__.get('_latest_business', {}).get(record['operation']) != sequence:
            return
        if record['size'] > 1_000_000:
            self._fatal('native_observation_limit'); return
        def store(result):
            if (record['epoch'] != self._epoch or self._closing or self._halted
                    or (sequence is not None and self.__dict__.get('_latest_business', {}).get(record['operation']) != sequence)):
                return
            data=result.get('body','')
            if len(data)>1_400_000:
                self._fatal('native_observation_limit'); return
            raw=base64.b64decode(data,validate=True) if result.get('base64Encoded') else data.encode('utf-8')
            if len(raw)>1_000_000 or self._observed_bytes+len(raw)>4_000_000 or len(self._observations)>=20:
                self._fatal('native_observation_limit'); return
            try:
                payload=json.loads(raw)
                if not isinstance(payload,(dict,list)):
                    raise ValueError()
            except (ValueError, RecursionError):
                self._fatal('native_business_response_invalid'); return
            self._observations.append(BusinessObservation(self._epoch,record['operation'],time.time(),payload,record.get('context', {})))
            self._observed_bytes+=len(raw)
        self._send(session,'Network.getResponseBody',{'requestId':event['requestId']},store)

    def snapshot(self):
        self._check_error()
        return replace(super().snapshot(), business=self.observations(),
                       business_required=self.adapter.key == 'liepin')

    def observations(self):
        """Private local payloads for a reviewed site adapter, never diagnostic API."""
        return tuple(self._observations)

    def _check_error(self):
        self._drain_rejected_pages()
        if not getattr(self, 'policy_check', lambda: True)():
            self._fatal('native_policy_changed')
        if self.error:
            raise self.wait_error or CrawlError(self.error)
        if self.cancelled.is_set():
            raise CrawlError('paused')

    def _load_robots(self):
        main=self.page
        for origin in self.contract.rule_origins:
            if not any('https://' + rule.host == origin and
                       (not rule.authentication or self.auth_mode) for rule in self.contract.rules):
                continue
            if origin in self.wire.rules:
                continue
            self._loading_robots=True; self._robots_url=origin+'/robots.txt'
            scratch=None
            try:
                scratch=self._new_page()
                response=scratch.goto(self._robots_url,wait_until='load',timeout=45000)
                self._check_error()
                if response is None:
                    raise CrawlError('robots_unavailable')
                raw=response.body()
                self.wire.install_robots(origin,response.status,response.header_value('content-type') or '',raw)
            except CrawlError:
                raise
            except Exception as exc:
                code = self.error or native_transport_failure(exc, self.tunnel.last_error)
                raise CrawlError(code or 'robots_unavailable') from exc
            finally:
                try:
                    if scratch:
                        self._close_owned_page(scratch)
                finally:
                    self._loading_robots=False; self._robots_url=''; self.page=main

    @traced('navigation','browser',url=True)
    @read_attempt
    def open(self,url,*,authentication=False):
        self.adapter.accept_url(url)
        self.contract.match(url,'GET','Document',authentication=authentication)
        self.auth_mode=authentication; self.error=self.wait_error=None; self._halted=False
        self._epoch+=1; self._observations.clear()
        self.__dict__.get('_latest_business', {}).clear(); self._observed_bytes=0
        self._load_robots()
        self.wire.ensure_robots(url)
        try:
            self.page.goto(url,wait_until='domcontentloaded',timeout=90000)
            self._settle()
            return self.snapshot()
        except CrawlError:
            raise
        except Exception as exc:
            raise self.wait_error or CrawlError(self.error or native_transport_failure(exc, self.tunnel.last_error) or 'page_not_ready') from exc

    def _settle(self, *, search=False):
        deadline=time.monotonic()+15
        while time.monotonic()<deadline:
            self._check_error()
            # Readiness is site content/response/challenge, not network-idle or a
            # fixed sleep. The existing parser still determines usable job data.
            try:
                body = self.page.locator('body')
                if not body.count():
                    # A navigation can replace the document after DOMContentLoaded.
                    self.page.wait_for_timeout(100)
                    continue
                text=body.inner_text(timeout=1000)
                if self.adapter.challenged(text,self.page.url):
                    raise CrawlError('manual_required')
                if self.auth_mode and text.strip() and not search:
                    return
                ready = getattr(self.adapter, 'native_ready', None)
                if callable(ready) and ready(self.observations()):
                    return
                snap=self.snapshot()
                try:
                    if self.adapter.cards(snap):
                        return
                except CrawlError:
                    try:
                        self.adapter.detail(snap); return
                    except CrawlError as exc:
                        if exc.code == 'job_unavailable':
                            raise  # A closed job is final; its recommendations are not its JD.
                        pass
            except PageSnapshotChanged:
                # Discard this read if a normal navigation/history update ran
                # while CDP was answering it. Reobserve inside the same deadline;
                # never navigate, resubmit input, or turn another error into a retry.
                pass
            self.page.wait_for_timeout(100)
        # Pumping browser callbacks can consume the remaining deadline. Preserve
        # a native refusal delivered there instead of replacing it with timeout.
        self._check_error()
        raise CrawlError('page_not_ready')

    def search_entry_url(self, url, *, keyword):
        # Only the default keyword-only request has an implemented form route.
        # An explicit seed with extra conditions keeps its checked navigation;
        # those conditions must never disappear when switching to the form.
        if (self.adapter.key != 'liepin'
                or self.adapter.accept_url(url) != self.adapter.search_url(keyword)):
            return url
        return self.adapter.search_base

    def open_search(self, url, *, keyword, authentication=False):
        entry = self.search_entry_url(url, keyword=keyword)
        if entry == url:
            return self.open(url, authentication=authentication)
        opened = self.open(entry, authentication=authentication)
        if authentication:
            # Login controls must remain reachable even if a login overlay
            # obscures the search field. The user's later resume searches with
            # that same session; a login action does not submit a keyword first.
            return opened
        from .liepin_form import submit_search
        return submit_search(self, keyword)

    def next_page(self):
        self._check_error()
        return super().next_page()

    def ensure_page_access(self, url):
        self._check_error()
        if not self.page or self.page.is_closed():
            raise CrawlError('browser_closed')
        if self.page.url != url:
            raise PageSnapshotChanged()
        accepted = self.adapter.accept_url(url)
        document = getattr(self.page, 'document_url', None)
        base = self.adapter.search_base
        # Only the observed query-free Liepin document can update its search
        # address in place. The committed document URL comes from the browser's
        # frame navigation event, never from the task or publisher HTML. Actual
        # document/API requests still pass _request_paused and their own robots.
        if (self.adapter.key == 'liepin' and isinstance(document, str) and document == base
                and self.page in self._bound_pages):
            current, entry = urlsplit(accepted), urlsplit(base)
            if ((current.scheme, current.netloc, current.path) ==
                    (entry.scheme, entry.netloc, entry.path) and not entry.query):
                self.wire.ensure_robots(document)
                return
        self.wire.ensure_robots(url)

    def _before_pagination_click(self):
        # Looking for a next button is not a navigation. Retain the current
        # API result when the button is absent/disabled or permission/quota
        # rejects the action. Invalidate only immediately before an actual
        # click, so late responses cannot populate the next page with old data.
        self._check_error()
        self._epoch += 1
        self._observations.clear()
        self.__dict__.get('_latest_business', {}).clear()
        self._observed_bytes = 0

    def collection_mode(self):
        super().collection_mode()
        if self.error is None:
            self._halted = False

    def pump(self):
        self._drain_rejected_pages()
        if not self._closing and not getattr(self, 'policy_check', lambda: True)():
            self._fatal('native_policy_changed')
        super().pump()
        # A page can clear itself after open() returned while the service waits
        # for normal login. Surface the fatal event on the owning worker then,
        # instead of leaving the UI indefinitely claiming the login page is open.
        if not self._closing and self.error:
            raise self.wait_error or CrawlError(self.error)

    def close(self):
        self._closing=True
        if pacer := self.__dict__.get('_request_pacer'):
            pacer.close()
        super().close()
        if self.tunnel:
            self.tunnel.close(); self.tunnel=None
        self._sessions.clear(); self._pending.clear(); self._requests.clear(); self._hops.clear()
        self._observations.clear()
        self.__dict__.get('_latest_business', {}).clear(); self._auth_attempts.clear()
        self._page_sessions.clear(); self._bound_pages.clear()
        self.__dict__.get('_committed_pages', set()).clear()
        self._rejected_targets.clear(); self._pending_rejected_targets.clear(); self._rejected_pages.clear()
