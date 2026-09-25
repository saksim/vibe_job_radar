"""Real workbench/collection browsers, artificial direct-to-detail login.

No external accounts, recruitment data, password input or authentication bypass.
The test actor alone clicks the fixture's normal login button on the owner thread.
"""
from __future__ import annotations
import argparse
import dataclasses
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import traceback
from urllib.parse import urlsplit
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'src'),str(ROOT/'tests')]
from test_login_detail_return import wait_diagnostic
from vibe_job_radar.guided.adapters import Registry, builtins
from vibe_job_radar.guided.browser import PlaywrightBackend
from vibe_job_radar.guided.contracts import CrawlError
from vibe_job_radar.guided.rate import Limits, RateLedger
from vibe_job_radar.guided.service import GuidedService
from vibe_job_radar.guided.transport import WireResponse
from vibe_job_radar.workbench import LocalServer
from vibe_job_radar.workspace import Workspace

ORIGIN='https://jobs.fixture.test'
BODY='岗位职责：负责时间序列预测与模型评估。任职要求：熟悉Python、统计学，使用Cursor编写测试并审查代码。仅为人工测试正文。'

def main():
    from playwright.sync_api import expect, sync_playwright
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--automatic', action='store_true')
    args=parser.parse_args()
    out=ROOT/'browser-acceptance'/('automatic-collection' if args.automatic else 'login-detail-return');out.mkdir(parents=True,exist_ok=True)
    result={'success':False,'checks':[],'page_errors':[],'stage':'setup',
            'scope':'Real Chromium UI and production bridge; artificial TLS/HTTP supplier. No live-site or native-backend certification.'}
    calls=[];human_click=threading.Event()
    class Wire:
        def __init__(self,adapter,ledger,cancelled,progress):
            self.ledger,self.cancelled,self.blocked=ledger,cancelled,set()
        def allowed_resource(self,url):return url.startswith(ORIGIN+'/')
        def reserve(self,kind):
            if self.cancelled.is_set():raise CrawlError('paused')
            self.ledger.reserve('liepin',kind)
        def ensure_robots(self,url):pass  # No supplier traffic; production denial tests remain separate.
        def fetch(self,url,method='GET',headers=None,body=None,*,required=True):
            self.reserve('request');path=urlsplit(url).path;calls.append((method,path))
            cookies=();logged='artificial_return=yes' in (headers or {}).get('cookie','')
            if path=='/normal-login' and method=='POST':
                cookies=('artificial_return=yes; Path=/; Secure; HttpOnly',)
                markup='<h1>Artificial login accepted</h1><script>location.href="/job/2.shtml"</script>'
            elif path=='/zhaopin/':
                markup='<h1>Artificial search</h1><a href="/job/1.shtml">时间序列算法一</a><a href="/job/2.shtml">时间序列算法二</a>'
            elif path=='/job/2.shtml' and not logged:
                markup='<h1>登录后查看职位</h1><form method="post" action="/normal-login"><button id="human-confirm" type="submit">人工确认登录</button></form>'
            elif path in {'/job/1.shtml','/job/2.shtml'}:
                markup='<h1>时间序列算法工程师</h1><div class="job-description">'+BODY+'</div><aside><a href="/job/900.shtml">猜你喜欢</a></aside>'
            elif path=='/':markup='<h1>Artificial home; direct detail login must not navigate here.</h1>'
            else:raise CrawlError('unknown_fixture_route')
            return WireResponse(200,{'content-type':'text/html; charset=utf-8'},markup.encode('utf-8'),cookies)
    class HumanBackend(PlaywrightBackend):
        def pump(self):
            if human_click.is_set():
                human_click.clear()
                self.page.locator('#human-confirm').click()
                self.fixture_login_clicked=True
            super().pump()
        def snapshot(self):
            # Artificial supplier only: move a known tracking value between
            # the original URL and content reads, without another request.
            # This makes the real DOM observation race deterministic in CI.
            if (args.automatic and getattr(self,'fixture_login_clicked',False)
                    and not result.get('snapshot_url_change_injected')
                    and self.page.url==ORIGIN+'/job/2.shtml'):
                original_content=self.page.content
                def changing_content():
                    content=original_content()
                    self.page.evaluate("history.replaceState(null,'',location.pathname+'?d_sfrom=fixture')")
                    result['snapshot_url_change_injected']=True
                    return content
                with patch.object(self.page,'content',side_effect=changing_content):return super().snapshot()
            return super().snapshot()
    executable=os.environ.get('RADAR_TEST_CHROMIUM')
    options={'headless':True}
    if executable:options['executable_path']=executable
    try:
        with tempfile.TemporaryDirectory(prefix='detail-login-') as tmp:
            workspace=Workspace(tmp)
            adapter=dataclasses.replace(builtins().get('liepin'),domains=('jobs.fixture.test',),resource_domains=(),
                search_base=ORIGIN+'/zhaopin/',login_url=ORIGIN+'/',login_hosts=('jobs.fixture.test',))
            server=LocalServer(workspace);server.guided.close()
            ledger=RateLedger(Path(tmp)/'guided'/'rates.sqlite',Limits(page_interval=0,request_interval=0,login_interval=0))
            server.guided=GuidedService(workspace,registry=Registry([adapter]),ledger=ledger,
                backend_factory=lambda a,l,c,p:HumanBackend(a,l,c,p,headless=True,
                    executable_path=executable,transport_factory=Wire))
            server.guided._login_return.interval=.05
            thread=threading.Thread(target=server.serve_forever,kwargs={'poll_interval':.01},daemon=True);thread.start()
            try:
                with sync_playwright() as pw:
                    browser=pw.chromium.launch(**options)
                    try:
                        context=browser.new_context(viewport={'width':1280,'height':960})
                        context.route('**/*',lambda route:route.continue_() if route.request.url.startswith(server.origin+'/') else route.abort())
                        page=context.new_page();page.on('pageerror',lambda e:result['page_errors'].append(str(e)))
                        def response_seen(response):
                            if (result['stage']=='login_wait' and response.request.method=='POST'
                                    and response.url==server.origin+'/api/guided/action'):
                                result['login_action_http_status']=response.status
                        page.on('response',response_seen)
                        page.goto(server.entry_url);page.locator('a[href="/guided"]').click()
                        result['stage']='create_task'
                        form=page.locator('#search-form')
                        form.locator('[name=keyword]').fill('时间序列算法工程师')
                        form.locator('[name=rights_note]').fill('仅人工测试，不是真实猎聘授权或数据')
                        expect(form.locator('[name=auto_collect]')).not_to_be_checked()
                        if args.automatic:form.locator('[name=auto_collect]').check()
                        form.locator('[name=consent]').check();page.locator('#find').click()
                        expect(page.locator('#cards .card')).to_have_count(2,timeout=30000)
                        if not args.automatic:
                            page.locator('#select-all').click();page.locator('#collect').click()
                        expect(page.locator('#task-status')).to_contain_text('需要你操作',timeout=30000)
                        result['stage']='partial_report';partial=server.guided.state()['jobs'][0]
                        assert [r['status'] for r in partial['cards']]==['ok','manual_required']
                        assert partial.get('selection_source')==('query_order' if args.automatic else 'manual')
                        assert bool(partial.get('auto_selection_applied'))==args.automatic
                        first_report=partial['report_id'];first_record=partial['cards'][0]['record_id']
                        result['checks'].append('first selected JD and partial report survive second detail login wall')
                        if args.automatic:
                            result['checks'].append('create-time checkbox starts ordinary collection without selecting jobs or clicking Collect; login gate still pauses')
                        result['stage']='login_wait'
                        page.locator('#auto-login-return').check();page.locator('#login').click()
                        # Wait for the owner-thread action to finish. No navigation,
                        # capture or resume is issued by this UI after login.
                        end=time.monotonic()+30
                        while time.monotonic()<end:
                            state=server.guided.state()
                            if not state['busy'] and state['jobs'][0].get('login_continuation')=='watching':break
                            page.wait_for_timeout(50)
                        else:raise AssertionError('direct-detail login watcher did not arm')
                        result['stage']='login_request_counts'
                        assert calls.count(('GET','/job/2.shtml'))==2
                        assert ('GET','/') not in calls
                        result['stage']='returned_detail';human_click.set()
                        expect(page.locator('#task-status')).to_contain_text('本批次已结束',timeout=30000)
                        # Completed can render while the worker is saving its
                        # final continuation metadata. Wait for one coherent
                        # idle view, then assert every final field unchanged.
                        result['stage']='final_metadata';end=time.monotonic()+30
                        while time.monotonic()<end:
                            view=server.guided.state()
                            if not view['busy']:
                                finished=view['jobs'][0]
                                break
                            page.wait_for_timeout(50)
                        else:raise AssertionError('returned-detail worker did not finish')
                        assert all(r['status']=='ok' for r in finished['cards'])
                        assert finished['cards'][0]['record_id']==first_record
                        assert finished['cards'][1]['acquisition_path']=='login_returned_detail'
                        assert finished['authentication']=='user_resumed'
                        assert finished['login_continuation']=='resumed_detail'
                        history=finished['detail_attempt_history']
                        assert history['origin']=='task_creation'
                        assert [a['outcome'] for a in history['attempts']]==['ok','manual_required','ok']
                        assert [a['mode'] for a in history['attempts']]==['navigation','navigation','login_returned_detail']
                        assert all(a['elapsed_ms']>=0 and a['finished_at'] for a in history['attempts'])
                        assert len(history['selections'])==1 and len(history['selections'][0]['items'])==2
                        result['stage']='final_request_counts'
                        assert calls.count(('POST','/normal-login'))==1
                        # Failed visit + explicitly opened login gate + natural
                        # login redirect. No fourth collector navigation is allowed.
                        assert calls.count(('GET','/job/2.shtml'))==3,calls
                        assert calls.count(('GET','/job/1.shtml'))==1
                        assert calls.count(('GET','/zhaopin/'))==1
                        assert not any(path=='/job/900.shtml' for _,path in calls)
                        result['stage']='final_report'
                        assert workspace.report_file(first_report,'run_manifest.json').is_file()
                        assert first_report!=finished['report_id']
                        assert workspace.report(finished['report_id'])['manifest']['stats']['current_source_records']==2
                        audit=json.loads(workspace.report_file(finished['report_id'],'guided_acquisition.json').read_text(encoding='utf-8'))
                        assert audit['detail_attempt_history']['attempts']==history['attempts']
                        assert audit['detail_attempt_history']['selections']==history['selections']
                        prior=json.loads(workspace.report_file(first_report,'guided_acquisition.json').read_text(encoding='utf-8'))
                        assert [a['outcome'] for a in prior['detail_attempt_history']['attempts']]==['ok','manual_required']
                        result['detail_attempts']={'recorded':3,'outcomes':['ok','manual_required','ok'],
                            'modes':['navigation','navigation','login_returned_detail'],'measured_durations':3,
                            'original_report_bound':True,'prior_failure_retained':True}
                        result['checks'].append('durable detail history retains failed gate before successful login return and measured processing time; HTTP counts remain separate')
                        if args.automatic:assert result.get('snapshot_url_change_injected') is True
                        result['checks'].append('one manual fixture login returns to selected detail; no Capture/Resume, home/list detour or duplicate detail GET')
                        result['checks'].append('returned full JD enters original report, previous result/report retained, no recommended job fetched')
                        result['stage']='screenshot';page.screenshot(path=str(out/'selected-detail-completed.png'),full_page=True)
                        result['requests']=calls
                        result['browser_version']=browser.version
                        assert not result['page_errors']
                        result['success']=True;result['stage']='verified'
                    except Exception as exc:
                        # Snapshot before browser/server cleanup changes ownership.
                        # Never include exception text, source lines, DOM or tokens.
                        result['script_line']=next((f.lineno for f in reversed(traceback.extract_tb(exc.__traceback__))
                            if Path(f.filename).resolve()==Path(__file__).resolve()),None)
                        result['fixture_requests']={
                            'search_get':calls.count(('GET','/zhaopin/')),
                            'first_detail_get':calls.count(('GET','/job/1.shtml')),
                            'selected_detail_get':calls.count(('GET','/job/2.shtml')),
                            'login_post':calls.count(('POST','/normal-login')),
                            'home_get':calls.count(('GET','/')),
                            'other':sum(pair not in {('GET','/zhaopin/'),('GET','/job/1.shtml'),
                                ('GET','/job/2.shtml'),('POST','/normal-login'),('GET','/')} for pair in calls)}
                        try:result['wait_diagnostic']=wait_diagnostic(server.guided,server.guided.state())
                        except Exception as diagnostic_error:result['diagnostic_error_type']=type(diagnostic_error).__name__
                        raise
                    finally:browser.close()
            finally:
                server.shutdown();server.server_close();thread.join(timeout=5)
    except Exception as exc:
        # Record fixed classifications, not exception text which can contain
        # the local workbench's ephemeral authentication token.
        result['error_type']=type(exc).__name__
        result['error_code']='browser_policy_blocked' if 'ERR_BLOCKED_BY_ADMINISTRATOR' in str(exc) else 'acceptance_failed'
    finally:
        (out/'results.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(result,ensure_ascii=False,indent=2))
    return 0 if result['success'] else 1

if __name__=='__main__':raise SystemExit(main())
