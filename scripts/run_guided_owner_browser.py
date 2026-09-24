"""Actual local HTTP/UI ownership; artificial browser content, no site requests."""
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
from test_guided import FakeBackend,fixture_adapter
from vibe_job_radar.guided.adapters import Registry
from vibe_job_radar.workbench import LocalServer
from vibe_job_radar.workspace import Workspace


def main():
    from playwright.sync_api import sync_playwright,expect
    out=ROOT/'browser-acceptance'/'guided-owner';out.mkdir(parents=True,exist_ok=True)
    result={'success':False,'checks':[],'external_requests':[],'page_errors':[],
            'scope':'Two real local HTTP services and browser pages, one temporary workspace and artificial acquisition backend; no live site or account certification.'}
    entered,release=threading.Event(),threading.Event()
    class HeldBackend(FakeBackend):
        def open(self,*args,**kwargs):
            entered.set()
            if not release.wait(30):raise AssertionError('artificial acquisition was not released')
            return super().open(*args,**kwargs)
    clean={k:v for k,v in os.environ.items() if not k.startswith('VIBE_RADAR_')}
    with tempfile.TemporaryDirectory(prefix='radar-guided-owner-') as tmp,\
         patch.dict(os.environ,clean,clear=True),patch('urllib.request.getproxies',return_value={}):
        workspace=Workspace(tmp)
        workspace.config['platforms']['fixture']={'label':'人工测试站点','domains':['jobs.fixture.test']}
        servers=[LocalServer(workspace),LocalServer(workspace)];observer_factory=Mock(side_effect=FakeBackend)
        for server,factory in zip(servers,(HeldBackend,observer_factory)):
            server.guided.registry=Registry([fixture_adapter()]);server.guided.factory=factory
        threads=[threading.Thread(target=s.serve_forever,kwargs={'poll_interval':.01},daemon=True) for s in servers]
        for thread in threads:thread.start()
        try:
            with sync_playwright() as pw:
                options={'headless':True}
                if os.environ.get('RADAR_TEST_CHROMIUM'):options['executable_path']=os.environ['RADAR_TEST_CHROMIUM']
                browser=pw.chromium.launch(**options)
                try:
                    context=browser.new_context(viewport={'width':1280,'height':900})
                    def local_only(route):
                        if any(route.request.url.startswith(s.origin+'/') for s in servers):route.continue_()
                        else:result['external_requests'].append('unexpected_nonlocal');route.abort()
                    context.route('**/*',local_only)
                    def page_for(server):
                        page=context.new_page();page.on('pageerror',lambda e:result['page_errors'].append(type(e).__name__))
                        page.goto(server.entry_url)
                        page.goto(server.origin+'/guided');expect(page.locator('#site option')).to_have_count(1)
                        return page
                    first=page_for(servers[0]);form=first.locator('#search-form')
                    form.locator('[name=rights_note]').fill('人工离线双工作台测试')
                    form.locator('[name=consent]').check();form.locator('[name=max_jobs]').select_option('1')
                    first.locator('#find').click();assert entered.wait(5)
                    ident=servers[0].guided.state()['jobs'][0]['id'];path=servers[0].guided._path(ident);before=path.read_bytes()
                    second=page_for(servers[1]);expect(second.locator('#busy')).to_contain_text('另一个本机工作台')
                    expect(second.locator('#task-status')).to_contain_text('执行中')
                    for name in ('find','stop','pause','resume','login','collect','check-browser','install','submit-password-login','forget-session'):
                        expect(second.locator('#'+name)).to_be_disabled()
                    def request(server,path,data):
                        return context.request.post(server.origin+path,data=data,
                            headers={'X-Radar-Token':server.token,'Origin':server.origin})
                    for data in ({'id':ident,'action':'stop'}, {'id':ident,'action':'resume'},
                                 {'id':ident,'action':'login_password','username':'artificial',
                                  'password':'ARTIFICIAL-NOT-REAL','credential_consent':True}):
                        response=request(servers[1],'/api/guided/action',data)
                        assert response.status==409 and response.json()['code']=='guided_task_busy'
                    assert path.read_bytes()==before and not servers[0].guided._cancel.is_set()
                    assert observer_factory.call_count==0
                    second.set_viewport_size({'width':390,'height':844})
                    assert second.evaluate('document.documentElement.scrollWidth<=innerWidth')
                    second.screenshot(path=str(out/'observing-mobile.png'),full_page=True)
                    result['checks'].append('other workbench displays active owner and unmodified running state; action controls disabled; forced stop/resume/password API requests return 409 without state mutation or browser creation; 390px')
                    release.set();expect(first.locator('#task-status')).to_contain_text('可以选择岗位',timeout=10000)
                    expect(second.locator('#stop')).to_be_disabled()
                    first.locator('#pause').click();expect(first.locator('#task-status')).to_contain_text('已暂停')
                    expect(second.locator('#resume')).to_be_disabled()
                    first.locator('#stop').click();expect(first.locator('#task-status')).to_contain_text('已停止')
                    expect(second.locator('#search-again')).to_be_enabled(timeout=10000)
                    second.locator('#search-again').click();expect(second.locator('#task-status')).to_contain_text('可以选择岗位',timeout=10000)
                    second.locator('#select-all').click();second.locator('#collect').click()
                    expect(second.locator('#result a',has_text='查看本批研究结论')).to_be_visible(timeout=10000)
                    state=servers[1].guided._load(ident);report=state['report_id'];assert state['status']=='completed' and report
                    assert observer_factory.call_count==1 and workspace.report(report)
                    result['checks'].append('idle/paused browser remains owned; explicit stop closes it, observer can then explicitly continue the original query and produce its original report')
                    first.close();servers[0].shutdown();servers[0].server_close();threads[0].join(5)
                    assert ident in servers[1].guided._backends
                    second.locator('#result a',has_text='查看本批研究结论').click()
                    expect(second.locator('#brief-capabilities')).to_contain_text('Cursor')
                    assert workspace.report(report)
                    result['checks'].append('closing observer service does not close the new owner browser or remove its completed report; original report UI renders the artificial Cursor requirement')
                    assert not result['page_errors'] and not result['external_requests']
                    result.update(success=True,browser_version=browser.version,source_requests=0)
                finally:release.set();browser.close()
        finally:
            release.set()
            for server in servers:server.shutdown();server.server_close()
            for thread in threads:thread.join(5)
            (out/'results.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(result,ensure_ascii=False,indent=2))


if __name__=='__main__':main()
