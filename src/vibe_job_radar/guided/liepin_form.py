"""Submit one keyword through the publisher's observed visible search field."""
from __future__ import annotations

import hashlib
import json

from .contracts import CrawlError, PageSnapshotChanged
from .search_scope import conditions


def matching_search_entry_signature(backend, state, snapshot):
    """Observe the published account/search controls without reading their values.

    Only a new keyword-only native task can leave the query-free login entry.
    Existing results, explicit/frozen filters and selected JDs keep their own
    return paths. Account UI availability is not authentication certification.
    """
    if (state.get('platform') != 'liepin' or state.get('backend') != 'native'
            or state.get('query_scope_version') != 1
            or state.get('auto_continue_after_login') is not True
            or state.get('authentication') != 'manual_pending'
            or state.get('phase') not in {'search', 'select'}
            or state.get('cards') or state.get('selection')
            or state.get('effective_search') is not None
            or snapshot.business_required is not True):
        return None
    adapter, page = backend.adapter, backend.page
    if (adapter.key != 'liepin' or backend.auth_mode is not True
            or state.get('search_url') != adapter.search_url(state['keyword'])
            or snapshot.url != adapter.search_base):
        return None
    backend._check_error()
    if not page or page.is_closed():
        raise CrawlError('browser_closed')
    if page.url != snapshot.url:
        raise PageSnapshotChanged()
    navigation = getattr(page, 'navigation', None)
    target = getattr(page, 'ident', None)
    if type(navigation) is not int or not isinstance(target, str):
        return None
    backend.ensure_page_access(page.url)
    text = page.locator('body').inner_text(timeout=5000)
    if adapter.challenged(text, page.url):
        raise CrawlError('manual_required')
    checks = (
        ('#header-quick-menu-user-info:visible', 1),
        ('input[type="text"][placeholder="搜索职位、公司"]:visible', 1),
        ('#header-quick-menu-login:visible', 0),
        ('input[data-nick="login-pwd"]:visible', 0),
    )
    ready = all(page.locator(selector).count() == count for selector, count in checks)
    backend._check_error()
    if (page is not backend.page or page.url != snapshot.url
            or page.navigation != navigation):
        raise PageSnapshotChanged()
    if not ready:
        return None
    value = json.dumps((state['search_url'], target, navigation), ensure_ascii=False)
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def submit_search(backend, keyword):
    page, adapter = backend.page, backend.adapter

    def check_entry():
        backend._check_error()
        if page is not backend.page or page.is_closed():
            raise CrawlError('browser_closed')
        if page.url != adapter.search_base:
            raise CrawlError('search_scope_changed')
        backend.ensure_page_access(page.url)
        if adapter.challenged(page.locator('body').inner_text(timeout=5000), page.url):
            raise CrawlError('manual_required')

    try:
        check_entry()
        field = page.locator('input[type="text"][placeholder="搜索职位、公司"]:visible')
        field.wait_for(state='visible', timeout=15000)
        check_entry()
        if field.count() != 1:
            raise CrawlError('search_form_changed')
        field.click(timeout=5000)
        check_entry()
        field.fill(keyword, timeout=5000)
        check_entry()
        backend.wire.reserve('page')
        backend._pagination_page = page
        try:
            backend._before_pagination_click()
            field.press('Enter', timeout=90000)
            backend._settle(search=True)
            snapshot = backend.snapshot()
            conditions(adapter, snapshot.url, keyword)
            adapter.cards(snapshot)  # Requires this native query's own response, even if empty.
            return snapshot
        finally:
            backend._pagination_page = None
    except CrawlError:
        raise
    except Exception:
        backend._check_error()  # Preserve a native refusal received during the input wait.
        raise CrawlError('search_form_changed') from None
