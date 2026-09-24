"""Actual UI -> independent process -> original report -> UI stop; authored source."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
from unittest.mock import Mock,patch

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'src'),str(ROOT/'tests')]
from test_local_public import payload
from test_public_worker import wait_for
from vibe_job_radar.local_public import LocalPublicDataClient
from vibe_job_radar.public_schedule import DAY,PublicSchedule
from vibe_job_radar.public_tasks import PublicTasks
from vibe_job_radar.workbench import LocalServer
from vibe_job_radar.workspace import Workspace


def main():
    from playwright.sync_api import sync_playwright,expect
    out=ROOT/'browser-acceptance/public-worker';out.mkdir(parents=True,exist_ok=True)
    result={'success':False,'checks':[],'page_errors':[],'external_requests':[],
        'scope':'Actual UI and independent Python process; only an authored artificial catalog and injected initial schedule clock, no real source or OS service registration.'}
    clean={k:v for k,v in os.environ.items() if not k.startswith('VIBE_RADAR_')}
    with tempfile.TemporaryDirectory(prefix='radar-worker-ui-') as temp,patch.dict(os.environ,clean,clear=True),\
            patch('urllib.request.getproxies',return_value={}):
        root=Path(temp);workspace=Workspace(root);server=None;thread=None;child=None
        wire=Mock();wire.json.return_value=payload()
        def start_server(*,old_clock=False):
            value=LocalServer(workspace,public_client=LocalPublicDataClient(workspace,transport=wire))
            if old_clock:
                initial=time.time()-DAY-10
                value.public_schedule.clock=lambda:initial
            task=threading.Thread(target=value.serve_forever,kwargs={'poll_interval':.01},daemon=True);task.start()
            return value,task
        def stop_server():
            nonlocal server,thread
            if server:
                server.shutdown();server.server_close();thread.join(5)
                server=thread=None
        try:
            server,thread=start_server(old_clock=True)
            with sync_playwright() as pw:
                options={'headless':True}
                if os.environ.get('RADAR_TEST_CHROMIUM'):options['executable_path']=os.environ['RADAR_TEST_CHROMIUM']
                browser=pw.chromium.launch(**options)
                try:
                    result['browser_version']=browser.version
                    def page_for_current_server():
                        context=browser.new_context(viewport={'width':1280,'height':900})
                        origin=server.origin
                        def local_only(route):
                            if route.request.url.startswith(origin+'/'):route.continue_()
                            else:result['external_requests'].append('unexpected_nonlocal');route.abort()
                        context.route('**/*',local_only)
                        page=context.new_page();page.on('pageerror',lambda exc:result['page_errors'].append(type(exc).__name__))
                        page.goto(server.entry_url)
                        return context,page
                    context,page=page_for_current_server()
                    expect(page.locator('#schedule-save')).to_be_enabled()
                    page.locator('#public-search [name=query]').fill('Architect')
                    page.locator('#public-schedule > summary').click()
                    page.locator('#schedule-consent').check();page.locator('#schedule-save').click()
                    expect(page.locator('#schedule-query')).to_contain_text('Architect')
                    expect(page.locator('#schedule-status')).to_contain_text('已保存')
                    assert server.public_schedule.state()['status']=='scheduled' and wire.json.call_count==0
                    context.close();stop_server()
                    result['checks'].append('real UI explicitly confirms a 24-hour plan without source requests; original web server is completely stopped before worker launch')

                    env={**os.environ,'PYTHONUTF8':'1'}
                    child=subprocess.Popen([sys.executable,str(ROOT/'tests/test_public_worker.py'),
                        '--fixture-worker',str(root),'ui','normal'],stdout=subprocess.PIPE,stderr=subprocess.PIPE,
                        text=True,encoding='utf-8',env=env)
                    wait_for(lambda:(root/'ready-ui').exists())
                    tasks=PublicTasks(workspace,hybrid_client=LocalPublicDataClient(workspace,transport=wire))
                    schedule=PublicSchedule(workspace,tasks)
                    try:
                        state=wait_for(lambda:(s if (s:=schedule.state())['history'] else None))
                        report_id=state['history'][0]['report_id']
                        report=workspace.report(report_id)
                        assert report['manifest']['stats']['full_text_job_groups']==1
                        assert report['manifest']['stats']['selected_source_records']==2
                    finally:tasks.close()
                    assert (root/'fixture-requests.txt').read_text(encoding='utf-8').splitlines()==['request']
                    result['checks'].append('independent process with socket.bind forbidden performs one artificial due query and creates the original complete-JD report without a web server')

                    server,thread=start_server()
                    context,page=page_for_current_server()
                    page.locator('#public-schedule > summary').click()
                    expect(page.locator('#schedule-history a')).to_have_count(1)
                    page.locator('#schedule-history a').click()
                    expect(page.locator('#report')).to_be_visible();expect(page.locator('#brief-capabilities')).to_contain_text('Cursor')
                    page.locator('#public-schedule > summary').click()
                    expect(page.locator('#schedule-status')).to_contain_text('另一个本机进程',timeout=12000)
                    page.locator('#schedule-stop').click()
                    expect(page.locator('#schedule-stop')).to_be_disabled()
                    expect(page.locator('#schedule-status')).to_contain_text('计划未启用')
                    assert server.public_schedule.state()['status']=='disabled'
                    assert workspace.report(report_id)['requirements']==report['requirements']
                    page.set_viewport_size({'width':390,'height':844})
                    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                    page.locator('#public-schedule').screenshot(path=str(out/'public-worker-mobile.png'))
                    context.close()
                    (root/'stop-ui').write_text('stop',encoding='utf-8')
                    stdout,stderr=child.communicate(timeout=30)
                    assert child.returncode==0 and not stderr
                    assert 'worker_stopped' in stdout and 'Architect' not in stdout and 'Cursor' not in stdout
                    assert wire.json.call_count==0
                    assert (root/'fixture-requests.txt').read_text(encoding='utf-8').splitlines()==['request']
                    result['checks'].append('new actual UI reads the same report and foreign ownership, disables the plan without hiding its disabled state, preserves report evidence and fits 390px; independent worker stops cleanly')
                    assert not result['page_errors'] and not result['external_requests']
                    result.update(success=True,fixture_requests=1,source_requests=0)
                finally:browser.close()
        finally:
            stop_server()
            if child:
                (root/'stop-ui').write_text('stop',encoding='utf-8')
                try:child.communicate(timeout=30)
                except subprocess.TimeoutExpired:child.kill();child.communicate(timeout=5)
            (out/'results.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(result,ensure_ascii=True,indent=2))


if __name__=='__main__':main()
