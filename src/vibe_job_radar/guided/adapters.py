"""Versioned page adapters, not reverse-engineered private API clients.

Built-in selectors are best-effort and explicitly NOT live-certified. DOM parsing
is pure Python so a site can be tested without a browser, network or credentials.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from ..html_parser import Document, ParseError, parse_job_html, plain_text
from ..url_safety import credential_query_key
from ..utils import domain_matches
from .contracts import Card, CrawlError, PageSnapshot, SiteAdapter
from .selectors import select_nodes


@dataclass(frozen=True)
class DOMAdapter:
    key: str
    label: str
    domains: tuple[str, ...]
    search_base: str
    keyword_param: str
    detail_pattern: str
    login_url: str
    login_hosts: tuple[str, ...]
    resource_domains: tuple[str, ...] = ()
    card_selector: str = 'a[href]'
    username_selectors: tuple[str, ...] = (
        'input[autocomplete="username"]', 'input[name="username"]',
        'input[name="account"]', 'input[placeholder*="手机"]', 'input[type="tel"]')
    password_selectors: tuple[str, ...] = ('input[type="password"]',)
    submit_selectors: tuple[str, ...] = ('button[type="submit"]', 'button:has-text("登录")',
                                         'input[type="submit"]')
    next_selectors: tuple[str, ...] = ('a[rel="next"]', 'button.btn-next',
                                      'a[ka="page-next"]', 'button:has-text("下一页")',
                                      'a:has-text("下一页")')
    version: str = '1'
    certification: str = 'not_live_verified'
    native_contract: object = None  # Optional trusted-code contract; never accepted from the UI.

    def search_url(self, keyword: str) -> str:
        keyword = keyword.strip()
        if not keyword or len(keyword) > 100:
            raise CrawlError('keyword_required')
        p = urlsplit(self.search_base)
        return urlunsplit((p.scheme, p.netloc, p.path,
                           urlencode([*parse_qsl(p.query), (self.keyword_param, keyword)]), ''))

    def accept_url(self, url: str, *, detail: bool = False) -> str:
        if not isinstance(url, str) or len(url) > 2048:
            raise CrawlError('invalid_url')
        p = urlsplit(url)
        try:
            if (p.scheme != 'https' or not p.hostname or p.port not in (None, 443)
                    or p.username or p.password or '\\' in url or any(ord(c) < 32 for c in url)):
                raise CrawlError('invalid_url')
        except ValueError as exc:
            raise CrawlError('invalid_url') from exc
        if not any(domain_matches(p.hostname, d) for d in self.domains):
            raise CrawlError('wrong_platform')
        permitted_key = (not detail and p.path.rstrip('/') == urlsplit(self.search_base).path.rstrip('/'))
        if any(credential_query_key(k) and not (permitted_key and k == self.keyword_param)
               for k, _ in parse_qsl(p.query, keep_blank_values=True)):
            raise CrawlError('credential_url')
        if detail and not re.search(self.detail_pattern, p.path):
            raise CrawlError('not_job_url')
        query = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
                 if not k.casefold().startswith('utm_')]
        return urlunsplit((p.scheme, p.netloc.lower(), p.path or '/', urlencode(query), ''))

    def cards(self, page: PageSnapshot) -> list[Card]:
        self.accept_url(page.url)
        path = urlsplit(page.url).path.rstrip('/')
        search_path = urlsplit(self.search_base).path.rstrip('/')
        login_path = urlsplit(self.login_url).path.rstrip('/')
        # A detail page may contain many perfectly valid recommended job links.
        # They are not this task's search results, even after a successful login.
        if (re.search(self.detail_pattern, urlsplit(page.url).path)
                or (path != search_path and path in {'', login_path})):
            raise CrawlError('not_job_list')
        if self.challenged(plain_text(page.html), page.url):
            raise CrawlError('manual_required')
        out = {}
        for node in select_nodes(Document(page.html).root, self.card_selector):
            if node.tag != 'a' or not node.attrs.get('href'):
                continue
            try:
                url = self.accept_url(urljoin(page.url, node.attrs['href']), detail=True)
            except CrawlError:
                continue
            title = (node.attrs.get('title') or node.text()).strip()
            if not title:
                continue
            ident = hashlib.sha256(url.encode()).hexdigest()[:24]
            out.setdefault(ident, Card(ident, re.sub(r'\s+', ' ', title)[:300], url, page.url))
        return list(out.values())[:300]

    def challenged(self, text: str, url: str) -> bool:
        return bool(re.search(r'请完成.{0,12}验证|滑动.{0,8}验证|安全验证|访问异常|访问过于频繁|'
                              r'登录后.{0,8}(?:查看|浏览)|verify you are human|access denied', text, re.I)
                    or re.search(r'/(?:captcha(?:page)?|intercept|challenge)(?:[/_]|$)',
                                 urlsplit(url).path, re.I))

    def detail(self, page: PageSnapshot) -> dict:
        self.accept_url(page.url, detail=True)
        if self.challenged(plain_text(page.html), page.url):
            raise CrawlError('manual_required')
        try:
            markup = re.sub(r'<script\b[^>]*>.*?</script\s*>',
                lambda m: m.group(0) if 'application/ld+json' in m.group(0).split('>', 1)[0].lower() else '',
                page.html, flags=re.I | re.S)
            return parse_job_html(markup, source_url=page.url)
        except (ParseError, ValueError) as exc:
            raise CrawlError('structure_changed') from exc


class Registry:
    """Explicit dependency injection; installing a package never auto-loads code."""
    def __init__(self, adapters=()):
        self._adapters = {}
        for adapter in adapters:
            self.register(adapter)

    def register(self, adapter: SiteAdapter) -> None:
        if not re.fullmatch(r'[a-z0-9_]{2,32}', adapter.key) or adapter.key in self._adapters:
            raise ValueError('duplicate or invalid adapter key')
        adapter.accept_url(adapter.login_url)
        self._adapters[adapter.key] = adapter

    def get(self, key: str) -> SiteAdapter:
        try:
            return self._adapters[key]
        except (KeyError, TypeError) as exc:
            raise CrawlError('unknown_site') from exc

    def describe(self) -> list[dict]:
        from ..acquisition_status import describe_adapter
        return [{'key': a.key, 'label': a.label, 'version': getattr(a, 'version', 'custom'),
                 'certification': 'not_live_verified', 'acquisition':describe_adapter(a),
                 'login': 'manual_in_platform_browser'} for a in self._adapters.values()]


def builtins() -> Registry:
    from .liepin import LiepinAdapter
    return Registry([
        DOMAdapter('boss', 'BOSS直聘', ('zhipin.com',),
                   'https://www.zhipin.com/web/geek/job', 'query', r'^/job_detail/[^/]+\.html$',
                   'https://www.zhipin.com/web/user/', ('www.zhipin.com',),
                   ('zhipin.com', 'zhipin.cn')),
        LiepinAdapter('liepin', '猎聘', ('liepin.com',),
                   'https://www.liepin.com/zhaopin/', 'key',
                   r'^(?:/job/[^/]+\.(?:shtml|html)|/a/[0-9]+\.shtml|/lptjob/[0-9]+)$',
                   'https://www.liepin.com/', ('www.liepin.com', 'passport.liepin.com'),
                   ('liepin.com', 'liepin.cn'), version='3'),
        DOMAdapter('51job', '前程无忧', ('51job.com',),
                   'https://we.51job.com/pc/search', 'keyword', r'(?:/[^/]+/\d+\.html$|^/pc/jobdetail)',
                   'https://login.51job.com/', ('login.51job.com',),
                   ('51job.com', '51jobcdn.com')),
    ])
