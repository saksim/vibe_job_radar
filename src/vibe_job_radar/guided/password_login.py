"""One explicit normal form submission in the application's Liepin browser.

The queue owns these short-lived values. They never enter task state, reports,
diagnostics or session files. Website JavaScript performs its own login request;
this module neither constructs a login API request nor retries a submission.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import re
from urllib.parse import urljoin, urlsplit

from ..workspace import InputError
from .contracts import CrawlError


@dataclass
class LoginCredentials:
    username: str = field(repr=False)
    password: str = field(repr=False)

    @classmethod
    def from_input(cls, data):
        if data.get('credential_consent') is not True:
            raise InputError('请明确授权本次在猎聘页面填写并提交一次。')
        username, password = data.get('username'), data.get('password')
        if (not isinstance(username, str) or not username.strip() or len(username) > 200
                or not isinstance(password, str) or not password or len(password) > 256
                or any(ord(c) < 32 for c in username + password)):
            raise InputError('请输入有效的猎聘账号和密码；账号最多200字，密码最多256字。')
        return cls(username.strip(), password)

    def clear(self):
        # Drop our references; Python does not guarantee wiping string memory.
        self.username = self.password = ''


def submit_password_login(backend, credentials):
    """Fill the observed site's fields; leave agreements/2FA to its own UI."""
    page = backend.page

    def check():
        if backend.cancelled.is_set():
            raise CrawlError('paused')
        if backend.error:
            raise getattr(backend, 'wait_error', None) or CrawlError(backend.error)
        if backend.adapter.key != 'liepin' or not backend.auth_mode or page is not backend.page:
            raise CrawlError('login_origin_changed')
        url = backend.adapter.accept_url(page.url)
        p = urlsplit(url)
        if p.hostname != 'www.liepin.com':
            raise CrawlError('login_origin_changed')
        text = page.locator('body').inner_text(timeout=5000)
        if (re.search(r'请完成.{0,12}验证|滑动.{0,8}验证|安全验证|访问异常|访问过于频繁|verify you are human|access denied', text, re.I)
                or re.search(r'captcha|intercept|challenge', url, re.I)):
            raise CrawlError('manual_required')

    try:
        check()
        password = page.locator('input[data-nick="login-pwd"]:visible')
        if not password.count():
            trigger = page.locator('#header-quick-menu-login:visible')
            if trigger.count():
                trigger.click(timeout=10000)
            check()
            page.get_by_text('密码登录', exact=True).click(timeout=10000)
        password.wait_for(state='visible', timeout=10000)
        check()
        form = page.locator('form').filter(has=password)
        if form.count() != 1 or password.count() != 1:
            raise CrawlError('login_form_changed')
        action = form.get_attribute('action')
        if action and urlsplit(urljoin(page.url, action)).netloc != 'www.liepin.com':
            raise CrawlError('login_origin_changed')
        username = form.locator('input[data-nick="login-user"]:visible')
        submit = form.locator('button.login-submit-btn:visible')
        if username.count() != 1 or submit.count() != 1:
            raise CrawlError('login_form_changed')
        # The observed agreement is a sibling of the password form. Only read
        # its state; a local credential consent does not accept site terms.
        agreement = page.locator('label:visible:has-text("同意猎聘")').locator('input[type="checkbox"]')
        if agreement.count() != 1:
            raise CrawlError('login_form_changed')
        username.fill(credentials.username, timeout=5000)
        check()
        password.fill(credentials.password, timeout=5000)
        check()
        if agreement.evaluate('e => e.checked === true') is not True:
            # Checking the site's agreement does not resubmit its form. Leave
            # the filled page with an explicit two-step handoff, not a false
            # submitted result or a background password retry.
            return 'login_agreement_required'
        submit.click(timeout=10000)
        # The return watcher observes actual readable task content. A click is
        # not evidence of successful authentication or a reason to click again.
        if backend.error:
            raise getattr(backend, 'wait_error', None) or CrawlError(backend.error)
        return 'login_password_submitted'
    except CrawlError:
        raise
    except Exception:
        # Playwright exceptions may include form values; keep only a fixed code.
        raise CrawlError('login_form_changed') from None
    finally:
        credentials.clear()
