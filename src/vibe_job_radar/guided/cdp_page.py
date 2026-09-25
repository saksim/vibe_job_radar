"""Owned CDP page operations without global Runtime/console subscriptions."""
from __future__ import annotations

import base64
from collections import defaultdict
from contextlib import contextmanager
import json
from pathlib import Path
import re
import time

from .cdp_dom import Locator, PageOperationError, text_locator
from .contracts import CrawlError, PageSnapshotChanged


class Frame:
    def __init__(self, page, ident):
        self.page, self.ident, self.url = page, ident, 'about:blank'


class Response:
    def __init__(self, page, ident, response):
        self.page, self.ident = page, ident
        self.status = int(response['status'])
        self.headers = {k.lower(): v for k, v in response.get('headers', {}).items()}

    def header_value(self, name):
        return self.headers.get(name.lower())

    def body(self):
        result = self.page.client.send('Network.getResponseBody', {'requestId': self.ident})
        data = result.get('body', '')
        if len(data) > 6_700_000:
            raise PageOperationError('response_too_large')
        body = base64.b64decode(data, validate=True) if result.get('base64Encoded') else data.encode('utf-8')
        if len(body) > 5_000_000:
            raise PageOperationError('response_too_large')
        return body


class Dialog:
    def __init__(self, page):
        self.page = page

    def dismiss(self):
        self.page.client.send('Page.handleJavaScriptDialog', {'accept': False})


class CDPPage:
    def __init__(self, context, ident):
        self.context, self.ident = context, ident
        self.connection = context.browser.connection
        self.client = context.new_cdp_session(self)
        self.timeout = 6000
        self.callbacks = defaultdict(list)
        self.main_frame = Frame(self, ident)
        self.closed = False
        self.navigation = 0
        self.loader = ''
        self.loaded, self.responses = {}, {}
        self.client.on('Page.frameNavigated', self._navigated)
        self.client.on('Page.navigatedWithinDocument', self._within_document)
        self.client.on('Page.lifecycleEvent', self._lifecycle)
        self.client.on('Network.responseReceived', self._response)
        self.client.on('Page.javascriptDialogOpening', lambda _: self._emit('dialog', Dialog(self)))
        self.client.send('Page.enable')
        self.client.send('Page.setLifecycleEventsEnabled', {'enabled': True})
        self.client.send('Network.enable', {'maxTotalBufferSize': 5_000_000, 'maxResourceBufferSize': 5_000_000})

    @property
    def url(self):
        return self.main_frame.url

    def on(self, name, callback):
        self.callbacks[name].append(callback)

    def _emit(self, name, *args):
        for callback in tuple(self.callbacks.get(name, ())):
            callback(*args)

    def _navigated(self, event):
        frame = event['frame']
        if frame.get('parentId'):
            return
        self.main_frame.ident = frame['id']
        self.main_frame.url = frame['url']
        self.loader = frame.get('loaderId', '')
        self.navigation += 1
        self._emit('framenavigated', self.main_frame)

    def _within_document(self, event):
        if event['frameId'] == self.main_frame.ident:
            self.main_frame.url = event['url']
            self.navigation += 1
            self._emit('framenavigated', self.main_frame)

    def _lifecycle(self, event):
        if event['frameId'] != self.main_frame.ident:
            return
        self.loaded.setdefault(event['loaderId'], set()).add(event['name'])
        while len(self.loaded) > 20:
            self.loaded.pop(next(iter(self.loaded)))

    def _response(self, event):
        if event.get('type') == 'Document' and event.get('frameId') == self.main_frame.ident:
            self.responses[event['loaderId']] = Response(self, event['requestId'], event['response'])
            while len(self.responses) > 20:
                self.responses.pop(next(iter(self.responses)))

    def _wait(self, condition, timeout):
        deadline = time.monotonic() + timeout / 1000
        while not condition():
            if self.closed:
                raise PageOperationError('browser_closed')
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise PageOperationError('page_not_ready')
            self.connection.pump(min(.05, remaining))

    @staticmethod
    def _load_event(wait_until):
        if wait_until not in {'domcontentloaded', 'load'}:
            raise PageOperationError('page_not_ready')
        return 'DOMContentLoaded' if wait_until == 'domcontentloaded' else 'load'

    def goto(self, url, *, wait_until='load', timeout=30000):
        name = self._load_event(wait_until)
        try:
            result = self.client.send('Page.navigate', {'url': url}, timeout=timeout / 1000)
        except CrawlError:
            raise PageOperationError('page_not_ready') from None
        if result.get('errorText'):
            error = result['errorText']
            # Only the fixed browser net-error token can leave this layer.
            raise PageOperationError(error if re.fullmatch(r'(?:net::)?ERR_[A-Z0-9_]+', error) else 'page_not_ready')
        loader = result.get('loaderId')
        if loader:
            self._wait(lambda: name in self.loaded.get(loader, ()), timeout)
            return self.responses.get(loader)
        self._wait(lambda: self.url == url, timeout)
        return None

    @contextmanager
    def expect_navigation(self, *, wait_until='load', timeout=30000):
        name, before = self._load_event(wait_until), self.navigation
        yield
        self._wait(lambda: self.navigation > before and name in self.loaded.get(self.loader, ()), timeout)

    def wait_for_url(self, url, *, timeout=30000):
        self._wait(lambda: self.url == url, timeout)

    def wait_for_timeout(self, milliseconds):
        self.connection.wait(milliseconds / 1000)

    def set_default_timeout(self, milliseconds):
        self.timeout = milliseconds

    def evaluate(self, expression, arg=None):
        if self.closed:
            raise PageOperationError('browser_closed')
        before = self.navigation
        source = '(()=>{const value=(' + expression + ');return typeof value==="function"?value(' + json.dumps(arg) + '):value;})()'
        try:
            result = self.client.send('Runtime.evaluate', {'expression': source,
                'returnByValue': True, 'awaitPromise': True, 'timeout': self.timeout},
                timeout=self.timeout / 1000 + 1)
        except CrawlError:
            if self.navigation != before:
                raise PageSnapshotChanged() from None
            raise PageOperationError('page_not_ready') from None
        if self.navigation != before:
            raise PageSnapshotChanged()
        if result.get('exceptionDetails'):
            raise PageOperationError('page_not_ready')
        return result.get('result', {}).get('value')

    def locator(self, selector):
        return Locator.select(self, selector)

    def get_by_text(self, text, *, exact=False):
        return text_locator(self, text, exact=exact)

    def get_by_role(self, role, *, name):
        return text_locator(self, name, exact=True, role=role)

    def content(self):
        content = self.evaluate('(document.documentElement?document.documentElement.outerHTML:"").slice(0,5000001)')
        if len(content) > 5_000_000:
            raise PageOperationError('response_too_large')
        return content

    def title(self):
        return self.evaluate('document.title')

    def set_content(self, html):
        self.evaluate('()=>{document.open();document.write(' + json.dumps(html) + ');document.close();}')

    def wait_for_function(self, expression, *, timeout=30000):
        self._wait(lambda: bool(self.evaluate(expression)), timeout)

    def bring_to_front(self):
        self.client.send('Page.bringToFront')

    def screenshot(self, *, path):
        result = self.client.send('Page.captureScreenshot', {'format': 'png'})
        Path(path).write_bytes(base64.b64decode(result['data'], validate=True))

    def is_closed(self):
        return self.closed or self.connection.closed

    def _did_close(self):
        if self.closed:
            return
        self.closed = True
        self._emit('close')
        self.callbacks.clear()

    def close(self):
        if not self.closed:
            self.context.browser.connection.root.send('Target.closeTarget', {'targetId': self.ident})
            self._did_close()
