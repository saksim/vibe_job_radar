"""Actual local UI persistence/conflict/isolation; never contact proxy or source."""
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from vibe_job_radar.workbench import LocalServer
from vibe_job_radar.workspace import Workspace


def main():
    from playwright.sync_api import sync_playwright, expect
    vm='--vm' in sys.argv
    http_mode,socks_mode=('vm_http','vm_socks5') if vm else ('http','socks5')
    http_input='http://192.168.56.1:18990/' if vm else 'http://localhost:18990/'
    http_saved='http://192.168.56.1:18990' if vm else 'http://127.0.0.1:18990'
    socks_saved='socks5://10.0.2.2:18991' if vm else 'socks5://[::1]:18991'
    out=ROOT/'browser-acceptance'/('vm-proxy-settings' if vm else 'workspace-proxy');out.mkdir(parents=True,exist_ok=True)
    result={'success':False,'checks':[],'external_requests':[],'page_errors':[],
        'scope':'Actual local workbench UI and preferences; no proxy probes, DNS queries, accounts or recruiting requests.'}
    private=('VIBE_RADAR_HTTP_PROXY','VIBE_RADAR_SOCKS_PROXY','VIBE_RADAR_PROXY_USERNAME','VIBE_RADAR_PROXY_PASSWORD')
    clean={k:v for k,v in os.environ.items() if k not in private}
    with tempfile.TemporaryDirectory(prefix='radar-workspace-proxy-') as tmp,patch.dict(os.environ,clean,clear=True), \
            patch('urllib.request.getproxies',return_value={}), \
            patch('vibe_job_radar.network.PinnedHTTPSConnection',side_effect=AssertionError('no upstream request')):
        workspaces=[Workspace(Path(tmp)/name) for name in ('first','second')]
        servers=[LocalServer(w,public_client=None) for w in workspaces];threads=[]
        for server in servers:
            t=threading.Thread(target=server.serve_forever,kwargs={'poll_interval':.01},daemon=True);t.start();threads.append(t)
        try:
            with sync_playwright() as pw:
                options={'headless':True}
                if os.environ.get('RADAR_TEST_CHROMIUM'):options['executable_path']=os.environ['RADAR_TEST_CHROMIUM']
                browser=pw.chromium.launch(**options)
                try:
                    context=browser.new_context(viewport={'width':1280,'height':1000})
                    def allowed(route):
                        if any(route.request.url.startswith(s.origin+'/') for s in servers):route.continue_()
                        else:result['external_requests'].append(route.request.url);route.abort()
                    context.route('**/*',allowed)
                    page=context.new_page();page.on('pageerror',lambda e:result['page_errors'].append(str(e)))
                    server,workspace=servers[0],workspaces[0]
                    def expand(p):
                        expect(p.locator('#save-workspace-proxy')).to_be_enabled()
                        p.locator('#network-preferences summary').click()
                    def save(p,status=200):
                        with p.expect_response(lambda r:r.url.endswith('/api/network/proxy')) as response:
                            p.locator('#save-workspace-proxy').click()
                        assert response.value.status==status
                        expect(p.locator('#save-workspace-proxy')).to_be_enabled()
                    entry=page.goto(server.entry_url);expand(page)
                    assert "script-src 'self'" in entry.headers['content-security-policy']
                    assert 'unsafe-eval' not in entry.headers['content-security-policy']
                    expect(page.locator('#workspace-proxy-mode')).to_have_value('auto')
                    assert not (workspace.root/'network-preferences.json').exists()
                    page.locator('#workspace-proxy-mode').select_option(http_mode)
                    if vm:
                        expect(page.locator('#workspace-proxy-consent-label')).to_contain_text('宿主机授权给此来宾')
                        page.locator('#workspace-proxy-endpoint').fill('http://127.0.0.1:18990')
                        page.locator('#workspace-proxy-consent').check();save(page,400)
                        assert not (workspace.root/'network-preferences.json').exists()
                    page.locator('#workspace-proxy-endpoint').fill(http_input)
                    save(page,400)
                    assert not (workspace.root/'network-preferences.json').exists()
                    page.locator('#workspace-proxy-consent').check();save(page)
                    expect(page.locator('#workspace-proxy-endpoint')).to_have_value(http_saved)
                    page.locator('#encrypted-dns-consent').check()
                    with page.expect_response(lambda r:r.url.endswith('/api/network/preferences')) as response:
                        page.locator('#save-network-preferences').click()
                    assert response.value.status==200
                    assert workspace.network_policy().encrypted_dns and workspace.network_policy().proxy.port==18990
                    result['checks'].append('explicit consent required; canonical fixed HTTP persists, DNS save preserves proxy and shared revision')
                    stale=context.new_page();stale.goto(server.entry_url);expand(stale)
                    for path in ('/guided','/advanced'):
                        page.goto(server.origin+path);expand(page)
                        expect(page.locator('#workspace-proxy-endpoint')).to_have_value(http_saved)
                        expect(page.locator('#encrypted-dns-consent')).to_be_checked()
                    page.locator('#workspace-proxy-mode').select_option(socks_mode)
                    page.locator('#workspace-proxy-endpoint').fill(socks_saved)
                    page.locator('#workspace-proxy-consent').check();save(page)
                    stale.locator('#workspace-proxy-consent').check();save(stale,400)
                    assert workspace.network_state()['proxy_mode']==socks_mode
                    assert workspace.network_state()['revision']==3
                    result['checks'].append('three workbench pages share saved route; stale page cannot overwrite newer SOCKS setting')
                    page.reload();expand(page)
                    expect(page.locator('#workspace-proxy-endpoint')).to_have_value(socks_saved)
                    page.set_viewport_size({'width':390,'height':844})
                    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                    page.screenshot(path=str(out/'mobile.png'),full_page=True)
                    page.locator('#network-preferences').screenshot(path=str(out/'preferences.png'))
                    page.goto(servers[1].entry_url);expand(page)
                    expect(page.locator('#workspace-proxy-mode')).to_have_value('auto')
                    expect(page.locator('#encrypted-dns-consent')).not_to_be_checked()
                    assert not (workspaces[1].root/'network-preferences.json').exists()
                    result['checks'].append('reload and mobile layout pass; another workspace retains its own automatic/default DNS choices')
                    page.goto(server.entry_url);expand(page)
                    page.locator('#workspace-proxy-mode').select_option('auto');save(page)
                    assert workspace.network_policy().proxy is None and workspace.network_policy().encrypted_dns
                    assert workspace.network_state()['schema_version']==2
                    assert not result['external_requests'] and not result['page_errors']
                    assert all(not w.db.exists() for w in workspaces)
                    result['checks'].append('return to auto preserves DNS, leaves versioned rollback guard and creates no jobs or external requests')
                    result.update(success=True,browser_version=browser.version,proxy_scope='vm_host' if vm else 'loopback')
                finally:browser.close()
        finally:
            for server in servers:server.shutdown();server.server_close()
            for thread in threads:thread.join(5)
            (out/'results.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(result,ensure_ascii=False,indent=2))


if __name__=='__main__':main()
