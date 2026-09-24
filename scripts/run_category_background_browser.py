"""Actual browser close/reload against the default category Collector and ledger."""
import argparse
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'src'),str(ROOT/'tests')]
from vibe_job_radar.workbench import LocalServer
from vibe_job_radar.workspace import Workspace
from vibe_job_radar.guided.rate import Limits, RateLedger
from vibe_job_radar.network import SafeHTTP
from test_public_category import Wire, listing, card, job_url


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--channel',choices=['msedge'])
    args=parser.parse_args()
    from playwright.sync_api import sync_playwright, expect
    output=ROOT/'browser-acceptance/category-background';output.mkdir(parents=True,exist_ok=True)
    result=dict(success=False,checks=[],page_errors=[],external_browser_requests=[],
                scope='Real browser, local server, default factory and temporary ledger; authored upstream responses only.')
    releases=[]
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
                def page_for_batch():
                    page=context.new_page();page.on('pageerror',lambda e:result['page_errors'].append(str(e)))
                    page.goto(server.entry_url)
                    page.get_by_role('link',name='进入猎聘架构师公开分类采集').click()
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
                    assert entered.is_set()
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
                page=page_for_batch()
                page.locator('#collect-history').select_option(ident);page.locator('#collect-load').click()
                expect(page.locator('#collect-progress')).to_contain_text('completed')
                assert json.loads(page.locator('#collect-json').text_content())['report_id']==saved['report_id']
                result['checks'].append('a new page reopens the same completed task and report without starting another capture')

                wire,entered,release,fetch=blocked_wire(51)
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
                result['success']=True;browser.close()
        finally:
            for release in releases:release.set()
            server.shutdown();server.server_close();serving.join(5)
            (output/'results.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(result,ensure_ascii=True,indent=2))


if __name__=='__main__':main()
