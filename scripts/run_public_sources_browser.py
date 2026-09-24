"""Actual public-source UI, cache, reports and daily worker; artificial catalogs only."""
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
from test_public_sources import cloudflare_payload
from vibe_job_radar.local_public import LocalPublicDataClient
from vibe_job_radar.public_boards import ANTHROPIC, CLOUDFLARE
from vibe_job_radar.public_schedule import DAY
from vibe_job_radar.workbench import LocalServer
from vibe_job_radar.workspace import Workspace


def until(predicate):
    deadline=time.monotonic()+20
    while time.monotonic()<deadline:
        if predicate():return
        time.sleep(.02)
    raise AssertionError('local task did not reach expected state')


def main():
    from playwright.sync_api import sync_playwright, expect
    out=ROOT/'browser-acceptance/public-sources';out.mkdir(parents=True,exist_ok=True)
    result={'success':False,'checks':[],'page_errors':[],'external_requests':[],
        'scope':'Actual local browser and two-source pipeline; artificial catalogs and injected clock. Live-source observation is separate.'}
    clean={k:v for k,v in os.environ.items() if not k.startswith('VIBE_RADAR_')}
    try:
        with tempfile.TemporaryDirectory(prefix='radar-public-sources-') as tmp, \
                patch.dict(os.environ,clean,clear=True),patch('urllib.request.getproxies',return_value={}):
            workspace=Workspace(tmp);now=[time.time()];calls=[]
            def response(url):
                assert url in (ANTHROPIC.api_url,CLOUDFLARE.api_url)
                calls.append(url)
                return cloudflare_payload(50) if url==CLOUDFLARE.api_url else payload()
            transport=Mock();transport.json.side_effect=response
            server=LocalServer(workspace,public_client=LocalPublicDataClient(workspace,transport=transport,clock=lambda:now[0]))
            server.public_schedule.clock=lambda:now[0]
            thread=threading.Thread(target=server.serve_forever,kwargs={'poll_interval':.01},daemon=True);thread.start()
            try:
                with sync_playwright() as pw:
                    opts={'headless':True}
                    if os.environ.get('RADAR_TEST_CHROMIUM'):opts['executable_path']=os.environ['RADAR_TEST_CHROMIUM']
                    browser=pw.chromium.launch(**opts)
                    try:
                        context=browser.new_context(viewport={'width':1280,'height':1000})
                        def local_only(route):
                            if route.request.url.startswith(server.origin+'/'):route.continue_()
                            else:result['external_requests'].append('unexpected_nonlocal_request');route.abort()
                        context.route('**/*',local_only)
                        page=context.new_page();page.on('pageerror',lambda error:result['page_errors'].append(type(error).__name__))
                        page.goto(server.entry_url)
                        expect(page.locator('#public-source option')).to_have_count(3)
                        expect(page.locator('#public-source')).to_have_value(ANTHROPIC.source.key)
                        assert not calls
                        form=page.locator('#public-search');form.locator('[name=query]').fill('Architect')
                        page.locator('#public-source').select_option(CLOUDFLARE.source.key)
                        form.locator('[name=consent]').check();form.locator('button').click()
                        expect(page.locator('#report')).to_be_visible(timeout=20000)
                        expect(page.locator('#brief-capabilities')).to_contain_text('Cursor')
                        expect(page.locator('#public-status')).to_contain_text('Cloudflare')
                        first=server.public_tasks.snapshot()
                        assert first['returned_jobs']==20 and calls==[CLOUDFLARE.api_url]
                        assert workspace.report(first['report_id'])
                        result['checks'].append('three explicit sources, unchanged default and zero startup requests; selected Cloudflare catalog produces original first-page report')

                        # Editing the form does not turn the previous next-page
                        # cursor into the new company's source or another GET.
                        page.locator('#public-source').select_option(ANTHROPIC.source.key)
                        expect(page.locator('#public-next')).to_contain_text('Cloudflare')
                        expect(page.locator('#public-saved-query')).to_contain_text('Cloudflare')
                        page.locator('#public-next').click()
                        until(lambda:server.public_tasks.snapshot().get('id')!=first['id'] and server.public_tasks.snapshot().get('status')=='completed')
                        expect(page.locator('#public-next')).to_be_hidden(timeout=20000)
                        second=server.public_tasks.snapshot()
                        assert second['returned_jobs']==5 and second['cache_reused'] and calls==[CLOUDFLARE.api_url]
                        audit=json.loads((workspace.root/'reports'/second['report_id']/'public_source.json').read_text(encoding='utf-8'))
                        assert audit['source_scope']==[CLOUDFLARE.source.key]
                        result['checks'].append('next-page button and saved query retain Cloudflare after form switches; authenticated local cache yields remaining five and correct report audit with no extra GET')

                        now[0]+=31;form.locator('button').click()
                        until(lambda:server.public_tasks.snapshot().get('id')!=second['id'] and server.public_tasks.snapshot().get('status')=='completed')
                        expect(page.locator('#public-status')).to_contain_text('Anthropic',timeout=20000)
                        assert calls==[CLOUDFLARE.api_url,ANTHROPIC.api_url]
                        other=server.public_tasks.snapshot()
                        assert other['catalog_change']['status']=='baseline' and other['returned_jobs']==2
                        assert workspace.report(first['report_id']) and workspace.report(second['report_id'])
                        result['checks'].append('separate Anthropic selection keeps Cloudflare reports/cache and establishes its own change baseline')

                        page.locator('#public-source').select_option(CLOUDFLARE.source.key)
                        page.locator('#public-schedule > summary').click()
                        expect(page.locator('#schedule-save')).to_be_enabled()
                        page.locator('#schedule-consent').check();page.locator('#schedule-save').click()
                        expect(page.locator('#schedule-query')).to_contain_text('Cloudflare')
                        assert len(calls)==2
                        now[0]+=DAY;server.public_schedule._wake.set()
                        until(lambda:len(server.public_schedule.state()['history'])==1)
                        expect(page.locator('#schedule-history a')).to_be_visible(timeout=10000)
                        assert calls==[CLOUDFLARE.api_url,ANTHROPIC.api_url,CLOUDFLARE.api_url]
                        report=server.public_schedule.state()['history'][0]['report_id'];assert workspace.report(report)
                        page.locator('#schedule-history a').click()
                        expect(page).to_have_url(server.origin+'/#report='+report)
                        expect(page.locator('#report')).to_be_visible()
                        page.set_viewport_size({'width':390,'height':844})
                        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                        page.locator('#public-entry').screenshot(path=str(out/'public-sources-mobile.png'))
                        result['checks'].append('separate daily consent saves Cloudflare offline; injected 24-hour boundary refreshes only that source and opens original history report; 390px has no overflow')
                        assert not result['page_errors'] and not result['external_requests']
                        result.update(success=True,browser_version=browser.version,fixture_requests=len(calls),recruiting_requests=0)
                    finally:browser.close()
            finally:server.shutdown();server.server_close();thread.join(5)
    finally:
        (out/'results.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(result,ensure_ascii=False,indent=2))


if __name__=='__main__':main()
