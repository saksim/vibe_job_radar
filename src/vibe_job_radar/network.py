"""Small synchronous HTTPS transport: public IP pinning, verified TLS and shared static proxy policy.

Site fetching is opt-in and additionally robots-gated. API calls use documented
endpoints and explicit credentials, not the site-fetch path.
"""
from __future__ import annotations

import http.client
import ipaddress
import json
import math
import re
import errno
import socket
import ssl
import time
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit, urlunsplit
from .robots_rules import RobotsRules, RobotsError
from .utils import domain_matches
from .tls_context import create_client_context
from .loopback_proxy import LoopbackProxy, LocalProxyError
from .network_policy import NetworkPolicy, current_policy, use_policy

USER_AGENT = "VibeJobRadar/0.1"


class FetchError(RuntimeError):
    def __init__(self, code: str, message: str = "", *, retry_after: float | None = None):
        self.code = code
        self.retry_after = retry_after
        super().__init__(f"{code}: {message}" if message else code)


def retry_after_seconds(value: str, *, now=None) -> float:
    """Keep publisher cooldown metadata without retaining response headers."""
    try:
        seconds=float(value)
    except (ValueError,TypeError):
        try:
            seconds=parsedate_to_datetime(value).timestamp()-(time.time() if now is None else now)
        except (ValueError,TypeError,OverflowError):
            seconds=300
    return max(300,seconds) if math.isfinite(seconds) else 86400


@dataclass
class Response:
    status: int
    headers: dict[str, str]
    body: bytes
    url: str
    redirect_trace: list[dict] = field(default_factory=list)
    resolution: dict = field(default_factory=dict)

    def text(self) -> str:
        import re
        match = re.search(r'charset=["\']?([\w-]+)', self.headers.get("content-type", ""), re.I)
        if match:
            try:
                return self.body.decode(match.group(1))
            except (LookupError, UnicodeDecodeError):
                pass
        for enc in ("utf-8-sig", "gb18030"):
            try:
                return self.body.decode(enc)
            except UnicodeDecodeError:
                pass
        raise FetchError("encoding_unknown", "save and review the page manually")


def valid_etag(value) -> bool:
    # One bounded ASCII entity-tag, never '*', a list, controls or header lines.
    return isinstance(value,str) and len(value)<=512 and re.fullmatch(r'(?:W/)?"[\x21\x23-\x7e]*"',value) is not None


@dataclass(frozen=True)
class JSONRepresentation:
    status: int
    payload: dict | None
    etag: str | None


def _connection_candidates(ips: str | tuple[str, ...]) -> tuple[str, ...]:
    """Interleave families from one validated DNS snapshot; never resolve again.

    The destination snapshot is identical for system routing or an explicit
    loopback proxy. No remote DNS or private-destination exception is allowed.
    """
    supplied = (ips,) if isinstance(ips, str) else tuple(ips)
    if not supplied:
        raise FetchError("non_public_address")
    families: dict[int, list[str]] = {4: [], 6: []}
    order: list[int] = []
    for value in supplied:
        try:
            address = ipaddress.ip_address(value)
        except ValueError as exc:
            raise FetchError("non_public_address") from exc
        if not address.is_global or "%" in value:
            raise FetchError("non_public_address")
        family = address.version
        if family not in order:
            order.append(family)
        normal = str(address)
        if normal not in families[family]:
            families[family].append(normal)
    result: list[str] = []
    # Four bounded TCP/TLS attempts, preserving order within each family.
    # All supplied addresses have been validated BEFORE limiting the candidates.
    for index in range(max(map(len, families.values()))):
        for family in order:
            if index < len(families[family]):
                result.append(families[family][index])
                if len(result) == 4:
                    return tuple(result)
    return tuple(result)


class PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host: str, ip: str | tuple[str, ...], timeout: float, *,
                 network_policy: NetworkPolicy | None = None):
        if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("a finite positive connection timeout is required")
        self._pinned_ips = _connection_candidates(ip)
        try:
            self.network_policy = network_policy or current_policy()
            self._local_proxy = self.network_policy.for_host(host)
        except LocalProxyError as exc:
            raise FetchError(exc.code) from exc
        self.network_mode = self.network_policy.transport_name(self._local_proxy)
        self.connection_attempts: list[dict] = []
        self.connected_ip: str | None = None
        super().__init__(host, port=443, timeout=timeout, context=create_client_context())

    def connect(self) -> None:
        # HTTP bytes are not sent until this method returns. Only pre-request
        # TCP errors/timeouts may advance to the next already-validated IP.
        deadline = time.monotonic() + self.timeout
        self.connection_attempts = []
        self.connected_ip = None
        last_error: OSError | None = None
        for index, ip in enumerate(self._pinned_ips):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            candidates_left = len(self._pinned_ips) - index
            attempt_budget = remaining / candidates_left
            attempt_deadline = time.monotonic() + attempt_budget
            record = {"ip": ip, "phase": "proxy_connect" if self._local_proxy else "tcp", "outcome": "pending"}
            self.connection_attempts.append(record)
            sock = None
            try:
                self.network_policy.ensure_active()
                if self._local_proxy:
                    sock = self._local_proxy.open_tunnel(ip, attempt_budget, self.source_address)
                else:
                    sock = socket.create_connection((ip, 443), attempt_budget, self.source_address)
                remaining = min(deadline, attempt_deadline) - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("connection attempt deadline exceeded")
                sock.settimeout(remaining)
                record["phase"] = "tls"
                # Keep the original hostname for SNI and certificate validation.
                secured = self._context.wrap_socket(sock, server_hostname=self.host)
                sock = secured
                if time.monotonic() >= deadline:
                    raise TimeoutError("connection deadline exceeded")
                secured.settimeout(self.timeout)
                self.sock = secured
                self.connected_ip = ip
                record["outcome"] = "connected"
                return
            except LocalProxyError as exc:
                record.update(outcome='proxy_failed', error_type=type(exc).__name__)
                if sock is not None:
                    sock.close()
                # A selected proxy never falls back to a direct TCP connection.
                raise FetchError(exc.code) from exc
            except ssl.SSLError as exc:
                # Invalid certificates and protocol failures are NOT retried.
                # Never turn a TLS security failure into a different route.
                record.update(outcome="tls_rejected", error_type=type(exc).__name__)
                if sock is not None:
                    sock.close()
                raise
            except OSError as exc:
                record.update(outcome="connect_failed", error_type=type(exc).__name__)
                if sock is not None:
                    sock.close()
                if exc.errno in (errno.EACCES, errno.EPERM):
                    raise
                last_error = exc
        if last_error is not None:
            raise last_error
        raise TimeoutError("connection deadline exceeded")


def validate_url_target(url: str, allowed_domains: set[str]) -> tuple[str, str]:
    """Validate the permitted HTTPS target without DNS or a connection."""
    p = urlsplit(url)
    if p.scheme != "https" or not p.hostname or p.username or p.password or p.fragment:
        raise FetchError("unsafe_url", "only credential-free HTTPS URLs without fragments are fetched")
    if p.port not in (None, 443):
        raise FetchError("unsafe_port")
    host = p.hostname.lower().encode("idna").decode("ascii")
    if not any(domain_matches(host, d) for d in allowed_domains):
        raise FetchError("domain_not_permitted", host)
    target = urlunsplit(("", "", p.path or "/", p.query, ""))
    return host, target


def validate_public_url(url: str, allowed_domains: set[str], *, all_addresses: bool = False,
                        network_policy: NetworkPolicy | None = None, resolver=None, cancelled=None,
                        resolution_info: dict | None = None
                        ) -> tuple[str, str | tuple[str, ...], str]:
    host, target = validate_url_target(url, allowed_domains)
    if network_policy is not None:
        try:
            network_policy.for_host(host)  # Invalid explicit credentials must not leak DNS first.
        except LocalProxyError as exc:
            raise FetchError(exc.code) from exc
    if network_policy is not None and network_policy.encrypted_dns:
        from .encrypted_dns import PublicResolver, ResolutionError
        active = resolver or network_policy.resolver or PublicResolver()
        try:
            resolved = active.resolve(host, network_policy, cancelled=cancelled)
            ips = list(resolved.addresses)
            if resolution_info is not None:
                resolution_info.update(source=resolved.source, policy_id=resolved.policy_id,
                    cache_reused=resolved.cache_reused, address_count=len(ips),
                    ttl_remaining=max(0.0, resolved.expires_at-active.clock()))
        except ResolutionError as exc:
            raise FetchError(exc.code) from exc
    else:
        try:
            answers = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
            ips = list(dict.fromkeys(answer[4][0] for answer in answers))
            if not ips or any(not ipaddress.ip_address(ip).is_global for ip in ips):
                raise FetchError("non_public_address")
            if resolution_info is not None:
                resolution_info.update(source='system_dns', cache_reused=False, address_count=len(ips), ttl_remaining=None)
        except (socket.gaierror, ValueError) as exc:
            raise FetchError("dns_error", type(exc).__name__) from exc
    return host, tuple(ips) if all_addresses else ips[0], target


class SafeHTTP:
    def __init__(self, allowed_domains: set[str], *, timeout: float = 20.0, max_bytes: int = 5_000_000,
                 interval: float = 1.0, network_policy: NetworkPolicy | None = None):
        if timeout <= 0 or max_bytes <= 0 or interval < 0:
            raise ValueError("invalid transport limits")
        self.network_policy = network_policy or current_policy()
        from .encrypted_dns import PublicResolver
        self.resolver = self.network_policy.resolver or PublicResolver()
        self.allowed_domains = set(allowed_domains)
        self.timeout, self.max_bytes, self.interval = timeout, max_bytes, interval
        self.last_resolution: dict = {}
        self.last_request: dict[str, float] = {}
        self.blocked_hosts: set[str] = set()

    def request(self, url: str, *, method: str = "GET", headers: dict | None = None,
                body: bytes | None = None, return_redirect: bool = False,
                if_none_match: str | None = None) -> Response:
        if if_none_match is not None:
            if (not valid_etag(if_none_match) or method!='GET' or body is not None or return_redirect
                    or headers not in (None,{'Accept':'application/json'})):
                raise FetchError('invalid_conditional_request')
        host, _ = validate_url_target(url, self.allowed_domains)
        if host in self.blocked_hosts:
            raise FetchError("host_circuit_open", host)
        wait = self.interval - (time.monotonic() - self.last_request.get(host, 0))
        if wait > 0:
            time.sleep(wait)
        # Resolve AFTER pacing: a short DNS TTL must not expire while waiting.
        self.last_resolution = {}
        host, ip, target = validate_public_url(url, self.allowed_domains, all_addresses=True,
            network_policy=self.network_policy, resolver=self.resolver, resolution_info=self.last_resolution)
        with use_policy(self.network_policy):
            conn = PinnedHTTPSConnection(host, ip, self.timeout)
        request_headers = {"User-Agent": USER_AGENT, "Accept-Encoding": "identity"}
        request_headers.update(headers or {})
        if if_none_match is not None:request_headers['If-None-Match']=if_none_match
        try:
            conn.request(method, target, body=body, headers=request_headers)
            resp = conn.getresponse()
            raw_headers=resp.getheaders()
            response_headers = {k.lower(): v for k, v in raw_headers}
            if sum(k.lower()=='etag' for k,v in raw_headers)>1:
                response_headers['etag']=''  # Conflicting/list validators cannot qualify a 304.
            if resp.status in (401, 403, 429):
                self.blocked_hosts.add(host)
                raise FetchError(f"http_{resp.status}", "stopped; no bypass or retry",
                                 retry_after=retry_after_seconds(response_headers.get("retry-after", ""))
                                 if resp.status == 429 else None)
            if 300 <= resp.status < 400:
                if resp.status==304 and if_none_match is not None:
                    tag=response_headers.get('etag')
                    if not valid_etag(tag) or tag.removeprefix('W/')!=if_none_match.removeprefix('W/'):
                        raise FetchError('invalid_not_modified')
                    return Response(304,response_headers,b'',url,resolution=dict(self.last_resolution))
                if return_redirect and method == "GET" and not headers and body is None:
                    # Return metadata only; a higher-level policy validates the next
                    # target. API calls retain the original non-following contract.
                    return Response(resp.status, response_headers, b"", url, resolution=dict(self.last_resolution))
                raise FetchError("redirect_not_followed", "redirect requires an explicit site policy")
            if response_headers.get("content-encoding", "identity").lower() != "identity":
                raise FetchError("unexpected_compression")
            data = resp.read(self.max_bytes + 1)
            if len(data) > self.max_bytes:
                raise FetchError("response_too_large")
            return Response(resp.status, response_headers, data, url, resolution=dict(self.last_resolution))
        except FetchError:
            raise
        except ssl.SSLCertVerificationError as exc:
            raise FetchError("tls_verification_failed", type(exc).__name__) from exc
        except ssl.SSLError as exc:
            raise FetchError("tls_handshake_failed", type(exc).__name__) from exc
        except (OSError, http.client.HTTPException) as exc:
            raise FetchError("network_error", type(exc).__name__) from exc
        finally:
            self.last_request[host] = time.monotonic()
            conn.close()

    def public_get(self, url: str) -> Response:
        """Anonymous one-hop GET. Never follows or forwards credentials."""
        return self.request(url, return_redirect=True)

    def conditional_json(self, url: str, *, etag: str | None = None) -> JSONRepresentation:
        """Fixed anonymous JSON representation; only a valid condition admits 304."""
        result=self.request(url,headers={'Accept':'application/json'},if_none_match=etag)
        tag=result.headers.get('etag')
        if result.status==304:
            # Also validate injected Responses; no unconditional or mismatched 304.
            if (not valid_etag(etag) or not valid_etag(tag)
                    or tag.removeprefix('W/')!=etag.removeprefix('W/') or result.body):
                raise FetchError('invalid_not_modified')
            return JSONRepresentation(304,None,tag)
        if result.status!=200:raise FetchError(f'http_{result.status}')
        return JSONRepresentation(200,self._json_payload(result),tag if valid_etag(tag) else None)

    @staticmethod
    def _json_payload(result: Response) -> dict:
        try:data=json.loads(result.text())
        except (ValueError,UnicodeError) as exc:raise FetchError('invalid_api_json') from exc
        if not isinstance(data,dict):raise FetchError('invalid_api_shape')
        return data

    def json(self, url: str, *, method: str = "GET", headers: dict | None = None, payload: dict | None = None) -> dict:
        hdr = {"Accept": "application/json", **(headers or {})}
        body = None
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
            hdr["Content-Type"] = "application/json"
        result = self.request(url, method=method, headers=hdr, body=body)
        if result.status != 200:
            raise FetchError(f"http_{result.status}")
        return self._json_payload(result)


class SiteFetcher:
    """Anonymous HTML acquisition with a bounded, per-hop redirect policy.

    A redirect never grants permission, copies cookies, bypasses robots or resets
    the transport's request interval. API clients remain strict/non-following.
    """
    def __init__(self, permitted_domains: set[str], transport: SafeHTTP | None = None,
                 *, max_redirects: int = 3):
        if not permitted_domains or type(max_redirects) is not int or not 0 <= max_redirects <= 5:
            raise ValueError("explicit domains and a 0..5 redirect limit are required")
        self.domains = set(permitted_domains)
        self.transport = transport or SafeHTTP(permitted_domains, interval=2.0)
        self.max_redirects = max_redirects
        self.robots: dict[str, RobotsRules | None] = {}
        self.last_diagnostic: dict = {}

    def _get(self, url: str, phase: str) -> Response:
        from .redirect_policy import observed_origin
        self.last_diagnostic.update(phase=phase, last_origin=observed_origin(url))
        self.last_diagnostic["http_attempts"] += 1
        # Injected offline transports may expose only request(); production uses
        # SafeHTTP.public_get with pinned TLS and no implicit following.
        get = getattr(self.transport, "public_get", None) or self.transport.request
        response = get(url)
        self.last_diagnostic["http_status"] = response.status
        return response

    def _follow(self, current: str, response: Response, phase: str,
                visited: set[str]) -> str:
        from .redirect_policy import redirect_target, observed_origin
        hop = {"phase": phase, "status": response.status, "from_origin": observed_origin(current),
               "target_origin": observed_origin(response.headers.get("location", ""), current)}
        self.last_diagnostic["redirects"].append(hop)
        try:
            if len(self.last_diagnostic["redirects"]) > self.max_redirects:
                raise FetchError("redirect_limit")
            target = redirect_target(current, response.headers.get("location"), self.domains,
                                     status=response.status, robots=phase == "robots")
            if target in visited:
                raise FetchError("redirect_loop")
            visited.add(target)
            hop["result"] = "followed"
            return target
        except FetchError as exc:
            hop["result"] = exc.code
            raise

    def _ensure_robots(self, url: str) -> None:
        p = urlsplit(url)
        origin = f"https://{p.netloc}"
        if origin not in self.robots:
            self.robots[origin] = None
            current = origin + "/robots.txt"
            visited = {current}
            while True:
                result = self._get(current, "robots")
                if 300 <= result.status < 400:
                    current = self._follow(current, result, "robots", visited)
                    continue
                break
            # This legacy route still requires a present robots file; sharing
            # matching does not expand its existing 404/410 access policy.
            if result.status == 200:
                try:
                    # Preserve legacy header-less plain-text support, but the
                    # shared parser now requires valid UTF-8 rules, never HTML.
                    self.robots[origin] = RobotsRules(200,
                        result.headers.get('content-type') or 'text/plain', result.body,
                        user_agent=USER_AGENT)
                except RobotsError:
                    pass  # Cached unavailable decision; do not fetch the JD.
        rp = self.robots[origin]
        if rp is None:
            raise FetchError("robots_unavailable", "automation is not enabled for this origin")
        if not rp.allowed(url):
            raise FetchError("robots_denied")
        self.transport.interval = max(self.transport.interval, rp.delay,
            *(seconds / requests for requests, seconds in rp.windows))

    def fetch(self, url: str) -> Response:
        from .redirect_policy import validate_target, page_gate
        self.last_diagnostic = {"phase": "validation", "http_attempts": 0, "redirects": []}
        try:
            current = validate_target(url, self.domains)
            visited = {current}
            while True:
                # Every redirected detail path is checked, including same-origin
                # redirects into a path disallowed by an already cached robots.
                self.last_diagnostic["phase"] = "robots"
                self._ensure_robots(current)
                result = self._get(current, "detail")
                if 300 <= result.status < 400:
                    current = self._follow(current, result, "detail", visited)
                    continue
                if result.status != 200:
                    raise FetchError(f"http_{result.status}")
                if "html" not in result.headers.get("content-type", "").lower():
                    raise FetchError("not_html")
                # Visible text only: an unused captcha script is not a challenge.
                page_gate(result.text())
                result.url = current
                result.redirect_trace = list(self.last_diagnostic["redirects"])
                self.last_diagnostic.update(phase="complete", final_url=current, outcome="ok")
                return result
        except FetchError as exc:
            self.last_diagnostic["outcome"] = exc.code
            raise
