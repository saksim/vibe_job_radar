"""Optional real Chromium backend, run exclusively on its owner's worker thread."""
from __future__ import annotations

import html
import importlib
import stat
from pathlib import Path
import json
import re
from http.cookies import SimpleCookie
from urllib.parse import urljoin, urlsplit

from ..utils import domain_matches
from ..network_policy import current_policy
from .contracts import CrawlError, PageSnapshot
from .rate import RateLimit
from .read_retry import TransientReadFailure, document_failure, read_attempt
from .transport import PinnedTransport
from .diagnostic_trace import traced, notify
from .browser_health import (BrowserStartupError, HEALTH_MESSAGES, environment_report,
                             failed_report, supported_version, safe_text)


class PlaywrightBackend:
    def __init__(self, adapter, ledger, cancelled, progress=lambda *_: None, *,
                 headless=False, executable_path=None, transport_factory=PinnedTransport, channel=None, storage_state=None):
        if channel not in (None, 'msedge') or (channel and executable_path):
            raise ValueError('unsupported browser choice')
        self.adapter, self.cancelled = adapter, cancelled
        self.wire = transport_factory(adapter, ledger, cancelled, progress)
        # Capture the workspace snapshot here, on the owner context, not in the
        # first route callback: Playwright's dispatcher need not inherit ContextVars.
        # Injected offline/startup-only transports may not expose this capability.
        bind_policy = getattr(self.wire, 'bind_policy', None)
        if callable(bind_policy):
            bind_policy(current_policy())
        self.browser = self.context = self.page = self.runtime = None
        self.error = None
        self.wait_error = None
        self.auth_mode = False
        self.redirects = 0
        self.resource_denials = set()
        self._pagination_page = None
        self.startup_report = environment_report()
        self.startup_report['browser_channel'] = channel or 'bundled'
        self.startup_report['mode'] = 'headless' if headless else 'headed'
        try:
            # Reject incompatible metadata before importing any Playwright code.
            # Otherwise an in-process repair leaves the old client cached while
            # the installed driver/metadata already come from the new package.
            self.startup_report['stage'] = 'version'
            version = self.startup_report['playwright_version']
            if version is None:
                raise BrowserStartupError(failed_report(self.startup_report,
                    ModuleNotFoundError('Playwright distribution is absent'), code='playwright_missing'))
            try:
                compatible = supported_version(version)
            except ImportError as exc:
                raise BrowserStartupError(failed_report(self.startup_report, exc,
                    code='version_validator_missing')) from exc
            if not compatible:
                raise BrowserStartupError(failed_report(self.startup_report,
                    RuntimeError('unsupported Playwright version'), code='playwright_incompatible'))
            self.startup_report['stage'] = 'import'
            importlib.invalidate_caches()
            from playwright.sync_api import sync_playwright
            self.startup_report['stage'] = 'driver'
            self.runtime = sync_playwright().start()
            if channel:
                # Let the SDK resolve its documented stable Edge channel. The
                # bundled Chromium path says nothing about installed Edge. Never
                # attach to a daily profile or install/overwrite a system browser.
                self.startup_report.update(stage='executable', executable_path='',
                                           executable_exists=None)
            else:
                expected = str(executable_path or self.runtime.chromium.executable_path)
                self.startup_report.update(stage='executable', executable_path=safe_text(expected),
                                           executable_exists=None)
                try:
                    self.startup_report['executable_exists'] = stat.S_ISREG(Path(expected).stat().st_mode)
                except (FileNotFoundError, NotADirectoryError):
                    self.startup_report['executable_exists'] = False
                # PermissionError and other stat errors must retain their real cause;
                # a path that cannot be inspected is not a proved missing executable.
                # Headed collection needs the regular Chromium build, not only the
                # separately installed headless shell. Test backends can select a path.
                if (not headless or executable_path) and not self.startup_report['executable_exists']:
                    raise BrowserStartupError(failed_report(self.startup_report, FileNotFoundError(expected), code='browser_executable_missing'))
            args = ['--disable-background-networking', '--disable-quic', '--disable-sync',
                    '--force-webrtc-ip-handling-policy=disable_non_proxied_udp']
            options = {'headless': headless, 'args': args, 'timeout': 30000}
            if executable_path:
                options['executable_path'] = executable_path
            if channel:
                options['channel'] = channel
            self.startup_report.update(stage='launch', launch_tested=True)
            self.browser = self.runtime.chromium.launch(**self._launch_options(options))
            self.startup_report.update(stage='context', executable_exists=True)
            if channel:
                self.startup_report['browser_version'] = self.browser.version
            context_options = {'service_workers': 'block', 'accept_downloads': False}
            if storage_state is not None:
                context_options['storage_state'] = storage_state
            self.context = self.browser.new_context(**context_options)
            self._configure_context()
            self.page = self._new_page()
            self.page.set_default_timeout(6000)
            self.startup_report.update(stage='ready', code='browser_ready', ready=True,
                message=HEALTH_MESSAGES['browser_ready'])
        except Exception as exc:
            report = exc.report if isinstance(exc, BrowserStartupError) else failed_report(self.startup_report, exc)
            self.close()
            raise BrowserStartupError(report) from exc

    def _new_page(self):
        return self.context.new_page()

    def _launch_options(self, options):
        return options

    def _configure_context(self):
        self.context.route('**/*', self._route)
        self.context.route_web_socket('**/*', lambda ws: ws.close())
        self.context.on('page', self._bind_page)

    def _bind_page(self, page):
        page.on('download', lambda download: download.cancel())
        page.on('dialog', lambda dialog: dialog.dismiss())
        page.on('close', lambda *_: self._restore_open_page())
        self.page = page

    def _restore_open_page(self):
        # Native login popups commonly close themselves. Do not destroy the
        # surviving context (and its in-memory session) just because its newest
        # tab closed. This only selects a page; it never navigates or clears an
        # error, and snapshot()/cards() still validate the selected surface.
        if self.page and not self.page.is_closed():
            return
        self.page = None
        for candidate in reversed(self.context.pages if self.context else []):
            if candidate.is_closed():
                continue
            try:
                self.adapter.accept_url(candidate.url)
            except (CrawlError, ValueError):
                continue
            self.page = candidate
            return

    def _cookies(self, url, values):
        host, secure = urlsplit(url).hostname, urlsplit(url).scheme == 'https'
        for value in values:
            try:
                jar = SimpleCookie(); jar.load(value)
                for name, morsel in jar.items():
                    domain = morsel['domain'].lstrip('.') or host
                    if not domain_matches(host, domain):
                        continue
                    # Delegate domain/path matching to Chromium; keep sessions in memory.
                    entry = {'name': name, 'value': morsel.value, 'domain': morsel['domain'] or host,
                             'path': morsel['path'] or '/', 'secure': bool(morsel['secure']) or secure,
                             'httpOnly': bool(morsel['httponly'])}
                    same = morsel['samesite'].capitalize()
                    if same in {'Lax', 'Strict', 'None'}:
                        entry['sameSite'] = same
                    if morsel['max-age']:
                        import time
                        entry['expires'] = time.time() + int(morsel['max-age'])
                    self.context.add_cookies([entry])
            except Exception:
                # A malformed cookie never relaxes origin restrictions.
                continue

    def bind_diagnostics(self, trace):
        # Same explicit object in callback greenlets; no ambient ContextVar.
        self._diagnostics = trace
        self.wire._diagnostics = trace
        notify(trace, 'set_browser_version', value=getattr(getattr(self, 'browser', None), 'version', None))

    @traced('route', 'browser', route=True)
    def _route(self, route):
        request = route.request
        try:
            if self.cancelled.is_set():
                notify(getattr(self, '_diagnostics', None), 'mark', code='paused')
                route.abort('blockedbyclient')
                return  # Idle polling must not poison the next explicit action.
            if self.error == 'read_transient_failure':
                raise self.wait_error or CrawlError(self.error)
            url, kind, method = request.url, request.resource_type, request.method
            p = urlsplit(url)
            if not self.wire.allowed_resource(url):
                if p.hostname:
                    self.resource_denials.add(p.hostname)
                raise CrawlError('resource_domain_blocked')
            if method not in {'GET', 'HEAD', 'POST', 'OPTIONS'}:
                raise CrawlError('method_blocked')
            if method == 'POST' and (not self.auth_mode or p.hostname not in self.adapter.login_hosts):
                raise CrawlError('write_not_allowed')
            if kind == 'document':
                self.adapter.accept_url(url) if not self.auth_mode else self._auth_navigation(url)
                self._reserve_document_page(request)
                if not self.auth_mode:
                    self.wire.ensure_robots(url)
            # Reject credentials crossing a redirect: browser redirects are replaced
            # by a new, checked navigation; XHR redirects fail closed.
            result = self.wire.fetch(url, method, request.all_headers(), request.post_data_buffer,
                                     required=kind in {'document', 'xhr', 'fetch'})
            notify(getattr(self, '_diagnostics', None), 'mark', status=result.status)
            self._cookies(url, result.cookies)
            if 300 <= result.status < 400:
                destination = urljoin(url, result.headers.get('location', ''))
                self.adapter.accept_url(destination) if not self.auth_mode else self._auth_navigation(destination)
                self.redirects += 1
                if kind != 'document' or self.redirects > 5:
                    raise CrawlError('redirect_requires_attention')
                # This is a fresh navigation, so browser routing runs again. Never
                # return a 30x that Chromium could follow outside this handler.
                escaped = html.escape(destination, quote=True)
                route.fulfill(status=200, content_type='text/html',
                              body=f'<meta http-equiv="refresh" content="0;url={escaped}">')
                return
            if result.status >= 500:
                raise document_failure(self, url, method, kind, result.status,
                    result.headers.get('retry-after', ''),
                    main=bool(getattr(request, 'frame', None) is not None
                              and request.frame == getattr(self.page, 'main_frame', None)))
            filtered = {k: v for k, v in result.headers.items() if k not in {
                'content-length', 'content-encoding', 'transfer-encoding', 'connection',
                'set-cookie', 'alt-svc', 'report-to', 'nel'}}
            route.fulfill(status=result.status, headers=filtered, body=result.body)
        except CrawlError as exc:
            notify(getattr(self, '_diagnostics', None), 'mark', code=exc.code)
            if (request.resource_type not in {'document', 'xhr', 'fetch'}
                    and exc.code in {'http_401', 'http_403'}):
                self.resource_denials.add('optional_' + exc.code)
                route.abort('blockedbyclient')
                return
            if (request.resource_type == 'document' or exc.code not in {
                    'resource_domain_blocked', 'write_not_allowed', 'method_blocked'}):
                transient = {'rate_wait', 'publisher_wait', 'cooldown', 'http_429',
                             'hourly_limit', 'daily_limit', 'read_transient_failure'}
                if not isinstance(exc, (RateLimit, TransientReadFailure)) or self.error is None or self.error in transient:
                    self.error = exc.code
                    self.wait_error = exc if isinstance(exc, (RateLimit, TransientReadFailure)) else None
            try:
                route.abort('blockedbyclient')
            except Exception:
                pass
        except Exception:
            notify(getattr(self, '_diagnostics', None), 'mark', code='network_error')
            self.error = 'network_error'
            try:
                route.abort('failed')
            except Exception:
                pass

    def _reserve_document_page(self, request):
        prepaid = self._pagination_page
        if prepaid is not None and request.frame == prepaid.main_frame:
            self._pagination_page = None
            return
        self.wire.reserve('page')

    def _auth_navigation(self, url):
        p = urlsplit(url)
        if (p.scheme != 'https' or not p.hostname or p.port not in (None, 443) or p.username or p.password
                or not any(domain_matches(p.hostname, d) for d in self.adapter.domains)):
            raise CrawlError('login_origin_changed')

    def _settle(self):
        # Bounded dynamic wait; no blind unbounded scrolling or fixed success claim.
        for _ in range(12):
            if self.cancelled.is_set():
                raise CrawlError('paused')
            if self.error:
                raise getattr(self, 'wait_error', None) or CrawlError(self.error)
            self.page.wait_for_timeout(250)
        if self.error:
            raise getattr(self, 'wait_error', None) or CrawlError(self.error)

    @traced('navigation', 'browser', url=True)
    @read_attempt
    def open(self, url: str, *, authentication: bool = False) -> PageSnapshot:
        self.auth_mode, self.error, self.redirects = authentication, None, 0
        self.wait_error = None
        self.adapter.accept_url(url)
        self.wire.blocked.clear()  # a new explicit navigation still obeys durable cooldown
        try:
            self.page.goto(url, wait_until='domcontentloaded', timeout=90000)
            self._settle()
            return self.snapshot()
        except CrawlError:
            raise
        except Exception as exc:
            raise (getattr(self, 'wait_error', None) or CrawlError(self.error or 'page_not_ready')) from exc

    def snapshot(self) -> PageSnapshot:
        if self.error:
            raise getattr(self, 'wait_error', None) or CrawlError(self.error)
        if not self.page or self.page.is_closed():
            raise CrawlError('browser_closed')
        url = self.page.url
        self.adapter.accept_url(url)
        text = self.page.locator('body').inner_text(timeout=5000)
        if self.adapter.challenged(text, url):
            raise CrawlError('manual_required')
        content = self.page.content()
        if len(content) > 5_000_000:
            raise CrawlError('response_too_large')
        return PageSnapshot(url, content)

    def _visible(self, selectors, scope=None):
        for selector in selectors:
            loc = (scope or self.page).locator(selector)
            for i in range(min(loc.count(), 6)):
                candidate = loc.nth(i)
                if candidate.is_visible() and candidate.is_enabled():
                    return candidate
        return None

    @traced('pagination', 'browser')
    def next_page(self) -> bool:
        self.auth_mode, self.error, self.redirects = False, None, 0
        self.wait_error = None
        button = self._visible(self.adapter.next_selectors)
        if not button or button.get_attribute('aria-disabled') == 'true' or 'disabled' in (button.get_attribute('class') or '').split():
            return False
        self.wire.ensure_robots(self.page.url)
        self.wire.reserve('page')  # Reserve once, before either a document or SPA action.
        self._pagination_page = self.page
        try:
            button.click(timeout=90000)
            self._settle()
            return True
        finally:
            # Never leak a SPA/no-navigation credit to an unrelated later visit.
            self._pagination_page = None

    def collection_mode(self):
        self.auth_mode = False
        # Reading an existing DOM must not erase a failed required request.
        # A fresh explicit open() starts a new checked navigation. Retain the
        # pre-existing transient wait handling for publisher-paced recovery.
        if self.error in {'rate_wait', 'publisher_wait', 'cooldown', 'http_429',
                          'hourly_limit', 'daily_limit', 'paused'}:
            self.error = None
            self.wait_error = None

    def pump(self):
        self._restore_open_page()
        # Keeps a visible browser responsive while waiting for manual assistance.
        if self.page and not self.page.is_closed():
            self.page.wait_for_timeout(50)

    def export_session_cookies(self):
        # Browser-owned context only; no hidden pages, HTTP, DOM or form fields.
        return self.context.cookies()

    def alive(self):
        self._restore_open_page()
        return bool(self.browser and self.browser.is_connected() and self.page and not self.page.is_closed())

    def close(self):
        if self.browser:
            try:
                self.browser.close()
            except Exception:
                pass
        if self.runtime:
            try:
                self.runtime.stop()
            except Exception:
                pass
        self.runtime = self.browser = self.context = self.page = None
