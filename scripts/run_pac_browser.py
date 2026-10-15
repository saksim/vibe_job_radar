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
from vibe_job_radar import pac_native, pac_url_worker
from vibe_job_radar.workbench import LocalServer
from vibe_job_radar.workspace import Workspace

SCRIPT=b'function FindProxyForURL(url,host){return "SOCKS5 127.0.0.1:1080; DIRECT";}'


def main():
    from playwright.sync_api import sync_playwright,expect
    out=ROOT/'browser-acceptance'/'windows-pac';out.mkdir(parents=True,exist_ok=True)
    result={'success':False,'checks':[],'external_requests':[],'page_errors':[],
        'native_evaluator':os.name=='nt','full_url_success':False,'full_url_checks':[],
        'scope':'Authored local PAC and actual workbench UI. No proxy, source request, personal PAC or OS setting changes. Only the legacy Windows domain evaluator is substituted on non-Windows; the full-URL worker uses the actual optional engine on all tested systems.'}
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
                        with page.expect_response(lambda r:r.url.endswith(('/api/network/pac','/api/network/pac-url','/api/network/pac-url-dns','/api/network/pac-domain','/api/network/pac-domain-dns'))) as response:
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
                    assert pac_url_worker.available(), 'required optional URL PAC test component missing'
                    real_url_evaluate=pac_url_worker.evaluate
                    with patch.object(pac_url_worker,'evaluate',wraps=real_url_evaluate) as url_calls:
                        page.locator('#workspace-pac-consent').check()
                        page.locator('#workspace-pac-kind').select_option('url')
                        expect(page.locator('#workspace-pac-consent')).not_to_be_checked()
                        expect(page.locator('#workspace-pac-disclosure')).to_contain_text('完整HTTPS路径和查询参数')
                        full_script=b'function FindProxyForURL(url,host){return url==="https://pac-check.invalid/path?probe=explicit" && host==="pac-check.invalid" ? "PROXY 127.0.0.1:19081" : "UNKNOWN missing-path:9; DIRECT";}'
                        file(full_script,'完整网址.pac');save()
                        expect(page.locator('#workspace-proxy-mode')).to_have_value('pac_url')
                        assert workspace.network_state()['schema_version']==6
                        old_url=workspace.network_policy()
                        url_calls.assert_not_called()
                        result['full_url_checks'].append('scope change clears consent; separate full-path disclosure and schema6 grant; import does not execute')
                        for path in ('/guided','/advanced','/'):
                            page.goto(server.origin+path);expand()
                            expect(page.locator('#workspace-pac-kind')).to_have_value('url')
                            expect(page.locator('#workspace-proxy-mode')).to_have_value('pac_url')
                        url_calls.assert_not_called()
                        assert check(True)['transport']=='loopback_http_proxy'
                        assert url_calls.call_args.args[1:3]==('https://pac-check.invalid/path?probe=explicit','pac-check.invalid')
                        result['full_url_checks'].append('all three pages preserve full URL scope; actual isolated optional engine receives path and query; no target connection')
                        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                        page.locator('#network-preferences').screenshot(path=str(out/'pac-url-mobile.png'))
                        file(b'function FindProxyForURL(url,host){dnsResolve(host);return "DIRECT";}','unsupported-helper.pac');save()
                        assert check(False)['code']=='pac_invalid_script'
                        result['full_url_checks'].append('unprovided DNS helper fails without DIRECT fallback; 390px no horizontal overflow')
                        page.locator('#workspace-proxy-mode').select_option('auto')
                        with page.expect_response(lambda r:r.url.endswith('/api/network/proxy')):
                            page.locator('#save-workspace-proxy').click()
                        expect(page.locator('#workspace-proxy-mode')).to_have_value('auto')
                        assert not old_url.pac.permitted() and workspace.network_state()['schema_version']==2
                        assert url_calls.call_count==2 and calls.call_count==2
                        assert not workspace.db.exists() and not result['external_requests'] and not result['page_errors']
                        result['full_url_checks'].append('rollback revokes URL grant; old domain evaluator count unchanged; zero jobs or external requests')
                    page.locator('#workspace-pac-consent').check()
                    page.locator('#workspace-pac-kind').select_option('url_dns')
                    expect(page.locator('#workspace-pac-consent')).not_to_be_checked()
                    expect(page.locator('#workspace-pac-disclosure')).to_contain_text('DNS')
                    file(b'function FindProxyForURL(){return isInNet(dnsResolve("localhost"),"127.0.0.0","255.0.0.0")?"DIRECT":"INVALID";}','dns-helpers.pac');save()
                    for path in ('/guided','/advanced','/'):
                        page.goto(server.origin+path);expand()
                        expect(page.locator('#workspace-pac-kind')).to_have_value('url_dns')
                        expect(page.locator('#workspace-proxy-mode')).to_have_value('pac_url_dns')
                    assert check(True)['target_requested'] is False
                    assert workspace.network_state()['schema_version']==7
                    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                    page.locator('#network-preferences').screenshot(path=str(out/'pac-dns-mobile.png'))
                    result['full_url_checks'].append('separate DNS-helper consent persists across all pages; actual localhost DNS helper passes without a target request')
                    result['portable_domain_checks']=[]
                    for kind,mode,schema in (('portable_domain','pac_domain',9),('portable_domain_dns','pac_domain_dns',10)):
                        page.locator('#workspace-pac-consent').check()
                        page.locator('#workspace-pac-kind').select_option(kind)
                        expect(page.locator('#workspace-pac-consent')).not_to_be_checked()
                        expect(page.locator('#workspace-pac-disclosure')).to_contain_text('不提供路径')
                        condition='url==="https://pac-check.invalid/" && host==="pac-check.invalid"'
                        condition+=(' && isInNet(dnsResolve("localhost"),"127.0.0.0","255.0.0.0")' if schema==10 else ' && typeof dnsResolve==="undefined"')
                        body=('function FindProxyForURL(url,host){return ('+condition+')?"SOCKS5 127.0.0.1:19081":"INVALID path-leaked";}').encode()
                        with patch.object(pac_url_worker,'evaluate',wraps=real_url_evaluate) as domain_calls:
                            file(body,'domain.pac');save()
                            previous=workspace.network_policy()
                            for path in ('/guided','/advanced','/'):
                                page.goto(server.origin+path);expand()
                                expect(page.locator('#workspace-pac-kind')).to_have_value(kind)
                                expect(page.locator('#workspace-proxy-mode')).to_have_value(mode)
                            domain_calls.assert_not_called()
                            assert workspace.network_state()['schema_version']==schema
                            assert previous.describe()['native_browser_supported'] is True
                            assert check(True)['transport']=='loopback_socks5_proxy'
                            assert domain_calls.call_args.args[1:3]==('https://pac-check.invalid/','pac-check.invalid')
                            assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                            page.locator('#network-preferences').screenshot(path=str(out/(mode+'-mobile.png')))
                        page.locator('#workspace-proxy-mode').select_option('auto')
                        with page.expect_response(lambda r:r.url.endswith('/api/network/proxy')):
                            page.locator('#save-workspace-proxy').click()
                        expect(page.locator('#workspace-proxy-mode')).to_have_value('auto')
                        from vibe_job_radar.loopback_proxy import LocalProxyError
                        try:previous.for_host('pac-check.invalid')
                        except LocalProxyError as exc:assert exc.code=='pac_revoked'
                        else:raise AssertionError('domain grant survived rollback')
                        result['portable_domain_checks'].append(dict(mode=mode,schema=schema,three_pages=True,
                            import_readonly=True,canonical_root_only=True,rollback_revoked=True))
                    assert not result['external_requests'] and not result['page_errors']
                    result['portable_domain_success']=True
                    result.update(success=True,full_url_success=True,url_engine_version=pac_url_worker.ENGINE_VERSION,browser_version=browser.version)
                finally:browser.close()
        finally:
            server.shutdown();server.server_close();thread.join(5)
            (out/'results.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(result,ensure_ascii=False,indent=2))


if __name__=='__main__':main()
