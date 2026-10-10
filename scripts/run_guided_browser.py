"""Real Chromium UI + real Chromium collection backend; only upstream HTTP is a fixture.

No external accounts/sites are contacted. The transport fixture is injected only
in this developer test, never by an HTTP/API parameter. Artifacts contain synthetic
job/evidence data; fixture credentials are deliberately non-real and never saved.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from vibe_job_radar.workspace import Workspace
from vibe_job_radar.workbench import LocalServer
from vibe_job_radar.guided.adapters import DOMAdapter, Registry
from vibe_job_radar.guided.browser import PlaywrightBackend
from vibe_job_radar.guided.contracts import CrawlError
from vibe_job_radar.guided.rate import RateLedger, Limits
from vibe_job_radar.guided.service import GuidedService
from vibe_job_radar.guided.transport import WireResponse

PASSWORD='BROWSER-FIXTURE-PASSWORD-NOT-REAL'
ACCOUNT='browser-fixture-user'
CALLS=[]
MANUAL_LOGIN=threading.Event()


class FixtureWire:
    def __init__(self,adapter,ledger,cancelled,progress):
        self.ledger,self.cancelled=ledger,cancelled
        self.blocked=set()
        self.network_policy=None
        self.progress=progress
    def bind_policy(self, policy):
        self.network_policy=policy
    def reserve(self,kind):
        if self.cancelled.is_set():raise CrawlError('paused')
        self.ledger.reserve('fixture',kind)
    def allowed_resource(self,url):return url.startswith('https://jobs.fixture.test/')
    def ensure_robots(self,url):pass
    def fetch(self,url,method='GET',headers=None,body=None,*,required=True):
        self.reserve('request')
        p=urlsplit(url);CALLS.append((method,p.path))
        cookie=(headers or {}).get('cookie','')
        logged='fixture_login=yes' in cookie
        response_headers={'content-type':'text/html; charset=utf-8'}
        if p.path=='/robots.txt':return WireResponse(200,{'content-type':'text/plain'},b'User-agent: *\nAllow: /')
        if p.path=='/login' and method=='POST':
            return WireResponse(302,{'location':'/search'},b'',('fixture_login=yes; Path=/; Secure; HttpOnly',))
        if p.path=='/login':
            text='''<!doctype html><h1>人工测试登录页</h1><form action="/login" method="post">
                <p>测试人员在原生页确认登录；不是应用自动填写密码。</p>
                <button type="submit">登录</button></form>'''
        elif p.path=='/search' and not logged:
            text='<h1>请登录后查看岗位</h1><p>登录后查看职位</p>'
        elif p.path=='/search':
            second='page=2' in p.query
            links='<a href="/job/3">时间序列算法工程师 3</a>' if second else (
                '<a href="/job/1">时间序列算法工程师 1</a><a href="/job/2">时间序列算法工程师 2</a>'
                '<a rel="next" href="/search?page=2">下一页</a>')
            text='<h1>人工测试岗位列表</h1><div id="jobs"></div><script>setTimeout(()=>{document.getElementById("jobs").innerHTML='+json.dumps(links)+'},150); setInterval(()=>fetch("/poll").catch(()=>{}),250)</script>'
        elif p.path=='/job/1':
            return WireResponse(302,{'location':'/job/1-final'},b'')
        elif p.path.startswith('/job/'):
            text='<h1>时间序列算法工程师</h1><div class="job-description">要求熟练使用 Cursor 进行 AI 辅助编程，编写单元测试与代码审查，负责时间序列预测系统。</div><script>const captchaLibrary=true;</script>'
        else:text='<h1>Unknown fixture route</h1>'
        return WireResponse(200,response_headers,text.encode())


class ManualFixtureBackend(PlaywrightBackend):
    # Simulates a human click ONLY for the controlled upstream fixture, on the
    # owning thread. This test hook is never selectable from a production API.
    def pump(self):
        if MANUAL_LOGIN.is_set():
            MANUAL_LOGIN.clear()
            self.page.locator('form button[type="submit"]').click()
            self.page.wait_for_timeout(500)
        super().pump()


def completed_report(page, server, workspace):
    from playwright.sync_api import expect
    ident=page.locator('#task').input_value()
    expect(page.locator('#task-status')).to_contain_text('本批次已结束',timeout=30000)
    expect(page.locator('#result')).to_contain_text('已保存 1 个岗位',timeout=30000)
    assert page.locator('#task').input_value()==ident
    state=server.guided._load(ident);report=state['report_id']
    assert state['status']=='completed' and len(report)==32
    assert all(c in '0123456789abcdef' for c in report)
    assert workspace.report(report)['requirements']
    expect(page.locator('#result a[href^="/#report="]')).to_have_attribute('href','/#report='+report)
    return state



def check_recorded_liepin_pagination(browser, output):
    """Real DOM clicks on the observed control shape; no upstream or credentials."""
    from collections import deque
    from types import SimpleNamespace
    from vibe_job_radar.guided.adapters import builtins
    from vibe_job_radar.guided.native_browser import NativeBackend
    from vibe_job_radar.guided.rate import RateLimit

    checks=[];network=[]
    context=browser.new_context()
    fixture_url='https://www.liepin.com/zhaopin/'
    def route_fixture(route):
        if route.request.url==fixture_url and route.request.is_navigation_request():
            route.fulfill(status=200,content_type='text/html',body='<!doctype html>')
        else:
            network.append(route.request.url)
            route.abort()
    context.route('**/*',route_fixture)
    page=context.new_page()
    page.goto(fixture_url)  # Fulfilled in memory; no upstream request.
    def backend(kind, refusal=None):
        reservations=[];settled=[];permissions=[]
        def ensure_robots(url):
            permissions.append(url)
            if refusal=='robots':raise CrawlError('robots_disallowed')
        def reserve(value):
            reservations.append(value)
            if refusal=='quota':raise RateLimit(60,'daily_limit')
        b=kind.__new__(kind)
        b.adapter=builtins().get('liepin');b.page=page
        b.error=b.wait_error=None;b.cancelled=threading.Event()
        b.auth_mode=False;b.redirects=0;b._pagination_page=None
        b.wire=SimpleNamespace(ensure_robots=ensure_robots,reserve=reserve)
        b._settle=lambda:settled.append(True)
        b._epoch=7;b._observations=deque(['prior-page'])
        b._latest_business={'liepin_search':7};b._observed_bytes=40
        # Supply owned-document state so production native permission checks
        # run too. This fixture does not establish a CDP/network session.
        if kind is NativeBackend:
            document=SimpleNamespace(url=page.url,document_url=fixture_url,target='pager-fixture')
            b._bound_pages={page:'fixture-session'}
            b._page_sessions={'fixture-session':object()}
            b._sessions={'fixture-session':document.target}
            b._page_documents={'fixture-session':document}
        return b,reservations,settled,permissions

    def control(*, parent='', button='', scope='list-pagination-box', jump=False, transition=False):
        classes='ant-pagination-jump-next' if jump else 'ant-pagination-next'
        title='Next 5 Pages' if jump else 'Next Page'
        change=("this.disabled=true;this.parentElement.setAttribute('aria-disabled','true');"
                "this.parentElement.classList.add('ant-pagination-disabled');" if transition else '')
        return ('<div class="'+scope+'"><ul class="ant-pagination">'
                '<li class="ant-pagination-prev ant-pagination-disabled" aria-disabled="true">'
                '<button disabled type="button">previous</button></li>'
                '<li class="'+classes+'" title="'+title+'" '+parent+'>'
                '<button type="button" class="ant-pagination-item-link" '+button+
                ' onclick="window.pagerEvents.push(\'next\');'+change+'">'
                '<span role="img" aria-label="right" class="anticon anticon-right">'
                '<svg width="14" height="14"><path d="M 3 1 L 11 7 L 3 13"/></svg></span>'
                '</button></li></ul></div>')
    cases=[
        ('enabled icon-only button',control(parent='aria-disabled="false"'),True,None),
        ('disabled child button',control(parent='aria-disabled="false"',button='disabled'),False,None),
        ('disabled parent ARIA',control(parent='aria-disabled="true"'),False,None),
        ('disabled parent class',control(parent='aria-disabled="false"').replace(
            'class="ant-pagination-next"','class="ant-pagination-next ant-pagination-disabled"'),False,None),
        ('hidden pager',control(parent='aria-disabled="false" style="display:none"'),False,None),
        ('jump-five control is not next-page',control(jump=True),False,None),
        ('unrelated pager is not the list pager',control(scope='unrelated-carousel'),False,None),
        ('legacy next link remains available','<a rel="next" href="#next" onclick="window.pagerEvents.push(\'next\')">下一页</a>',True,None),
        ('robots refusal precedes quota and click',control(parent='aria-disabled="false"'),False,'robots'),
        ('quota refusal precedes click',control(parent='aria-disabled="false"'),False,'quota'),
    ]
    try:
        for kind in (PlaywrightBackend,NativeBackend):
            for name,markup,clicks,refused in cases:
                page.set_content('<!doctype html><meta charset="utf-8"><style>button{width:40px;height:32px}</style>'
                                 '<script>window.pagerEvents=[];</script>'+markup)
                b,reservations,settled,permissions=backend(kind,refused)
                if refused:
                    try:b.next_page()
                    except CrawlError as error:
                        assert error.code==('daily_limit' if refused=='quota' else 'robots_disallowed')
                    else:raise AssertionError('refusal did not reach the caller')
                else:
                    assert b.next_page() is clicks,(kind.__name__,name)
                assert page.evaluate('window.pagerEvents')==(['next'] if clicks else []),(kind.__name__,name)
                assert reservations==(['page'] if clicks or refused=='quota' else []),(kind.__name__,name)
                assert len(permissions)==int(bool(clicks or refused)),(kind.__name__,name)
                assert settled==([True] if clicks else []) and b._pagination_page is None
                if kind is NativeBackend:
                    assert b._epoch==(8 if clicks else 7)
                    assert list(b._observations)==([] if clicks else ['prior-page'])
                    assert b._latest_business==({} if clicks else {'liepin_search':7})
                    assert b._observed_bytes==(0 if clicks else 40)
                checks.append({'backend':kind.__name__,'case':name,'passed':True})
            # A real click changes the same DOM control into a disabled end.
            page.set_content('<script>window.pagerEvents=[];</script>'+control(
                parent='aria-disabled="false"',transition=True))
            b,reservations,settled,permissions=backend(kind)
            assert b.next_page() is True
            if kind is NativeBackend:
                b._observations.append('second-page');b._latest_business={'liepin_search':8};b._observed_bytes=44
                before_epoch=b._epoch
            assert b.next_page() is False
            assert page.evaluate('window.pagerEvents')==['next'] and reservations==['page'] and settled==[True]
            assert len(permissions)==1
            if kind is NativeBackend:
                assert list(b._observations)==['second-page'] and b._epoch==before_epoch
                assert b._latest_business=={'liepin_search':8} and b._observed_bytes==44
            checks.append({'backend':kind.__name__,'case':'same control becomes disabled without another click or quota reservation','passed':True})
        assert not network
        page.screenshot(path=str(output/'liepin-pagination-disabled.png'),full_page=True)
        value={'success':True,'checks':checks,'external_requests':0,
            'scope':'actual browser DOM and production next_page methods; synthetic control interaction, not site pagination or native network certification'}
        (output/'liepin-pagination.json').write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf8')
        return value
    finally:context.close()


def main():
    from playwright.sync_api import sync_playwright, expect
    output=ROOT/'browser-acceptance'/'guided';output.mkdir(parents=True,exist_ok=True)
    result={'success':False,'checks':[],'page_errors':[],'upstream':'in-memory HTTP fixture; NO real job platforms or accounts',
            'two_real_browsers':True,'checkout_sha':os.environ.get('GITHUB_SHA','local')}
    executable=os.environ.get('RADAR_TEST_CHROMIUM')
    options={'headless':True}
    if executable:options['executable_path']=executable
    with tempfile.TemporaryDirectory() as tmp:
        workspace=Workspace(tmp)
        workspace.config['platforms']['fixture']={'label':'人工测试站点','domains':['jobs.fixture.test']}
        adapter=DOMAdapter('fixture','人工测试站点',('jobs.fixture.test',),'https://jobs.fixture.test/search','q',r'^/job/[a-z0-9-]+$',
            'https://jobs.fixture.test/login',('jobs.fixture.test',),next_selectors=('a[rel="next"]',))
        server=LocalServer(workspace)
        server.guided.close()
        ledger=RateLedger(Path(tmp)/'guided'/'rates.sqlite',Limits(page_interval=0,request_interval=0,login_interval=0))
        server.guided=GuidedService(workspace,registry=Registry([adapter]),ledger=ledger,
            backend_factory=lambda a,l,c,p,**saved:ManualFixtureBackend(a,l,c,p,headless=True,executable_path=executable,transport_factory=FixtureWire,**saved))
        # Hold the first one-job report after its record has been saved. A
        # saved-record counter is not proof that the report was committed.
        report_entered,report_release=threading.Event(),threading.Event()
        original_report=server.guided._finalize_report
        def held_report(state,adapter):
            if len(state.get('selection',[]))==1 and not report_entered.is_set():
                report_entered.set()
                assert report_release.wait(20),'controlled report gate was not released'
            return original_report(state,adapter)
        server.guided._finalize_report=held_report
        thread=threading.Thread(target=server.serve_forever,kwargs={'poll_interval':.01},daemon=True);thread.start()
        try:
            with sync_playwright() as pw:
                browser=pw.chromium.launch(**options)
                pager=check_recorded_liepin_pagination(browser,output)
                result['checks'].append('observed icon-only Liepin pager: '+str(len(pager['checks']))+' bridge/native DOM, disabled-end, scope and quota checks')
                context=browser.new_context(viewport={'width':1360,'height':1000},accept_downloads=True)
                context.route('**/*',lambda r:r.continue_() if r.request.url.startswith(server.origin+'/') else r.abort())
                page=context.new_page();page.on('pageerror',lambda e:result['page_errors'].append(str(e)))
                page.goto(server.entry_url)
                page.locator('a[href="/guided"]').click()
                expect(page.locator('#site')).to_contain_text('人工测试站点')
                page.get_by_text('各平台当前支持与验证范围', exact=True).click()
                expect(page.locator('#capability-status')).to_contain_text('没有匹配的已记录验收')
                expect(page.locator('#capability-status')).to_contain_text('尚无本站原生访问契约')
                result['checks'].append('capability UI uses registry metadata; an injected fixture never inherits a real-site certification')
                result['checks'].append('guided page accessible with existing local session')
                form=page.locator('#search-form')
                form.locator('[name=max_pages]').select_option('2')
                form.locator('[name=rights_note]').fill('人工测试数据，仅用于自动验收，不是市场数据。')
                form.locator('[name=diagnostics]').check()
                form.locator('[name=consent]').check();page.locator('#find').click()
                expect(page.locator('#task-status')).to_contain_text('需要你操作',timeout=30000)
                result['checks'].append('anonymous search yields manual assistance, not false success')
                page.locator('#login').click()
                expect(page.locator('#task-status')).to_contain_text('已打开平台登录页面',timeout=30000)
                login_trace=server.guided.diagnostics({'id':server.guided.state()['jobs'][0]['id']})
                assert 'login' in {event['stage'] for event in login_trace['events']}
                MANUAL_LOGIN.set()
                deadline=time.monotonic()+20
                while CALLS.count(('POST','/login'))!=1 and time.monotonic()<deadline:
                    page.wait_for_timeout(100)
                assert CALLS.count(('POST','/login'))==1
                page.wait_for_timeout(1500)
                result['checks'].append('human-assisted native login in separate Chromium; workbench receives no credentials')
                page.locator('#capture').click()
                expect(page.locator('#cards .card')).to_have_count(3,timeout=30000)
                result['checks'].append('authenticated JS-rendered list and next page become three deduplicated cards')
                page.wait_for_timeout(500)
                requests_after_ready = len(CALLS)
                page.wait_for_timeout(900)
                assert len(CALLS) == requests_after_ready
                result['checks'].append('ready list freezes background polling until an explicit next action')
                page.locator('#select-all').click();page.locator('#collect').click()
                expect(page.locator('#task-status')).to_contain_text('本批次已结束',timeout=60000)
                expect(page.locator('#result')).to_contain_text('已保存 3 个岗位')
                expect(page.locator('#cards')).to_contain_text('/job/1-final')
                job=server.guided.state()['jobs'][0]
                assert job['report_id'] and workspace.report(job['report_id'])['requirements']
                result['checks'].append('selected cards open details, resolve redirect URL, ingest full JD and execute original report pipeline')
                with page.expect_download() as download:
                    page.locator('#export').click()
                download.value.save_as(output/'fixture-urls.txt')
                assert '/job/1-final' in (output/'fixture-urls.txt').read_text(encoding='utf-8')
                result['checks'].append('observed/resolved URLs exported instead of manually prepared by user')
                before_diagnostic = len(CALLS)
                page.locator('#preview-acquisition-trace').click()
                expect(page.locator('#acquisition-trace')).to_contain_text('trace_id')
                with page.expect_download() as diagnostic_download:
                    page.locator('#download-acquisition-trace').click()
                diagnostic_download.value.save_as(output/'acquisition-diagnostic.json')
                diagnostic=json.loads((output/'acquisition-diagnostic.json').read_text(encoding='utf-8'))
                assert diagnostic['trace_id']==job['id'] and diagnostic['enabled']
                assert {'detail_parse','persist','report'} <= {e['stage'] for e in diagnostic['events']}
                assert '时间序列算法工程师' not in json.dumps(diagnostic,ensure_ascii=False)
                assert PASSWORD not in json.dumps(diagnostic) and ACCOUNT not in json.dumps(diagnostic)
                assert len(CALLS)==before_diagnostic
                page.locator('#disable-acquisition-trace').click()
                expect(page.locator('#acquisition-trace')).to_contain_text('已关闭并清除')
                assert not server.guided.diagnostics({'id':job['id']})['enabled']
                assert server.guided._load(job['id'])['report_id']==job['report_id']
                result['checks'].append('D01 opt-in -> local metadata preview -> snapshot download -> clear; no additional upstream requests or loss of report')
                page.screenshot(path=str(output/'guided-desktop.png'),full_page=True)
                for p in Path(tmp).rglob('*.json'):
                    text=p.read_text(encoding='utf-8');assert PASSWORD not in text and ACCOUNT not in text
                assert PASSWORD not in json.dumps(server.guided.state())
                page.reload();expect(page.locator('#result')).to_contain_text('已保存 3 个岗位')
                page.locator('#stop').click();expect(page.locator('#task-status')).to_contain_text('已停止',timeout=15000)
                assert not server.guided.state()['jobs'][0]['browser_open']
                result['checks'].append('credentials absent from disk/status; reload preserves results; stop revokes live browser session')
                # Preserve the original manual-return journey above. A separate
                # one-page task opts into local return observation; no login
                # credentials or extra upstream endpoint are introduced.
                form=page.locator('#search-form')
                form.locator('[name=max_pages]').select_option('1')
                form.locator('details:has(input[name=list_url]) > summary').click()
                form.locator('[name=list_url]').fill('https://jobs.fixture.test/search')
                form.locator('[name=rights_note]').fill('独立自动接续验收；仅人工站点，不是市场数据。')
                expect(form.locator('[name=persist_session]')).not_to_be_checked()
                form.locator('[name=persist_session]').check()
                form.locator('[name=consent]').check()
                page.locator('#find').click()
                expect(page.locator('#task-status')).to_contain_text('需要你操作',timeout=30000)
                expect(page.locator('#auto-login-return')).not_to_be_checked()
                page.locator('#auto-login-return').check()
                page.locator('#login').click()
                expect(page.locator('#login-return-status')).to_contain_text('原检索页',timeout=30000)
                login_posts=CALLS.count(('POST','/login'))
                MANUAL_LOGIN.set()
                # No Capture/Resume click: two matching local DOM observations
                # must enter the original service and render its actual cards.
                expect(page.locator('#cards .card')).to_have_count(2,timeout=30000)
                expect(page.locator('#login-return-status')).to_contain_text('已自动接回')
                assert CALLS.count(('POST','/login'))==login_posts+1
                page.locator('#cards input[type=checkbox]').first.check()
                page.locator('#collect').click()
                expect(page.locator('#result')).to_contain_text('已保存 1 个岗位',timeout=30000)
                assert report_entered.is_set()
                partial=server.guided._load(page.locator('#task').input_value())
                assert partial['report_id']=='' and partial['status']!='completed'
                expect(page.locator('#result a[href^="/#report="]')).to_have_count(0)
                expect(page.locator('#find')).to_be_disabled()
                result['report_checkpoint']={'saved_count':1,'report_id':partial['report_id'],
                    'status':partial['status'],'new_query_disabled':True}
                report_release.set()
                completed_report(page,server,workspace)
                result['checks'].append('saved-record count appears before report commit; new query remains disabled and acceptance waits for a completed, accessible report')

                result['checks'].append('opt-in matching login return automatically reads the original list without Capture/Resume; selected detail enters the same report pipeline')
                # A separate query explicitly reuses this still-open, now idle
                # browser. Login count must not change and prior reports survive.
                previous_id=page.locator('#task').input_value()
                previous_backend=server.guided._backends[previous_id]
                previous_report=server.guided._load(previous_id)['report_id']
                assert previous_report and workspace.report(previous_report)['requirements']
                login_posts=CALLS.count(('POST','/login'))
                expect(form.locator('[name=reuse_current_session]')).not_to_be_checked()
                form.locator('[name=reuse_current_session]').check()
                form.locator('[name=keyword]').fill('时间序列算法新查询')
                form.locator('[name=list_url]').fill('https://jobs.fixture.test/search?q=new-query')
                page.locator('#find').click()
                expect(page.locator('#session-reuse-status')).to_contain_text('已复用当前采集浏览器会话',timeout=30000)
                expect(page.locator('#cards .card')).to_have_count(2,timeout=30000)
                current_id=page.locator('#task').input_value()
                assert current_id!=previous_id
                assert server.guided._backends[current_id] is previous_backend
                assert previous_id not in server.guided._backends
                assert CALLS.count(('POST','/login'))==login_posts
                assert server.guided._load(previous_id)['report_id']==previous_report
                page.locator('#cards input[type=checkbox]').first.check()
                page.locator('#collect').click()
                completed_report(page,server,workspace)
                assert server.guided._load(current_id)['report_id']!=previous_report
                assert CALLS.count(('POST','/login'))==login_posts
                result['checks'].append('new opted-in query reuses the same authenticated browser with no additional login; selected detail creates a separate report and prior report survives')
                page.locator('#stop').click()
                expect(page.locator('#task-status')).to_contain_text('已停止',timeout=15000)
                assert current_id not in server.guided._backends

                # Reconstruct the service AND launch a new collection browser.
                # Only the on-disk opt-in cookies can authenticate the first query.
                expect(page.locator('#saved-session-status')).to_contain_text('已保存到本机')
                assert (Path(tmp)/'.radar-sessions/fixture.json').is_file()
                server.guided.close()
                assert previous_backend.browser is None
                server.guided=GuidedService(workspace,registry=Registry([adapter]),
                    ledger=RateLedger(Path(tmp)/'guided'/'rates.sqlite',Limits(page_interval=0,request_interval=0,login_interval=0)),
                    backend_factory=lambda a,l,c,p,**saved:ManualFixtureBackend(a,l,c,p,headless=True,
                        executable_path=executable,transport_factory=FixtureWire,**saved))
                form.locator('[name=reuse_current_session]').uncheck()
                form.locator('[name=list_url]').fill('https://jobs.fixture.test/search?q=after-restart')
                page.locator('#find').click()
                expect(page.locator('#task')).not_to_have_value(current_id,timeout=30000)
                expect(page.locator('#task-status')).to_contain_text('可以选择岗位',timeout=30000)
                expect(page.locator('#cards .card')).to_have_count(2,timeout=30000)
                expect(page.locator('#saved-session-status')).to_contain_text('已保存到本机',timeout=30000)
                restarted_id=page.locator('#task').input_value()
                assert server.guided._backends[restarted_id] is not previous_backend
                assert server.guided._load(restarted_id)['authentication']=='restored_session_unverified'
                assert CALLS.count(('POST','/login'))==login_posts
                page.locator('#cards input[type=checkbox]').first.check();page.locator('#collect').click()
                completed_report(page,server,workspace)
                assert server.guided._load(previous_id)['report_id']==previous_report
                assert 'fixture_login' not in json.dumps(server.guided.state())
                result['checks'].append('opt-in cookies restore into a new service and new collection browser; no additional login POST, full selected JD reaches report, old reports remain')
                page.once('dialog',lambda dialog:dialog.accept())
                page.locator('#forget-session').click()
                expect(page.locator('#task-status')).to_contain_text('保存的会话已清除',timeout=30000)
                assert not (Path(tmp)/'.radar-sessions/fixture.json').exists()
                assert not server.guided._backends
                assert not server.guided._load(previous_id)['persist_session']
                assert server.guided._load(previous_id)['report_id']==previous_report
                result['checks'].append('explicit forget closes the collection browser, removes private snapshot and revokes old task persistence without deleting reports or quotas')

                page.set_viewport_size({'width':390,'height':844})
                page.screenshot(path=str(output/'guided-mobile.png'),full_page=True)
                assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
                assert not result['page_errors'],result['page_errors']
                result['checks'].append('mobile layout has no page overflow and no JS errors')
                result['success']=True
                browser.close()
        finally:
            report_release.set()
            server.shutdown();server.server_close();thread.join(timeout=5)
            (output/'results.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(result,ensure_ascii=True,indent=2))


if __name__=='__main__':
    main()
    # Keep related UI checks in the existing browser acceptance entry point.
    # A failure in either suite must fail this process and the original CI job.
    from run_login_quota_browser import main as login_quota_main
    login_quota_main()
