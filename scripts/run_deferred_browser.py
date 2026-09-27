"""Actual Chromium lifecycle with artificial jobs; no source/account requests."""
from dataclasses import replace
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
sys.path.insert(0,str(ROOT/'src'))
from vibe_job_radar.guided.adapters import builtins, Registry
from vibe_job_radar.guided.browser import PlaywrightBackend
from vibe_job_radar.guided.contracts import CrawlError
from vibe_job_radar.guided.rate import Limits, RateLedger, RateLimit
from vibe_job_radar.guided.service import GuidedService
from vibe_job_radar.guided.transport import WireResponse
from vibe_job_radar.workspace import Workspace

ORIGIN='https://jobs.fixture.test'


def main():
    out=ROOT/'browser-acceptance'/'deferred-browser';out.mkdir(parents=True,exist_ok=True)
    result={'success':False,'checks':[],'scope':'Real Chromium lifecycle; artificial supplier and quota clock; no live account or source requests.'}
    now=[1000.0];close_window=threading.Event();instances=[];calls=[];versions=[]
    class Wire:
        def __init__(self,adapter,ledger,cancelled,progress):
            self.ledger,self.cancelled,self.blocked=ledger,cancelled,set()
        def allowed_resource(self,url):return url.startswith(ORIGIN+'/')
        def reserve(self,kind):
            if self.cancelled.is_set():raise CrawlError('paused')
            self.ledger.reserve('liepin',kind)
        def ensure_robots(self,url):pass  # Fixed artificial source; real denial paths have separate tests.
        def fetch(self,url,method='GET',headers=None,body=None,*,required=True):
            if not self.allowed_resource(url) or method!='GET':raise CrawlError('fixture_unexpected_request')
            self.reserve('request');path=urlsplit(url).path;calls.append(path)
            if path=='/zhaopin/':
                html='<h1>人工列表</h1><a href="/job/1.shtml">时间序列算法工程师1</a><a href="/job/2.shtml">时间序列算法工程师2</a>'
            elif path in {'/job/1.shtml','/job/2.shtml'}:
                html='<h1>时间序列算法工程师</h1><div class="job-description">人工测试岗位，不是真实招聘。使用Cursor辅助编程、编写单元测试和审查代码，负责时间序列预测和模型评估。</div>'
            else:raise CrawlError('fixture_unexpected_request')
            return WireResponse(200,{'content-type':'text/html; charset=utf-8'},html.encode())
    class ClosableBrowser(PlaywrightBackend):
        def __init__(self,*args,**kwargs):
            super().__init__(*args,headless=True,executable_path=os.environ.get('RADAR_TEST_CHROMIUM'),
                             transport_factory=Wire,**kwargs)
            instances.append(self);versions.append(self.browser.version)
        def open(self,url,**kwargs):
            if url.endswith('/job/2.shtml') and now[0]<2000:
                raise RateLimit(2000-now[0],'publisher_wait',next_allowed_at=2000.0)
            return super().open(url,**kwargs)
        def pump(self):
            if close_window.is_set():
                close_window.clear();self.page.close()
                return
            super().pump()
    with tempfile.TemporaryDirectory(prefix='radar-deferred-browser-') as tmp:
        workspace=Workspace(tmp)
        adapter=replace(builtins().get('liepin'),domains=('jobs.fixture.test',),resource_domains=('jobs.fixture.test',),
            login_hosts=('jobs.fixture.test',),search_base=ORIGIN+'/zhaopin/',login_url=ORIGIN+'/')
        ledger=RateLedger(workspace.root/'rates.sqlite',Limits(page_interval=0,request_interval=0),clock=lambda:now[0])
        service=GuidedService(workspace,registry=Registry([adapter]),backend_factory=ClosableBrowser,ledger=ledger)
        def job():return next(item for item in service.state()['jobs'] if item['id']==ident)
        def wait_for(predicate):
            deadline=time.monotonic()+20
            while time.monotonic()<deadline:
                value=job()
                if not service.state()['busy'] and predicate(value):return value
                time.sleep(.03)
            raise AssertionError('fixture state did not complete: '+str(job().get('code')))
        try:
            ident=service.create({'platform':'liepin','keyword':'时间序列算法','roles':['time_series'],
                'max_pages':1,'max_jobs':2,'consent':True,'rights_note':'ARTIFICIAL BROWSER TEST ONLY'})['id']
            ready=wait_for(lambda j:j['status']=='ready')
            selected=[row['id'] for row in ready['cards']]
            assert len(selected)==2
            service.action({'id':ident,'action':'collect','selected':selected})
            deferred=wait_for(lambda j:j['status']=='waiting_rate')
            assert deferred['report_id'] and deferred['auto_resume'] and deferred['next_allowed_at']==2000
            old_report=workspace.root/'reports'/deferred['report_id']/'run_manifest.json'
            original=hashlib.sha256(old_report.read_bytes()).hexdigest();cost=ledger.summary('liepin')
            result['checks'].append('first full artificial JD and its original report survive a later publisher wait')
            close_window.set()
            stopped=wait_for(lambda j:j['code']=='automatic_resume_unavailable')
            assert stopped['selection']==selected and stopped['next_allowed_at']==2000
            assert not stopped['auto_resume'] and not stopped['automatic_resume_available']
            before_calls=list(calls);now[0]=2001
            time.sleep(.35)  # Several real worker timer turns after the deadline.
            assert len(instances)==1 and calls==before_calls
            assert ledger.summary('liepin')==cost
            assert hashlib.sha256(old_report.read_bytes()).hexdigest()==original
            result['checks'].append('closing actual page disarms timer; expiry opens no replacement, sends no request and preserves report/quota/deadline')
            service.action({'id':ident,'action':'resume'})
            finished=wait_for(lambda j:j['status']=='completed')
            assert len(instances)==2 and all(row['status']=='ok' for row in finished['cards'])
            assert calls.count('/job/1.shtml')==1 and calls.count('/job/2.shtml')==1
            assert finished['report_id']!=deferred['report_id'] and old_report.is_file()
            result['checks'].append('explicit resume creates one new owned browser, skips saved JD and completes remaining JD into a new report')
            result.update(success=True,browser_versions=versions,source_requests=0)
        finally:
            service.close()
            (out/'results.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(result,ensure_ascii=False,indent=2))


if __name__=='__main__':main()
