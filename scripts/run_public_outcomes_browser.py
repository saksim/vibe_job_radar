"""Real UI: manual query after scheduled completion keeps the original history."""
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
from test_local_public import payload
from vibe_job_radar.local_public import LocalPublicDataClient
from vibe_job_radar.public_schedule import DAY
from vibe_job_radar.workspace import Workspace
from vibe_job_radar.workbench import LocalServer
from run_public_sources_browser import until


def main():
    from playwright.sync_api import sync_playwright,expect
    out=ROOT/'browser-acceptance/public-outcomes';out.mkdir(parents=True,exist_ok=True)
    result={'success':False,'checks':[],'page_errors':[],'external_requests':[],
        'scope':'Actual browser and original tasks/reports, artificial source, controlled clock/tick. No live source or long-running certification.'}
    clean={k:v for k,v in os.environ.items() if not k.startswith('VIBE_RADAR_')}
    try:
        with tempfile.TemporaryDirectory(prefix='radar-public-outcomes-') as temp, \
                patch.dict(os.environ,clean,clear=True),patch('urllib.request.getproxies',return_value={}):
            workspace=Workspace(temp);now=[time.time()-DAY];wire=Mock();wire.json.return_value=payload()
            server=LocalServer(workspace,public_client=LocalPublicDataClient(workspace,transport=wire,clock=lambda:now[0]))
            server.public_schedule.close();server.public_schedule._stop.clear();server.public_schedule.clock=lambda:now[0]
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
                            else:result['external_requests'].append('unexpected_nonlocal');route.abort()
                        context.route('**/*',local_only)
                        page=context.new_page();page.on('pageerror',lambda error:result['page_errors'].append(type(error).__name__))
                        page.goto(server.entry_url)
                        form=page.locator('#public-search');form.locator('[name=query]').fill('Architect')
                        page.locator('#public-schedule summary').click();page.locator('#schedule-consent').check()
                        page.locator('#schedule-save').click();expect(page.locator('#schedule-query')).to_contain_text('Architect')
                        assert wire.json.call_count==0
                        now[0]+=DAY;server.public_schedule.tick()
                        until(lambda:server.public_tasks.snapshot()['status']=='completed')
                        original=server.public_tasks.snapshot()
                        csv=workspace.root/'reports'/original['report_id']/'requirements.csv';before=csv.read_bytes()
                        assert server.public_schedule.state()['status']=='running'

                        form.locator('[name=query]').fill('Engineer');form.locator('[name=consent]').check()
                        form.locator('button').click()
                        until(lambda:server.public_tasks.snapshot().get('id')!=original['id'] and server.public_tasks.snapshot()['status']=='completed')
                        manual=server.public_tasks.snapshot()
                        assert manual['report_id']!=original['report_id'] and wire.json.call_count==1
                        server.public_schedule.tick();state=server.public_schedule.state()
                        assert state['status']=='scheduled' and state['history'][0]['report_id']==original['report_id']
                        assert server.public_tasks.snapshot()['id']==manual['id'] and csv.read_bytes()==before
                        result['checks'].append('scheduled query completes; manual cached query replaces current slot; original receipt keeps plan scheduled and points to original report without cancelling manual work')
                        expect(page.locator('#schedule-history a')).to_be_visible()
                        page.locator('#schedule-history a').click();expect(page.locator('#report')).to_be_visible()
                        assert 'report='+original['report_id'] in page.url
                        assert workspace.report(manual['report_id']) and csv.read_bytes()==before
                        page.locator('#public-schedule summary').click()
                        page.set_viewport_size({'width':390,'height':844})
                        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                        page.locator('#public-schedule').screenshot(path=str(out/'public-outcomes-mobile.png'))
                        page.locator('#schedule-stop').click();expect(page.locator('#schedule-status')).to_contain_text('未启用')
                        result['checks'].append('history opens the scheduled original report while manual report remains; 390px fits and explicit plan disable still works')
                        assert not result['page_errors'] and not result['external_requests']
                        result.update(success=True,browser_version=browser.version,fixture_requests=wire.json.call_count)
                    finally:browser.close()
            finally:server.shutdown();server.server_close();thread.join(5)
    finally:(out/'results.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(result,ensure_ascii=False,indent=2))


if __name__=='__main__':main()
