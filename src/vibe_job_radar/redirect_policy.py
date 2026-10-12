"""Pure redirect decisions. Do not persist raw Location headers or their queries."""
from __future__ import annotations

import ipaddress
import re
from urllib.parse import parse_qsl, urljoin, urlsplit, urlunsplit

from .network import FetchError
from .url_safety import credential_query_key
from .utils import domain_matches

REDIRECT_STATUSES = {301, 302, 303, 307, 308}
AUTH_PATH = re.compile(r'/(?:login|signin|sign-in|passport|oauth|sso|web/user)(?:[/.]|$)', re.I)
CHALLENGE_PATH = re.compile(r'/(?:captcha|challenge|verify|verification|intercept|wapi/zppass)(?:[/.]|$)', re.I)


def observed_origin(value: str, base: str = '') -> str:
    """Origin-only evidence is enough to explain a redirect without copying secrets."""
    try:
        p = urlsplit(urljoin(base, value))
        if p.scheme not in {'https', 'http'} or not p.hostname:
            return '[invalid target]'
        host = p.hostname.encode('idna').decode('ascii')
        return f'{p.scheme}://{host}'[:300]
    except (ValueError, UnicodeError):
        return '[invalid target]'


def validate_target(url: str, domains: set[str]) -> str:
    if (not isinstance(url, str) or not url or len(url) > 4096
            or '\\' in url or any(ord(c) <= 32 or ord(c) == 127 for c in url)):
        raise FetchError('redirect_invalid')
    try:
        p = urlsplit(url)
        if p.scheme != 'https' or not p.hostname or p.username or p.password or p.port not in (None, 443):
            raise FetchError('redirect_unsafe_target')
        host = p.hostname.encode('idna').decode('ascii')
        if not any(domain_matches(host, d) for d in domains):
            raise FetchError('redirect_domain_not_permitted')
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            address = None
        if address is not None and not address.is_global:
            raise FetchError('non_public_address')
        if any(credential_query_key(k) for k, _ in parse_qsl(p.query, keep_blank_values=True)):
            raise FetchError('redirect_credentials_blocked')
        # DNS is not resolved by this pure function; SafeHTTP re-resolves, checks
        # every returned address and pins verified TLS for each actual request.
        return urlunsplit((p.scheme, p.netloc.lower(), p.path or '/', p.query, ''))
    except (ValueError, UnicodeError) as exc:
        raise FetchError('redirect_invalid') from exc


def redirect_target(current: str, location: str | None, domains: set[str],
                    *, status: int, robots: bool = False) -> str:
    if status not in REDIRECT_STATUSES:
        raise FetchError('redirect_unsupported_status')
    if not isinstance(location, str) or not location.strip():
        raise FetchError('redirect_missing_location')
    # Never strip embedded control characters into a seemingly valid URL.
    if any(ord(c) <= 32 or ord(c) == 127 for c in location) or '\\' in location:
        raise FetchError('redirect_invalid')
    try:
        target = urljoin(current, location)
        p = urlsplit(target)
        host = p.hostname or ''
        if CHALLENGE_PATH.search(p.path):
            raise FetchError('redirect_verification_required')
        if AUTH_PATH.search(p.path) or host.split('.')[0].lower() in {'login', 'passport', 'auth', 'sso'}:
            raise FetchError('redirect_login_required')
        target = validate_target(target, domains)
        if robots:
            source = urlsplit(current)
            destination = urlsplit(target)
            # validate_target already requires HTTPS and its default port.
            # An explicit :443 or host case change is still the same origin.
            if (source.scheme != destination.scheme or source.hostname != destination.hostname
                    or source.port not in (None, 443)):
                raise FetchError('robots_redirect_cross_origin')
        return target
    except (ValueError, UnicodeError) as exc:
        raise FetchError('redirect_invalid') from exc


def page_gate(markup: str) -> None:
    from .html_parser import plain_text
    text = plain_text(markup)
    if re.search(r'请完成.{0,12}验证|访问过于频繁|滑动.{0,8}验证|安全验证|访问异常|'
                 r'登录后.{0,8}(?:查看|浏览)|verify you are human|access denied', text, re.I):
        raise FetchError('login_or_challenge')
