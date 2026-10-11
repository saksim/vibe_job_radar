"""Committed quota charges during detail processing, never HTTP success counts.

Two monotonic session counters are sampled around an already-recorded attempt.
Failures keep spent quota; missing counters, interrupted work and legacy data
stay unknown. No ledger query, quota reset, URL, credential or extra request.
"""
from __future__ import annotations

import copy
from . import attempt_history
from .contracts import CrawlError
from ..utils import parse_time, utc_now

KEY = 'detail_quota_costs'
KINDS = ('request', 'page')


def new_history(*, legacy=False, checkpoint_at=None):
    return {'version': 1, 'origin': 'legacy_partial' if legacy else 'task_creation',
            'checkpoint_at': checkpoint_at or utc_now(), 'attempts': []}


def validate(state):
    if KEY not in state or state[KEY] is None:
        return None
    try:
        history = state[KEY]
        if (not isinstance(history, dict)
                or set(history) != {'version','origin','checkpoint_at','attempts'}
                or type(history['version']) is not int or history['version'] != 1
                or history['origin'] not in {'task_creation','legacy_partial'}):
            raise ValueError()
        if not isinstance(history['checkpoint_at'], str) or len(history['checkpoint_at']) > 50:
            raise ValueError()
        parse_time(history['checkpoint_at'])
        rows = history['attempts']
        attempts = {a['sequence']: a for a in (state.get(attempt_history.KEY) or {}).get('attempts', [])}
        if not isinstance(rows, list) or len(rows) > attempt_history.MAX_ATTEMPTS:
            raise ValueError()
        previous = 0
        for row in rows:
            if (not isinstance(row, dict) or set(row) != {'attempt','status','request','page'}
                    or type(row['attempt']) is not int or row['attempt'] <= previous
                    or row['attempt'] not in attempts
                    or row['status'] not in {'unfinished','measured','unavailable'}):
                raise ValueError()
            previous = row['attempt']
            if row['status'] == 'measured':
                if any(type(row[k]) is not int or not 0 <= row[k] <= 1_000_000_000 for k in KINDS):
                    raise ValueError()
            elif any(row[k] is not None for k in KINDS):
                raise ValueError()
            if row['status'] != 'unfinished' and attempts[row['attempt']]['outcome'] == 'unfinished':
                raise ValueError()
        return history
    except (KeyError, ValueError, TypeError, AttributeError, OverflowError):
        raise CrawlError('quota_cost_invalid') from None


def checkpoint(state, before, updated):
    history = validate(state)
    if history is not None:
        if history['checkpoint_at'] != before:
            history['origin'] = 'legacy_partial'
        history['checkpoint_at'] = updated


def _counters(backend):
    try:
        wire = getattr(backend, 'wire', None)
        value = getattr(wire, 'quota_reservations', None)
        uncertain = getattr(wire, 'quota_uncertain', None)
        if (type(uncertain) is not int or not 0 <= uncertain <= 1_000_000_000
                or not isinstance(value, dict) or set(value) != set(KINDS)
                or any(type(value[k]) is not int or not 0 <= value[k] <= 1_000_000_000 for k in KINDS)):
            return None
        return wire, dict(value), uncertain
    except Exception:
        return None


def begin(state, attempt, backend):
    history = validate(state)
    if history is None:
        history = state[KEY] = new_history(legacy=True, checkpoint_at=state.get('updated_at'))
    history['attempts'].append({'attempt': attempt['sequence'], 'status': 'unfinished',
                                'request': None, 'page': None})
    validate(state)
    return _counters(backend)


def finish(state, attempt, before, backend):
    row = state[KEY]['attempts'][-1]
    if row['attempt'] != attempt['sequence'] or row['status'] != 'unfinished':
        raise CrawlError('quota_cost_invalid')
    after = _counters(backend)
    if (before is not None and after is not None and before[0] is after[0] and before[2] == after[2]
            and all(after[1][k] >= before[1][k] for k in KINDS)):
        row.update(status='measured', **{k: after[1][k]-before[1][k] for k in KINDS})
    else:
        row.update(status='unavailable')
    validate(state)


def snapshot(state):
    history = copy.deepcopy(validate(state))
    if history and history['checkpoint_at'] != state.get('updated_at'):
        history['origin'] = 'legacy_partial'
    return history


def extends(previous, current):
    if previous is None:
        return
    if (current is None or (previous['origin'] == 'legacy_partial' and current['origin'] != 'legacy_partial')
            or len(current['attempts']) < len(previous['attempts'])):
        raise CrawlError('quota_cost_invalid')
    for old, new in zip(previous['attempts'], current['attempts']):
        if old['attempt'] != new['attempt'] or (old['status'] != 'unfinished' and old != new):
            raise CrawlError('quota_cost_invalid')


def require_report(current, audit):
    saved = validate(audit)
    extends(saved, current)
    refs = {r['attempt']: r for r in (current or {}).get('attempts', [])}
    recorded = {r['attempt']: r for r in (saved or {}).get('attempts', [])}
    for attempt in (audit.get(attempt_history.KEY) or {}).get('attempts', []):
        sequence = attempt['sequence']
        if sequence in refs and recorded.get(sequence) != refs[sequence]:
            raise CrawlError('quota_cost_invalid')


def summarize(histories, attempts_by_task):
    rows = [r for h in histories.values() if h for r in h['attempts']]
    measured = [r for r in rows if r['status'] == 'measured']
    total = sum(len(h['attempts']) for h in attempts_by_task.values() if h)
    return {'measured_attempts': len(measured), 'attempts_without_measured_cost': total-len(measured),
            'request_quota_charged': sum(r['request'] for r in measured),
            'page_quota_charged': sum(r['page'] for r in measured),
            'tasks_with_unknown_prior_history': sum(not h or h['origin'] != 'task_creation'
                                                   for h in histories.values()),
            'scope': 'Confirmed shared-ledger quota charges during service detail processing, including failed attempts '
                     'and asset/robots work in that window. Not HTTP successes, wire timings/bytes, permission proof, '
                     'or all search/login/background traffic; unknown costs are not zero.'}
