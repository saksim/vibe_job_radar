"""Submit one keyword through the publisher's observed visible search field."""
from __future__ import annotations

from .contracts import CrawlError
from .search_scope import conditions


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
