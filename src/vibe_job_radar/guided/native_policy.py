"""Trusted-code native access contracts. Web forms cannot add hosts or operations.

Liepin has a recorded read-only search operation and explicit normal password
login operations, available only during a user-requested login action.
"""
from __future__ import annotations

from dataclasses import dataclass
import re
from urllib.parse import quote, unquote, urlsplit

from .contracts import CrawlError
from ..network import USER_AGENT


@dataclass(frozen=True)
class NativeRule:
    key: str
    host: str
    path: str
    methods: tuple[str, ...] = ('GET',)
    resources: tuple[str, ...] = ('Fetch', 'XHR')
    role: str = 'business'
    authentication: bool = False
    cors_origin: str = ''
    cors_headers: tuple[str, ...] = ()

    def __post_init__(self):
        if not re.fullmatch(r'[a-z][a-z0-9_]{1,39}', self.key):
            raise ValueError('invalid native operation identifier')
        if self.role not in {'document', 'business', 'asset', 'login'}:
            raise ValueError('invalid native operation role')
        if not self.methods or any(m not in {'GET', 'HEAD', 'POST', 'OPTIONS'} for m in self.methods):
            raise ValueError('invalid native operation method')
        if not re.fullmatch(r'[a-z0-9.-]{1,253}', self.host) or len(self.path) > 512:
            raise ValueError('invalid native operation target')
        re.compile(self.path)
        if self.cors_origin:
            p = urlsplit(self.cors_origin)
            if p.scheme != 'https' or p.path or p.query or p.fragment or p.username or p.password or p.port:
                raise ValueError('invalid native CORS origin')
            if not p.hostname or not self.cors_headers or any(not re.fullmatch(r'[a-z0-9-]+', h) for h in self.cors_headers):
                raise ValueError('invalid native CORS header contract')

    def validate_headers(self, method, headers):
        if not self.cors_origin:
            return
        values = {k.lower(): v for k, v in headers.items()}
        if values.get('origin') != self.cors_origin:
            raise CrawlError('native_operation_unreviewed')
        if method == 'OPTIONS':
            requested = {h.strip().lower() for h in values.get('access-control-request-headers', '').split(',') if h.strip()}
            if (values.get('access-control-request-method') != 'POST'
                    or not requested or not requested <= set(self.cors_headers)):
                raise CrawlError('native_operation_unreviewed')


@dataclass(frozen=True)
class NativeContract:
    key: str
    hosts: tuple[str, ...]
    rules: tuple[NativeRule, ...]
    bootstrap_only: bool = True
    ignored_rules: tuple[NativeRule, ...] = ()  # Abort-only optional dependencies, never egress permission.

    def __post_init__(self):
        if (not self.hosts or len(self.hosts) > 16 or len(self.rules) > 64 or len(self.ignored_rules) > 16
                or len(set(self.hosts)) != len(self.hosts)):
            raise ValueError('invalid native contract size')
        for host in self.hosts:
            if not re.fullmatch(r'[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?', host):
                raise ValueError('invalid exact hostname')
        if any(r.host not in self.hosts for r in self.rules):
            raise ValueError('rule host not declared')
        if any(r.role != 'asset' or r.authentication or r.cors_origin for r in self.ignored_rules):
            raise ValueError('ignored rule must be an optional abort-only dependency')

    def target(self, url, *, _hosts=None):
        if (not isinstance(url, str) or len(url) > 8192 or '\\' in url
                or any(ord(c) < 32 or ord(c) == 127 for c in url)):
            raise CrawlError('invalid_url')
        try:
            p = urlsplit(url)
            if (p.scheme != 'https' or p.hostname not in (self.hosts if _hosts is None else _hosts)
                    or p.port not in (None, 443) or p.username or p.password):
                raise CrawlError('resource_domain_blocked')
            # No path-smuggling interpretation differences in code-owned rules.
            path = unquote(p.path, errors='strict')
            if ('\\' in path or '%' in path or any(ord(c) < 32 for c in path)
                    or any(s in {'.', '..'} for s in path.split('/'))):
                raise CrawlError('invalid_url')
            return p, path
        except (ValueError, UnicodeError) as exc:
            raise CrawlError('invalid_url') from exc

    def ignored_request(self, url, method, resource):
        """Identify requests to abort, not targets the tunnel may connect to."""
        if not self.ignored_rules:
            return False
        try:
            p, path = self.target(url, _hosts=tuple(r.host for r in self.ignored_rules))
        except CrawlError:
            return False
        return any(r.host == p.hostname and method in r.methods and resource in r.resources
                   and re.fullmatch(r.path, path) for r in self.ignored_rules)

    def match(self, url, method, resource, *, authentication=False):
        p, path = self.target(url)
        for rule in self.rules:
            if (rule.host == p.hostname and method in rule.methods and resource in rule.resources
                    and (not rule.authentication or authentication) and re.fullmatch(rule.path, path)):
                return rule
        raise CrawlError('native_operation_unreviewed')

    @property
    def rule_origins(self):
        return tuple(dict.fromkeys('https://' + r.host for r in self.rules if r.role != 'asset'))


def liepin_bootstrap():
    # Historical request/markup evidence is recorded in LIEPIN_SEARCH_NATIVE.md.
    # The code contract supports search and filter initialization; it does NOT
    # certify current platform access. Login paths were checked against the
    # publisher's public frontend on 2026-09-23 (LIEPIN_PASSWORD_LOGIN.md).
    host, api, cdn, image = 'www.liepin.com', 'api-c.liepin.com', 'concat.lietou-static.com', 'image0.lietou-static.com'
    search = r'/api/com\.liepin\.searchfront4c\.pc-search-job'
    cors = dict(cors_origin='https://' + host, cors_headers=(
        'content-type', 'x-client-type', 'x-fscp-version', 'x-requested-with',
        'x-fscp-std-info', 'x-fscp-trace-id', 'x-fscp-fe-version', 'x-fscp-bi-stat', 'x-xsrf-token'))
    passport = 'api-passport.liepin.com'
    login = r'/api/com\.liepin\.passport\.account\.(?:account-pwd-login|check-login|v2\.check-login|get-category)'
    manifest = 'feim.liepin.com'
    return NativeContract('liepin_search_login_v2', (host, api, cdn, image, passport, manifest), (
        NativeRule('liepin_navigation', host, r'(?:/|/zhaopin/|/job/[^/]+\.(?:shtml|html)|/a/[0-9]+\.shtml|/lptjob/[0-9]+)',
                   resources=('Document',), role='document'),
        NativeRule('liepin_same_host_assets', host, r'.+\.(?:js|css|png|jpg|jpeg|gif|webp|svg|ico|woff2?|ttf)',
                   resources=('Script','Stylesheet','Image','Font'), role='asset'),
        NativeRule('liepin_static_assets', cdn,
                   r'/(?:fe-www-pc|fe-c-pc|fe-lib-pc|fe-im-pc)/v6/(?!apmplus/).+\.(?:js|css|png|jpg|jpeg|gif|webp|svg|ico|woff2?|ttf)',
                   resources=('Script','Stylesheet','Image','Font'), role='asset'),
        # The published search entry imports its shared UI container from this
        # static manifest. This permits neither chat APIs nor sending messages.
        NativeRule('liepin_ui_manifest', manifest, r'/lp-manifest\.json',
                   resources=('Fetch','XHR'), role='asset'),
        NativeRule('liepin_ui_manifest_script', manifest, r'/lp-manifest\.js',
                   resources=('Script',), role='asset'),
        NativeRule('liepin_static_images', image, r'/.+\.(?:png|jpg|jpeg|gif|webp|svg|ico)',
                   resources=('Image',), role='asset'),
        NativeRule('liepin_search', api, search, methods=('POST',), **cors),
        NativeRule('liepin_search_preflight', api, search, methods=('OPTIONS',),
                   resources=('Preflight', 'Other', 'Fetch', 'XHR'), role='business', **cors),
        NativeRule('liepin_search_filters', api, search + '-cond-init', methods=('POST',), **cors),
        NativeRule('liepin_filters_preflight', api, search + '-cond-init', methods=('OPTIONS',),
                   resources=('Preflight', 'Other', 'Fetch', 'XHR'), role='business', **cors),
        NativeRule('liepin_password_login', passport, login, methods=('POST',),
                   role='login', authentication=True, **cors),
        NativeRule('liepin_login_preflight', passport, login, methods=('OPTIONS',),
                   resources=('Preflight', 'Other', 'Fetch', 'XHR'), role='login',
                   authentication=True, **cors),
    ), bootstrap_only=False, ignored_rules=(
        # Public frontend's marketing placements/metrics are optional. Blocking
        # these must not kill the search document; no request is sent to them.
        NativeRule('liepin_marketing', 'api-wanda.liepin.com',
                   r'/api/com\.liepin\.cbp\.baizhong\.op\.(?:v2-show-4pc|v2-log-4pc|log-4pc)',
                   methods=('POST', 'OPTIONS'), resources=('Fetch', 'XHR', 'Preflight', 'Other'), role='asset'),
        NativeRule('liepin_telemetry', 'statistic.liepin.com',
                   r'/statisticPlatform/standardFLog\.json', methods=('POST', 'OPTIONS'),
                   resources=('Fetch', 'XHR', 'Preflight', 'Other'), role='asset'),
    ))


def contract_for(adapter):
    contract = getattr(adapter, 'native_contract', None)
    if contract is not None:
        if not isinstance(contract, NativeContract):
            raise CrawlError('native_contract_invalid')
        return contract
    if adapter.key == 'liepin':
        return liepin_bootstrap()
    raise CrawlError('native_contract_unavailable')


def capability(adapter):
    try:
        contract = contract_for(adapter)
        return {'available': True, 'contract': contract.key, 'bootstrap_only': contract.bootstrap_only,
                'certification': 'not_live_verified'}
    except CrawlError:
        return {'available': False, 'certification': 'not_live_verified'}


def _octets(value):
    """RFC9309 comparison: UTF-8 octets, unreserved %-escapes decoded only."""
    def escape(m):
        c = chr(int(m[0][1:], 16))
        return c if c.isascii() and (c.isalnum() or c in '-._~') else m[0].upper()
    return re.sub(r'%[0-9A-Fa-f]{2}', escape, quote(value, safe="/%*?$&=:+,;@!'-._~()"))


def _glob_matches(pattern, target, anchored):
    """Literal chunks only: publisher wildcards cannot cause regex backtracking."""
    chunks = pattern.split('*')
    if not target.startswith(chunks[0]):
        return False
    offset = len(chunks[0])
    if len(chunks) == 1:
        return not anchored or offset == len(target)
    for chunk in chunks[1:-1]:
        found = target.find(chunk, offset)
        if found < 0:
            return False
        offset = found + len(chunk)
    last = chunks[-1]
    if anchored:
        return target.endswith(last) and len(target) - len(last) >= offset
    return target.find(last, offset) >= 0


class NativeRobots:
    """Bounded explicit rules, with conservative unavailable/invalid handling.

    A 404/410 means no robots file (RFC 9309 section 2.3.1.3). Other errors,
    including authentication/rate denials and invalid 200 bodies, still stop.
    This does not grant an operation outside the site's request contract.
    Crawl-delay and Request-rate never reduce the existing ledger policy.
    """
    def __init__(self, status, content_type, body):
        self.rules, self.delay, self.windows = [], 0.0, []
        if status in {404, 410}:
            # Do not interpret an error document as publisher rules. In the
            # native browser it is delivered with scripts/resources disabled.
            return
        if (status != 200 or content_type.split(';')[0].strip().lower() != 'text/plain'
                or len(body) > 512 * 1024):
            raise CrawlError('robots_unavailable')
        try:
            text = body.decode('utf-8-sig')
        except UnicodeError as exc:
            raise CrawlError('robots_unavailable') from exc
        if re.search(r'<\s*(?:!doctype|html|script|body)\b', text, re.I):
            raise CrawlError('robots_unavailable')
        groups, agents, entries = [], [], []
        for raw in text.splitlines():
            line = raw.split('#', 1)[0].strip()
            if not line or ':' not in line:
                continue
            k, value = (x.strip() for x in line.split(':', 1)); k = k.lower()
            if k == 'user-agent':
                if entries:
                    groups.append((agents, entries)); agents, entries = [], []
                agents.append(value.lower())
            elif agents and k in {'allow','disallow','crawl-delay','request-rate'}:
                if len(value) > 2048:
                    raise CrawlError('robots_unavailable')
                entries.append((k, value))
        if agents:
            groups.append((agents, entries))
        if not groups:
            raise CrawlError('robots_unavailable')
        token = USER_AGENT.split('/')[0].lower()
        selected = [v for a,v in groups if token in a]
        if not selected:
            selected = [v for a,v in groups if '*' in a]
        for group in selected:
            for key, value in group:
                if key in {'allow','disallow'} and value:
                    if not value.startswith('/'):
                        raise CrawlError('robots_unavailable')
                    value = _octets(value)
                    end = value.endswith('$')
                    pattern = value[:-1] if end else value
                    # Literal chunk matching bounds work even for hostile wildcards.
                    if pattern.count('*') > 32:
                        raise CrawlError('robots_unavailable')
                    pattern = re.sub(r'\*+', '*', pattern)
                    self.rules.append((len(value.replace('*','').rstrip('$')), key == 'allow', pattern, end))
                elif key == 'crawl-delay':
                    if not re.fullmatch(r'[0-9]{1,6}(?:\.[0-9]{1,3})?', value):
                        raise CrawlError('robots_unavailable')
                    self.delay = max(self.delay, float(value))
                elif key == 'request-rate':
                    m = re.fullmatch(r'([1-9][0-9]{0,6})\s*/\s*([1-9][0-9]{0,7})', value)
                    if not m:
                        raise CrawlError('robots_unavailable')
                    self.windows.append(tuple(map(int, m.groups())))
        if len(self.rules) > 2000:
            raise CrawlError('robots_unavailable')

    def allowed(self, url):
        p = urlsplit(url)
        target = _octets(p.path or '/') + (('?' + _octets(p.query)) if p.query else '')
        matches = [(n, allow) for n,allow,pattern,end in self.rules if _glob_matches(pattern,target,end)]
        return max(matches, default=(0, True))[1]
