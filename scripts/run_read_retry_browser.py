"""Actual Edge/Chromium UI and document failures; artificial upstream only."""
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
from urllib.parse import urlsplit

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'src'),str(ROOT/'tests')]
from test_guided import fixture_adapter
from vibe_job_radar.guided.adapters import Registry
from vibe_job_radar.guided.browser import PlaywrightBackend
from vibe_job_radar.guided.contracts import CrawlError
from vibe_job_radar.guided.rate import Limits, RateLedger
from vibe_job_radar.guided.service import GuidedService
from vibe_job_radar.guided.transport import WireResponse
from vibe_job_radar.workbench import LocalServer
from vibe_job_radar.workspace import Workspace

ORIGIN='https://jobs.fixture.test'


def main():
    from playwright.sync_api import sync_playwright, expect
    out=ROOT/'browser-acceptance'/'read-retry';out.mkdir(parents=True,exist_ok=True)
    result={'success':False,'checks':[],'source_requests':0,'page_errors':[],
        'scope':'Actual browser/UI and production bridge/controller; artificial responses and clock. No live platform or account.'}
    now=[1000.0];failures={'/job/2':1};calls=[];instances=[]
    class Wire:
        def __init__(self,adapter,ledger,cancelled,progress):
            self.ledger,self.cancelled,self.blocked=ledger,cancelled,set()
        def allowed_resource(self,url):return url.startswith(ORIGIN+'/')
        def reserve(self,kind):
            if self.cancelled.is_set():raise CrawlError('paused')
            self.ledger.reserve('fixture',kind)
        def ensure_robots(self,url):pass
        def fetch(self,url,method='GET',headers=None,body=None,*,required=True):
            if not self.allowed_resource(url) or method!='GET':raise CrawlError('fixture_unexpected_request')
            self.reserve('request');path=urlsplit(url).path;calls.append(path)
            if failures.get(path,0):
                failures[path]-=1
                return WireResponse(503,{'retry-after':'30'},b'artificial unavailable')
            if path=='/search':html='<h1>人工列表</h1><a href="/job/1">时间序列算法1</a><a href="/job/2">时间序列算法2</a>'
            elif path in ('/job/1','/job/2'):
                html='<h1>时间序列算法工程师</h1><div class="job-description">人工测试，非真实招聘。使用Cursor辅助编程、编写单元测试与代码审查，负责时间序列预测和模型评估。</div>'
            else:raise CrawlError('fixture_unexpected_request')
            return WireResponse(200,{'content-type':'text/html; charset=utf-8'},html.encode())
    class Browser(PlaywrightBackend):
        def __init__(self,*args,**kwargs):
            super().__init__(*args,headless=True,executable_path=os.environ.get('RADAR_TEST_CHROMIUM'),transport_factory=Wire,**kwargs)
            instances.append(self)
    with tempfile.TemporaryDirectory(prefix='radar-read-retry-') as tmp:
        workspace=Workspace(tmp);server=LocalServer(workspace)
        ledger=RateLedger(workspace.root/'retry-rates.sqlite',Limits(page_interval=0,request_interval=0),clock=lambda:now[0])
        service=GuidedService(workspace,registry=Registry([fixture_adapter()]),backend_factory=Browser,ledger=ledger)
        server.guided.close();server.guided=service
        thread=threading.Thread(target=server.serve_forever,kwargs={'poll_interval':.01},daemon=True);thread.start()
        try:
            with sync_playwright() as pw:
                options={'headless':True}
                if os.environ.get('RADAR_TEST_CHROMIUM'):options['executable_path']=os.environ['RADAR_TEST_CHROMIUM']
                browser=pw.chromium.launch(**options)
                try:
                    page=browser.new_page(viewport={'width':1320,'height':980})
                    page.on('pageerror',lambda e:result['page_errors'].append(str(e)))
                    page.route('**/*',lambda route:route.continue_() if route.request.url.startswith(server.origin+'/') else route.abort())
                    page.goto(server.entry_url)
                    def create():return service.create({'platform':'fixture','keyword':'时间序列算法','roles':['time_series'],
                        'max_pages':1,'max_jobs':2,'consent':True,'rights_note':'ARTIFICIAL BROWSER TEST ONLY'})['id']
                    def wait(ident,predicate):
                        deadline=time.monotonic()+30
                        while time.monotonic()<deadline:
                            state=service._load(ident)
                            if not service.state()['busy'] and predicate(state):return state
                            page.wait_for_timeout(30)
                        raise AssertionError('fixture state: '+state['code'])
                    ident=create();ready=wait(ident,lambda s:s['status']=='ready')
                    page.goto(server.origin+'/guided?task='+ident)
                    expect(page.locator('#task-status')).to_contain_text('可以选择岗位')
                    selected=[c['id'] for c in ready['cards']]
                    service.action({'id':ident,'action':'collect','selected':selected})
                    deferred=wait(ident,lambda s:s['code']=='read_retry_wait')
                    expect(page.locator('#task-status')).to_contain_text('1/2 次自动读页重试')
                    assert deferred['outcome']['saved']==1 and deferred['outcome']['pending']==1
                    old_report=workspace.root/'reports'/deferred['report_id']/'run_manifest.json'
                    before=hashlib.sha256(old_report.read_bytes()).hexdigest();old_calls=list(calls)
                    assert deferred['next_allowed_at']==1030
                    page.wait_for_timeout(250);assert calls==old_calls
                    now[0]=1031;finished=wait(ident,lambda s:s['status']=='completed')
                    assert all(c['status']=='ok' for c in finished['cards'])
                    assert calls.count('/job/1')==1 and calls.count('/job/2')==2 and len(instances)==1
                    assert hashlib.sha256(old_report.read_bytes()).hexdigest()==before
                    assert ledger.summary('fixture')['request']['day']==4
                    result['checks'].append('503 waits for Retry-After; same browser retries only interrupted JD, preserves first JD/report and counts failed request')
                    failures['/search']=9;ident2=create()
                    for used in (1,2):
                        waiting=wait(ident2,lambda s:s['code']=='read_retry_wait' and s['read_retry']['used']==used)
                        now[0]=waiting['next_allowed_at']+1
                    ended=wait(ident2,lambda s:s['code']=='read_retry_exhausted')
                    assert ended['read_retry']['used']==2 and not ended['auto_resume']
                    count=len(calls);page.wait_for_timeout(350);assert len(calls)==count
                    assert calls.count('/search')==4  # First successful batch plus exactly three failed reads.
                    assert hashlib.sha256(old_report.read_bytes()).hexdigest()==before
                    result['checks'].append('persistent 503 stops after initial GET plus two retries; no fourth attempt and previous report retained')
                    page.goto(server.origin+'/guided?task='+ident2)
                    expect(page.locator('#task-status')).to_contain_text('两次自动读页重试已用完')
                    page.set_viewport_size({'width':390,'height':844})
                    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                    assert not result['page_errors']
                    result.update(success=True,browser_version=browser.version)
                    result['checks'].append('actual UI shows retry count, exhaustion and mobile layout without script errors')
                finally:browser.close()
        finally:
            server.shutdown();server.server_close();thread.join(5)
            (out/'results.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(result,ensure_ascii=False,indent=2))


if __name__=='__main__':main()
