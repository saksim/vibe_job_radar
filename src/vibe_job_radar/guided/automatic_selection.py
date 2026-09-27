"""Pure bounded selection after a successful query. No networking or login.

Only an explicit create-time opt-in can choose jobs automatically. The caller
persists these identifiers before invoking the ordinary collector, so partial
results, login continuation and restart use the same frozen batch.
"""
from __future__ import annotations

from .contracts import CrawlError


def select_ready_batch(state: dict) -> list[str]:
    """Return first-discovered job IDs once; do not infer account readiness.

    A ready *empty* result, failed parsing, a login wall or partial prior batch
    must never be converted into a selection using stale cards.
    """
    if (state.get('auto_collect') is not True
            or state.get('auto_selection_applied') is True
            or state.get('selection')
            or state.get('status') != 'ready'
            or state.get('code') != 'ready'
            or state.get('phase') != 'select'):
        return []
    limit = state.get('max_jobs')
    rows = state.get('cards')
    if type(limit) is not int or not 1 <= limit <= 20 or not isinstance(rows, list):
        raise CrawlError('invalid_job_data')
    selected = []
    seen = set()
    for row in rows:
        if not isinstance(row, dict):
            raise CrawlError('invalid_job_data')
        ident = row.get('id')
        if not isinstance(ident, str) or not ident or ident in seen:
            raise CrawlError('invalid_job_data')
        seen.add(ident)
        # Never silently skip earlier failures in favour of an easier next job.
        if row.get('status') != 'discovered':
            raise CrawlError('invalid_job_data')
        if len(selected) < limit:
            selected.append(ident)
    return selected
