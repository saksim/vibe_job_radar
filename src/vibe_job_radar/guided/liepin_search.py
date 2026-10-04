"""Read the browser's matching Liepin search response; never send a request.

Request/response shape is historical external evidence, not live certification.
No recruiter identities, response bodies, query values or cookies enter logs.
"""
from __future__ import annotations

import hashlib
import json
import re
from urllib.parse import parse_qsl, urlsplit

from ..html_parser import _unique_object
from .contracts import Card, CrawlError

_FIELDS = {'key', 'city', 'otherCity', 'dq', 'pubTime', 'workYearCode', 'compId', 'compName',
           'compTag', 'industry', 'salary', 'jobKind', 'compScale', 'compKind',
           'compStage', 'eduLevel', 'currentPage', 'pageSize', 'salaryCode', 'suggestTag'}
_PASS_THROUGH = {'scene', 'skId', 'fkId', 'ckId', 'suggest', 'sfrom'}
_QUERY_FIELDS = _FIELDS | _PASS_THROUGH | {'suggestId', 'init'}
# These publisher pass-through fields identify the search interaction, not a
# mainSearchPcConditionForm filter. Suggestion IDs remain search constraints.
SEARCH_INTERACTION_FIELDS = frozenset({'scene', 'skId', 'fkId', 'ckId'})


def _same_scalar(value, expected):
    return type(value) in (str, int) and str(value) == expected


def _bind_published_form(query, form, through):
    """Pair the observed URL with publisher transformations, without requests.

    The publisher moves pubTime to hrActiveTimeCode, splits salaryCode at '$',
    and rotates only passThroughForm.ckId after updating history. Each other
    filter and suggestion must still match; unknown or extra filters fail.
    """
    aliases = {'hrActiveTimeCode', 'salaryLow', 'salaryHigh'}
    if set(form) - (_FIELDS | aliases) or not isinstance(through, dict) or set(through) - _PASS_THROUGH:
        raise ValueError()
    direct = _FIELDS - {'pubTime', 'salaryCode'}
    for name in direct:
        if name in query and not _same_scalar(form.get(name), query[name]):
            raise ValueError()
        if name in form and (type(form[name]) not in (str, int)
                or (name not in query and name not in {'currentPage', 'pageSize'} and form[name] != '')):
            raise ValueError()
    dates = [form[name] for name in ('pubTime', 'hrActiveTimeCode') if name in form]
    if ('pubTime' in query and not dates) or any(not _same_scalar(value, query.get('pubTime', '')) for value in dates):
        raise ValueError()
    code = query.get('salaryCode', '')
    bounds = code.split('$') if '$' in code else None
    if bounds is not None and len(bounds) != 2:
        raise ValueError()
    expected = dict(salaryCode='' if bounds else code,
                    salaryLow=bounds[0] if bounds else '', salaryHigh=bounds[1] if bounds else '')
    for name, value in expected.items():
        required = (name == 'salaryCode' and 'salaryCode' in query) or bounds is not None
        if (required and name not in form) or not _same_scalar(form.get(name, ''), value):
            raise ValueError()
    # A normal Enter submission also carries sfrom in passThroughForm, while
    # otherCity is a direct main-form filter. Neither may be silently dropped.
    for name in ('scene', 'skId', 'fkId', 'sfrom'):
        if not _same_scalar(through.get(name, ''), query.get(name, '')):
            raise ValueError()
    if 'ckId' in query or 'ckId' in through:
        for value in (query.get('ckId'), through.get('ckId')):
            if not isinstance(value, str) or re.fullmatch(r'[a-z0-9]{32}', value) is None:
                raise ValueError()
    suggestion = through.get('suggest')
    if suggestion is None:
        if query.get('suggest', 'null') != 'null' or query.get('suggestId', '') != '':
            raise ValueError()
    elif (not isinstance(suggestion, dict) or set(suggestion) != {'suggestId'}
            or not isinstance(suggestion['suggestId'], str) or not suggestion['suggestId']
            or query.get('suggest') != '[object Object]' or query.get('suggestId') != suggestion['suggestId']):
        raise ValueError()


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
        # Recognize only fields with an explicit publisher mapping. Unknown
        # filters cannot become permission to drop a user's search constraints.
        if any(k not in _QUERY_FIELDS for k, _ in params):
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
        _bind_published_form(query, form, body['data'].get('passThroughForm', {}))
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
        if page.business_required:
            # A live native search must wait for its own response. During a
            # history update the DOM can still contain the previous results.
            raise CrawlError('page_not_ready')
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
