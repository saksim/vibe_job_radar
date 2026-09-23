"""Actual local UI and worker; artificial catalog and injected clock, no site access."""
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
from unittest.mock import Mock, patch

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'src'),str(ROOT/'tests')]
from test_local_public import payload
from vibe_job_radar.local_public import LocalPublicDataClient
from vibe_job_radar.public_schedule import DAY
from vibe_job_radar.workbench import LocalServer
from vibe_job_radar.workspace import Workspace


def until(predicate):
    deadline=time.monotonic()+15
    while time.monotonic()<deadline:
        if predicate():return
        time.sleep(.02)
    raise AssertionError('local worker did not reach expected state')


def main():
    from playwright.sync_api import sync_playwright, expect
    out=ROOT/'browser-acceptance'/'public-schedule';out.mkdir(parents=True,exist_ok=True)
    result={'success':False,'checks':[],'page_errors':[],'external_requests':[],
        'scope':'Actual local browser, scheduler, cache and reports; artificial upstream and injected time. Not a 24-hour live-site certification.'}
    clean={k:v for k,v in os.environ.items() if not k.startswith('VIBE_RADAR_')}
    with tempfile.TemporaryDirectory(prefix='radar-schedule-') as tmp,patch.dict(os.environ,clean,clear=True), \
            patch('urllib.request.getproxies',return_value={}):
        now=[time.time()];workspace=Workspace(tmp);transport=Mock();transport.json.return_value=payload(50)
        def launch():
            client=LocalPublicDataClient(workspace,transport=transport,clock=lambda:now[0])
            server=LocalServer(workspace,public_client=client);server.public_schedule.clock=lambda:now[0]
            thread=threading.Thread(target=server.serve_forever,kwargs={'poll_interval':.01},daemon=True);thread.start()
            return server,thread
        server,thread=launch()
        try:
            with sync_playwright() as pw:
                options={'headless':True}
                if os.environ.get('RADAR_TEST_CHROMIUM'):options['executable_path']=os.environ['RADAR_TEST_CHROMIUM']
                browser=pw.chromium.launch(**options)
                try:
                    context=browser.new_context(viewport={'width':1280,'height':1000})
                    def local_only(route):
                        if route.request.url.startswith(server.origin+'/'):route.continue_()
                        else:result['external_requests'].append(route.request.url);route.abort()
                    context.route('**/*',local_only)
                    def open_page(url=None):
                        page=context.new_page();page.on('pageerror',lambda e:result['page_errors'].append(str(e)))
                        page.goto(url or server.entry_url)
                        expect(page.locator('#schedule-save')).to_be_enabled()
                        expect(page.locator('#public-source option')).to_have_count(2)
                        page.locator('#public-schedule > summary').click()
                        return page
                    page=open_page();assert not server.public_schedule.path.exists()
                    stale=open_page();old=server.public_schedule.state()
                    stale.route('**/api/public/schedule/state',lambda route:route.fulfill(json=old))
                    for p in (page,stale):p.locator('#public-search [name=query]').fill('Architect')
                    page.locator('#schedule-save').click()
                    expect(page.locator('#schedule-message')).to_contain_text('明确勾选')
                    assert not server.public_schedule.path.exists()
                    page.locator('#schedule-consent').check()
                    page.locator('#public-search [name=region]').fill('London')
                    expect(page.locator('#schedule-consent')).not_to_be_checked()
                    page.locator('#schedule-consent').check()
                    with page.expect_response(lambda r:r.url.endswith('/schedule/configure')) as response:
                        page.locator('#schedule-save').click()
                    assert response.value.status==200
                    expect(page.locator('#schedule-query')).to_contain_text('London')
                    assert server.public_schedule.state()['next_due']==now[0]+DAY
                    transport.json.assert_not_called()
                    stale.locator('#schedule-consent').check()
                    with stale.expect_response(lambda r:r.url.endswith('/schedule/configure')) as response:
                        stale.locator('#schedule-save').click()
                    assert response.value.status==400
                    assert server.public_schedule.state()['query']['region']=='London'
                    stale.close();page.close()
                    result['checks'].append('separate daily consent; changing form revokes consent; stale page rejected; saving stays offline')

                    now[0]+=DAY;server.public_schedule._wake.set()
                    until(lambda:len(server.public_schedule.state()['history'])==1)
                    first=server.public_schedule.state()['history'][0]['report_id']
                    assert first and workspace.report(first)
                    assert transport.json.call_count==1 and server.public_tasks._state['returned_jobs']==20
                    page=open_page(server.entry_url+'&report='+first)
                    expect(page.locator('#report')).to_be_visible()
                    expect(page.locator('#schedule-history a')).to_have_attribute('href','/#report='+first)
                    page.set_viewport_size({'width':390,'height':844})
                    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                    page.locator('#public-schedule').screenshot(path=str(out/'schedule-mobile.png'))
                    result['checks'].append('closed pages do not stop worker; injected 24-hour boundary produces original report and first 20 results; 390px layout')

                    # A newer scheduled result must not replace the report the
                    # user explicitly opened and is currently reviewing.
                    now[0]+=DAY;server.public_schedule._wake.set()
                    until(lambda:len(server.public_schedule.state()['history'])==2)
                    expect(page.locator('#schedule-history li')).to_have_count(2,timeout=10000)
                    assert page.url.endswith('#report='+first)
                    page.locator('#schedule-history a').first.click()
                    second=server.public_schedule.state()['history'][-1]['report_id']
                    expect(page).to_have_url(server.origin+'/#report='+second)
                    expect(page.locator('#report')).to_be_visible()
                    result['checks'].append('new scheduled report retains pinned report; explicit history link opens correct batch')

                    page.close();server.shutdown();server.server_close();thread.join(5)
                    now[0]+=8*DAY;server,thread=launch()
                    until(lambda:len(server.public_schedule.state()['history'])==3)
                    assert transport.json.call_count==3
                    assert server.public_schedule.state()['next_due']==now[0]+DAY
                    page=open_page()
                    with page.expect_response(lambda r:r.url.endswith('/schedule/disable')) as response:
                        page.locator('#schedule-stop').click()
                    assert response.value.status==200
                    expect(page.locator('#schedule-status')).to_contain_text('未启用')
                    now[0]+=2*DAY;server.public_schedule._wake.set();server.public_schedule.tick()
                    assert transport.json.call_count==3 and len(server.public_schedule.state()['history'])==3
                    assert all(workspace.report(item['report_id']) for item in server.public_schedule.state()['history'])
                    result['checks'].append('service restart after eight missed days runs once; explicit stop prevents later run and preserves three reports')
                    assert not result['external_requests'] and not result['page_errors']
                    result.update(success=True,browser_version=browser.version,upstream_fixture_requests=transport.json.call_count)
                finally:browser.close()
        finally:
            server.shutdown();server.server_close();thread.join(5)
            (out/'results.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(result,ensure_ascii=False,indent=2))


if __name__=='__main__':main()
