"""Small typed boundaries: site semantics are independent of browser and HTTP/UI."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


@dataclass(frozen=True)
class Card:
    id: str
    title: str
    url: str
    source_url: str


@dataclass(frozen=True)
class PageSnapshot:
    url: str
    html: str = field(repr=False)
    business: tuple = field(default=(), repr=False)  # Private observations, never task/diagnostic output.


class CrawlError(RuntimeError):
    """Only fixed, non-secret messages cross the worker/UI boundary."""
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class SiteAdapter(Protocol):
    key: str
    label: str
    domains: tuple[str, ...]
    resource_domains: tuple[str, ...]
    login_url: str
    login_hosts: tuple[str, ...]
    username_selectors: tuple[str, ...]
    password_selectors: tuple[str, ...]
    submit_selectors: tuple[str, ...]
    next_selectors: tuple[str, ...]
    card_selector: str

    def search_url(self, keyword: str) -> str: ...
    def accept_url(self, url: str, *, detail: bool = False) -> str: ...
    def cards(self, page: PageSnapshot) -> list[Card]: ...
    def detail(self, page: PageSnapshot) -> dict: ...
    def challenged(self, text: str, url: str) -> bool: ...


class BrowserBackend(Protocol):
    def open(self, url: str, *, authentication: bool = False) -> PageSnapshot: ...
    def snapshot(self) -> PageSnapshot: ...
    def next_page(self) -> bool: ...
    def pump(self) -> None: ...
    def close(self) -> None: ...
