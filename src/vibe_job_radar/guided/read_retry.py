"""Bounded retries for an explicitly opened main GET document, never a click."""
from email.utils import parsedate_to_datetime
from functools import wraps
import math
import re
import secrets

from .contracts import CrawlError

STATUSES = frozenset({502, 503, 504})
MAX_RETRIES = 2


class TransientReadFailure(CrawlError):
    def __init__(self, url, status, retry_after=''):
        self.url, self.status, self.retry_after = url, status, retry_after
        super().__init__('read_transient_failure')


def read_attempt(method):
    @wraps(method)
    def opened(backend, url, *, authentication=False):
        backend._read_target = None if authentication else url
        backend._read_redirected = False
        try:
            return method(backend, url, authentication=authentication)
        finally:
            backend._read_target = None
            backend._read_redirected = False
    return opened


def document_failure(backend, url, method, kind, status, retry_after='', *, main=False):
    if (type(status) is int and status in STATUSES and method == 'GET'
            and kind == 'document' and main is True
            and not getattr(backend, 'auth_mode', False)
            and not getattr(backend, '_read_redirected', False)
            and getattr(backend, '_read_target', None) == url):
        return TransientReadFailure(url, status, retry_after)
    return CrawlError('remote_server_error')


def budget(state):
    value = state.get('read_retry', {'version': 1, 'used': 0, 'last_status': 0})
    if (not isinstance(value, dict) or set(value) != {'version', 'used', 'last_status'}
            or type(value['version']) is not int or value['version'] != 1
            or type(value['used']) is not int or not 0 <= value['used'] <= MAX_RETRIES
            or type(value['last_status']) is not int
            or (value['last_status'] != 0 if value['used'] == 0 else value['last_status'] not in STATUSES)):
        raise CrawlError('read_retry_state_invalid')
    return dict(value)


def retry_after_seconds(retry_after, now):
    if type(now) not in (int, float) or not math.isfinite(now):
        raise CrawlError('clock_rollback')
    after = 0.0
    if retry_after:
        try:
            if not isinstance(retry_after, str) or len(retry_after) > 128:
                raise ValueError()
            if re.fullmatch(r'[0-9]{1,9}', retry_after):
                after = float(retry_after)
            else:
                date = parsedate_to_datetime(retry_after)
                if date.tzinfo is None:
                    raise ValueError()
                after = max(0, date.timestamp() - now)
            if not math.isfinite(after) or after > 366 * 86400:
                raise ValueError()
        except (ValueError, TypeError, OverflowError, IndexError):
            # An unusable publisher deadline cannot authorize an earlier retry.
            raise CrawlError('read_retry_after_invalid') from None
    return after


def retry_delay(used, retry_after, now, *, random=None):
    """5–7.5s, then 10–15s; a valid publisher deadline can only extend this."""
    if type(used) is not int or not 0 <= used < MAX_RETRIES:
        raise CrawlError('read_retry_exhausted')
    after = retry_after_seconds(retry_after, now)
    base = 5.0 * (2 ** used)
    sample = (random or secrets.SystemRandom().random)()
    if type(sample) not in (int, float) or not math.isfinite(sample) or not 0 <= sample <= 1:
        raise CrawlError('read_retry_state_invalid')
    return max(base * (1 + sample / 2), after)


def action_for(state, failure):
    if type(failure.status) is not int or failure.status not in STATUSES:
        raise CrawlError('read_retry_unavailable')
    if state.get('phase') == 'collect':
        selected = set(state['selection'])
        pending = next((c for c in state['cards'] if c['id'] in selected and c['status'] != 'ok'), None)
        if pending and pending['url'] == failure.url:
            return 'resume'
    elif failure.url == state['search_url']:
        return 'search'
    raise CrawlError('read_retry_unavailable')
