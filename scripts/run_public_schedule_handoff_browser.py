
"""Real browser: a pre-mutation schedule read must not undo a confirmed stop."""
from pathlib import Path
import json,os,sys,tempfile,threading
from unittest.mock import Mock,patch
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'src'),str(ROOT/'tests')]
from playwright.sync_api import sync_playwright,expect
from vibe_job_radar.workbench import LocalServer
from vibe_job_radar.workspace import Workspace
from vibe_job_radar.local_public import LocalPublicDataClient

TRACK=r"""
(() => {
 const original = Response.prototype.json;
 window.scheduleReadsCompleted = 0;
 Response.prototype.json = async function(...args) {
  try { return await original.apply(this, args); }
  finally { if (this.url.includes('/api/public/schedule/state')) window.scheduleReadsCompleted++; }
 };
})();
"""

def journey(error):
 result={'success':False,'scenario':'failed-old-state' if error else 'delayed-old-state',
         'page_errors':[],'external_requests':[]}
 transport=Mock()
 with tempfile.TemporaryDirectory(prefix='schedule-handoff-') as tmp,patch('urllib.request.getproxies',return_value={}):
  w=Workspace(tmp);server=LocalServer(w,public_client=LocalPublicDataClient(w,transport=transport))
  thread=threading.Thread(target=server.serve_forever,kwargs={'poll_interval':.01},daemon=True);thread.start()
  try:
   with sync_playwright() as pw:
    browser=pw.chromium.launch(headless=True,executable_path=os.environ.get('RADAR_TEST_CHROMIUM'))
    try:
     result['browser_version']=browser.version
     context=browser.new_context();context.add_init_script(TRACK)
     def local(route):
      if route.request.url.startswith(server.origin+'/'):route.continue_()
      else:result['external_requests'].append(route.request.url);route.abort()
     context.route('**/*',local)
     page=context.new_page();page.on('pageerror',lambda e:result['page_errors'].append(str(e)))
     page.goto(server.entry_url);expect(page.locator('#schedule-save')).to_be_enabled()
     page.locator('#public-schedule > summary').click()
     page.locator('#public-search [name=query]').fill('Architect')
     page.locator('#schedule-consent').check()
     with page.expect_response(lambda r:r.url.endswith('/schedule/configure')) as saved:
      page.locator('#schedule-save').click()
     assert saved.value.status==200
     expect(page.locator('#schedule-status')).to_contain_text('已保存')
     expect(page.locator('#schedule-stop')).to_be_enabled()
     original=server.public_schedule.state();held=[];armed=[True]
     def hold(route):
      if armed[0]:armed[0]=False;held.append(route)
      else:route.continue_()
     page.route('**/api/public/schedule/state',hold)
     with page.expect_request(lambda r:r.url.endswith('/schedule/state'),timeout=8000):pass
     expect(page.locator('#schedule-stop')).to_be_enabled()
     assert len(held)==1
     with page.expect_response(lambda r:r.url.endswith('/schedule/disable')) as stopped:
      page.locator('#schedule-stop').click()
     assert stopped.value.status==200
     expect(page.locator('#schedule-status')).to_contain_text('未启用')
     expect(page.locator('#schedule-save')).to_be_enabled()
     expect(page.locator('#schedule-stop')).to_be_disabled()
     stopped_state=server.public_schedule.state()
     assert stopped_state['status']=='disabled' and stopped_state['revision']>original['revision']
     completed=page.evaluate('window.scheduleReadsCompleted')
     held[0].fulfill(status=503 if error else 200,content_type='application/json',
                     body=json.dumps({'error':'ARTIFICIAL delayed old state failure'} if error else original))
     page.wait_for_function('(n) => window.scheduleReadsCompleted > n',arg=completed,timeout=3000)
     page.evaluate('() => new Promise(resolve => requestAnimationFrame(resolve))')
     status=page.locator('#schedule-status').inner_text()
     result.update(status_text=status,save_enabled=page.locator('#schedule-save').is_enabled(),
       stop_disabled=page.locator('#schedule-stop').is_disabled(),backend_disabled=server.public_schedule.state()['status']=='disabled',
       backend_revision_unchanged=server.public_schedule.state()['revision']==stopped_state['revision'],
       source_requests=transport.json.call_count)
     result['success']=('未启用' in status and result['save_enabled'] and result['stop_disabled']
          and result['backend_disabled'] and result['backend_revision_unchanged']
          and not result['source_requests'] and not result['page_errors'] and not result['external_requests'])
     page.screenshot(path=str(OUT/(result['scenario']+'.png')),full_page=True)
    finally:browser.close()
  finally:server.shutdown();server.server_close();thread.join(5)
 return result

OUT=ROOT/'browser-acceptance'/'public-schedule-handoff'
def main():
 OUT.mkdir(parents=True,exist_ok=True);rows=[]
 for error in (False,True):
  try:rows.append(journey(error))
  except Exception as exc:rows.append({'success':False,'error':str(exc),'error_type':type(exc).__name__})
 result={'success':all(r['success'] for r in rows),'checks':rows,
         'scope':'Actual local browser and schedule APIs, held pre-stop state response success/error; no external source, real24h, or account claim.'}
 (OUT/'results.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
 print(json.dumps(result,ensure_ascii=False,indent=2))
 if not result['success']:raise SystemExit(1)
if __name__=='__main__':main()
