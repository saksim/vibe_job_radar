"""Main collection workflow using real browsers and artificial supplier documents.

No live accounts, third-party text, egress fallback or user secrets are involved.
"""
from __future__ import annotations
import dataclasses
import json
import os
import sys
import tempfile
import threading
from pathlib import Path
from urllib.parse import urlsplit

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from vibe_job_radar.workspace import Workspace
from vibe_job_radar.workbench import LocalServer
from vibe_job_radar.guided.adapters import Registry,builtins
from vibe_job_radar.guided.browser import PlaywrightBackend
from vibe_job_radar.guided.service import GuidedService
from vibe_job_radar.guided.rate import Limits,RateLedger
from vibe_job_radar.guided.transport import WireResponse
from vibe_job_radar.guided.contracts import CrawlError

ORIGIN='https://jobs.fixture.test'
PATHS=('/job/1.shtml','/a/2.shtml','/lptjob/3','/job/4.shtml','/job/5.shtml')
CALLS=[]
BODY='人工岗位：负责时间序列预测，使用 Cursor 进行 AI 辅助编程，编写单元测试并完成代码审查。'

def posting(path, title='时间序列算法工程师', body=BODY):
    return {'@type':'JobPosting','url':ORIGIN+path,'title':title,'description':body}

class TargetWire:
    def __init__(self,adapter,ledger,cancelled,progress):
        self.adapter,self.ledger,self.cancelled=adapter,ledger,cancelled
        self.blocked=set()
    def allowed_resource(self,url): return url.startswith(ORIGIN+'/')
    def reserve(self,kind):
        if self.cancelled.is_set(): raise CrawlError('paused')
        self.ledger.reserve('liepin',kind)
    def ensure_robots(self,url): pass  # All policy-denial behavior remains covered separately.
    def fetch(self,url,method='GET',headers=None,body=None,*,required=True):
        if not self.allowed_resource(url) or method!='GET': raise CrawlError('unexpected_fixture_request')
        self.reserve('request');path=urlsplit(url).path;CALLS.append(path)
        if path=='/zhaopin/':
            html='<h1>人工检索结果</h1>'+''.join(f'<a href="{p}">时间序列算法工程师 {i+1}</a>' for i,p in enumerate(PATHS))
        elif path in PATHS:
            if path=='/job/4.shtml':
                docs=[posting('/job/9.shtml'),posting('/job/8.shtml')]
            elif path=='/job/5.shtml':
                docs=[posting(path,body='原文'*80000)]
            else:
                docs=[posting('/job/9.shtml',title='不属于本批的推荐岗位'),posting(path)]
            html='<h1>人工详情</h1><script type="application/ld+json">'+json.dumps(docs,ensure_ascii=False)+'</script>'
        else: raise CrawlError('unexpected_fixture_request')
        return WireResponse(200,{'content-type':'text/html; charset=utf-8'},html.encode('utf-8'))


def main():
    from playwright.sync_api import expect,sync_playwright
    out=ROOT/'browser-acceptance'/'target-acquisition';out.mkdir(parents=True,exist_ok=True)
    result={'success':False,'checks':[],'page_errors':[],
        'scope':'Actual local app and Chromium; artificial supplier URLs/documents, NOT live platform acceptance.'}
    options={'headless':True}
    executable=os.environ.get('RADAR_TEST_CHROMIUM')
    if executable: options['executable_path']=executable
    try:
        with tempfile.TemporaryDirectory(prefix='target-jobs-') as tmp:
            ws=Workspace(tmp)
            adapter=dataclasses.replace(builtins().get('liepin'),domains=('jobs.fixture.test',),resource_domains=(),
                search_base=ORIGIN+'/zhaopin/',login_url=ORIGIN+'/',login_hosts=('jobs.fixture.test',))
            server=LocalServer(ws);server.guided.close()
            ledger=RateLedger(Path(tmp)/'guided'/'rates.sqlite',Limits(page_interval=0,request_interval=0))
            server.guided=GuidedService(ws,registry=Registry([adapter]),ledger=ledger,
                backend_factory=lambda a,l,c,p:PlaywrightBackend(a,l,c,p,transport_factory=TargetWire,**options))
            thread=threading.Thread(target=server.serve_forever,kwargs={'poll_interval':.01},daemon=True);thread.start()
            try:
                with sync_playwright() as pw:
                    browser=pw.chromium.launch(**options)
                    try:
                        context=browser.new_context(viewport={'width':1280,'height':960},accept_downloads=True)
                        context.route('**/*',lambda r:r.continue_() if r.request.url.startswith(server.origin+'/') else r.abort())
                        page=context.new_page();page.on('pageerror',lambda e:result['page_errors'].append(str(e)))
                        page.goto(server.entry_url)
                        page.locator('a[href="/guided"]').click()
                        form=page.locator('#search-form')
                        expect(page.locator('#site')).to_have_value('liepin')
                        form.locator('[name=keyword]').fill('时间序列算法')
                        form.locator('[name=rights_note]').fill('人工测试，不是实站授权')
                        form.locator('[name=consent]').check()
                        page.locator('#find').click()
                        expect(page.locator('#cards .card')).to_have_count(5,timeout=30000)
                        result['checks'].append('all three observed Liepin path families survive original list and selection flow')
                        page.locator('#select-all').click();page.locator('#collect').click()
                        expect(page.locator('#acquisition-outcome')).to_contain_text('部分目标岗位',timeout=60000)
                        state=server.guided.state()['jobs'][0]
                        assert [c['status'] for c in state['cards']]==['ok','ok','ok','structure_changed','invalid_job_data']
                        assert state['outcome']['selected']==5 and state['outcome']['saved']==3 and state['outcome']['failed']==2
                        assert state['outcome']['target_jobs']==3 and state['outcome']['ai_jobs']==3
                        expect(page.locator('#acquisition-counts')).to_contain_text('发现 5 · 所选 5 · 完整正文 3 · 目标岗位 3 · 有明确AI要求的岗位 3')
                        assert all(CALLS.count(p)==1 for p in PATHS)
                        assert '/job/9.shtml' not in CALLS and '/job/8.shtml' not in CALLS
                        result['checks'].append('target JSON-LD chosen by current URL; recommendations never requested or saved; invalid row does not abort batch')
                        with page.expect_download() as download:
                            page.get_by_role('button',name='下载本批采集结果',exact=True).click()
                        file=out/'fixture-acquisition.json';download.value.save_as(str(file))
                        audit=json.loads(file.read_text(encoding='utf-8'))
                        assert audit['outcome']==state['outcome'] and len(audit['items'])==5
                        assert [item['result'] for item in audit['items']] == ['complete_with_explicit_ai_requirements']*3+['unconfirmed_full_jd']*2
                        assert all(item['collected_at'] and item['explicit_ai_requirement_ids'] for item in audit['items'][:3])
                        result['checks'].append('funnel distinguishes source-job counts from requirement rows; each complete result traces to original requirement IDs')
                        result['outcome']=audit['outcome']
                        page.get_by_role('link',name='查看本批研究结论').click()
                        expect(page.locator('#brief-acquisition')).to_contain_text('失败 2 条')
                        expect(page.locator('#brief-capabilities')).to_contain_text('Cursor')
                        assert '不属于本批的推荐岗位' not in page.locator('#brief-capabilities').inner_text()
                        result['checks'].append('partial failures remain visible in local state, authenticated audit download and same-batch research report')
                        page.reload();expect(page.locator('#brief-acquisition')).to_contain_text('失败 2 条')
                        page.set_viewport_size({'width':390,'height':844})
                        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                        page.screenshot(path=str(out/'target-results.png'),full_page=True)
                        result['checks'].append('report refresh retains exact batch and failure counts; narrow-screen layout usable')
                        assert not result['page_errors']
                        result['success']=True
                    finally: browser.close()
            finally:
                server.shutdown();server.server_close();thread.join(timeout=5)
    finally:
        (out/'results.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(result,ensure_ascii=False,indent=2))

if __name__=='__main__': main()
