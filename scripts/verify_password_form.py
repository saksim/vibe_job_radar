"""Controlled real-browser login form check. Every URL is fulfilled locally.

Usage: python scripts/verify_password_form.py --channel msedge
No provider request, real credentials or account certification is involved.
"""
import argparse
import base64
from contextlib import contextmanager
import json
from pathlib import Path
import sys
import socket
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
<label><input id="terms" type="checkbox" checked>同意猎聘</label>
<script>window.submissions=0;window.clicks=0;document.querySelector('button').onclick=()=>window.clicks++;
document.querySelector('form').onsubmit=e=>{
e.preventDefault();if(!document.querySelector('#terms').checked)return;
window.submissions++;window.filled=[...document.querySelectorAll('form input')].map(i=>i.value);
document.querySelector('button').textContent='账号或密码错误';};</script></body></html>'''


@contextmanager
def fixture_browser(runtime, args):
    if args.native:
        from vibe_job_radar.guided.cdp_browser import CDPBrowser, edge_executable
        # Reserve an unlistened loopback port for the lifetime of the browser,
        # so another local service cannot turn this fixture into an egress proxy.
        denied_proxy = socket.socket()
        denied_proxy.bind(('127.0.0.1', 0))
        browser = None
        try:
            browser = CDPBrowser(executable_path=edge_executable() if args.channel == 'msedge' else runtime.chromium.executable_path,
                                 headless=True, args=[], proxy={'server': 'http://127.0.0.1:' + str(denied_proxy.getsockname()[1])})
            page = browser.new_context().new_page()
            # All document requests are fulfilled inside the owned CDP page.
            # The closed loopback proxy also prevents browser background egress.
            def fulfill(event):
                page.client.send('Fetch.fulfillRequest', {'requestId': event['requestId'],
                    'responseCode': 200, 'responseHeaders': [{'name': 'Content-Type', 'value': 'text/html; charset=utf-8'}],
                    'body': base64.b64encode(FORM.encode()).decode()})
            page.client.on('Fetch.requestPaused', fulfill)
            page.client.send('Fetch.enable', {'patterns': [{'urlPattern': '*', 'requestStage': 'Request'}]})
            yield browser, page
        finally:
            try:
                if browser is not None:
                    browser.close()
            finally:
                denied_proxy.close()
    else:
        browser = runtime.chromium.launch(headless=True, **({'channel': args.channel} if args.channel else {}))
        try:
            context = browser.new_context(service_workers='block')
            context.route('**/*', lambda route: route.fulfill(status=200, content_type='text/html', body=FORM))
            yield browser, context.new_page()
        finally:
            browser.close()


def main():
    parser = argparse.ArgumentParser(); parser.add_argument('--channel')
    parser.add_argument('--native', action='store_true', help='Exercise the production CDP page/input implementation')
    args = parser.parse_args()
    from playwright.sync_api import sync_playwright
    checks = []
    with sync_playwright() as runtime:
        with fixture_browser(runtime, args) as (browser, page):
            backend = SimpleNamespace(page=page, adapter=builtins().get('liepin'), auth_mode=True,
                                      cancelled=threading.Event(), error=None)
            url = backend.adapter.search_url('synthetic login fixture')
            for scenario in ('submit_once', 'unchecked_agreement', 'revoked_agreement', 'hidden_duplicate',
                             'missing_agreement', 'duplicate_agreement', 'captcha',
                             'foreign_action', 'cancelled', 'changed_form'):
                page.goto(url)
                credentials = LoginCredentials('synthetic-user', 'synthetic-password')
                expected = None
                manual = scenario in {'unchecked_agreement', 'revoked_agreement'}
                if scenario == 'unchecked_agreement':
                    page.locator('#terms').click()
                elif scenario == 'revoked_agreement':
                    page.locator('input[data-nick="login-pwd"]').evaluate(
                        'e => e.oninput=()=>document.querySelector("#terms").checked=false')
                elif scenario == 'missing_agreement':
                    page.locator('label').evaluate('e => {e.remove();return true;}')
                    expected = 'login_form_changed'
                elif scenario in {'duplicate_agreement', 'hidden_duplicate'}:
                    page.locator('label').evaluate('e => {e.after(e.cloneNode(true));return true;}')
                    if scenario == 'hidden_duplicate':
                        page.locator('label').nth(1).evaluate('e => e.hidden=true')
                    else:
                        expected = 'login_form_changed'
                elif scenario == 'captcha':
                    page.locator('body').evaluate("e=>{e.insertAdjacentHTML('afterbegin','<h1>安全验证</h1>');return true;}")
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
                    result = submit_password_login(backend, credentials)
                    assert expected is None, scenario
                    assert result == ('login_agreement_required' if manual else 'login_password_submitted'), scenario
                except CrawlError as exc:
                    assert exc.code == expected, (scenario, exc.code)
                finally:
                    backend.cancelled.clear()
                assert not credentials.username and not credentials.password, scenario
                automatic = expected is None and not manual
                assert page.evaluate('window.submissions') == int(automatic), scenario
                assert page.evaluate('window.clicks') == int(automatic), scenario
                if manual:
                    assert page.locator('#terms').evaluate('e => e.checked') is False, scenario
                    # Simulated human actions on this local fixture only. A tick
                    # alone must not submit; the later human click submits once.
                    page.locator('#terms').click()
                    assert page.evaluate('window.submissions') == 0, scenario
                    assert page.evaluate('window.clicks') == 0, scenario
                    page.locator('button').click()
                    assert page.evaluate('window.submissions') == 1, scenario
                    assert page.evaluate('window.clicks') == 1, scenario
                if expected is None:
                    assert page.evaluate('window.filled') == ['synthetic-user', 'synthetic-password']
                    try:
                        PlaywrightBackend.snapshot(backend)
                    except CrawlError as exc:
                        assert exc.code == 'login_credentials_rejected', exc.code
                    else:
                        raise AssertionError('bad password must not resume the list behind its dialog')
                else:
                    assert page.locator('input[data-nick="login-pwd"]').evaluate('e => e.value === ""') is True, scenario
                checks.append(scenario)
            print(json.dumps({'success': True, 'checks': checks, 'external_requests': 0,
                              'real_account_tested': False, 'input_backend': 'native_cdp' if args.native else 'playwright',
                              'browser_version': browser.version}))


if __name__ == '__main__': main()
