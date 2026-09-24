"""Reviewed share links for the explicit public-URL collection form.

This is input preparation, never a fallback after a refused request. Browser
selections, discovery results, redirects and historical records are unchanged.
"""
from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlsplit, urlunsplit

LIEPIN_SHARE_V1 = 'liepin_share_v1'
# Observed on the user's normal search -> /job/<number>.shtml link, 2026-09-24.
# Do not generalize to d_*, unknown keys, other URL families or other hosts.
_LIEPIN_SHARE_KEYS = frozenset({
    'pgRef', 'd_sfrom', 'd_ckId', 'd_curPage', 'd_pageSize', 'd_headId',
    'd_posi', 'skId', 'fkId', 'ckId', 'sfrom', 'curPage', 'pageSize', 'index',
})


def prepare_public_job_link(value: str) -> tuple[str, dict]:
    from .collection import safe_url
    # Validate credentials before removing anything. Reuse legacy tracking and
    # URL validation; this policy cannot launder auth or unknown parameters.
    url = safe_url(value)
    parsed = urlsplit(url)
    pairs = parse_qsl(parsed.query, keep_blank_values=True)
    if (parsed.netloc != 'www.liepin.com'
            or not re.fullmatch(r'/job/[0-9]{1,80}\.shtml', parsed.path)
            or not pairs or any(key not in _LIEPIN_SHARE_KEYS for key, _ in pairs)):
        return url, {}
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, '', '')), {
        'policy': LIEPIN_SHARE_V1,
        'removed_parameters': sorted({key for key, _ in pairs}),
    }


def parse_prepared_job(row: dict, final_url: str, markup: str) -> dict:
    """A prepared share link must still resolve to the selected job's full JD."""
    from .guided.adapters import builtins
    from .guided.contracts import CrawlError, PageSnapshot
    from .network import FetchError
    if row.get('link_normalization', {}).get('policy') != LIEPIN_SHARE_V1:
        raise FetchError('invalid_link_normalization')
    adapter = builtins().get('liepin')
    snapshot = PageSnapshot(final_url, markup)
    try:
        adapter.validate_detail_identity(row['url'], snapshot)
        return adapter.detail(snapshot)
    except CrawlError as exc:
        raise FetchError(exc.code) from exc
