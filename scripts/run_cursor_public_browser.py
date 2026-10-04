"""Actual third-source UI and original reports with authored catalog fixtures."""
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
from test_public_ashby import cursor_payload
from vibe_job_radar.local_public import LocalPublicDataClient
from vibe_job_radar.public_boards import ANTHROPIC, CURSOR
from vibe_job_radar.public_schedule import DAY
from vibe_job_radar.workbench import LocalServer
from vibe_job_radar.workspace import Workspace
from run_public_sources_browser import until


def main():
    from playwright.sync_api import sync_playwright, expect
    out=ROOT/'browser-acceptance/cursor-public';out.mkdir(parents=True,exist_ok=True)
    result={'success':False,'checks':[],'page_errors':[],'external_requests':[],
        'scope':'Actual browser and original public pipeline; authored Ashby catalog and injected clock. No live source or market claims.'}
    clean={k:v for k,v in os.environ.items() if not k.startswith('VIBE_RADAR_')}
    try:
        with tempfile.TemporaryDirectory(prefix='radar-cursor-public-') as tmp, \
                patch.dict(os.environ,clean,clear=True),patch('urllib.request.getproxies',return_value={}):
            workspace=Workspace(tmp);now=[time.time()-DAY];calls=[]
            def response(url):
                assert url==CURSOR.api_url;calls.append(url)
                data=cursor_payload(50)
                data['jobs'].append({'isListed':False,'descriptionPlain':'unlisted artificial text'})
                return data
            wire=Mock();wire.json.side_effect=response
            server=LocalServer(workspace,public_client=LocalPublicDataClient(workspace,transport=wire,clock=lambda:now[0]))
            server.public_schedule.clock=lambda:now[0]
            thread=threading.Thread(target=server.serve_forever,kwargs={'poll_interval':.01},daemon=True);thread.start()
            try:
                with sync_playwright() as pw:
                    options={'headless':True}
                    if os.environ.get('RADAR_TEST_CHROMIUM'):options['executable_path']=os.environ['RADAR_TEST_CHROMIUM']
                    browser=pw.chromium.launch(**options)
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
                        form=page.locator('#public-search')
                        page.locator('#public-source').select_option(CURSOR.source.key)
                        form.locator('[name=query]').fill('Architect');form.locator('[name=region]').fill('New York')
                        assert not calls
                        form.locator('[name=consent]').check();form.locator('button').click()
                        expect(page.locator('#report')).to_be_visible(timeout=20000)
                        expect(page.locator('#public-status')).to_contain_text('Cursor')
                        first=server.public_tasks.snapshot()
                        assert first['status']=='completed' and first['available_jobs']==50 and first['returned_jobs']==20
                        report=workspace.report(first['report_id'])
                        assert report['manifest']['stats']['full_text_job_groups']==20
                        original=(workspace.root/'reports'/first['report_id']/'requirements.csv').read_bytes()
                        assert b'Cursor' in original and calls==[CURSOR.api_url]
                        assert 'unlisted artificial text' not in server.public_tasks.hybrid._paths(CURSOR)[0].read_text(encoding='utf-8')
                        result['checks'].append('explicit Cursor selection and secondary location produce 20 original full-JD groups; unlisted fixture excluded, no startup request')

                        page.locator('#public-source').select_option(ANTHROPIC.source.key)
                        expect(page.locator('#public-next')).to_contain_text('Cursor')
                        page.locator('#public-next').click()
                        until(lambda:server.public_tasks.snapshot().get('id')!=first['id'] and server.public_tasks.snapshot()['status']=='completed')
                        second=server.public_tasks.snapshot()
                        assert second['source_scope']==[CURSOR.source.key] and len(calls)==1
                        assert workspace.report(second['report_id'])['manifest']['stats']['full_text_job_groups']==20
                        assert (workspace.root/'reports'/first['report_id']/'requirements.csv').read_bytes()==original
                        result['checks'].append('form source edits do not change bound Cursor pagination; second page makes no GET and preserves the previous report')

                        page.locator('#public-source').select_option(CURSOR.source.key)
                        page.locator('#public-schedule summary').click()
                        page.locator('#schedule-consent').check();page.locator('#schedule-save').click()
                        expect(page.locator('#schedule-query')).to_contain_text('Cursor')
                        assert len(calls)==1
                        now[0]+=DAY+1;server.public_schedule._wake.set()
                        until(lambda:len(server.public_schedule.state()['history'])==1)
                        history=server.public_schedule.state()['history'][0]
                        assert history['status']=='completed' and calls==[CURSOR.api_url]*2
                        assert workspace.report(history['report_id'])['manifest']['stats']['full_text_job_groups']==20
                        page.locator('#schedule-history a').click()
                        expect(page.locator('#report')).to_be_visible()
                        # The history link opens the saved report as a new
                        # page, where the optional plan panel starts collapsed.
                        if not page.locator('#schedule-stop').is_visible():
                            page.locator('#public-schedule summary').click()
                        page.locator('#schedule-stop').click()
                        expect(page.locator('#schedule-status')).to_contain_text('未启用')
                        page.set_viewport_size({'width':390,'height':844})
                        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                        page.locator('#public-entry').screenshot(path=str(out/'cursor-public-mobile.png'))
                        result['checks'].append('explicit 24-hour plan refreshes Cursor only, opens original report and can be disabled; 390px without overflow')
                        assert not result['page_errors'] and not result['external_requests']
                        result.update(success=True,browser_version=browser.version,fixture_requests=len(calls),recruiting_requests=0)
                    finally:browser.close()
            finally:server.shutdown();server.server_close();thread.join(5)
    finally:
        (out/'results.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(result,ensure_ascii=False,indent=2))


if __name__=='__main__':main()
