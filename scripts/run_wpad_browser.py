"""Actual UI and owned PAC downloader; OS discovery is a synthetic fixture."""
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'src'),str(ROOT/'tests')]
from test_system_pac import SourceServer
from vibe_job_radar import system_pac,pac_native,wpad_native
from vibe_job_radar.loopback_proxy import LocalProxyError
from vibe_job_radar.workspace import Workspace
from vibe_job_radar.workbench import LocalServer


def main():
    from playwright.sync_api import sync_playwright,expect
    out=ROOT/'browser-acceptance'/'wpad';out.mkdir(parents=True,exist_ok=True)
    result={'success':False,'checks':[],'page_errors':[],'external_requests':[],
        'native_evaluator':os.name=='nt','real_wpad_discovery':False,
        'scope':'Synthetic OS discovery. Actual UI, owned PAC download and Windows native evaluation. No LAN WPAD, personal settings or job requests.'}
    fixture=SourceServer()
    fixture.body=b'function FindProxyForURL(url,host){return "SOCKS5 127.0.0.1:1080; DIRECT";}'
    clean={k:v for k,v in os.environ.items() if not k.startswith('VIBE_RADAR_')}
    real_evaluate=pac_native.evaluate
    def evaluate(source,url,permitted):
        if os.name=='nt':return real_evaluate(source,url,permitted)
        assert permitted() and url=='https://pac-check.invalid/'
        return 'SOCKS5 127.0.0.1:1080; DIRECT'
    try:
        with tempfile.TemporaryDirectory(prefix='fixture-',dir=out) as temp, \
                patch.dict(os.environ,clean,clear=True),patch('urllib.request.getproxies',return_value={}), \
                patch.object(pac_native,'available',return_value=True),patch.object(pac_native,'evaluate',side_effect=evaluate), \
                patch.object(wpad_native,'available',return_value=True), \
                patch.object(wpad_native,'discover',return_value=fixture.source) as discovery, \
                patch('vibe_job_radar.network.PinnedHTTPSConnection',side_effect=AssertionError('no job target')):
            workspace=Workspace(temp);server=LocalServer(workspace,public_client=None)
            thread=threading.Thread(target=server.serve_forever,kwargs={'poll_interval':.01},daemon=True);thread.start()
            try:
                with sync_playwright() as pw:
                    options={'headless':True}
                    if '--edge' in sys.argv:options['channel']='msedge'
                    if os.environ.get('RADAR_TEST_CHROMIUM'):options['executable_path']=os.environ['RADAR_TEST_CHROMIUM']
                    browser=pw.chromium.launch(**options)
                    try:
                        context=browser.new_context(viewport={'width':1280,'height':1000})
                        def local_only(route):
                            if route.request.url.startswith(server.origin+'/'):route.continue_()
                            else:result['external_requests'].append('nonlocal');route.abort()
                        context.route('**/*',local_only)
                        page=context.new_page()
                        page.on('pageerror',lambda error:result['page_errors'].append(type(error).__name__))
                        def open_page(target,path='/'):
                            target.goto(server.entry_url if path=='/' else server.origin+path)
                            expect(target.locator('#discover-wpad')).to_be_enabled()
                            target.locator('#network-preferences summary').click()
                        def preview():
                            page.locator('#wpad-discovery-consent').check()
                            with page.expect_response(lambda r:r.url.endswith('/api/network/wpad/discover')) as response:
                                page.locator('#discover-wpad').click()
                            assert response.value.status==200
                            expect(page.locator('#save-wpad')).to_be_enabled()
                            expect(page.locator('#wpad-discovery-consent')).not_to_be_checked()
                            expect(page.locator('#wpad-trust-consent')).not_to_be_checked()
                        def confirm(target,expected=200):
                            target.locator('#wpad-trust-consent').check()
                            with target.expect_response(lambda r:r.url.endswith('/api/network/wpad')) as response:
                                target.locator('#save-wpad').click()
                            assert response.value.status==expected
                            expect(target.locator('#wpad-trust-consent')).not_to_be_checked()
                        open_page(page);discovery.assert_not_called()
                        page.locator('#discover-wpad').click()
                        expect(page.locator('#wpad-status')).to_contain_text('勾选')
                        discovery.assert_not_called()
                        preview()
                        expect(page.locator('#wpad-origin')).not_to_contain_text('SYNTHETIC')
                        assert not fixture.requests
                        page.locator('#save-wpad').click()
                        expect(page.locator('#wpad-status')).to_contain_text('单独勾选')
                        stale=context.new_page();open_page(stale)
                        confirm(page);confirm(stale,400)
                        assert not fixture.requests and discovery.call_count==1
                        result['checks'].append('separate discovery/trust consent, no default probe or download during confirmation, sanitized source, stale-tab rejection')
                        old=workspace.network_policy()
                        for path in ('/guided','/advanced','/'):
                            open_page(page,path)
                            expect(page.locator('#workspace-proxy-mode')).to_have_value('wpad')
                        assert discovery.call_count==1 and not fixture.requests
                        with page.expect_response(lambda r:r.url.endswith('/api/network/pac/check')) as response:
                            page.locator('#check-wpad').click()
                        check=response.value.json()
                        assert check['passed'] and check['transport']=='loopback_socks5_proxy' and not check['target_requested']
                        assert discovery.call_count==2 and len(fixture.requests)==1
                        result['checks'].append('three read-only pages; explicit check rediscovers once and uses real download child and Windows native SOCKS5 evaluation')
                        discovery.return_value=system_pac.Source(fixture.url+'&version=2')
                        with page.expect_response(lambda r:r.url.endswith('/api/network/pac/check')) as response:
                            page.locator('#check-wpad').click()
                        check=response.value.json()
                        assert not check['passed'] and check['code']=='wpad_source_changed' and len(fixture.requests)==1
                        preview();confirm(page)
                        assert not old.pac.permitted() and len(fixture.requests)==1
                        result['checks'].append('changed full source URL blocks download and requires renewed preview/trust')
                        entered=threading.Event()
                        def pending(permitted):
                            entered.set()
                            while permitted():threading.Event().wait(.005)
                            raise LocalProxyError('pac_revoked')
                        discovery.side_effect=pending
                        page.locator('#wpad-discovery-consent').check()
                        page.locator('#discover-wpad').click()
                        expect(page.locator('#cancel-wpad')).to_be_enabled()
                        assert entered.is_set()
                        with page.expect_response(lambda r:r.url.endswith('/api/network/wpad/cancel')):
                            page.locator('#cancel-wpad').click()
                        expect(page.locator('#wpad-status')).to_contain_text('已取消')
                        expect(page.locator('#discover-wpad')).to_be_enabled()
                        expect(page.locator('#save-wpad')).to_be_disabled()
                        assert len(fixture.requests)==1
                        result['checks'].append('UI cancels asynchronous discovery; owned permission closes without download')
                        page.set_viewport_size({'width':390,'height':844})
                        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                        page.locator('#discover-wpad').locator('..').screenshot(path=str(out/'wpad-mobile.png'))
                        page.locator('#workspace-proxy-mode').select_option('auto')
                        with page.expect_response(lambda r:r.url.endswith('/api/network/proxy')):
                            page.locator('#save-workspace-proxy').click()
                        expect(page.locator('#check-wpad')).to_be_disabled()
                        assert workspace.network_state()['schema_version']==2
                        assert not result['page_errors'] and not result['external_requests'] and len(fixture.requests)==1
                        result['checks'].append('390px layout fits; explicit rollback writes strict v2 with no extra request')
                        result.update(success=True,browser_version=browser.version,
                            source_downloads=len(fixture.requests),synthetic_discoveries=discovery.call_count)
                    finally:browser.close()
            finally:server.shutdown();server.server_close();thread.join(5)
    finally:
        fixture.close()
        (out/'results.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(result,ensure_ascii=False,indent=2))


if __name__=='__main__':main()
