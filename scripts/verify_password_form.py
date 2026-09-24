"""Controlled real-browser login form check. Every URL is fulfilled locally.

Usage: python scripts/verify_password_form.py --channel msedge
No provider request, real credentials or account certification is involved.
"""
import argparse
import json
from pathlib import Path
import sys
import threading
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from vibe_job_radar.guided.adapters import builtins
from vibe_job_radar.guided.browser import PlaywrightBackend
from vibe_job_radar.guided.contracts import CrawlError
from vibe_job_radar.guided.password_login import LoginCredentials, submit_password_login

FORM = '''<html><head><meta charset="utf-8"></head><body><div hidden>登录后查看</div>
<form><input data-nick="login-user"><input data-nick="login-pwd" type="password">
<button class="login-submit-btn" type="submit">登录</button></form>
<script>window.submissions=0; document.querySelector('form').onsubmit=e=>{
e.preventDefault();window.submissions++;window.filled=[...document.querySelectorAll('input')].map(i=>i.value);
document.querySelector('button').textContent='账号或密码错误';};</script></body></html>'''


def main():
    parser = argparse.ArgumentParser(); parser.add_argument('--channel')
    args = parser.parse_args()
    from playwright.sync_api import sync_playwright
    checks = []
    with sync_playwright() as runtime:
        browser = runtime.chromium.launch(headless=True, **({'channel': args.channel} if args.channel else {}))
        try:
            context = browser.new_context(service_workers='block')
            context.route('**/*', lambda route: route.fulfill(status=200, content_type='text/html', body=FORM))
            page = context.new_page()
            backend = SimpleNamespace(page=page, adapter=builtins().get('liepin'), auth_mode=True,
                                      cancelled=threading.Event(), error=None)
            url = backend.adapter.search_url('synthetic login fixture')
            for scenario in ('submit_once', 'captcha', 'foreign_action', 'cancelled', 'changed_form'):
                page.goto(url)
                credentials = LoginCredentials('synthetic-user', 'synthetic-password')
                expected = None
                if scenario == 'captcha':
                    page.locator('body').evaluate("e=>e.insertAdjacentHTML('afterbegin','<h1>安全验证</h1>')")
                    expected = 'manual_required'
                elif scenario == 'foreign_action':
                    page.locator('form').evaluate("e=>e.action='https://outside.example.test/login'")
                    expected = 'login_origin_changed'
                elif scenario == 'cancelled':
                    backend.cancelled.set(); expected = 'paused'
                elif scenario == 'changed_form':
                    page.locator('button').evaluate("e=>e.className='unrelated'")
                    expected = 'login_form_changed'
                try:
                    submit_password_login(backend, credentials)
                    assert expected is None, scenario
                except CrawlError as exc:
                    assert exc.code == expected, (scenario, exc.code)
                finally:
                    backend.cancelled.clear()
                assert not credentials.username and not credentials.password, scenario
                assert page.evaluate('window.submissions') == (1 if expected is None else 0), scenario
                if expected is None:
                    assert page.evaluate('window.filled') == ['synthetic-user', 'synthetic-password']
                    try:
                        PlaywrightBackend.snapshot(backend)
                    except CrawlError as exc:
                        assert exc.code == 'login_credentials_rejected', exc.code
                    else:
                        raise AssertionError('bad password must not resume the list behind its dialog')
                else:
                    assert page.locator('input[data-nick="login-pwd"]').input_value() == '', scenario
                checks.append(scenario)
            print(json.dumps({'success': True, 'checks': checks, 'external_requests': 0,
                              'real_account_tested': False, 'browser_version': browser.version}))
        finally:
            browser.close()


if __name__ == '__main__': main()
