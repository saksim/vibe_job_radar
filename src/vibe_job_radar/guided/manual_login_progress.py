"""Finite human-login feedback. No form values, response bodies or credentials."""
import re
import time
from urllib.parse import urlsplit


class ManualLoginProgress:
    def __init__(self, *, clock=time.monotonic):
        self.clock = clock
        self.active = False
        self.pending = {}
        self.stage = 'inactive'
        self.sends = self.submits = 0
        self.deadline = 0
        self.status = None
        self.sequence = 0
        self.last_login_response = None
        self.return_offered = False
        self.duplicate_cancellations = 0

    def start(self):
        self.active = True
        self.pending.clear()
        self.stage, self.sends, self.submits, self.status = 'waiting_user', 0, 0, None
        self.deadline = self.clock() + 600
        self.sequence = 0
        self.last_login_response = None
        self.return_offered = False
        self.duplicate_cancellations = 0

    def stop(self, stage='cancelled'):
        self.active = False
        self.pending.clear()
        self.last_login_response = None
        self.stage = stage if stage in {'cancelled', 'needs_attention', 'returned', 'timed_out'} else 'needs_attention'

    @staticmethod
    def operation(url, method):
        if method != 'POST':
            return None
        p = urlsplit(url)
        if (p.scheme != 'https' or p.hostname != 'api-passport.liepin.com'
                or p.port not in (None, 443) or p.username or p.password):
            return None
        return {
            '/api/com.liepin.passport.captcha.send-sms': 'sms',
            '/api/com.liepin.passport.account.send-two-factor-sms-code': 'sms',
            '/api/com.liepin.passport.account.tel-sms-login': 'login',
            '/api/com.liepin.passport.account.verify-two-factor-sms-code': 'login',
            '/api/com.liepin.passport.account.account-pwd-login': 'login',
        }.get(p.path)

    def requested(self, key, url, method):
        op = self.operation(url, method)
        if not self.active or self.clock() >= self.deadline or op is None or key in self.pending:
            return
        if len(self.pending) >= 32:
            self.stop('needs_attention')
            return
        self.sequence += 1
        self.last_login_response = None
        self.pending[key] = (op, self.sequence)
        self.stage, self.status = op + '_requested', None
        if op == 'sms':
            self.sends = min(999, self.sends + 1)
        else:
            self.submits = min(999, self.submits + 1)

    def received(self, key, status):
        pending = self.pending.pop(key, None)
        if not self.active or self.clock() >= self.deadline or pending is None:
            return
        op, sequence = pending
        if sequence != self.sequence:
            return  # A late SMS response cannot overwrite a newer login submit.
        self.status = status if type(status) is int and 100 <= status <= 599 else None
        self.stage = 'request_failed' if self.status is not None and self.status >= 400 else op + '_response_received'
        if op == 'login' and self.status == 200:
            self.last_login_response = sequence

    def inspect_surface(self, page):
        if not self.active or self.clock() >= self.deadline or not page or page.is_closed():
            return
        p = urlsplit(page.url)
        if p.scheme != 'https' or p.hostname not in {'www.liepin.com', 'c.liepin.com'}:
            return
        if page.locator('iframe[src^="https://turing.captcha.qcloud.com/template/"]:visible').count():
            self.stage = 'verification_required'
            self.last_login_response = None
            return
        # Existing publisher error wording only; never copy visible account
        # text, OTP fields or challenge tokens into state/diagnostics.
        body = page.locator('body')
        if body.count():
            text = body.inner_text(timeout=1000)
            if re.search(r'验证码(?:不正确|错误|已过期|已失效|无效|错误或过期|不正确或已过期)', text):
                self.stage = 'code_rejected'
                self.last_login_response = None
            elif (page.locator('#header-quick-menu-user-info:visible').count() == 1
                    and not page.locator('#header-quick-menu-login:visible').count()):
                self.stage = 'account_ui_visible'
            elif self.stage == 'verification_required':
                self.stage = 'result_unknown'

    def snapshot(self):
        if self.active and self.clock() >= self.deadline:
            self.stop('timed_out')
        return {'active': self.active, 'stage': self.stage, 'sms_requests': self.sends,
                'login_submissions': self.submits, 'http_status': self.status,
                'duplicate_cancellations': self.duplicate_cancellations}


def current(backend):
    """Use only an actual tracker; optional/custom backend mocks are not data."""
    value = backend.__dict__.get('_manual_login_progress')
    return value if isinstance(value, ManualLoginProgress) else None
