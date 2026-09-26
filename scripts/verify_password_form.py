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
        from vibe_job_radar.guided.cdp_browser import CDPBrowser, edge_executable, chrome_executable
        # Reserve an unlistened loopback port for the lifetime of the browser,
        # so another local service cannot turn this fixture into an egress proxy.
        denied_proxy = socket.socket()
        denied_proxy.bind(('127.0.0.1', 0))
        browser = None
        try:
            browser = CDPBrowser(executable_path=(edge_executable() if args.channel == 'msedge' else chrome_executable() if args.channel == 'chrome' else runtime.chromium.executable_path),
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



def setup_entry(page, scenario):
    """An SMS panel with the same entry/tab/field roles as the public bundle."""
    page.evaluate("""scenario => {
        const saved=document.querySelector('form').outerHTML+document.querySelector('label').outerHTML;
        document.body.innerHTML='';
        window.headerClicks=0;window.tabClicks=0;window.clicks=0;window.submissions=0;
        const header=document.createElement('span');header.id='header-quick-menu-login';
        header.textContent='登录/注册';header.style='position:fixed;top:8px;left:8px';
        document.body.append(header);
        const panel=document.createElement('div');panel.id='login-panel';
        panel.style=scenario==='sms_inline'?'margin-top:45px':'position:fixed;inset:0;background:white;z-index:10';
        panel.innerHTML='<div id="password-tab" role="tab">密码登录</div><form id="sms-form"><input placeholder="手机号"><input placeholder="短信验证码"></form><div id="password-panel" hidden>'+saved+'</div>';
        document.body.append(panel);document.querySelector('#terms').checked=false;
        const tab=document.querySelector('#password-tab');
        const closed=['closed_login','hidden_switch','delayed_switch'].includes(scenario);
        panel.hidden=closed;
        header.onclick=()=>{
            window.headerClicks++;panel.hidden=false;
            if(scenario==='delayed_switch'){
                tab.hidden=true;requestAnimationFrame(()=>requestAnimationFrame(()=>tab.hidden=false));
            }
        };
        tab.onclick=()=>{
            window.tabClicks++;
            if(scenario==='unchanged_sms')return;
            document.querySelector('#sms-form').hidden=true;
            document.querySelector('#password-panel').hidden=false;
        };
        if(scenario==='hidden_switch'||scenario==='duplicate_switch'){
            const duplicate=tab.cloneNode(true);duplicate.id='other-password-tab';
            duplicate.hidden=scenario==='hidden_switch';document.body.append(duplicate);
        }
        const form=document.querySelector('#password-panel form');
        form.onsubmit=e=>{e.preventDefault();window.submissions++;};
        form.querySelector('button').onclick=()=>window.clicks++;
        return true;
    }""", scenario)


def verify_entry_paths(page, backend):
    checks=[]
    for scenario in ('sms_overlay', 'sms_inline', 'closed_login', 'hidden_switch',
                     'delayed_switch', 'duplicate_switch', 'unchanged_sms'):
        page.goto(backend.adapter.search_url('synthetic login entry fixture'))
        setup_entry(page, scenario)
        credentials=LoginCredentials('synthetic-user', 'synthetic-password')
        expected={'duplicate_switch':'login_password_tab_unavailable',
                  'unchanged_sms':'login_password_form_unavailable'}.get(scenario)
        try:
            code=submit_password_login(backend,credentials)
            assert expected is None and code=='login_agreement_required', (scenario,code)
        except CrawlError as exc:
            assert exc.code==expected, (scenario,exc.code)
        assert not credentials.username and not credentials.password, scenario
        assert page.evaluate('window.headerClicks')==int(scenario in {'closed_login','hidden_switch','delayed_switch'}), scenario
        assert page.evaluate('window.tabClicks')==int(scenario!='duplicate_switch'), scenario
        assert page.evaluate('window.clicks')==0 and page.evaluate('window.submissions')==0, scenario
        assert page.locator('#terms').evaluate('e=>e.checked') is False, scenario
        # Read booleans for these synthetic fields; no real account is involved.
        values=page.evaluate("Array.from(document.querySelectorAll('#password-panel form input')).map(e=>e.value)")
        assert values==(['',''] if expected else ['synthetic-user','synthetic-password']), scenario
        assert page.evaluate("Array.from(document.querySelectorAll('#sms-form input')).every(e=>e.value==='')") is True, scenario
        checks.append(scenario)
    return checks


def main():
    parser = argparse.ArgumentParser(); parser.add_argument('--channel', choices=['msedge', 'chrome'])
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
            checks.extend(verify_entry_paths(page, backend))
            print(json.dumps({'success': True, 'checks': checks, 'external_requests': 0,
                              'real_account_tested': False, 'input_backend': 'native_cdp' if args.native else 'playwright',
                              'browser_version': browser.version}))


if __name__ == '__main__': main()
