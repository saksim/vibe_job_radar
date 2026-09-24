"""Actual PAC import/check/rollback UI; native evaluator only on Windows."""
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from vibe_job_radar import pac_native
from vibe_job_radar.workbench import LocalServer
from vibe_job_radar.workspace import Workspace

SCRIPT=b'function FindProxyForURL(url,host){return "SOCKS5 127.0.0.1:1080; DIRECT";}'


def main():
    from playwright.sync_api import sync_playwright,expect
    out=ROOT/'browser-acceptance'/'windows-pac';out.mkdir(parents=True,exist_ok=True)
    result={'success':False,'checks':[],'external_requests':[],'page_errors':[],
        'native_evaluator':os.name=='nt',
        'scope':'Authored local PAC and actual workbench UI. No proxy, source request, personal PAC or OS setting changes. Non-Windows uses a controlled evaluator seam.'}
    real_evaluate=pac_native.evaluate
    def evaluate(source,url,permitted):
        if os.name=='nt':return real_evaluate(source,url,permitted)
        assert url=='https://pac-check.invalid/' and permitted()
        return 'UNKNOWN 127.0.0.1:1; DIRECT' if 'UNKNOWN' in source else 'SOCKS5 127.0.0.1:1080; DIRECT'
    clean={k:v for k,v in os.environ.items() if not k.startswith('VIBE_RADAR_')}
    with tempfile.TemporaryDirectory(prefix='radar-pac-ui-') as tmp,patch.dict(os.environ,clean,clear=True), \
            patch('urllib.request.getproxies',return_value={}),patch.object(pac_native,'available',return_value=True), \
            patch.object(pac_native,'evaluate',side_effect=evaluate) as calls, \
            patch('vibe_job_radar.network.PinnedHTTPSConnection',side_effect=AssertionError('no target requests')):
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
                    def route(request):
                        if request.request.url.startswith(server.origin+'/'):request.continue_()
                        else:result['external_requests'].append(request.request.url);request.abort()
                    context.route('**/*',route)
                    page=context.new_page();page.on('pageerror',lambda e:result['page_errors'].append(str(e)))
                    def expand():
                        expect(page.locator('#save-workspace-pac')).to_be_enabled()
                        page.locator('#network-preferences summary').click()
                    def file(body=SCRIPT,name='可信.pac'):
                        page.locator('#workspace-pac-file').set_input_files({'name':name,'mimeType':'application/x-ns-proxy-autoconfig','buffer':body})
                    def save():
                        page.locator('#workspace-pac-consent').check()
                        with page.expect_response(lambda r:r.url.endswith('/api/network/pac')) as response:
                            page.locator('#save-workspace-pac').click()
                        assert response.value.status==200
                        expect(page.locator('#save-workspace-pac')).to_be_enabled()
                    def check(passed):
                        with page.expect_response(lambda r:r.url.endswith('/api/network/pac/check')) as response:
                            page.locator('#check-workspace-pac').click()
                        value=response.value.json();assert value['passed'] is passed and value['target_requested'] is False
                        expect(page.locator('#check-workspace-pac')).to_be_enabled()
                        return value
                    page.goto(server.entry_url);expand();calls.assert_not_called()
                    file();page.locator('#save-workspace-pac').click()
                    expect(page.locator('#workspace-pac-status')).to_contain_text('勾选')
                    assert not (workspace.root/'network-preferences.json').exists()
                    page.locator('#workspace-pac-consent').check();file()
                    expect(page.locator('#workspace-pac-consent')).not_to_be_checked()
                    save();calls.assert_not_called()
                    old=workspace.network_policy()
                    expect(page.locator('#workspace-proxy-mode')).to_have_value('pac')
                    result['checks'].append('initial read/file selection/import never evaluates; explicit consent required and changing file clears it')
                    for path in ('/guided','/advanced','/'):
                        page.goto(server.origin+path);expand()
                        expect(page.locator('#workspace-proxy-mode')).to_have_value('pac')
                        expect(page.locator('#workspace-pac-status')).to_contain_text('可信.pac')
                    calls.assert_not_called()
                    page.locator('#encrypted-dns-consent').check()
                    with page.expect_response(lambda r:r.url.endswith('/api/network/preferences')):
                        page.locator('#save-network-preferences').click()
                    expect(page.locator('#save-workspace-pac')).to_be_enabled()
                    assert workspace.network_policy().pac_id==old.pac_id
                    assert check(True)['transport']=='loopback_socks5_proxy'
                    result['checks'].append('three pages reload same frozen file; DNS change preserves grant; explicit fixed-domain check returns SOCKS5 without contacting it')
                    page.set_viewport_size({'width':390,'height':844})
                    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                    page.locator('#network-preferences').screenshot(path=str(out/'preferences-mobile.png'))
                    file(b'function FindProxyForURL(){return "UNKNOWN 127.0.0.1:1; DIRECT";}','unknown.pac');save()
                    assert check(False)['code']=='pac_invalid_result'
                    page.locator('#workspace-proxy-mode').select_option('auto')
                    with page.expect_response(lambda r:r.url.endswith('/api/network/proxy')):
                        page.locator('#save-workspace-proxy').click()
                    expect(page.locator('#workspace-proxy-mode')).to_have_value('auto')
                    expect(page.locator('#check-workspace-pac')).to_be_disabled()
                    assert workspace.network_state()['schema_version']==2 and not old.pac.permitted()
                    assert calls.call_count==2 and not workspace.db.exists()
                    assert not result['external_requests'] and not result['page_errors']
                    result['checks'].append('unknown directive plus DIRECT fails; 390px has no horizontal overflow; rollback revokes old grant and preserves DNS without jobs/requests')
                    result.update(success=True,browser_version=browser.version)
                finally:browser.close()
        finally:
            server.shutdown();server.server_close();thread.join(5)
            (out/'results.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(result,ensure_ascii=False,indent=2))


if __name__=='__main__':main()
