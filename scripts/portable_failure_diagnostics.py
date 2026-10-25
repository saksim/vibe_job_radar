"""Bounded code locations only: no exception text, locals, paths or source lines."""
import json
from pathlib import Path
import re
import traceback

SCRIPTS = Path(__file__).resolve().parent
ALLOWED = frozenset({'verify_windows_portable.py', 'system_pac_acceptance.py', 'wpad_dns_acceptance.py', 'wpad_dns_protocol.py', 'automatic_update_acceptance.py'})


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


class AutomaticUpdateObservation:
    """Fixed vocabulary only; freeze the failed handoff before owned cleanup."""

    PHASES = frozenset({'workspace_prepare', 'archive_payload', 'read_manifest',
        'stage_package', 'journal_prepare', 'old_app_start', 'parent_identity',
        'write_request', 'helper_start', 'wait_armed', 'old_app_stop', 'wait_ready',
        'child_identity', 'wait_helper', 'committed_record', 'http_ready',
        'backup_check', 'original_data_check', 'cleanup', 'proof_check'})
    STATUSES = frozenset({'armed', 'starting', 'committed', 'failed', 'ready'})

    def __init__(self, failure=None):
        self.failure = failure
        self.phase = 'workspace_prepare'
        self.helper = None
        self.journal = None
        self.manifest_within_limit = None
        self.manifest_size_bytes = None
        self.observer_failed = False
        self.frozen = False
        self.cleanup_started = False

    def observe_manifest(self, path):
        try:
            size = path.stat().st_size
            self.manifest_within_limit = size <= 100000
            if 0 <= size <= 1000000:
                self.manifest_size_bytes = size
            else:
                self.observer_failed = True
        except Exception:
            self.observer_failed = True

    def _status(self, name):
        if self.journal is None:
            return 'absent'
        try:
            path = self.journal/name
            if path.is_symlink():
                raise ValueError()
            with path.open('rb') as stream:
                raw = stream.read(100001)
            if len(raw) > 100000:
                raise ValueError()
            value = json.loads(raw)
            status = value.get('status') if isinstance(value, dict) else None
            if type(status) is not str or status not in self.STATUSES:
                raise ValueError()
            return status
        except FileNotFoundError:
            return 'absent'
        except Exception:
            self.observer_failed = True
            return 'unavailable'

    def capture(self):
        if self.frozen:
            return
        self.frozen = True
        code = None
        if self.helper is not None:
            try:
                value = self.helper.poll()
                if value is None or type(value) is int and -(2**31) <= value < 2**32:
                    code = value
                else:
                    self.observer_failed = True
            except Exception:
                self.observer_failed = True
        status, ready = self._status('status.json'), self._status('ready.json')
        result = dict(schema_version=1,
            phase=self.phase if self.phase in self.PHASES else 'unavailable',
            captured_before_cleanup=not self.cleanup_started,
            helper_started=self.helper is not None, helper_returncode=code,
            journal_created=self.journal is not None, journal_status=status,
            ready_status=ready, manifest_within_limit=self.manifest_within_limit,
            manifest_size_bytes=self.manifest_size_bytes, manifest_limit_bytes=100000,
            observer_failed=self.observer_failed)
        if self.failure is not None:
            self.failure.update(result)
