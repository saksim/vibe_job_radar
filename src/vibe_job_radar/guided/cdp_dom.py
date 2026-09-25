"""The small DOM/input surface used by the native acquisition workflow.

Reads use explicit Runtime.evaluate commands, never Runtime event collection.
Clicks and text input use normal browser Input commands. No website function,
automation marker, console method or credential storage implementation changes.
"""
from __future__ import annotations

import json
import re
import time


class PageOperationError(RuntimeError):
    """Fixed-message operation failure, with no expression or input values."""


VISIBLE = "e=>!!(e.getClientRects().length&&getComputedStyle(e).visibility!=='hidden')"


class Locator:
    def __init__(self, page, expression):
        self.page, self.expression = page, expression

    @classmethod
    def select(cls, page, selector, scope='[document]'):
        # Support only the selectors used by the reviewed adapters. Unknown
        # Playwright-specific syntax remains an explicit operation failure.
        visible = ':visible' in selector
        selector = selector.replace(':visible', '')
        text = re.search(r':has-text\(("[^"\\]*"|\'[^\'\\]*\')\)$', selector)
        needle = None
        if text:
            needle = text[1][1:-1]
            selector = selector[:text.start()]
        expression = ('Array.from(new Set((' + scope + ').flatMap(e=>Array.from(e.querySelectorAll('
                      + json.dumps(selector) + ')))))')
        if needle is not None:
            expression += '.filter(e=>(e.textContent||"" ).includes(' + json.dumps(needle) + '))'
        if visible:
            expression += '.filter(' + VISIBLE + ')'
        return cls(page, expression)

    def locator(self, selector):
        return self.select(self.page, selector, self.expression)

    def filter(self, *, has):
        if has.page is not self.page:
            raise PageOperationError('page_not_ready')
        return Locator(self.page, '(' + self.expression + ').filter(e=>('
                       + has.expression + ').some(child=>e.contains(child)))')

    def nth(self, index):
        if not isinstance(index, int) or index < 0:
            raise PageOperationError('page_not_ready')
        return Locator(self.page, '(' + self.expression + ').slice(' + str(index) + ',' + str(index + 1) + ')')

    def count(self):
        return self.page.evaluate('(' + self.expression + ').length')

    def _read(self, body):
        return self.page.evaluate('(()=>{const elements=' + self.expression
            + ';if(elements.length!==1)return {found:false};const e=elements[0];'
            + 'return {found:true,value:(' + body + ')};})()')

    def _wait(self, read, *, timeout=None):
        deadline = time.monotonic() + (self.page.timeout if timeout is None else timeout) / 1000
        while True:
            result = read()
            if result is not None:
                return result
            if time.monotonic() >= deadline:
                raise PageOperationError('page_not_ready')
            self.page.connection.pump(min(.05, max(0, deadline - time.monotonic())))

    def wait_for(self, *, state='visible', timeout=None):
        if state not in {'visible', 'attached', 'hidden', 'detached'}:
            raise PageOperationError('page_not_ready')
        def ready():
            count = self.count()
            if state == 'detached':
                return True if count == 0 else None
            if state == 'hidden':
                return True if count == 0 or not self.is_visible() else None
            if count == 1 and (state == 'attached' or self.is_visible()):
                return True
            return None
        self._wait(ready, timeout=timeout)

    def is_visible(self):
        result = self._read('(' + VISIBLE + ')(e)')
        return bool(result.get('found') and result.get('value'))

    def is_enabled(self):
        result = self._read('!e.matches(":disabled")')
        return bool(result.get('found') and result.get('value'))

    def inner_text(self, *, timeout=None):
        def read():
            result = self._read('(e.innerText||"").slice(0,5000001)')
            return result['value'] if result['found'] else None
        value = self._wait(read, timeout=timeout)
        if len(value) > 5_000_000:
            raise PageOperationError('response_too_large')
        return value

    def get_attribute(self, name, *, timeout=None):
        self.wait_for(state='attached', timeout=timeout)
        result = self._read('e.getAttribute(' + json.dumps(name) + ')')
        if not result['found']:
            raise PageOperationError('page_not_ready')
        return result['value']

    def evaluate(self, expression):
        result = self._read('(' + expression + ')(e)')
        if not result['found']:
            raise PageOperationError('page_not_ready')
        return result['value']

    def click(self, *, timeout=None):
        self.page.bring_to_front()
        def point():
            coordinates = ('(()=>{if(!(' + VISIBLE + ')(e)||e.matches(":disabled"))return null;'
                'e.scrollIntoView({block:"center",inline:"center"});const r=e.getBoundingClientRect();'
                'const x=r.x+r.width/2,y=r.y+r.height/2;const hit=document.elementFromPoint(x,y);'
                'return hit&&(hit===e||e.contains(hit))?{x,y}:null;})()')
            before = self._read(coordinates)
            if not before['found'] or before.get('value') is None:
                return None
            # DOM geometry can precede the first compositor frame. A click at
            # that point can silently miss an otherwise visible form button.
            # Wait for paint opportunities, then recheck hit target/geometry;
            # no fixed sleep, synthetic DOM click or repeated form submission.
            self.page.evaluate('() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))')
            after = self._read(coordinates)
            return after.get('value') if after == before else None
        position = self._wait(point, timeout=timeout)
        with self.page.input_action(timeout=timeout):
            for kind in ('mousePressed', 'mouseReleased'):
                self.page.client.send('Input.dispatchMouseEvent', {
                    'type': kind, **position, 'button': 'left', 'clickCount': 1})

    def fill(self, value, *, timeout=None):
        if not isinstance(value, str):
            raise PageOperationError('page_not_ready')
        self.wait_for(state='visible', timeout=timeout)
        selected = self._read('(()=>{if(!e.matches("input,textarea")||e.readOnly||e.disabled)return false;'
            'e.focus();if(document.activeElement!==e)return false;e.select();return true;})()')
        if not selected['found'] or selected['value'] is not True:
            raise PageOperationError('page_not_ready')
        self.page.client.send('Input.insertText', {'text': value})
        # Only an equality boolean leaves the page; no credential readback.
        actual = self._read('e.value===' + json.dumps(value))
        if not actual['found'] or actual['value'] is not True:
            raise PageOperationError('page_not_ready')

    def press(self, key, *, timeout=None):
        if key != 'Enter':
            raise PageOperationError('page_not_ready')
        self.wait_for(state='visible', timeout=timeout)
        target = self._read('(()=>{if(document.activeElement!==e||e.disabled||e.readOnly)return false;'
            'const r=e.getBoundingClientRect(),hit=document.elementFromPoint(r.x+r.width/2,r.y+r.height/2);'
            'return hit===e;})()')
        if not target['found'] or target['value'] is not True:
            raise PageOperationError('page_not_ready')
        with self.page.input_action(timeout=timeout):
            for kind in ('keyDown', 'keyUp'):
                self.page.client.send('Input.dispatchKeyEvent', {'type':kind,'key':'Enter','code':'Enter',
                    'windowsVirtualKeyCode':13,'nativeVirtualKeyCode':13,
                    **({'text':'\r'} if kind=='keyDown' else {})})


def text_locator(page, text, *, exact=False, role=None):
    if role not in (None, 'button'):
        raise PageOperationError('page_not_ready')
    selector = 'button,[role="button"],input[type="submit"]' if role else '*'
    compare = ('text===' if exact else 'text.includes(') + json.dumps(text) + ('' if exact else ')')
    expression = ('Array.from(document.querySelectorAll(' + json.dumps(selector)
        + ')).filter(e=>{const text=(e.getAttribute("aria-label")||e.innerText||e.value||e.textContent||"").trim();return '
        + compare + ';}).filter(' + VISIBLE + ')')
    if not role:
        expression = '(()=>{const nodes=' + expression + ';return nodes.filter(e=>!nodes.some(child=>child!==e&&e.contains(child)));})()'
    return Locator(page, expression)
