"""Trusted-code native access contracts. Web forms cannot add hosts or operations.

Liepin has a recorded read-only search operation and explicit normal password
login operations, available only during a user-requested login action.
"""
from __future__ import annotations

from dataclasses import dataclass
import re
from urllib.parse import unquote, urlsplit

from .contracts import CrawlError
from ..network import USER_AGENT
from ..robots_rules import RobotsRules, RobotsError


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
                   r'/statisticPlatform/standard[FT]Log\.json', methods=('POST', 'OPTIONS'),
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


class NativeRobots(RobotsRules):
    """Preserve the existing browser error contract while sharing matching."""
    def __init__(self, status, content_type, body):
        try:
            super().__init__(status, content_type, body, user_agent=USER_AGENT)
        except RobotsError as exc:
            raise CrawlError(exc.code) from exc
