"""Opt-in Fake-IP repair, preserving public targets, target TLS and proxy route.

Only an entirely 198.18.0.0/15 *system DNS answer* triggers DoH. Private/mixed
answers, blocked requests and DNS failures are not reinterpreted as Fake-IP.
No recursive bootstrap lookup, arbitrary resolver URL or OS configuration write.
"""
from __future__ import annotations

import copy
import http.client
import ipaddress
import math
import re
import socket
import ssl
import threading
import time
from dataclasses import dataclass, replace

from .dns_wire import Answer, ResolutionError, hostname, parse_answer, query
from .loopback_proxy import LocalProxyError

PROVIDER = 'cloudflare-doh-v1'
DOH_HOST = 'cloudflare-dns.com'
BOOTSTRAP = ('1.1.1.1', '1.0.0.1', '2606:4700:4700::1111', '2606:4700:4700::1001')
FAKE_RANGE = ipaddress.ip_network('198.18.0.0/15')
# Do not disclose special/private names to a public resolver. In particular,
# RFC 7686 recommends a non-Tor application avoid ordinary DNS for .onion.
PRIVATE_ZONES = ('localhost', 'local', 'internal', 'home.arpa', 'invalid',
                 'test', 'example', 'onion')
TRANSIENT_ERRORS = frozenset({'encrypted_dns_unavailable', 'encrypted_dns_timeout'})


@dataclass(frozen=True)
class ResolutionSnapshot:
    addresses: tuple[str, ...]
    source: str
    expires_at: float
    policy_id: str
    cache_reused: bool = False


class PublicResolver:
    """Workspace-shared, bounded in-memory cache; no saved domain-query history."""
    def __init__(self, *, clock=time.monotonic, permission=None):
        self.clock = clock
        self.permission = permission
        self._cache = {}
        self._lock = threading.Lock()
        self._cooldown = 0.0
        self._cooldown_reason = 'encrypted_dns_cooldown'
        self._cooldown_diagnostic = None
        self._last_clock = None
        self._requests = []

    def clear(self):
        # Revocation clears answers but does not reset resolver rate limits.
        with self._lock:
            self._cache.clear()
            self._cooldown_diagnostic = None

    def resolve(self, host, policy, *, timeout=10.0, cancelled=None):
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError('invalid resolution budget')
        self._cancel(cancelled)
        try:
            literal = ipaddress.ip_address(host)
        except ValueError:
            pass
        else:
            if not literal.is_global or '%' in host:
                raise ResolutionError('non_public_address')
            return ResolutionSnapshot((str(literal),), 'literal_public_ip', 0, policy.fingerprint)
        host = hostname(host)
        if any(host == zone or host.endswith('.' + zone) for zone in PRIVATE_ZONES):
            raise ResolutionError('non_public_address')
        self._cancel(cancelled)
        try:
            policy.for_host(host)  # A broken explicit proxy cannot leak DNS.
        except LocalProxyError as exc:
            raise ResolutionError(exc.code) from exc
        try:
            raw = tuple(dict.fromkeys(a[4][0] for a in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)))
            ips = tuple(ipaddress.ip_address(ip) for ip in raw)
        except (OSError, ValueError):
            raise ResolutionError('dns_error') from None
        self._cancel(cancelled)
        if not ips or any('%' in ip for ip in raw):
            raise ResolutionError('non_public_address')
        if all(ip.is_global for ip in ips):
            return ResolutionSnapshot(tuple(str(ip) for ip in ips), 'system_dns', 0, policy.fingerprint)
        if not all(ip.version == 4 and ip in FAKE_RANGE for ip in ips):
            raise ResolutionError('non_public_address')
        if not policy.encrypted_dns:
            raise ResolutionError('non_public_address')
        # Only public DNS names, never a literal, local hostname or special zone.
        try:
            ipaddress.ip_address(host)
        except ValueError:
            pass
        else:
            raise ResolutionError('non_public_address')
        self._permission()
        deadline = self.clock() + min(timeout, 10.0)
        while not self._lock.acquire(timeout=min(0.05, max(0.001, deadline-self.clock()))):
            self._cancel(cancelled)
            if self.clock() >= deadline:
                raise ResolutionError('encrypted_dns_timeout')
        try:
            self._cancel(cancelled)
            # The user may have revoked permission while this lookup waited for
            # another lookup's lock. Never reuse its cached result on old consent.
            self._permission()
            now = self.clock()
            if self._last_clock is not None and now < self._last_clock:
                self._cache.clear()
                raise ResolutionError('encrypted_dns_clock_rollback')
            self._last_clock = now
            key = (host, policy.fingerprint, PROVIDER)
            saved = self._cache.get(key)
            if saved and now < saved.expires_at:
                return replace(saved, cache_reused=True)
            if now < self._cooldown:
                details = copy.deepcopy(self._cooldown_diagnostic)
                if details is not None:
                    details['reused_failure'] = True
                    details['matches_current_policy'] = details.get('policy_id') == policy.fingerprint
                raise ResolutionError(self._cooldown_reason, diagnostic=details)
            self._requests = [stamp for stamp in self._requests if now-stamp < 300]
            if len(self._requests) >= 60:
                raise ResolutionError('encrypted_dns_budget')
            # Reserve once per paired lookup (up to 2 HTTP calls), never refunded.
            self._requests.append(now)
            try:
                results = []
                for kind in (1, 28):
                    self._permission()
                    self._cancel(cancelled)
                    if self.clock() >= deadline:
                        raise ResolutionError('encrypted_dns_timeout')
                    answer = self._exchange(host, kind, policy, deadline, cancelled=cancelled)
                    self._permission()
                    self._cancel(cancelled)
                    if self.clock() >= deadline:
                        raise ResolutionError('encrypted_dns_timeout')
                    results.append((answer, self.clock()))
                ips = tuple(dict.fromkeys(ip for answer, _ in results for ip in answer.addresses))
                if not ips:
                    raise ResolutionError('encrypted_dns_empty_answer')
                now = self.clock()
                if now < self._last_clock or any(now < at for _, at in results):
                    raise ResolutionError('encrypted_dns_clock_rollback')
                # A no-store directive forbids reuse, not expiry checks. Negative
                # family answers also have to remain valid while the pair finishes.
                if any((a.valid_for if a.valid_for is not None else a.ttl) > 0
                       and now-at >= (a.valid_for if a.valid_for is not None else a.ttl)
                       for a, at in results):
                    raise ResolutionError('encrypted_dns_expired_answer')
                self._last_clock = now
                ttl = min(300.0, *(max(0.0, a.ttl-(now-at)) for a, at in results))
                snapshot = ResolutionSnapshot(ips, PROVIDER, now+ttl, policy.fingerprint)
                self._cache = {k: v for k, v in self._cache.items() if now < v.expires_at}
                if len(self._cache) >= 256:
                    self._cache.pop(next(iter(self._cache)))
                self._permission()
                self._cancel(cancelled)
                if ttl > 0:
                    self._cache[key] = snapshot
                return snapshot
            except ResolutionError as exc:
                if exc.code not in {'paused', 'encrypted_dns_disabled'}:
                    self._cooldown = max(self._cooldown, self.clock()+30)
                    # Preserve hard failures across subsequent attempts. Relabelling
                    # a certificate/private-answer error as temporary could allow
                    # the job cache to conceal it on the user's second click.
                    self._cooldown_diagnostic = copy.deepcopy(exc.diagnostic)
                    self._cooldown_reason = ('encrypted_dns_cooldown'
                                             if exc.code in TRANSIENT_ERRORS else exc.code)
                raise
        finally:
            self._lock.release()

    def _permission(self):
        if self.permission is not None:
            try:
                permitted = self.permission() is True
            except Exception:
                permitted = False
            if not permitted:
                raise ResolutionError('encrypted_dns_disabled')

    @staticmethod
    def _cancel(cancelled):
        if cancelled is not None and cancelled.is_set():
            raise ResolutionError('paused')

    def _exchange(self, host, kind, policy, deadline, *, cancelled=None) -> Answer:
        # Import lazily to keep codec and transport dependencies acyclic.
        from .network import FetchError, PinnedHTTPSConnection
        selected = policy.for_host(host)
        # Resolver traffic uses the SAME target route. A resolver NO_PROXY rule
        # cannot accidentally bypass the proxy selected for the job hostname.
        route = replace(policy, source='explicit_application', proxy=selected, bypass=(),
                        encrypted_dns=False, resolver=None,
                        pac_route_host=host if policy.pac is not None else '')
        conn = None
        phase = 'tls_context'
        try:
            self._permission()
            self._cancel(cancelled)
            budget = max(0.001, deadline-self.clock())
            conn = PinnedHTTPSConnection(DOH_HOST, BOOTSTRAP, budget, network_policy=route)
            phase = 'tls_handshake'
            conn.connect()
            def remaining():
                self._permission()
                self._cancel(cancelled)
                value = deadline-self.clock()
                if value <= 0:
                    raise ResolutionError('encrypted_dns_timeout')
                if conn.sock is not None:
                    conn.sock.settimeout(value)
            remaining()
            started = self.clock()
            phase = 'dns_request'
            conn.request('POST', '/dns-query', body=query(host, kind), headers={
                'Accept': 'application/dns-message', 'Content-Type': 'application/dns-message',
                'Accept-Encoding': 'identity', 'User-Agent': 'VibeJobRadar-DNS/1'})
            remaining()
            phase = 'response_headers'
            response = conn.getresponse()
            headers = {}
            for key, value in response.getheaders():
                key = key.lower()
                if key == 'cache-control':
                    headers[key] = ','.join(filter(None, (headers.get(key), value)))
                else:
                    if key in headers and key in {'age', 'content-type', 'content-encoding'}:
                        raise ResolutionError('encrypted_dns_invalid_response')
                    headers[key] = value
            if response.status != 200:
                if response.status == 429:
                    from .network import retry_after_seconds
                    self._cooldown = max(self._cooldown, self.clock()+retry_after_seconds(headers.get('retry-after','')))
                raise ResolutionError('encrypted_dns_http_rejected')
            if (headers.get('content-type','').split(';')[0].strip().lower() != 'application/dns-message'
                    or headers.get('content-encoding','identity').lower() != 'identity'):
                raise ResolutionError('encrypted_dns_invalid_response')
            phase = 'response_body'
            parts = bytearray()
            while True:
                remaining()
                # read1 prevents a slow body from resetting the overall deadline.
                part = response.read1(min(4096, 65536-len(parts)))
                if not part:
                    break
                parts.extend(part)
                if len(parts) > 65535:
                    raise ResolutionError('encrypted_dns_invalid_response')
            remaining()
            age = headers.get('age','0')
            if not re.fullmatch(r'[0-9]{1,10}', age):
                raise ResolutionError('encrypted_dns_invalid_response')
            elapsed = self.clock() - started
            if elapsed < 0:
                raise ResolutionError('encrypted_dns_clock_rollback')
            result = parse_answer(bytes(parts), host, kind, age=int(age), elapsed=elapsed)
            cache = headers.get('cache-control','').lower()
            if len(cache) > 4096:
                raise ResolutionError('encrypted_dns_invalid_response')
            matches = re.findall(r'(?:^|,)\s*max-age\s*=\s*(?:"([0-9]{1,10})"|([0-9]{1,10}))\s*(?=,|$)', cache)
            directives = re.findall(r'(?:^|,)\s*max-age\s*(?==|,|$)', cache)
            if len(matches) != len(directives) or len(matches) > 1:
                raise ResolutionError('encrypted_dns_invalid_response')
            if matches:
                limit = int(matches[0][0] or matches[0][1])
                spent = int(age) + elapsed
                if (limit > 0 and spent >= limit) or (limit == 0 and int(age) > 0):
                    raise ResolutionError('encrypted_dns_expired_answer')
                cache_ttl = max(0, limit - spent)
                result = replace(result, ttl=min(result.ttl, cache_ttl),
                                 valid_for=min(result.valid_for, cache_ttl)
                                 if limit > 0 else result.valid_for)
            if re.search(r'(?:^|,)\s*(?:no-store|no-cache)\s*(?==|,|$)', cache):
                result = replace(result, ttl=0)
            return result
        except ResolutionError:
            raise
        except ssl.SSLError as exc:
            from .tls_diagnostic import failure_details
            # Keep the stable hard-failure code. Details do not change retries,
            # trust, SNI, target route, consent or the request budget.
            details = failure_details(exc, phase=phase, connection=conn,
                                      bootstrap=BOOTSTRAP, policy_id=policy.fingerprint)
            raise ResolutionError('encrypted_dns_tls_failed', diagnostic=details) from None
        except FetchError as exc:
            # Keep the reason category only, never upstream headers or URL data.
            raise ResolutionError('encrypted_dns_route_failed') from exc
        except (OSError, http.client.HTTPException):
            raise ResolutionError('encrypted_dns_unavailable') from None
        finally:
            if conn is not None:
                conn.close()
