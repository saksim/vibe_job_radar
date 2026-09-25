"""Actual browser close/reload against the default category Collector and ledger."""
import argparse
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
from urllib.parse import urlsplit
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'src'),str(ROOT/'tests')]
from vibe_job_radar.workbench import LocalServer
from vibe_job_radar.workspace import Workspace
from vibe_job_radar.guided.rate import Limits, RateLedger
from vibe_job_radar.network import SafeHTTP, FetchError
from test_public_category import Wire, listing, card, job_url


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--channel',choices=['msedge'])
    args=parser.parse_args()
    from playwright.sync_api import sync_playwright, expect
    output=ROOT/'browser-acceptance/category-background';output.mkdir(parents=True,exist_ok=True)
    result=dict(success=False,checks=[],page_errors=[],external_browser_requests=[],api_responses=[],phase='starting',
                scope='Real browser, local server, default factory and temporary ledger; authored upstream responses only.')
    releases=[];page=None
    with tempfile.TemporaryDirectory() as tmp:
        workspace=Workspace(tmp)
        ledger=RateLedger(workspace.root/'guided/rates.sqlite',Limits(page_interval=0,request_interval=0))
        server=LocalServer(workspace)
        serving=threading.Thread(target=server.serve_forever,kwargs={'poll_interval':.01},daemon=True);serving.start()
        try:
            with patch('vibe_job_radar.collection_rate.RateLedger',return_value=ledger), sync_playwright() as pw:
                browser=pw.chromium.launch(headless=True,**({'channel':args.channel} if args.channel else {}))
                result['browser_version']=browser.version
                context=browser.new_context()
                def local_only(route):
                    if route.request.url.startswith(server.origin+'/'):route.continue_()
                    else:result['external_browser_requests'].append(route.request.url);route.abort()
                context.route('**/*',local_only)
                context.on('response',lambda response:result['api_responses'].append(
                    {'path':urlsplit(response.url).path,'status':response.status})
                    if urlsplit(response.url).path.startswith('/api/') else None)
                page=None
                def start_page():
                    nonlocal page
                    page=context.new_page();page.on('pageerror',lambda e:result['page_errors'].append(str(e)))
                    page.goto(server.entry_url)
                    page.get_by_role('link',name='进入猎聘架构师公开分类采集').click()
                    return page
                def page_for_batch():
                    page=start_page()
                    form=page.locator('#collect-form')
                    form.locator('[name=detail_budget]').fill('2')
                    form.locator('[name=rights_note]').fill('后台分类人工上游验收，临时工作区。')
                    page.locator('#collect-permits input[value=liepin]').check();form.locator('[name=consent]').check()
                    return page
                def blocked_wire(first):
                    wire=Wire(listing(card(first)+card(first+1)))
                    entered,release=threading.Event(),threading.Event();releases.append(release)
                    original=wire.public_get
                    def fetch(url,**kwargs):
                        if url==job_url(first):
                            entered.set()
                            if not release.wait(15):raise AssertionError('controlled response was not released')
                        return original(url,**kwargs)
                    return wire,entered,release,fetch
                def await_request(page,entered):
                    # Sync Playwright must keep pumping its local route callbacks
                    # while the actual server dispatches the first body request.
                    deadline=time.monotonic()+8
                    while not entered.is_set() and time.monotonic()<deadline:
                        page.wait_for_timeout(20)
                    if not entered.is_set():
                        result['failure_ui']={key:page.locator(selector).inner_text() for key,selector in
                            [('notice','#notice'),('preview','#collect-check-results'),('progress','#collect-progress')]}
                        form=page.locator('#collect-form')
                        result['failure_ui'].update(mode=form.locator('[name=mode]').input_value(),
                            budget=form.locator('[name=detail_budget]').input_value(),
                            consent=form.locator('[name=consent]').is_checked(),
                            permission=page.locator('#collect-permits input[value=liepin]').is_checked())
                    assert entered.is_set(), 'first body not reached; see phase, API responses and failure_ui'
                # Hold the actual initialization responses. The previous UI
                # accepted input here, then its later preset erased it.
                held={}
                for endpoint in ('/api/evidence/state','/api/collection/background/state'):
                    context.route(server.origin+endpoint,lambda route:held.setdefault(urlsplit(route.request.url).path,route))
                result['phase']='delayed_initialization'
                page=start_page();form=page.locator('#collect-form')
                expect(page.locator('#collection-guide')).to_contain_text('自动读取猎聘架构师公开分类')
                for selector in ('[name=detail_budget]','[name=consent]','[name=rights_note]'):
                    expect(form.locator(selector)).to_be_disabled()
                expect(page.locator('#collect-start')).to_be_disabled()
                held['/api/evidence/state'].continue_()
                expect(page.locator('#guide-liepin_category')).to_be_visible()
                expect(form.locator('[name=detail_budget]')).to_have_value('5')
                expect(form.locator('[name=detail_budget]')).to_be_disabled()
                expect(page.locator('#collect-start')).to_be_disabled()
                assert server.collector.list()['runs']==[]
                deadline=time.monotonic()+8
                while '/api/collection/background/state' not in held and time.monotonic()<deadline:
                    page.wait_for_timeout(20)
                assert '/api/collection/background/state' in held
                held['/api/collection/background/state'].continue_()
                for endpoint in held:context.unroute(server.origin+endpoint)
                expect(form.locator('[name=detail_budget]')).to_be_enabled()
                form.locator('[name=detail_budget]').fill('2')
                page.locator('#collect-permits input[value=liepin]').check();form.locator('[name=consent]').check()
                expect(form.locator('[name=detail_budget]')).to_have_value('2')
                expect(form.locator('[name=consent]')).to_be_checked()
                expect(page.locator('#collect-permits input[value=liepin]')).to_be_checked()
                assert server.collector.list()['runs']==[]
                page.close()
                result['checks'].append('delayed profile and background-state responses keep native form controls disabled until the final preset is applied; user input then persists without creating a task')
                result['phase']='close_during_first_body'
                wire,entered,release,fetch=blocked_wire(41)
                with patch.object(SafeHTTP,'public_get',side_effect=fetch):
                    page=page_for_batch();page.locator('#collect-start').click()
                    await_request(page,entered)
                    ident=server.collector.list()['runs'][0]['id']
                    page.close()  # No remaining UI can issue step requests.
                    release.set()
                    deadline=time.monotonic()+10
                    while time.monotonic()<deadline:
                        saved=server.collector.status({'id':ident})
                        if saved['status'] in {'completed','needs_attention','empty'}:break
                        time.sleep(.03)
                    result['closed_page_observation']=dict(status=saved['status'],
                        details=[d['status'] for d in saved['details']],requests=list(wire.calls))
                    assert saved['status']=='completed' and saved['saved_detail_count']==2
                    assert wire.calls==['https://www.liepin.com/robots.txt',
                        'https://www.liepin.com/career/360321/',job_url(41),job_url(42)]
                report=workspace.root/'reports'/saved['report_id']
                report_hashes={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in report.iterdir() if p.is_file()}
                result['checks'].append('closing the actual page during its first body still completes exactly the two selected bodies and original report without UI step calls')
                result['phase']='reopen_completed'
                page=page_for_batch()
                page.locator('#collect-history').select_option(ident);page.locator('#collect-load').click()
                expect(page.locator('#collect-progress')).to_contain_text('completed')
                assert json.loads(page.locator('#collect-json').text_content())['report_id']==saved['report_id']
                result['checks'].append('a new page reopens the same completed task and report without starting another capture')

                wire,entered,release,fetch=blocked_wire(51)
                result['phase']='pause_reload_resume'
                with patch.object(SafeHTTP,'public_get',side_effect=fetch):
                    page.get_by_role('button',name='自动读取猎聘架构师公开分类').click()
                    form=page.locator('#collect-form');form.locator('[name=detail_budget]').fill('2')
                    form.locator('[name=rights_note]').fill('暂停与刷新后的后台续接人工验收。')
                    page.locator('#collect-permits input[value=liepin]').check();form.locator('[name=consent]').check()
                    page.locator('#collect-start').click();await_request(page,entered)
                    expect(page.locator('#collect-background-note')).to_contain_text('可以关闭或刷新网页')
                    page.locator('#collect-pause').click()
                    expect(page.locator('#notice')).to_contain_text('已请求暂停')
                    page.reload();release.set()
                    expect(page.locator('#collect-progress')).to_contain_text('paused',timeout=10000)
                    expect(page.locator('#collect-start')).to_be_enabled()
                    paused=json.loads(page.locator('#collect-json').text_content())
                    assert [d['status'] for d in paused['details']]==['ok','pending']
                    assert job_url(52) not in wire.calls
                    page.locator('#collect-resume').click()
                    expect(page.locator('#collect-progress')).to_contain_text('completed',timeout=10000)
                    expect(page.locator('#collect-start')).to_be_enabled()
                    assert wire.calls.count(job_url(51))==wire.calls.count(job_url(52))==1
                    assert len(wire.calls)==4  # Original client reuses its robots within this task.
                result['checks'].append('pause survives actual reload; explicit resume fetches only the remaining body and keeps prior task/report bytes')
                assert report_hashes=={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in report.iterdir() if p.is_file()}
                assert not result['page_errors'] and not result['external_browser_requests']
                result['ledger']=ledger.summary('liepin')
                assert result['ledger']['page']['day']==6 and result['ledger']['request']['day']==8
                result['checks'].append('both original batches use the same persistent ledger: six page reservations, eight actual HTTP reservations, no added list or login')
                result['phase']='explicit_same_page_retry'
                wire=Wire(listing(card(61)+card(62)))
                def failed_page(url,**kwargs):
                    if url=='https://www.liepin.com/career/360321/':
                        wire.calls.append(url);raise FetchError('network_error','Authored browser acceptance failure')
                    return wire.public_get(url)
                with patch.object(SafeHTTP,'public_get',side_effect=failed_page):
                    page.get_by_role('button',name='自动读取猎聘架构师公开分类').click()
                    form=page.locator('#collect-form');form.locator('[name=detail_budget]').fill('2')
                    form.locator('[name=rights_note]').fill('同页显式重试人工验收；无真实网站请求。')
                    page.locator('#collect-permits input[value=liepin]').check();form.locator('[name=consent]').check()
                    page.locator('#collect-start').click()
                    expect(page.locator('#collect-progress')).to_contain_text('needs_attention',timeout=10000)
                failed=json.loads(page.locator('#collect-json').text_content())
                parent=server.collector._path(failed['id']);parent_bytes=parent.read_bytes()
                assert failed['category_attempts']==1 and failed['details']==[]
                before=ledger.summary('liepin');requests=list(wire.calls)
                page.get_by_role('button',name='预览失败分类页的同页重试（不联网）').click()
                expect(page.locator('#collect-result')).to_contain_text('第 1/3 次显式重试，最多 2 条正文')
                page.get_by_role('button',name='确认保存同页重试（暂不联网）').click()
                expect(page.locator('#collect-progress')).to_contain_text('paused')
                child=json.loads(page.locator('#collect-json').text_content())
                assert wire.calls==requests and ledger.summary('liepin')==before and parent.read_bytes()==parent_bytes
                page.reload()
                page.locator('#collect-history').select_option(child['id']);page.locator('#collect-load').click()
                expect(page.locator('#collect-progress')).to_contain_text('paused')
                assert parent.read_bytes()==parent_bytes and ledger.summary('liepin')==before
                with patch.object(SafeHTTP,'public_get',side_effect=wire.public_get):
                    page.locator('#collect-resume').click()
                    expect(page.locator('#collect-progress')).to_contain_text('completed',timeout=10000)
                done=json.loads(page.locator('#collect-json').text_content())
                assert done['id']==child['id'] and done['saved_detail_count']==2 and done['category_attempts']==1
                assert wire.calls[len(requests):]==['https://www.liepin.com/robots.txt',
                    'https://www.liepin.com/career/360321/',job_url(61),job_url(62)]
                assert parent.read_bytes()==parent_bytes and report_hashes=={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in report.iterdir() if p.is_file()}
                result['same_page_retry']=dict(original_status=failed['status'],saved_status=child['status'],
                    completed_status=done['status'],full_bodies=2,preview_save_extra_requests=0,
                    original_failure_unchanged=True,ledger=ledger.summary('liepin'))
                assert not result['page_errors'] and not result['external_browser_requests']
                result['checks'].append('network-failed category page is explicitly previewed and saved offline; actual reload preserves pause, resume reads only that page and two authored bodies, original failure/report and quota remain')
                result['success']=True;result['phase']='completed';browser.close()
        finally:
            if not result['success']:
                result['tasks']=server.collector.list()['runs']
                result['background']=server.collection_runner.state({})
                try:
                    if 'failure_ui' not in result and page is not None and not page.is_closed():
                        result['failure_ui']=page.evaluate('''() => {
                            const form=document.getElementById('collect-form');
                            return {notice:document.getElementById('notice')?.textContent,
                                preview:document.getElementById('collect-check-results')?.textContent,
                                progress:document.getElementById('collect-progress')?.textContent,
                                mode:form?.elements.mode.value,budget:form?.elements.detail_budget.value,
                                consent:form?.elements.consent.checked,
                                permission:document.querySelector('#collect-permits input[value=liepin]')?.checked};
                        }''')
                except Exception as error:result['diagnostic_error']=type(error).__name__
            for release in releases:release.set()
            server.shutdown();server.server_close();serving.join(5)
            (output/'results.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(result,ensure_ascii=True,indent=2))


if __name__=='__main__':main()
