"""Bounded, checkpoint-owned detail history; not HTTP or permission accounting.

The service saves a start before processing a detail and the result with its
normal checkpoint. An unfinished start survives a crash as unknown. No network
observation, credential, URL, body or exception text is copied into this history.
"""
from __future__ import annotations

import copy
import math
import re
from time import monotonic

from ..utils import parse_time, utc_now
from .contracts import CrawlError

KEY = 'detail_attempt_history'
MAX_ATTEMPTS = 500
MAX_SELECTIONS = 200
_RESULT_FIELDS = {'finished_at', 'elapsed_ms', 'outcome', 'record_id'}


def new_history(*, legacy=False, checkpoint_at=None):
    return {'version': 1, 'origin': 'legacy_partial' if legacy else 'task_creation',
            'checkpoint_at': checkpoint_at or utc_now(), 'selections': [], 'attempts': []}


def _text(value, limit=128):
    return isinstance(value, str) and 0 < len(value) <= limit and not any(ord(c) < 32 for c in value)


def _timestamp(value):
    if not _text(value, 50):
        raise ValueError()
    parse_time(value)


def validate(state):
    """Absent legacy history is unknown; malformed present history is an error."""
    if KEY not in state:
        return None
    try:
        history = state[KEY]
        if (not isinstance(history, dict) or set(history) != {'version', 'origin', 'checkpoint_at', 'selections', 'attempts'}
                or type(history['version']) is not int or history['version'] != 1
                or history['origin'] not in {'task_creation', 'legacy_partial'}):
            raise ValueError()
        _timestamp(history['checkpoint_at'])
        selections, attempts = history['selections'], history['attempts']
        if (not isinstance(selections, list) or len(selections) > MAX_SELECTIONS
                or not isinstance(attempts, list) or len(attempts) > MAX_ATTEMPTS):
            raise ValueError()
        known = {row['id'] for row in state['cards']}
        for index, selection in enumerate(selections, 1):
            if (not isinstance(selection, dict) or set(selection) != {'sequence', 'selected_at', 'items'}
                    or type(selection['sequence']) is not int or selection['sequence'] != index):
                raise ValueError()
            _timestamp(selection['selected_at'])
            items = selection['items']
            if (not isinstance(items, list) or len(items) > 20 or not all(_text(i) for i in items)
                    or len(set(items)) != len(items) or not set(items) <= known):
                raise ValueError()
        for index, attempt in enumerate(attempts, 1):
            if (not isinstance(attempt, dict) or set(attempt) != {'sequence', 'selection', 'item_id',
                    'started_at', 'mode'} | _RESULT_FIELDS
                    or type(attempt['sequence']) is not int or attempt['sequence'] != index
                    or type(attempt['selection']) is not int or not 1 <= attempt['selection'] <= len(selections)
                    or attempt['item_id'] not in selections[attempt['selection']-1]['items']
                    or attempt['mode'] not in {'navigation', 'login_returned_detail'}):
                raise ValueError()
            _timestamp(attempt['started_at'])
            outcome, elapsed = attempt['outcome'], attempt['elapsed_ms']
            if not isinstance(outcome, str) or not re.fullmatch('[a-z][a-z0-9_]{0,79}', outcome):
                raise ValueError()
            if outcome == 'unfinished':
                if attempt['finished_at'] is not None or elapsed is not None or attempt['record_id'] != '':
                    raise ValueError()
            else:
                _timestamp(attempt['finished_at'])
                if type(elapsed) not in (int, float) or not math.isfinite(elapsed) or not 0 <= elapsed <= 366*86400*1000:
                    raise ValueError()
                record = attempt['record_id']
                if (not isinstance(record, str) or (outcome == 'ok' and not re.fullmatch('j_[a-f0-9]{24}', record))
                        or (outcome != 'ok' and record != '')):
                    raise ValueError()
        return history
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
        raise CrawlError('attempt_history_invalid') from None


def note_selection(state):
    history = validate(state)
    if history is None:
        if not state['selection']:
            return
        history = state[KEY] = new_history(legacy=True, checkpoint_at=state.get('updated_at'))
    if history['checkpoint_at'] != state.get('updated_at'):
        history['origin'] = 'legacy_partial'
    items = list(state['selection'])
    if not history['selections'] and not items:
        return
    if history['selections'] and history['selections'][-1]['items'] == items:
        return
    if len(history['selections']) >= MAX_SELECTIONS:
        raise CrawlError('attempt_history_limit')
    history['selections'].append({'sequence': len(history['selections'])+1,
                                 'selected_at': utc_now(), 'items': items})
    validate(state)


def begin(state, item_id, *, returned=False):
    note_selection(state)
    history = state[KEY]
    if len(history['attempts']) >= MAX_ATTEMPTS:
        raise CrawlError('attempt_history_limit')
    attempt = {'sequence': len(history['attempts'])+1, 'selection': len(history['selections']),
               'item_id': item_id, 'started_at': utc_now(),
               'mode': 'login_returned_detail' if returned else 'navigation',
               'finished_at': None, 'elapsed_ms': None, 'outcome': 'unfinished', 'record_id': ''}
    history['attempts'].append(attempt)
    validate(state)
    return attempt


def finish(attempt, started, *, error=None, record_id=''):
    elapsed = (monotonic()-started)*1000
    if not math.isfinite(elapsed) or elapsed < 0:
        raise CrawlError('attempt_history_invalid')
    # CrawlError has a fixed public code; never retain exception text.
    code = error.code if isinstance(error, CrawlError) else 'operation_error'
    if not isinstance(code, str) or not re.fullmatch('[a-z][a-z0-9_]{0,79}', code) or code in {'ok', 'unfinished'}:
        code = 'operation_error'
    attempt.update(finished_at=utc_now(), elapsed_ms=round(elapsed, 3),
                   outcome=code if error is not None else 'ok',
                   record_id='' if error is not None else record_id)


def snapshot(state):
    history = copy.deepcopy(validate(state))
    if history and history['checkpoint_at'] != state.get('updated_at'):
        history['origin'] = 'legacy_partial'
    return history


def extends(previous, current):
    """Captures must keep every start and completed result they already saw."""
    if previous is None:
        return
    if (current is None or (previous['origin'] == 'legacy_partial' and current['origin'] != 'legacy_partial')
            or current['selections'][:len(previous['selections'])] != previous['selections']
            or len(current['attempts']) < len(previous['attempts'])):
        raise CrawlError('attempt_history_invalid')
    for old, new in zip(previous['attempts'], current['attempts']):
        fields = set(old) - _RESULT_FIELDS if old['outcome'] == 'unfinished' else set(old)
        if any(old[k] != new[k] for k in fields):
            raise CrawlError('attempt_history_invalid')


def summarize(histories):
    from collections import Counter
    attempts = [attempt for history in histories.values() if history for attempt in history['attempts']]
    elapsed = [a['elapsed_ms'] for a in attempts if a['elapsed_ms'] is not None]
    unknown = sum(not h or h['origin'] != 'task_creation' for h in histories.values())
    unstarted = sum(len({i for s in h['selections'] for i in s['items']}
                       - {a['item_id'] for a in h['attempts']}) for h in histories.values() if h)
    return {'recorded': len(attempts), 'outcomes': dict(Counter(a['outcome'] for a in attempts)),
            'measured_durations': len(elapsed), 'total_elapsed_ms': round(sum(elapsed), 3),
            'max_elapsed_ms': max(elapsed, default=None), 'tasks_with_unknown_prior_history': unknown,
            'recorded_selections_without_detail_start': unstarted,
            'scope': 'Service detail processing only; includes local waits/parsing/persistence, '
                     'excludes login waiting and report generation; not HTTP counts, wire latency or permission proof.'}
