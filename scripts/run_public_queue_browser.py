"""Real UI, original tasks/reports and independent worker; authored source only."""
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
from vibe_job_radar.public_queue import GAP,PublicQueue
from vibe_job_radar.public_tasks import PublicTasks
from vibe_job_radar.workbench import LocalServer
from vibe_job_radar.workspace import Workspace


def main():
    from playwright.sync_api import sync_playwright,expect
    out=ROOT/'browser-acceptance/public-queue';out.mkdir(parents=True,exist_ok=True)
    result={'success':False,'checks':[],'page_errors':[],'external_requests':[],
        'scope':'Actual local UI, tasks, reports and second Python process. Authored catalog; injected time and controlled first-server queue ticks. No live-site or 24-hour certification.'}
    clean={k:v for k,v in os.environ.items() if not k.startswith('VIBE_RADAR_')}
    with tempfile.TemporaryDirectory(prefix='radar-queue-ui-') as temp,patch.dict(os.environ,clean,clear=True),\
            patch('urllib.request.getproxies',return_value={}):
        root=Path(temp);workspace=Workspace(root/'interactive');now=[time.time()]
        wire=Mock();wire.json.return_value=payload(50);server=None;thread=None;child=None
        def launch(*,controlled=True):
            nonlocal server,thread
            server=LocalServer(workspace,public_client=LocalPublicDataClient(workspace,transport=wire,clock=lambda:now[0]))
            if controlled:
                server.public_queue.clock=lambda:now[0]
                server.public_queue.start=lambda:None
            thread=threading.Thread(target=server.serve_forever,kwargs={'poll_interval':.01},daemon=True);thread.start()
        def stop_server():
            nonlocal server,thread
            if server:
                server.shutdown();server.server_close();thread.join(5);server=thread=None
        def completed_tick():
            server.public_queue.tick()
            wait_for(lambda:not server.public_tasks.is_running())
            assert server.public_tasks.snapshot()['status']=='completed'
            task=server.public_tasks.snapshot()
            server.public_queue.tick()
            return task
        try:
            launch()
            with sync_playwright() as pw:
                options={'headless':True}
                if os.environ.get('RADAR_TEST_CHROMIUM'):options['executable_path']=os.environ['RADAR_TEST_CHROMIUM']
                browser=pw.chromium.launch(**options)
                try:
                    result['browser_version']=browser.version
                    def open_context():
                        context=browser.new_context(viewport={'width':1280,'height':1000});origin=server.origin
                        def local_only(route):
                            if route.request.url.startswith(origin+'/'):route.continue_()
                            else:result['external_requests'].append('unexpected_nonlocal');route.abort()
                        context.route('**/*',local_only)
                        return context
                    def open_page(context):
                        page=context.new_page();page.on('pageerror',lambda exc:result['page_errors'].append(type(exc).__name__))
                        page.goto(server.entry_url);expect(page.locator('#queue-add')).to_be_enabled()
                        expect(page.locator('#public-source option')).to_have_count(3)
                        page.locator('#public-queue > summary').click()
                        return page
                    def add(page,word):
                        page.locator('#public-search [name=query]').fill(word)
                        page.locator('#queue-add-consent').check()
                        with page.expect_response(lambda r:r.url.endswith('/queue/enqueue')) as response:page.locator('#queue-add').click()
                        assert response.value.status==200
                        expect(page.locator('#queue-add-consent')).not_to_be_checked()
                    context=open_context();page=open_page(context);stale=open_page(context)
                    result['stage']='consent'
                    assert not server.public_queue.path.exists() and wire.json.call_count==0
                    old=server.public_queue.state();stale.route('**/api/public/queue/state',lambda route:route.fulfill(json=old))
                    page.locator('#public-search [name=query]').fill('Architect');page.locator('#queue-add').click()
                    expect(page.locator('#queue-message')).to_contain_text('勾选');assert not server.public_queue.path.exists()
                    page.locator('#queue-add-consent').check();page.locator('#public-search [name=region]').fill('London')
                    expect(page.locator('#queue-add-consent')).not_to_be_checked()
                    page.locator('#queue-add-consent').check();page.locator('#public-source').select_option('greenhouse_cloudflare')
                    expect(page.locator('#queue-add-consent')).not_to_be_checked()
                    page.locator('#public-source').select_option('greenhouse_anthropic');page.locator('#public-search [name=region]').fill('')
                    add(page,'Architect');expect(page.locator('#queue-items li')).to_have_count(1)
                    stale.locator('#public-search [name=query]').fill('Engineer');stale.locator('#queue-add-consent').check()
                    with stale.expect_response(lambda r:r.url.endswith('/queue/enqueue')) as response:stale.locator('#queue-add').click()
                    assert response.value.status==400;stale.close()
                    assert len(server.public_queue.state()['items'])==1 and wire.json.call_count==0
                    result['checks'].append('empty startup and status stay offline; separate one-shot consent; region/source changes revoke consent; stale-page revision rejected')

                    result['stage']='pause_remove_resume'
                    add(page,'Engineer');add(page,'Temporary artificial query');expect(page.locator('#queue-items li')).to_have_count(3)
                    page.locator('#queue-items li').last.get_by_role('button',name='移除').click()
                    expect(page.locator('#queue-items li')).to_have_count(2)
                    page.locator('#queue-pause').click();expect(page.locator('#queue-resume')).to_be_enabled()
                    page.locator('#queue-resume').click();expect(page.locator('#queue-message')).to_contain_text('勾选')
                    page.locator('#queue-resume-consent').check();page.locator('#public-search [name=query]').fill('Architect')
                    expect(page.locator('#queue-resume-consent')).not_to_be_checked()
                    page.locator('#queue-resume-consent').check();page.locator('#queue-resume').click()
                    expect(page.locator('#queue-status')).to_contain_text('按顺序');assert wire.json.call_count==0
                    result['checks'].append('pending removal and pause are explicit; resuming requires new consent bound to the displayed remaining queue')

                    result['stage']='manual_busy_and_fifo'
                    entered,release=threading.Event(),threading.Event()
                    def hold(url):
                        entered.set()
                        if not release.wait(15):raise AssertionError('manual fixture not released')
                        return payload(50)
                    wire.json.side_effect=hold
                    try:
                        page.locator('#public-search [name=consent]').check();page.locator('#public-search-button').click()
                        assert entered.wait(10);manual=server.public_tasks.snapshot()['id'];server.public_queue.tick()
                        assert server.public_tasks.snapshot()['id']==manual
                        assert all(row['phase']=='pending' for row in server.public_queue.state()['items'])
                    finally:release.set()
                    wait_for(lambda:not server.public_tasks.is_running());manual_report=server.public_tasks.snapshot()['report_id']
                    first_task=completed_tick();first=server.public_queue.state()['history'][0]['report_id']
                    server.public_queue.tick();assert len(server.public_queue.state()['history'])==1
                    now[0]+=GAP;second_task=completed_tick();history=server.public_queue.state()['history']
                    assert [row['query']['query'] for row in history]==['Architect','Engineer']
                    assert len({row['report_id'] for row in history})==2 and wire.json.call_count==1
                    assert first_task['returned_jobs']==second_task['returned_jobs']==20
                    selected=[workspace.report(row['report_id'])['manifest']['stats']['selected_source_records'] for row in history]
                    assert selected==[20,0]  # Generic Engineer is outside the existing report role taxonomy.
                    result['returned_jobs']=[20,20];result['report_selected_records']=selected
                    assert workspace.report(manual_report)
                    expect(page.locator('#queue-history a')).to_have_count(2,timeout=10000)
                    result['checks'].append('busy manual query is preserved; two FIFO entries run through original cache/tasks/reports with a 30-second injected gap, first 20 only and no extra source request')

                    result['stage']='report_links_and_mobile'
                    page.locator('#queue-history a').last.click();expect(page).to_have_url(server.origin+'/#report='+first)
                    expect(page.locator('#report')).to_be_visible();page.locator('#public-queue > summary').click()
                    page.set_viewport_size({'width':390,'height':844});assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                    page.locator('#public-queue').screenshot(path=str(out/'queue-mobile.png'))
                    result['checks'].append('explicit queue history link opens the original report; 390px layout fits without horizontal overflow')
                    context.close();stop_server()

                    result['stage']='independent_worker'
                    workspace=Workspace(root/'independent');now[0]=time.time();launch()
                    context=open_context();page=open_page(context);add(page,'Architect')
                    assert not server.public_tasks.is_running();context.close();stop_server()
                    child=subprocess.Popen([sys.executable,str(ROOT/'tests/test_public_worker.py'),'--fixture-worker',
                        str(workspace.root),'queue-ui','normal'],cwd=ROOT,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
                        text=True,encoding='utf-8',env={**os.environ,'PYTHONUTF8':'1'},creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
                    observer_tasks=PublicTasks(workspace,hybrid_client=LocalPublicDataClient(workspace,transport=wire))
                    try:
                        queue=PublicQueue(workspace,observer_tasks)
                        state=wait_for(lambda:(s if (s:=queue.state())['history'] else None));worker_report=state['history'][0]['report_id']
                        assert workspace.report(worker_report)['manifest']['stats']['selected_source_records']==2
                    finally:observer_tasks.close()
                    assert (workspace.root/'fixture-requests.txt').read_text(encoding='utf-8').splitlines()==['request']
                    launch(controlled=False);context=open_context();page=open_page(context)
                    expect(page.locator('#queue-status')).to_contain_text('另一个本机进程',timeout=10000)
                    expect(page.locator('#queue-status')).to_contain_text('没有待运行')
                    page.locator('#queue-history a').click();expect(page).to_have_url(server.origin+'/#report='+worker_report)
                    expect(page.locator('#report')).to_be_visible();context.close()
                    (workspace.root/'stop-queue-ui').write_text('stop',encoding='utf-8')
                    stdout,stderr=child.communicate(timeout=30)
                    assert child.returncode==0 and not stderr and 'worker_stopped' in stdout and 'Architect' not in stdout
                    assert not (workspace.root/'public_schedule').exists() and wire.json.call_count==1
                    result['checks'].append('UI confirms queue then entire web server closes; real independent process with bind forbidden runs once; new workbench reads original report and truthful foreign-owner/empty status; worker exits cleanly')
                    assert not result['page_errors'] and not result['external_requests']
                    result.update(success=True,stage='verified',interactive_fixture_requests=wire.json.call_count,independent_fixture_requests=1)
                finally:browser.close()
        finally:
            stop_server()
            if child and child.poll() is None:
                (workspace.root/'stop-queue-ui').write_text('stop',encoding='utf-8')
                try:child.communicate(timeout=30)
                except subprocess.TimeoutExpired:child.kill();child.communicate(timeout=5)
            (out/'results.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(result,ensure_ascii=True,indent=2))


if __name__=='__main__':main()
