"""Fresh application-owned Chromium process with minimal CDP instrumentation.

Never attach to an existing browser or reuse a user's profile. Its control
channel uses inherited pipes, with no TCP listener; only this process tree is
closed. Website traffic still goes through NativeTunnel.
"""
from __future__ import annotations

from collections import defaultdict
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

from .cdp_connection import CDPConnection
from .cdp_page import CDPPage
from .cdp_pipe import PipeTransport, node_executable
from .contracts import CrawlError


def edge_executable():
    if sys.platform == 'win32':
        roots = [os.environ.get(name) for name in ('PROGRAMFILES(X86)', 'PROGRAMFILES', 'LOCALAPPDATA')]
        candidates = [Path(root) / 'Microsoft/Edge/Application/msedge.exe' for root in roots if root]
    elif sys.platform == 'darwin':
        candidates = [Path('/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge')]
    else:
        candidates = [Path('/opt/microsoft/msedge/msedge')]
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    raise FileNotFoundError('selected Edge executable is unavailable')


def chrome_executable():
    # Only fixed application locations, never a daily profile or arbitrary path.
    if sys.platform == 'win32':
        roots = [os.environ.get(name) for name in ('PROGRAMFILES', 'PROGRAMFILES(X86)', 'LOCALAPPDATA')]
        candidates = [Path(root) / 'Google/Chrome/Application/chrome.exe' for root in roots if root]
    elif sys.platform == 'darwin':
        candidates = [Path('/Applications/Google Chrome.app/Contents/MacOS/Google Chrome')]
    else:
        candidates = [Path('/opt/google/chrome/chrome')]
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    raise FileNotFoundError("Executable doesn't exist for selected Chrome channel")


class CDPContext:
    def __init__(self, browser, ident, storage_state=None):
        self.browser, self.ident = browser, ident
        self._pages = {}
        self.callbacks = defaultdict(list)
        self.browser.connection.root.send('Browser.setDownloadBehavior', {
            'behavior': 'deny', 'browserContextId': ident})
        if storage_state is not None:
            if not isinstance(storage_state, dict) or storage_state.get('origins'):
                raise CrawlError('session_restore_failed')
            self.add_cookies(storage_state.get('cookies', []))

    @property
    def pages(self):
        return [page for page in self._pages.values() if not page.is_closed()]

    def on(self, name, callback):
        self.callbacks[name].append(callback)

    def new_page(self):
        if len(self.pages) >= 8:
            raise CrawlError('native_observation_limit')
        result = self.browser.connection.root.send('Target.createTarget', {
            'url': 'about:blank', 'browserContextId': self.ident})
        page = CDPPage(self, result['targetId'])
        self._pages[page.ident] = page
        for callback in tuple(self.callbacks.get('page', ())):
            callback(page)
        return page

    def new_cdp_session(self, page):
        if page.context is not self:
            raise CrawlError('native_protocol_error')
        result = self.browser.connection.root.send('Target.attachToTarget', {
            'targetId': page.ident, 'flatten': True})
        return self.browser.connection.session(result['sessionId'])

    def cookies(self, urls=None):
        if urls:
            # Browser cookie scope is delegated to Chromium, not string suffixes.
            page = next(iter(self.pages), None)
            if page is None:
                return []
            cookies = page.client.send('Network.getCookies', {'urls': list(urls)})['cookies']
        else:
            cookies = self.browser.connection.root.send('Storage.getCookies', {'browserContextId': self.ident})['cookies']
        # Keep the prior browser API's default for unspecified SameSite. Retain
        # partition markers so SavedSession excludes cookies it cannot restore
        # with an exact top-level-site binding, including opaque partition keys.
        return [{'sameSite': 'Lax', **cookie,
                 **({'partitionKey': None} if cookie.get('partitionKeyOpaque') and 'partitionKey' not in cookie else {})}
                for cookie in cookies]

    def add_cookies(self, cookies):
        self.browser.connection.root.send('Storage.setCookies', {
            'browserContextId': self.ident, 'cookies': cookies})

    def close(self):
        self.browser.connection.root.send('Target.disposeBrowserContext', {'browserContextId': self.ident})
        for page in self._pages.values():
            page._did_close()


class CDPBrowser:
    minimal_events = True

    def __init__(self, *, executable_path, headless, args, proxy, timeout=30000):
        self.connection = self.process = None
        self.contexts, self.callbacks = [], defaultdict(list)
        self.profile = Path(tempfile.mkdtemp(prefix='vibe-native-cdp-')).resolve()
        self.profile_parent = self.profile.parent
        self.cleanup_failed = False
        try:
            browser_args = [*args, '--enable-automation', '--no-first-run',
                # Match the previous SDK's stable startup configuration. The
                # testing build's field trials can spawn an initially blank
                # toolbar target; component extensions can spawn workers.
                '--disable-field-trial-config', '--disable-component-extensions-with-background-pages',
                '--disable-extensions', '--disable-default-apps', '--disable-component-update',
                '--no-default-browser-check', '--remote-debugging-pipe', '--user-data-dir=' + str(self.profile),
                '--proxy-server=' + proxy['server']]
            if headless:
                browser_args.append('--headless=new')
            # Preserve the existing Playwright Linux launch policy. Its default
            # chromiumSandbox=False applies to unprivileged CI runners too;
            # limiting this flag to uid 0 prevents those browsers from starting.
            if sys.platform.startswith('linux'):
                browser_args.append('--no-sandbox')
            browser_args.append('--no-startup-window')
            bridge = Path(__file__).with_name('cdp_bridge.js')
            if not bridge.is_file():
                raise CrawlError('playwright_driver_failed')
            command = [node_executable(), str(bridge), executable_path, json.dumps(browser_args)]
            self.process = subprocess.Popen(command, stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0,
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0)
            self.connection = CDPConnection(PipeTransport(self.process))
            self.version = self.connection.root.send('Browser.getVersion', timeout=timeout / 1000)['product'].split('/', 1)[-1]
            self.connection.root.on('Target.targetDestroyed', self._destroyed)
            self.connection.root.send('Target.setDiscoverTargets', {'discover': True})
        except Exception:
            self.close()
            raise

    def _destroyed(self, event):
        for context in self.contexts:
            page = context._pages.get(event['targetId'])
            if page:
                page._did_close()

    def on(self, name, callback):
        self.callbacks[name].append(callback)

    def new_context(self, *, service_workers='block', accept_downloads=False, storage_state=None):
        if service_workers != 'block' or accept_downloads or self.contexts:
            raise CrawlError('native_protocol_error')
        ident = self.connection.root.send('Target.createBrowserContext', {'disposeOnDetach': True})['browserContextId']
        context = CDPContext(self, ident, storage_state)
        self.contexts.append(context)
        return context

    def new_browser_cdp_session(self):
        ident = self.connection.root.send('Target.attachToBrowserTarget')['sessionId']
        return self.connection.session(ident)

    def is_connected(self):
        return bool(self.connection and not self.connection.closed and self.process.poll() is None)

    def close(self):
        if self.connection:
            if not self.connection.closed:
                try:
                    self.connection.root.send('Browser.close', timeout=3)
                except Exception:
                    pass
        if self.process and self.process.poll() is None:
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                # First let the bridge terminate its own browser via stdin
                # EOF. Do not send EOF immediately after Browser.close: that
                # would kill Chromium while it is flushing its private profile.
                if self.connection:
                    self.connection.close()
                try:
                    self.process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    pass
                if sys.platform == 'win32' and self.process.poll() is None:
                    subprocess.run(['taskkill', '/PID', str(self.process.pid), '/T', '/F'],
                        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                        creationflags=subprocess.CREATE_NO_WINDOW, timeout=10)
                elif self.process.poll() is None:
                    self.process.kill()
                self.process.wait(timeout=5)
        if self.connection:
            self.connection.close()
        if self.process:
            self.process.stdout.close()
        for context in self.contexts:
            for page in context._pages.values():
                page._did_close()
        # Validate the exact owned path before recursive Windows cleanup. No
        # search by browser name, reuse of a profile, or deletion of caller paths.
        if self.profile.resolve() != self.profile or self.profile.parent != self.profile_parent:
            raise CrawlError('native_protocol_error')
        if not self.profile.name.startswith('vibe-native-cdp-'):
            raise CrawlError('native_protocol_error')
        try:
            shutil.rmtree(self.profile)
        except FileNotFoundError:
            pass
        except OSError:
            self.cleanup_failed = True
