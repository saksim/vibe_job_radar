"""Real browser regression for a new public query during an older report read.

Only artificial sources and temporary workspaces; no upstream transport allowed.
The old report is deliberately held, not a timing-dependent click or retry.
"""
from pathlib import Path
from datetime import datetime,timezone
import contextlib,hashlib,json,os,sys,tempfile,threading
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'src'))
from playwright.sync_api import sync_playwright,expect
from vibe_job_radar.workbench import LocalServer
from vibe_job_radar.workspace import Workspace
from vibe_job_radar.network import SafeHTTP
board={'jobs':[{'id':880000+i,'absolute_url':f'https://job-boards.greenhouse.io/anthropic/jobs/{880000+i}','title':f'Architect FIXTURE {i}','location':{'name':'TEST ONLY'},'content':'<p>ARTIFICIAL PUBLIC POLLING FIXTURE, NOT MARKET DATA.</p><p>Use Cursor for AI-assisted coding, review generated code, write comprehensive tests and design dependable software. This controlled text is not a vacancy.</p>'} for i in range(21)],'meta':{'total':21}}

def journey(label, old_report_error):
    folder=ROOT/'browser-acceptance'/'public-poll-handoff';folder.mkdir(parents=True,exist_ok=True)
    OUT=folder/(label+'.json')
    value={'created_at':datetime.now(timezone.utc).isoformat(),'scope':'Real browser, production local server/store/pipeline; delayed first report and gated second cached query. Artificial source only.','page_errors':[],'external_requests':[]};gate=threading.Event();entered=threading.Event()
    with tempfile.TemporaryDirectory(prefix='poll-handoff-') as tmp:
     server=LocalServer(Workspace(tmp));original=server.public_tasks.hybrid.search
     def search(query,**kwargs):
      if query.cursor:
       entered.set();assert gate.wait(15),'fixture gate timed out'
      return original(query,**kwargs)
     thread=threading.Thread(target=server.serve_forever,kwargs={'poll_interval':.01},daemon=True);thread.start()
     try:
      with contextlib.ExitStack() as stack,sync_playwright() as pw:
       source=stack.enter_context(patch.object(SafeHTTP,'json',return_value=board))
       outgoing=stack.enter_context(patch.object(SafeHTTP,'request',side_effect=AssertionError('fixture refuses real upstream transport')))
       conditional=None
       if hasattr(SafeHTTP,'conditional_json'):
        from vibe_job_radar.network import JSONRepresentation
        conditional=stack.enter_context(patch.object(SafeHTTP,'conditional_json',return_value=JSONRepresentation(200,board,None)))
       stack.enter_context(patch.object(server.public_tasks.hybrid,'search',side_effect=search))
       browser=pw.chromium.launch(headless=True,executable_path=os.environ.get('RADAR_TEST_CHROMIUM'));value['browser_version']=browser.version
       context=browser.new_context();held=[];report_urls=[];local_state_requests=[]
       def route(r):
        url=r.request.url
        if not url.startswith(server.origin+'/'):
         value['external_requests'].append(url);r.abort();return
        if '/api/public/state' in url:local_state_requests.append(url)
        if '/api/report/' in url:
         report_urls.append(url)
         if len(report_urls)==1:held.append(r);return
        r.continue_()
       context.route('**/*',route);page=context.new_page();page.on('pageerror',lambda e:value['page_errors'].append(str(e)));page.goto(server.entry_url)
       expect(page.locator('#public-search-button')).to_be_enabled();page.locator('#public-search [name=query]').fill('Architect');page.locator('#public-search [name=consent]').check()
       with page.expect_request(lambda r:'/api/report/' in r.url,timeout=15000):page.locator('#public-search-button').click()
       expect(page.locator('#public-status')).to_contain_text('本页 20 条');assert len(held)==1
       first=server.public_tasks.state()['task'];manifest=server.workspace.root/'reports'/first['report_id']/'run_manifest.json';first_hash=hashlib.sha256(manifest.read_bytes()).hexdigest()
       with page.expect_response(lambda r:r.url.endswith('/api/public/search') and r.request.method=='POST') as submission:page.locator('#public-next').click()
       assert submission.value.status==200 and entered.wait(3);expect(page.locator('#public-status')).to_contain_text('正在获取公开数据')
       gate.set();server.public_tasks._thread.join(10);second=server.public_tasks.state()['task'];assert second['status']=='completed' and second['report_id']!=first['report_id'] and second['returned_jobs']==1
       if old_report_error:held[0].fulfill(status=404,content_type='application/json',body=json.dumps({'error':'Artificial old report failure'}))
       else:held[0].continue_()
       try:
        expect(page.locator('#public-status')).to_contain_text('本页 1 条',timeout=5000)
        expect(page.locator('#report-title')).to_contain_text(second['report_id'][:8],timeout=5000)
       except AssertionError as exc:value['observation_error']=str(exc)

       status=page.locator('#public-status').inner_text();shown=page.locator('#report-title').inner_text();current=second['report_id'][:8] in shown
       source_count=source.call_count+(conditional.call_count if conditional else 0)
       assert outgoing.call_count==0 and source_count==1 and hashlib.sha256(manifest.read_bytes()).hexdigest()==first_hash and not value['page_errors'] and not value['external_requests']
       value.update(backend_completed=True,first_report_preserved=True,source_calls=source_count,blocked_transport_calls=outgoing.call_count,first_report_id=first['report_id'],second_report_id=second['report_id'],status_text=status,displayed_report_title=shown,current_report_shown=current,second_page_status_shown='本页 1 条' in status,lost_status_refresh=not current and '本页 1 条' not in status,local_state_requests=len(local_state_requests),report_requests=len(report_urls))
       browser.close()
     finally:
      gate.set();server.shutdown();server.server_close();thread.join(5)
    value.update(scenario=label,old_report_error=old_report_error,success=value['current_report_shown'] and value['second_page_status_shown'] and not value.get('observation_error'))
    OUT.write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf-8');return value


def main():
    rows=[]
    for label,old_report_error in [('delayed-report',False),('failed-old-report',True)]:
        try:rows.append(journey(label,old_report_error))
        except Exception as exc:rows.append({'scenario':label,'success':False,'error':str(exc),'error_type':type(exc).__name__})
    result={'success':all(r['success'] for r in rows),'checks':rows,
            'scope':'Actual local UI, worker, cache and original report pipeline. Artificial delayed/failed old report, source calls one per scenario, no real upstream transport. No workflow reruns or enlarged wait.'}
    folder=ROOT/'browser-acceptance'/'public-poll-handoff';folder.mkdir(parents=True,exist_ok=True)
    (folder/'results.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(result,ensure_ascii=True,indent=2))
    if not result['success']:raise SystemExit(1)


if __name__=='__main__':main()
