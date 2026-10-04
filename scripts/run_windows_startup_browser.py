"""Real local workbench UI; registry is an isolated fixture, never real startup."""
import json
import os
from pathlib import Path
import sys
import tempfile
import threading

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'src'),str(ROOT/'tests')]
from test_windows_startup import fixture
from vibe_job_radar.workbench import LocalServer


def main():
    from playwright.sync_api import sync_playwright,expect
    out=ROOT/'browser-acceptance/windows-startup';out.mkdir(parents=True,exist_ok=True)
    result={'success':False,'checks':[],'page_errors':[],'external_requests':[],
        'scope':'Real local HTTP/UI with isolated registry fixture; no system startup writes, Windows logon or live site requests.'}
    with tempfile.TemporaryDirectory(prefix='radar-startup-ui-') as tmp:
        workspace,exe,registry,manager=fixture(tmp)
        server=LocalServer(workspace);server.windows_startup=manager
        thread=threading.Thread(target=server.serve_forever,kwargs={'poll_interval':.01},daemon=True);thread.start()
        try:
            with sync_playwright() as pw:
                options={'headless':True}
                if os.environ.get('RADAR_TEST_CHROMIUM'):options['executable_path']=os.environ['RADAR_TEST_CHROMIUM']
                browser=pw.chromium.launch(**options)
                try:
                    result['browser_version']=browser.version
                    context=browser.new_context(viewport={'width':1280,'height':900})
                    def local_only(route):
                        if route.request.url.startswith(server.origin+'/'):route.continue_()
                        else:result['external_requests'].append('unexpected_nonlocal');route.abort()
                    context.route('**/*',local_only)
                    def open_page():
                        page=context.new_page();page.on('pageerror',lambda exc:result['page_errors'].append(type(exc).__name__))
                        page.goto(server.entry_url);page.locator('#windows-startup summary').click()
                        expect(page.locator('#startup-enable')).to_be_enabled()
                        return page
                    first=open_page();second=open_page()
                    expect(first.locator('#startup-consent')).not_to_be_checked()
                    first.locator('#startup-enable').click()
                    expect(first.locator('#startup-message')).to_contain_text('勾选登录启动')
                    assert registry.writes==[] and not manager.root.exists()
                    result['checks'].append('two real pages read default-off state; no consent means no registration or metadata')

                    first.locator('#startup-consent').check();first.locator('#startup-enable').click()
                    expect(first.locator('#startup-status')).to_contain_text('已登记登录启动')
                    assert len(registry.writes)==1
                    second.locator('#startup-consent').check();second.locator('#startup-enable').click()
                    expect(second.locator('#startup-message')).to_contain_text('已变化')
                    expect(second.locator('#startup-status')).to_contain_text('已登记登录启动')
                    assert len(registry.writes)==1
                    first.reload();first.locator('#windows-startup summary').click()
                    expect(first.locator('#startup-disable')).to_be_enabled()
                    expect(first.locator('#startup-consent')).not_to_be_checked()
                    first.set_viewport_size({'width':390,'height':844})
                    assert first.evaluate('document.documentElement.scrollWidth<=innerWidth')
                    first.locator('#windows-startup').screenshot(path=str(out/'windows-startup-mobile.png'))
                    result['checks'].append('explicit registration survives reload, other page gets revision conflict without duplicate write, 390px has no overflow')

                    first.locator('#startup-disable').click();expect(first.locator('#startup-status')).to_contain_text('尚未登记')
                    registry.fail_create=True
                    first.locator('#startup-consent').check();first.locator('#startup-enable').click()
                    expect(first.locator('#startup-message')).to_contain_text('未能登记')
                    expect(first.locator('#startup-status')).to_contain_text('尚未登记')
                    expect(first.locator('#startup-consent')).not_to_be_checked()
                    registry.fail_create=False
                    first.locator('#startup-consent').check();first.locator('#startup-enable').click()
                    expect(first.locator('#startup-status')).to_contain_text('已登记登录启动')
                    owned=registry.values[manager.name];registry.values[manager.name]=(1,'PRIVATE FOREIGN COMMAND')
                    first.locator('#startup-refresh').click();expect(first.locator('#startup-status')).to_contain_text('不符')
                    expect(first.locator('#startup-disable')).to_be_disabled();expect(first.locator('#startup-enable')).to_be_disabled()
                    assert 'PRIVATE FOREIGN' not in first.locator('body').inner_text()
                    registry.values[manager.name]=owned
                    first.locator('#startup-refresh').click();expect(first.locator('#startup-disable')).to_be_enabled()
                    first.locator('#startup-disable').click();expect(first.locator('#startup-status')).to_contain_text('尚未登记')
                    assert not registry.values and server.public_schedule.state()['status']=='disabled'
                    assert server.public_tasks.snapshot()['status']=='idle'
                    result['checks'].append('write failure stays off, external changes disable actions without exposing command, explicit removal preserves schedule and idle query')

                    manager.supported=False;first.reload();first.locator('#windows-startup summary').click()
                    expect(first.locator('#startup-status')).to_contain_text('仅支持 Windows 便携包')
                    expect(first.locator('#startup-enable')).to_be_disabled();expect(first.locator('#startup-disable')).to_be_disabled()
                    result['checks'].append('source/unsupported runtime explains manual startup and offers no registry mutation')
                    assert not result['page_errors'] and not result['external_requests']
                    result.update(success=True,fixture_writes=len(registry.writes),real_startup_writes=0)
                finally:browser.close()
        finally:
            server.shutdown();server.server_close();thread.join(5)
            (out/'results.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(result,ensure_ascii=True,indent=2))


if __name__=='__main__':main()
