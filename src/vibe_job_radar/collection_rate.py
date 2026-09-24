"""Bind anonymous advanced site reads to the existing guided workspace ledger."""
from __future__ import annotations

import threading
from types import SimpleNamespace
from urllib.parse import urlsplit

from .guided.contracts import CrawlError
from .guided.rate import RateLedger
from .guided.transport import PinnedTransport
from .network import FetchError, retry_after_seconds


class SharedSiteRate:
    def __init__(self, workspace_root, platform):
        self.root, self.platform = workspace_root, platform
        self._wire = None

    def _transport(self):
        if self._wire is None:
            root = self.root / 'guided'
            if root.is_symlink():
                raise FetchError('unsafe_workspace')
            # Same file, platform key and original Limits as GuidedService.
            # This is lazy: previews, cached bodies and API-only tasks do not
            # create a second ledger or reserve upstream visits.
            ledger = RateLedger(root / 'rates.sqlite')
            adapter = SimpleNamespace(key=self.platform, domains=(), resource_domains=())
            self._wire = PinnedTransport(adapter, ledger, threading.Event())
        return self._wire

    def _call(self, operation):
        try:
            return operation(self._transport())
        except CrawlError as exc:
            raise FetchError(exc.code, retry_after=getattr(exc, 'wait', None)) from exc
        except OSError as exc:
            raise FetchError('rate_storage_error') from exc

    @staticmethod
    def _origin(url):
        return 'https://' + urlsplit(url).hostname

    def before_page(self, url):
        self._call(lambda wire: wire.reserve('page', origin=self._origin(url)))

    def before_request(self, url):
        self._call(lambda wire: wire.reserve('request', origin=self._origin(url)))

    def robots(self, origin, rules):
        def save(wire):
            wire.ledger.set_publisher(self.platform, origin, delay=rules.delay)
            for count, seconds in rules.windows:
                wire.ledger.set_publisher(self.platform, origin, delay=rules.delay, requests=count, seconds=seconds)
        self._call(save)

    def failed(self, error):
        if error.code in {'http_401', 'http_403', 'http_429'}:
            delay = error.retry_after if error.retry_after is not None else 300
            self._call(lambda wire: wire.ledger.cool(self.platform, delay))

    def response(self, response):
        # The real SafeHTTP raises these statuses. Also preserve identical
        # semantics for injected one-hop responses used by acceptance checks.
        if response.status in {401, 403, 429}:
            error = FetchError(f'http_{response.status}', retry_after=
                retry_after_seconds(response.headers.get('retry-after', '')) if response.status == 429 else None)
            self.failed(error)
            raise error
