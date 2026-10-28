"""Observed human-login dependencies for the bridge browser only.

The publisher submits SMS/QR requests and renders its verification widget. We
never construct these requests, solve challenges or record their bodies. See
docs/LIEPIN_MANUAL_LOGIN.md for public-source evidence and scope.
The native backend's separate frame policy and checkpoint contract are unchanged.
"""
from urllib.parse import urlsplit
import re

from .contracts import CrawlError
from .native_policy import NativeContract, NativeRule
from ..utils import domain_matches


CAPTCHA_HOSTS = ('turing.captcha.qcloud.com', 'turing.captcha.gtimg.com')
SECURITY_CONFIG = 'dalisi4api.tongdao.cn'
EXTRA_HOSTS = (*CAPTCHA_HOSTS, 'api-security-liepin.liepin.com', SECURITY_CONFIG)
PASSPORT = 'api-passport.liepin.com'
SECURITY = 'api-security-liepin.liepin.com'
ACCOUNT = 'c.liepin.com'
_HEADERS = ('content-type', 'x-client-type', 'x-fscp-version', 'x-requested-with',
            'x-fscp-std-info', 'x-fscp-trace-id', 'x-fscp-fe-version',
            'x-fscp-bi-stat', 'x-xsrf-token')
_LOGIN_PATH = (r'/api/com\.liepin\.passport\.(?:'
               r'captcha\.(?:get-smart-captcha|send-sms)|'
               r'account\.(?:tel-sms-login|send-two-factor-sms-code|verify-two-factor-sms-code)|'
               r'qr\.(?:get-qrcode|get-mini-qrcode|ack))')


def _contract():
    rules = []
    for suffix, origin in (('www', 'https://www.liepin.com'), ('c', 'https://c.liepin.com')):
        for host, path, name in (
            (PASSPORT, _LOGIN_PATH, 'sms_qr'),
            (SECURITY, r'/api/com\.liepin\.security\.agreement\.unlogin\.save-agreement-log', 'agreement'),
        ):
            rules.append(NativeRule('liepin_' + name + '_' + suffix, host, path,
                methods=('POST', 'OPTIONS'), resources=('XHR', 'Fetch', 'Other', 'Preflight'),
                role='login', authentication=True, cors_origin=origin, cors_headers=_HEADERS))
    rules.extend((
        # Same required dependency as the native contract. A failed DNS
        # lookup is delivered to the publisher's existing failure handler;
        # no configuration or successful response is invented.
        NativeRule('liepin_security_config', SECURITY_CONFIG, r'/static/cfg/v2\.json',
                   handles_dns_failure=True),
        NativeRule('liepin_two_factor', ACCOUNT, r'/login/two-factor',
                   resources=('Document',), role='document', authentication=True),
        NativeRule('captcha_sdk', CAPTCHA_HOSTS[0], r'/(?:TCaptcha|TJCaptcha)\.js',
                   resources=('Script',), role='asset', authentication=True),
        NativeRule('captcha_frame', CAPTCHA_HOSTS[0],
                   r'/template/(?:placeholder_v2|new_slide_placeholder|drag_ele)\.html',
                   resources=('Document',), role='document', authentication=True),
        NativeRule('captcha_prehandle', CAPTCHA_HOSTS[0], r'/cap_union_prehandle',
                   resources=('Script',), role='login', authentication=True),
        NativeRule('captcha_image', CAPTCHA_HOSTS[0], r'/cap_union_new_getcapbysig',
                   resources=('Image',), role='asset', authentication=True),
        NativeRule('captcha_environment', CAPTCHA_HOSTS[0], r'/tdc\.js',
                   resources=('Script',), role='asset', authentication=True),
        NativeRule('captcha_monitor', CAPTCHA_HOSTS[0], r'/cap_monitor',
                   resources=('Image',), role='login', authentication=True),
        NativeRule('captcha_verify', CAPTCHA_HOSTS[0], r'/cap_union_new_(?:verify|getsig)',
                   methods=('POST', 'OPTIONS'), resources=('XHR', 'Fetch', 'Other', 'Preflight'),
                   role='login', authentication=True, cors_origin='https://' + CAPTCHA_HOSTS[0],
                   cors_headers=('content-type', 'x-requested-with')),
        NativeRule('captcha_static', CAPTCHA_HOSTS[1],
                   r'/(?:1|public/res)/[A-Za-z0-9_./-]+\.(?:js|css|png|jpg|jpeg|gif|webp|svg|woff2?|ttf)',
                   resources=('Script', 'Stylesheet', 'Image', 'Font'), role='asset', authentication=True),
    ))
    return NativeContract('liepin_bridge_human_login_v1',
                          (PASSPORT, SECURITY, ACCOUNT, *CAPTCHA_HOSTS, SECURITY_CONFIG), tuple(rules))


CONTRACT = _contract()


def applies(adapter):
    return (getattr(adapter, 'key', None) == 'liepin'
            and getattr(adapter, 'login_url', None) == 'https://www.liepin.com/')


def configure_transport(adapter, wire):
    if applies(adapter) and isinstance(getattr(wire, 'domains', None), set):
        wire.domains.update(EXTRA_HOSTS)


def match(url, method, kind, *, authentication, headers):
    """Return a supplemental rule or defer to the existing bridge policy.

    All requests to the new third-party hosts are checked, including GETs and
    subdomains. Merely adding a transport host never authorizes arbitrary paths.
    """
    host = urlsplit(url).hostname or ''
    protected = any(domain_matches(host, value) for value in EXTRA_HOSTS)
    resource = {'xhr': 'XHR', 'fetch': 'Fetch', 'document': 'Document',
                'script': 'Script', 'image': 'Image', 'stylesheet': 'Stylesheet',
                'font': 'Font', 'preflight': 'Preflight'}.get(kind, 'Other')
    if not protected and host not in (PASSPORT, ACCOUNT):
        return None
    parsed, path = CONTRACT.target(url)
    candidates = [rule for rule in CONTRACT.rules
                  if rule.host == parsed.hostname and re.fullmatch(rule.path, path)]
    # Two first-party page origins share the observed operations. A wrong
    # origin/method/resource cannot fall through to the older broad GET policy.
    origin = {k.lower(): v for k, v in headers.items()}.get('origin')
    for rule in candidates:
        if (method not in rule.methods or resource not in rule.resources
                or rule.authentication and not authentication
                or rule.cors_origin and rule.cors_origin != origin):
            continue
        rule.validate_headers(method, headers)
        return rule
    if protected or candidates or host == ACCOUNT and kind == 'document':
        raise CrawlError('native_operation_unreviewed')
    return None


def is_captcha(url):
    return urlsplit(url).hostname in CAPTCHA_HOSTS


def require_widget_frame(request):
    """Verification documents may be child frames of an owned Liepin page only."""
    frame = request.frame
    if frame.parent_frame is None:
        raise CrawlError('login_origin_changed')
    root = frame
    while root.parent_frame is not None:
        root = root.parent_frame
    p = urlsplit(root.url)
    if (p.scheme != 'https' or p.port not in (None, 443) or p.username or p.password
            or not (p.hostname == 'www.liepin.com'
                    or p.hostname == ACCOUNT and p.path == '/login/two-factor')):
        raise CrawlError('login_origin_changed')
