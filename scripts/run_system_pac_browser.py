"""Actual UI and PAC HTTP worker, with a fake OS configuration (no OS writes)."""
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
from vibe_job_radar import system_pac, pac_native
from vibe_job_radar.workspace import Workspace
from vibe_job_radar.workbench import LocalServer


def main():
    from playwright.sync_api import sync_playwright,expect
    out=ROOT/'browser-acceptance'/'system-pac';out.mkdir(parents=True,exist_ok=True)
    result={'success':False,'checks':[],'external_requests':[],'page_errors':[],
        'native_evaluator':os.name=='nt',
        'scope':'Fake OS configuration, real local PAC download process and workbench UI. Windows executes native PAC; other OS substitutes only its evaluator. No personal settings or recruiting traffic.'}
    fixture=SourceServer()
    fixture.body=b'function FindProxyForURL(url,host){return "SOCKS5 127.0.0.1:1080; DIRECT";}'
    clean={k:v for k,v in os.environ.items() if not k.startswith('VIBE_RADAR_')}
    real_evaluate=pac_native.evaluate
    def evaluate(source,url,permitted):
        if os.name=='nt':return real_evaluate(source,url,permitted)
        assert permitted() and url=='https://pac-check.invalid/'
        return 'SOCKS5 127.0.0.1:1080; DIRECT'
    try:
        with tempfile.TemporaryDirectory(prefix='radar-system-pac-ui-') as tmp,patch.dict(os.environ,clean,clear=True), \
                patch('urllib.request.getproxies',return_value={}),patch.object(pac_native,'available',return_value=True), \
                patch.object(pac_native,'evaluate',side_effect=evaluate), \
                patch.object(system_pac,'current_source',return_value=fixture.source) as config, \
                patch('vibe_job_radar.network.PinnedHTTPSConnection',side_effect=AssertionError('no job target')):
            workspace=Workspace(tmp);server=LocalServer(workspace,public_client=None)
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
                            else:result['external_requests'].append('nonlocal browser request');route.abort()
                        context.route('**/*',local_only)
                        page=context.new_page();page.on('pageerror',lambda e:result['page_errors'].append(type(e).__name__))
                        def open_page(page,path='/'):
                            page.goto(server.entry_url if path=='/' else server.origin+path)
                            expect(page.locator('#save-system-pac')).to_be_enabled()
                            page.locator('#network-preferences summary').click()
                        def save(page,expected=200):
                            page.locator('#system-pac-consent').check()
                            with page.expect_response(lambda r:r.url.endswith('/api/network/system-pac')) as response:
                                page.locator('#save-system-pac').click()
                            assert response.value.status==expected
                            expect(page.locator('#system-pac-consent')).not_to_be_checked()
                        open_page(page)
                        assert not fixture.requests
                        expect(page.locator('#system-pac-origin')).not_to_contain_text('SYNTHETIC')
                        page.locator('#save-system-pac').click()
                        expect(page.locator('#system-pac-status')).to_contain_text('勾选')
                        assert not (workspace.root/'network-preferences.json').exists()
                        stale=context.new_page();open_page(stale)
                        save(page);old=workspace.network_policy();save(stale,400)
                        assert not fixture.requests
                        result['checks'].append('default/configuration preview/save are offline; source path and token hidden; consent and two-page revision conflict enforced')
                        for path in ('/guided','/advanced','/'):
                            open_page(page,path)
                            expect(page.locator('#workspace-proxy-mode')).to_have_value('system_pac')
                        assert not fixture.requests
                        page.locator('#system-pac-consent').check()
                        with page.expect_response(lambda r:r.url.endswith('/api/network/state')):
                            page.locator('#refresh-system-pac').click()
                        expect(page.locator('#system-pac-consent')).not_to_be_checked()
                        result['checks'].append('all three pages share the same grant; refreshing the read-only configuration clears stale consent')
                        with page.expect_response(lambda r:r.url.endswith('/api/network/pac/check')) as response:
                            page.locator('#check-system-pac').click()
                        check=response.value.json();assert check['passed'] and check['transport']=='loopback_socks5_proxy' and not check['target_requested']
                        assert len(fixture.requests)==1
                        result['checks'].append('explicit check spawns one real source downloader and preserves SOCKS5 through the fixed-domain PAC check without connecting to a proxy or job target')
                        config.return_value=system_pac.Source(fixture.url+'&version=2')
                        save(page,400);assert not old.pac.permitted()
                        with page.expect_response(lambda r:r.url.endswith('/api/network/state')):
                            page.locator('#refresh-system-pac').click()
                        expect(page.locator('#system-pac-status')).to_contain_text('已改变')
                        expect(page.locator('#check-system-pac')).to_be_disabled()
                        save(page);assert len(fixture.requests)==1
                        page.set_viewport_size({'width':390,'height':844})
                        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                        page.locator('#network-preferences').screenshot(path=str(out/'system-pac-mobile.png'))
                        page.locator('#workspace-proxy-mode').select_option('auto')
                        with page.expect_response(lambda r:r.url.endswith('/api/network/proxy')):
                            page.locator('#save-workspace-proxy').click()
                        expect(page.locator('#check-system-pac')).to_be_disabled()
                        assert workspace.network_state()['schema_version']==2 and len(fixture.requests)==1
                        assert not result['external_requests'] and not result['page_errors']
                        result['checks'].append('changed source requires renewed consent; 390px layout fits; explicit rollback produces v2 and no extra source request')
                        result.update(success=True,browser_version=browser.version)
                    finally:browser.close()
            finally:server.shutdown();server.server_close();thread.join(5)
    finally:
        fixture.close();(out/'results.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(result,ensure_ascii=False,indent=2))


if __name__=='__main__':main()
