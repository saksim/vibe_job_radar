"""Real Chromium local consent UI; no upstream resolution or source request."""
from __future__ import annotations
import json
import os
import sys
import tempfile
import threading
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))

from playwright.sync_api import sync_playwright, expect
from vibe_job_radar.workbench import LocalServer
from vibe_job_radar.workspace import Workspace


def main():
    output=ROOT/'browser-acceptance';output.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory() as folder:
        workspace=Workspace(folder);server=LocalServer(workspace,public_client=None)
        thread=threading.Thread(target=server.serve_forever,kwargs={'poll_interval':.02},daemon=True);thread.start()
        try:
            with sync_playwright() as runtime:
                options={'headless':True}
                if os.environ.get('RADAR_TEST_CHROMIUM'):
                    options['executable_path']=os.environ['RADAR_TEST_CHROMIUM']
                browser=runtime.chromium.launch(**options)
                page=browser.new_page();external=[]
                def permitted(route):
                    if not route.request.url.startswith(server.origin+'/'):
                        external.append(route.request.url);route.abort()
                    else:route.continue_()
                page.route('**/*',permitted)
                entry=page.goto(server.entry_url)
                csp=entry.headers['content-security-policy']
                assert "script-src 'self'" in csp and 'unsafe-eval' not in csp
                page.locator('#network-preferences summary').click()
                expect(page.locator('#save-network-preferences')).to_be_enabled()
                assert not page.locator('#encrypted-dns-consent').is_checked()
                page.locator('#encrypted-dns-consent').check()
                with page.expect_response(lambda r:'/api/network/preferences' in r.url) as saved:
                    page.locator('#save-network-preferences').click()
                assert saved.value.status==200
                expect(page.locator('#network-dns-status')).to_contain_text('已保存')
                assert workspace.network_policy().encrypted_dns
                for path in ('/guided','/advanced'):
                    page.goto(server.origin+path)
                    page.locator('#network-preferences summary').click()
                    expect(page.locator('#encrypted-dns-consent')).to_be_checked()
                page.locator('#encrypted-dns-consent').uncheck()
                with page.expect_response(lambda r:'/api/network/preferences' in r.url) as revoked:
                    page.locator('#save-network-preferences').click()
                assert revoked.value.status==200
                assert not workspace.network_policy().encrypted_dns
                page.reload()
                page.locator('#network-preferences summary').click()
                expect(page.locator('#save-network-preferences')).to_be_enabled()
                assert not page.locator('#encrypted-dns-consent').is_checked()
                # A scope change never reuses the prior checkbox as consent.
                page.locator('#network-dns-scope').select_option('public_doh')
                expect(page.locator('#encrypted-dns-consent')).not_to_be_checked()
                assert not workspace.network_policy().public_dns
                with page.expect_response(lambda r:'/api/network/preferences' in r.url) as disabled:
                    page.locator('#save-network-preferences').click()
                assert disabled.value.json()['mode']=='system'
                for path in ('/','/guided','/advanced'):
                    page.goto(server.origin+path)
                    page.locator('#network-preferences summary').click()
                    expect(page.locator('#save-network-preferences')).to_be_enabled()
                    page.locator('#network-dns-scope').select_option('public_doh')
                    expect(page.locator('#encrypted-dns-consent')).not_to_be_checked()
                    expect(page.locator('#network-dns-disclosure')).to_contain_text('不先使用本机目标DNS')
                    page.locator('#encrypted-dns-consent').check()
                    with page.expect_response(lambda r:'/api/network/preferences' in r.url) as enabled:
                        page.locator('#save-network-preferences').click()
                    assert enabled.value.status==200 and enabled.value.json()['mode']=='public_doh'
                    expect(page.locator('#network-dns-status')).to_contain_text('已保存')
                    assert workspace.network_policy().public_dns
                    page.reload()
                    page.locator('#network-preferences summary').click()
                    expect(page.locator('#network-dns-scope')).to_have_value('public_doh')
                    expect(page.locator('#encrypted-dns-consent')).to_be_checked()
                    if path=='/advanced':
                        page.set_viewport_size({'width':390,'height':900})
                        page.locator('#network-preferences').screenshot(path=str(output/'public-dns-consent.png'))
                        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                    page.locator('#network-dns-scope').select_option('fake_ip_doh')
                    expect(page.locator('#encrypted-dns-consent')).not_to_be_checked()
                    assert workspace.network_policy().public_dns
                    with page.expect_response(lambda r:'/api/network/preferences' in r.url) as revoked_public:
                        page.locator('#save-network-preferences').click()
                    assert revoked_public.value.json()['mode']=='system'
                    assert not workspace.network_policy().encrypted_dns
                assert not external
                page.screenshot(path=str(output/'network-consent.png'),full_page=True)
                browser.close()
            value={'success':True,'real_chromium':True,'external_requests':0,
                   'checks':['default_off','explicit_consent','cross_page_persistence','revoke','reload','strict_csp_without_unsafe_eval','public_scope_requires_new_consent','public_mode_all_three_pages','public_mode_reloaded_and_revoked','mobile_no_horizontal_overflow'],
                   'scope':'Local UI only; upstream resolver and VPN/TUN are separate tests.'}
            (output/'network-consent.json').write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf-8')
        finally:
            server.shutdown();server.server_close();thread.join(timeout=5)
    return 0

if __name__=='__main__':raise SystemExit(main())
