"""Read the browser's matching Liepin search response; never send a request.

Request/response shape is historical external evidence, not live certification.
No recruiter identities, response bodies, query values or cookies enter logs.
"""
from __future__ import annotations

import hashlib
import json
from urllib.parse import parse_qsl, urlsplit

from ..html_parser import _unique_object
from .contracts import Card, CrawlError

_FIELDS = {'key', 'city', 'dq', 'pubTime', 'workYearCode', 'compId', 'compName',
           'compTag', 'industry', 'salary', 'jobKind', 'compScale', 'compKind',
           'compStage', 'eduLevel', 'currentPage', 'pageSize'}


def _page_url(adapter, url):
    accepted = adapter.accept_url(url)
    p = urlsplit(accepted)
    expected = urlsplit(adapter.search_base)
    if (p.scheme, p.netloc, p.path.rstrip('/')) != (expected.scheme, expected.netloc, expected.path.rstrip('/')):
        raise CrawlError('liepin_search_query_mismatch')
    return accepted


def request_context(adapter, operation, request, page_url):
    if operation != 'liepin_search':
        return {}
    try:
        url = _page_url(adapter, page_url)
        params = parse_qsl(urlsplit(url).query, keep_blank_values=True)
        # Only a harmless entrypoint marker can be absent from the search body.
        # Unknown filters require real mapping, not dropping user constraints.
        if any(k not in _FIELDS | {'init'} for k, _ in params):
            raise ValueError()
        if len({k for k, _ in params}) != len(params):
            raise ValueError()
        raw = request.get('postData', '')
        if not isinstance(raw, str) or len(raw) > 100_000:
            raise ValueError()
        body = json.loads(raw, object_pairs_hook=_unique_object)
        form = body['data']['mainSearchPcConditionForm']
        if not isinstance(form, dict):
            raise ValueError()
        keyword = form.get('key')
        query = dict(params)
        # The observed query-free entry asks for default recommendations before
        # rendering the search form. This is page initialization, never evidence
        # for a user's keyword. Only the exact initial page/size is recognized.
        if (page_url == url == adapter.search_base and not params and keyword == ''
                and type(form.get('currentPage')) is int and form['currentPage'] == 0
                and type(form.get('pageSize')) is int and form['pageSize'] == 40):
            return {'query': hashlib.sha256(url.encode()).hexdigest(), 'page': 0,
                    'size': 40, 'entry_bootstrap': True}
        if not isinstance(keyword, str) or keyword != query.get(adapter.keyword_param) or not keyword.strip():
            raise ValueError()
        for name, value in params:
            if name != 'init' and (type(form.get(name)) not in (str, int) or str(form[name]) != value):
                raise ValueError()
        page, size = form.get('currentPage'), form.get('pageSize')
        if (type(page) is not int or not 0 <= page <= 1000 or str(page) != query.get('currentPage', '0')
                or type(size) is not int or not 1 <= size <= 100):
            raise ValueError()
        # The URL and payload stay private in memory; response pairing uses a
        # digest so neither a signed parameter nor an account field is retained.
        return {'query': hashlib.sha256(url.encode()).hexdigest(), 'page': page, 'size': size}
    except (KeyError, ValueError, TypeError, RecursionError):
        raise CrawlError('liepin_search_query_mismatch') from None


def _records(adapter, payload, context, source):
    try:
        if not isinstance(payload, dict) or type(payload.get('flag')) is not int or payload['flag'] != 1:
            raise ValueError()
        data = payload['data']
        items = data['data']['jobCardList']
        pagination = data['pagination']
        if (not isinstance(items, list) or len(items) > 300 or not isinstance(pagination, dict)
                or type(pagination.get('currentPage')) is not int
                or pagination['currentPage'] != context['page']):
            raise ValueError()
        cards, seen = [], set()
        for item in items:
            job = item['job']
            title, link = job['title'], job['link']
            if not isinstance(title, str) or not title.strip() or len(title) > 500 or not isinstance(link, str):
                raise ValueError()
            # jobId is not substituted into a URL: /job and /a are different
            # families and historical jobId can differ from the link's number.
            url = adapter.accept_url(link, detail=True)
            adapter.job_identity(url)
            ident = hashlib.sha256(url.encode()).hexdigest()[:24]
            if ident not in seen:
                cards.append(Card(ident, title.strip(), url, source))
                seen.add(ident)
        return cards
    except CrawlError:
        raise
    except (KeyError, ValueError, TypeError, RecursionError):
        raise CrawlError('native_business_response_invalid') from None


def observed_cards(adapter, page):
    source = _page_url(adapter, page.url)
    query = hashlib.sha256(source.encode()).hexdigest()
    matching = []
    for item in page.business:
        if item.operation == 'liepin_search' and item.context.get('query') == query:
            matching.append(item)
    if not matching:
        return None  # Older backends and plain DOM snapshots keep their path.
    # Observations are delivery ordered. Any successful latest response must
    # still pair to this query/page; no guessing from a cached earlier response.
    current = max(matching, key=lambda o: o.context.get('sequence', 0))
    if current.context.get('entry_bootstrap'):
        # Do not fall back to the recommendation anchors in the same DOM. The
        # entry response can make the UI ready, but is not a search result or a
        # confirmed empty result for the task.
        raise CrawlError('not_job_list')
    return _records(adapter, current.payload, current.context, source)
