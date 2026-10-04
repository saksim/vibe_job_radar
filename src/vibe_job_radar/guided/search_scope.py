"""Freeze the observed Liepin search conditions without guessing UI filters."""
from __future__ import annotations

from urllib.parse import parse_qsl, urlsplit

from .contracts import CrawlError


def conditions(adapter, url, keyword):
    actual = urlsplit(adapter.accept_url(url))
    base = urlsplit(adapter.search_base)
    if (actual.scheme, actual.netloc, actual.path.rstrip('/')) != (base.scheme, base.netloc, base.path.rstrip('/')):
        raise CrawlError('search_scope_changed')
    pairs = parse_qsl(actual.query, keep_blank_values=True)
    if len({k for k, _ in pairs}) != len(pairs):
        raise CrawlError('search_scope_changed')
    query = dict(pairs)
    if query.get(adapter.keyword_param) != keyword:
        raise CrawlError('search_scope_changed')
    cursor = query.pop('currentPage', None)
    if cursor is not None:
        try:
            if not 0 <= int(cursor) <= 1000 or str(int(cursor)) != cursor:
                raise ValueError()
        except ValueError:
            raise CrawlError('search_scope_changed') from None
    query.pop('init', None)  # The recorded entry marker has no filter semantics.
    if adapter.key == 'liepin':
        from .liepin_search import SEARCH_INTERACTION_FIELDS
        for name in SEARCH_INTERACTION_FIELDS:
            query.pop(name, None)
        # Request/response pairing still binds the complete URL and each
        # pass-through value. Pagination freezes filters and suggestions while
        # the publisher updates its interaction IDs and event scene.
    return dict(sorted(query.items())), cursor


def check_scope(state, adapter, url):
    if state.get('query_scope_version') != 1:
        return None
    wanted, _ = conditions(adapter, state['search_url'], state['keyword'])
    actual, cursor = conditions(adapter, url, state['keyword'])
    if any(actual.get(key) != value for key, value in wanted.items()):
        raise CrawlError('search_scope_changed')
    effective = state.get('effective_search')
    if effective is not None and effective != actual:
        raise CrawlError('search_scope_changed')
    # The first valid list records publisher defaults as observed. Do not guess
    # city/filter defaults or silently ignore later additions/removals.
    state['effective_search'] = actual
    return cursor
