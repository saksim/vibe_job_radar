"""Bounded code locations only: no exception text, locals, paths or source lines."""
from pathlib import Path
import re
import traceback

SCRIPTS = Path(__file__).resolve().parent
ALLOWED = frozenset({'verify_windows_portable.py', 'system_pac_acceptance.py', 'wpad_dns_acceptance.py', 'wpad_dns_protocol.py'})


def failure_frames(error):
    frames = []
    for frame, line in traceback.walk_tb(error.__traceback__):
        path = Path(frame.f_code.co_filename)
        if path.name not in ALLOWED or path.resolve().parent != SCRIPTS:
            continue
        name = frame.f_code.co_name
        if not re.fullmatch(r'[A-Za-z_][A-Za-z_0-9]{0,63}|<module>|<lambda>', name):
            name = '<anonymous>'
        frames.append({'file': path.name, 'function': name, 'line': line})
    return frames[-8:]


class BrowserUiObservation:
    """Passive UI lifecycle facts, frozen before verifier-owned cleanup."""

    def __init__(self, browser):
        self.browser = browser
        self.page = None
        self.events = {key: False for key in ('page_crashed', 'page_closed',
                                             'context_closed', 'browser_disconnected')}
        self.observer_failed = False
        self._watch(browser, 'disconnected', 'browser_disconnected')

    def _watch(self, source, event, key):
        try:
            source.on(event, lambda *_: self.events.__setitem__(key, True))
        except Exception:
            self.observer_failed = True

    def watch_context(self, context):
        self._watch(context, 'close', 'context_closed')

    def watch_page(self, page):
        self.page = page
        self._watch(page, 'crash', 'page_crashed')
        self._watch(page, 'close', 'page_closed')

    def snapshot(self):
        result = {'events_before_cleanup': dict(self.events),
                  'observer_failed': self.observer_failed}
        for key, read in (
                ('page_closed', lambda: self.page.is_closed() if self.page is not None else None),
                ('browser_connected', self.browser.is_connected)):
            try:
                value = read()
                result[key] = value if type(value) is bool else None
            except Exception:
                result[key] = None
        return result
