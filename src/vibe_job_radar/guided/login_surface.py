"""Recognize an unfinished first-party login without reading form values.

These markers come from the public Liepin login UI and Tencent's published
widget. A readable list behind a login/verification dialog is not a return to
the user's task. This module never submits a form or answers a challenge.
"""
from urllib.parse import urlsplit


def liepin_manual_step(page, url):
    parsed = urlsplit(url)
    if (parsed.scheme != 'https' or parsed.port not in (None, 443)
            or parsed.username or parsed.password):
        return None
    if parsed.hostname == 'c.liepin.com' and parsed.path == '/login/two-factor':
        return 'login_sms_required'
    if parsed.hostname != 'www.liepin.com':
        return None
    # The widget can cover either the SMS or password form, so check it first.
    markers = (
        ('iframe[src^="https://turing.captcha.qcloud.com/template/"]:visible', 'login_captcha_required'),
        ('input[data-nick="login-smscode"]:visible', 'login_sms_required'),
        ('.ScanQrCode:visible', 'login_scan_required'),
        ('.lp-mini-scan-qrcode:visible', 'login_scan_required'),
    )
    for selector, code in markers:
        if page.locator(selector).count():
            return code
    if (not page.locator('input[data-nick="login-pwd"]:visible').count()
            and page.locator('#header-quick-menu-login:visible').count()):
        return 'manual_required'
    return None


MANUAL_LOGIN_CODES = frozenset({
    'manual_required', 'login_sms_required', 'login_captcha_required',
    'login_scan_required',
})
