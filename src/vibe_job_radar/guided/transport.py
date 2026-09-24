"""Bridge browser HTTP through public-IP-pinned TLS, never arbitrary direct egress.

The browser receives fulfilled responses only: no route.continue_(), no automatic
HTTP redirects, no proxy fallback and no weakening DNS checks for Fake-IP.
"""
from __future__ import annotations

import http.client
import ipaddress
import math
import socket
import ssl
import threading
import time
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit

from ..network import FetchError, PinnedHTTPSConnection, validate_public_url, validate_url_target
from ..utils import domain_matches
from ..network_policy import NetworkPolicy, current_policy, use_policy
from .contracts import CrawlError
from .rate import RateLedger, RateLimit
from .request_headers import browser_headers
from .diagnostic_trace import traced, notify, observe_robots
from .native_policy import NativeRobots


@dataclass(frozen=True)
class WireResponse:
    status: int
    headers: dict[str, str]
    body: bytes = field(repr=False)
    cookies: tuple[str, ...] = field(default=(), repr=False)


def diagnose_host(host: str) -> dict:
    """DNS only; fixed adapter host chosen server-side, no caller-supplied URLs."""
    try:
        values = sorted({a[4][0] for a in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)})
        addresses = [{'ip': v, 'public': ipaddress.ip_address(v).is_global,
                      'fake_ip_range': ipaddress.ip_address(v).version == 4
                          and ipaddress.ip_address(v) in ipaddress.ip_network('198.18.0.0/15')} for v in values]
        passed = bool(addresses) and all(a['public'] for a in addresses)
        return {'host': host, 'addresses': addresses, 'passed': passed,
                'code': 'dns_ok' if passed else 'non_public_address',
                'network_scope': 'DNS only; no job requests; not login or collection certification'}
    except (OSError, ValueError):
        return {'host': host, 'addresses': [], 'passed': False, 'code': 'dns_error'}


class PinnedTransport:
    """One instance per browser session; ledger limits are shared across sessions."""
    def __init__(self, adapter, ledger: RateLedger, cancelled: threading.Event,
                 progress=lambda *_: None, *, max_inline_wait=30):
        self.adapter, self.ledger, self.cancelled, self.progress = adapter, ledger, cancelled, progress
        self.network_policy = None  # Backend binds before callback dispatch; standalone callers stay lazy.
        self.domains = set((*adapter.domains, *adapter.resource_domains))
        if (isinstance(max_inline_wait, bool) or not isinstance(max_inline_wait, (int, float))
                or not math.isfinite(max_inline_wait) or not 0 <= max_inline_wait <= 60):
            raise ValueError('invalid inline wait budget')
        self.max_inline_wait = max_inline_wait
        self.robots = {}
        self.blocked = set()
        self.retry_until = {}

    def bind_policy(self, policy: NetworkPolicy) -> None:
        """Bind on the session owner's context, before Playwright starts callbacks.

        Sync Playwright dispatches routes on a different greenlet/context. Reading
        current_policy() there can lose workspace consent and its shared resolver.
        An already-bound session cannot be repurposed for another policy.
        """
        if not isinstance(policy, NetworkPolicy):
            raise TypeError('expected a network policy snapshot')
        if self.network_policy is not None and self.network_policy is not policy:
            raise ValueError('browser session network policy is already bound')
        self.network_policy = policy
        policy.bind_cancellation(self.cancelled)

    def reserve(self, kind: str, *, origin=None) -> None:
        deadline = time.monotonic() + self.max_inline_wait
        while not self.cancelled.is_set():
            try:
                self.ledger.reserve(self.adapter.key, kind, origin=origin)
                return
            except RateLimit as exc:
                if exc.code not in {'rate_wait', 'publisher_wait'} or exc.wait > deadline-time.monotonic():
                    raise  # Preserve the timestamp for durable task recovery.
                self.progress(exc.code, round(exc.wait, 1))
                if self.cancelled.wait(min(exc.wait, 1)):
                    break
        raise CrawlError('paused')

    @traced('http_request', 'transport', url=True)
    def fetch(self, url: str, method='GET', headers=None, body=None, *, required=True) -> WireResponse:
        try:
            host, target = validate_url_target(url, self.domains)
        except FetchError as exc:
            raise CrawlError(exc.code) from exc
        if self.cancelled.is_set():
            raise CrawlError('paused')
        if host in self.retry_until and self.ledger.clock() >= self.retry_until[host]:
            self.retry_until.pop(host)
            self.blocked.discard(host)
        if host in self.blocked:
            if host in self.retry_until:
                due = self.retry_until[host]
                raise RateLimit(due-self.ledger.clock(), 'cooldown', next_allowed_at=due)
            raise CrawlError('site_stopped')
        if body and len(body) > 1_000_000:
            raise CrawlError('request_too_large')
        # Validate before DNS, quota reservation or dialing; a bad field must
        # not send a partial HTTP request and then be retried.
        hdr = browser_headers(headers)
        if self.network_policy is None:
            self.bind_policy(current_policy())
        try:
            if not self.network_policy.encrypted_dns:
                host, ip, target = validate_public_url(url, self.domains, all_addresses=True,
                                                     network_policy=self.network_policy)
            self.reserve('request', origin='https://' + host)
            if self.network_policy.encrypted_dns:
                # A fresh snapshot is obtained after publisher waits, not before.
                host, ip, target = validate_public_url(url, self.domains, all_addresses=True,
                    network_policy=self.network_policy, cancelled=self.cancelled)
        except FetchError as exc:
            raise CrawlError(exc.code) from exc
        try:
            if self.network_policy is None:
                self.bind_policy(current_policy())
            with use_policy(self.network_policy):
                conn = PinnedHTTPSConnection(host, ip, 20)
        except FetchError as exc:
            raise CrawlError(exc.code) from exc
        try:
            conn.request(method, target, body=body, headers=hdr)
            response = conn.getresponse()
            notify(getattr(self, '_diagnostics', None), 'mark', status=response.status)
            pairs = response.getheaders()
            metadata = {k.lower(): v for k, v in pairs if k.lower() != 'set-cookie'}
            if response.status in {502, 503, 504} and sum(k.lower() == 'retry-after' for k, _ in pairs) > 1:
                metadata['retry-after'] = 'invalid'  # Ambiguous deadlines cannot authorize a retry.
            if response.status in {401, 403, 429}:
                if required or response.status == 429:
                    self.blocked.add(host)
                    delay = self._retry_seconds(metadata.get('retry-after', ''))
                    self.ledger.cool(self.adapter.key, delay)
                    if response.status == 429:
                        self.retry_until[host] = self.ledger.clock() + delay
                        raise RateLimit(delay, 'http_429', next_allowed_at=self.retry_until[host])
                raise CrawlError(f'http_{response.status}')
            content = response.read(5_000_001)
            if len(content) > 5_000_000:
                raise CrawlError('response_too_large')
            if metadata.get('content-encoding', 'identity').lower() != 'identity':
                raise CrawlError('unexpected_compression')
            return WireResponse(response.status, metadata, content,
                                tuple(v for k, v in pairs if k.lower() == 'set-cookie'))
        except FetchError as exc:
            raise CrawlError(exc.code) from exc
        except ssl.SSLCertVerificationError as exc:
            raise CrawlError('tls_verification_failed') from exc
        except ssl.SSLError as exc:
            raise CrawlError('tls_handshake_failed') from exc
        except (OSError, http.client.HTTPException) as exc:
            raise CrawlError('network_error') from exc
        finally:
            conn.close()

    @staticmethod
    def _retry_seconds(value: str) -> float:
        try:
            delay = float(value)
            return max(300, delay) if math.isfinite(delay) else 86400
        except ValueError:
            try:
                return max(300, parsedate_to_datetime(value).timestamp() - time.time())
            except (ValueError, TypeError, OverflowError):
                return 300

    @traced('robots', 'transport', url=True)
    def ensure_robots(self, url: str) -> None:
        p = urlsplit(url)
        origin = f'https://{p.netloc}'
        if origin not in self.robots:
            result = self.fetch(origin + '/robots.txt')
            observe_robots(getattr(self, '_diagnostics', None), result)
            # Both browser backends must interpret wildcards/statuses alike.
            # Preserve the bridge's missing-MIME compatibility for valid rules;
            # malformed/HTML/empty successful responses still cannot grant access.
            parser = NativeRobots(result.status,
                result.headers.get('content-type', 'text/plain'), result.body)
            self.robots[origin] = parser
        parser = self.robots[origin]
        if not parser.allowed(url):
            raise CrawlError('robots_denied')
        self.ledger.set_publisher(self.adapter.key, origin, delay=parser.delay)
        for count, seconds in parser.windows:
            self.ledger.set_publisher(self.adapter.key, origin, delay=parser.delay,
                                      requests=count, seconds=seconds)

    def allowed_resource(self, url: str) -> bool:
        p = urlsplit(url)
        return p.scheme == 'https' and bool(p.hostname) and any(domain_matches(p.hostname, d) for d in self.domains)
