"""Two actual local workbenches, one workspace; artificial upstream only."""
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
from unittest.mock import Mock,patch

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'src'),str(ROOT/'tests')]
from test_local_public import payload,query
from vibe_job_radar.local_public import LocalPublicDataClient
from vibe_job_radar.workbench import LocalServer
from vibe_job_radar.workspace import Workspace


def main():
    from playwright.sync_api import sync_playwright,expect
    out=ROOT/'browser-acceptance'/'public-owner';out.mkdir(parents=True,exist_ok=True)
    result={'success':False,'checks':[],'page_errors':[],'external_requests':[],
        'scope':'Two actual local HTTP services and browser windows, one temporary workspace, artificial catalog; no live-site data.'}
    clean={k:v for k,v in os.environ.items() if not k.startswith('VIBE_RADAR_')}
    with tempfile.TemporaryDirectory(prefix='radar-public-owner-') as tmp,patch.dict(os.environ,clean,clear=True), \
            patch('urllib.request.getproxies',return_value={}):
        workspace=Workspace(tmp);now=[time.time()];entered,release=threading.Event(),threading.Event()
        def held(url):
            entered.set()
            if not release.wait(30):raise AssertionError('fixture not released')
            return payload()
        a,b=Mock(),Mock();a.json.side_effect=held;b.json.return_value=payload()
        servers=[LocalServer(workspace,public_client=LocalPublicDataClient(workspace,transport=t,clock=lambda:now[0])) for t in (a,b)]
        threads=[threading.Thread(target=s.serve_forever,kwargs={'poll_interval':.01},daemon=True) for s in servers]
        for t in threads:t.start()
        try:
            with sync_playwright() as pw:
                opts={'headless':True}
                if os.environ.get('RADAR_TEST_CHROMIUM'):opts['executable_path']=os.environ['RADAR_TEST_CHROMIUM']
                browser=pw.chromium.launch(**opts)
                try:
                    context=browser.new_context(viewport={'width':1280,'height':1000})
                    def local_only(route):
                        if any(route.request.url.startswith(s.origin+'/') for s in servers):route.continue_()
                        else:result['external_requests'].append(route.request.url);route.abort()
                    context.route('**/*',local_only)
                    def page_for(server):
                        page=context.new_page();page.on('pageerror',lambda e:result['page_errors'].append(str(e)))
                        page.goto(server.entry_url);expect(page.locator('#public-source option')).to_have_count(1)
                        return page
                    first=page_for(servers[0]);form=first.locator('#public-search')
                    form.locator('[name=query]').fill('Architect');form.locator('[name=consent]').check()
                    form.locator('button').click();assert entered.wait(5)
                    ident=servers[0].public_tasks.snapshot()['id'];before=servers[0].public_tasks.path.read_bytes()
                    second=page_for(servers[1])
                    expect(second.locator('#public-status')).to_contain_text('另一个本机服务')
                    expect(second.locator('#public-search-button')).to_be_disabled()
                    expect(second.locator('#public-example')).to_be_disabled()
                    expect(second.locator('#public-cancel')).to_be_hidden()
                    expect(second.locator('#public-resume')).to_be_hidden()
                    for action,data in (('search',{'consent':True,'query':query().payload()}),('cancel',{'id':ident})):
                        reply=context.request.post(servers[1].origin+'/api/public/'+action,data=data,
                            headers={'X-Radar-Token':servers[1].token,'Origin':servers[1].origin})
                        assert reply.status==409 and reply.json()['code']=='public_task_busy'
                    assert servers[0].public_tasks.path.read_bytes()==before
                    assert not servers[0].public_tasks._cancel.is_set() and b.json.call_count==0
                    second.set_viewport_size({'width':390,'height':844})
                    assert second.evaluate('document.documentElement.scrollWidth<=innerWidth')
                    second.locator('#public-entry').screenshot(path=str(out/'observing-mobile.png'))
                    result['checks'].append('other service shows current owner and progress; controls hidden/disabled; direct duplicate or cancel gets 409 without state overwrite or source request; 390px')

                    first.locator('#public-cancel').click();release.set()
                    expect(second.locator('#public-resume')).to_be_visible(timeout=10000)
                    second.locator('#public-resume').click()
                    expect(second.locator('#report')).to_be_visible(timeout=10000)
                    completed=servers[1].public_tasks.snapshot()
                    assert completed['id']==ident and completed['attempt']==2 and completed['status']=='completed'
                    assert completed['cache_reused'] and b.json.call_count==0
                    assert workspace.report(completed['report_id'])
                    result['checks'].append('owner explicitly cancels; observer then confirms same saved query and ID, cached full descriptions produce original report without another fetch')

                    # Closing an observer must not cancel work in its peer.
                    now[0]+=601;entered.clear();release.clear()
                    first.locator('#refresh').click();expect(first.locator('#public-search-button')).to_be_enabled()
                    form.locator('[name=query]').fill('Engineer');form.locator('button').click();assert entered.wait(5)
                    second.reload();expect(second.locator('#public-status')).to_contain_text('另一个本机服务')
                    second.close();servers[1].shutdown();servers[1].server_close();threads[1].join(5)
                    assert not servers[0].public_tasks._cancel.is_set()
                    release.set();expect(first.locator('#public-status')).to_contain_text('本机',timeout=10000)
                    servers[0].public_tasks._thread.join(15);final=servers[0].public_tasks.snapshot()
                    assert final['status']=='completed' and workspace.report(final['report_id'])
                    assert workspace.report(completed['report_id']) and final['report_id']!=completed['report_id']
                    assert a.json.call_count==2 and b.json.call_count==0
                    result['checks'].append('closing observer service leaves owner task active; both completed reports retained, quotas/caches shared')
                    assert not result['external_requests'] and not result['page_errors']
                    result.update(success=True,browser_version=browser.version,fixture_requests={'owner':a.json.call_count,'observer':b.json.call_count})
                finally:release.set();browser.close()
        finally:
            release.set()
            for server in servers:server.shutdown();server.server_close()
            for thread in threads:thread.join(5)
            (out/'results.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(result,ensure_ascii=False,indent=2))


if __name__=='__main__':main()
