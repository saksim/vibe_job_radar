"""Actual browser verifies existing report and improved failure UI; fixture upstream.

Real public GET is separately exercised by check_live_public_example.py --live.
The local server adds only consent-based task handoff endpoints; no password or arbitrary remote-target endpoints.
"""
import contextlib
import copy
import io
import json
import os
import runpy
import sys
import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from vibe_job_radar.workbench import LocalServer
from vibe_job_radar.workspace import Workspace
from vibe_job_radar.public_example import JOB_ID, SOURCE_URL
from vibe_job_radar.network import SafeHTTP
from vibe_job_radar.guided.contracts import PageSnapshot



class HandoffFixtureBrowser:
    """Artificial source boundary for the actual local UI/browser acceptance."""
    opened = []
    def __init__(self, *args): pass
    def open(self, url, **kwargs):
        self.opened.append(url)
        return PageSnapshot(url, '<h1>AI架构师</h1><div class="job-sec-text">'
                            '人工验收夹具，不是市场数据。要求熟练使用 Cursor 辅助开发，编写单元测试并进行代码审查。'
                            '</div>')
    def pump(self): pass
    def close(self): pass


def local_query_journey(pw, options, result, out):
    """Real local UI, fixture upstream: no operator server/configuration."""
    from playwright.sync_api import expect
    from vibe_job_radar.local_public import API_URL
    board={'jobs':[{'id':880000+i,'absolute_url':f'https://job-boards.greenhouse.io/anthropic/jobs/{880000+i}',
            'title':f'Architect FIXTURE {i}', 'location':{'name':'TEST ONLY'},
            'content':'<p>ARTIFICIAL LOCAL QUERY FIXTURE — NOT MARKET DATA.</p><p>'
                      'Use Cursor for AI-assisted coding. Review generated code, write comprehensive unit tests, '
                      'and design dependable software. This is a controlled test, not a vacancy.</p>'}
            for i in range(21)],'meta':{'total':21}}
    with tempfile.TemporaryDirectory() as tmp:
        server=LocalServer(Workspace(tmp))
        now=[time.time()];server.public_tasks.hybrid.clock=lambda:now[0]
        entered, release = threading.Event(), threading.Event()
        def held_source(url):
            entered.set()
            if not release.wait(20):
                raise AssertionError('browser did not finish cancellation before releasing fixture source')
            return board
        thread=threading.Thread(target=server.serve_forever,kwargs={'poll_interval':.01},daemon=True);thread.start()
        try:
            with patch.object(SafeHTTP,'json',side_effect=held_source) as source:
                browser=pw.chromium.launch(**options)
                context=browser.new_context(viewport={'width':1360,'height':1000})
                context.route('**/*',lambda route:route.continue_() if route.request.url.startswith(server.origin+'/') else route.abort())
                page=context.new_page();page.on('pageerror',lambda error:result['page_errors'].append(str(error)))
                page.goto(server.entry_url)
                expect(page.locator('#public-service-status')).to_contain_text('默认在本机直接获取')
                expect(page.locator('#public-search-button')).to_be_enabled()
                source.assert_not_called()
                page.locator('#public-search [name=query]').fill('Architect')
                page.locator('#public-search-button').click()
                source.assert_not_called()  # Browser form requires explicit consent.
                page.locator('#public-search [name=consent]').check()
                page.locator('#public-search-button').click()
                assert entered.wait(5)
                interrupted_id = server.public_tasks.state()['task']['id']
                expect(page.locator('#public-cancel')).to_be_visible()
                page.locator('#public-cancel').click()
                expect(page.locator('#public-status')).to_contain_text('正在停止')
                release.set()
                expect(page.locator('#public-status')).to_contain_text('公开任务已停止', timeout=30000)
                expect(page.locator('#public-resume')).to_be_visible()
                assert not server.public_tasks.state()['task']['report_id']
                assert not list((server.workspace.root/'reports').iterdir())
                page.reload()
                expect(page.locator('#public-resume')).to_be_visible()
                expect(page.locator('#public-saved-query')).to_contain_text('Architect')
                source.assert_called_once_with(API_URL)
                page.locator('#public-resume').click()
                expect(page.locator('#public-status')).to_contain_text('本页 20 条',timeout=30000)
                expect(page.locator('#public-status')).to_contain_text('匹配 21 条')
                expect(page.locator('#public-changes')).to_contain_text('不将首次记录算作新增')
                expect(page.locator('#public-next')).to_be_visible()
                first=server.public_tasks.state()['task']
                assert first['report_id'] and first['next_cursor'] and first['execution_mode']=='local_direct'
                assert first['id'] == interrupted_id and first['attempt'] == 2 and first['cache_reused']
                result['checks'].append('UI stops an in-flight public query before report creation; reload stays offline; explicit resume uses same query/task and retained cache without a second source request')
                page.locator('#public-next').click()
                expect(page.locator('#public-status')).to_contain_text('本页 1 条',timeout=30000)
                expect(page.locator('#public-next')).to_be_hidden()
                second=server.public_tasks.state()['task']
                assert second['report_id']!=first['report_id'] and second['cache_reused']
                source.assert_called_once_with(API_URL)
                page.reload()
                expect(page.locator('#public-status')).to_contain_text('本页 1 条')
                source.assert_called_once_with(API_URL)
                changed=copy.deepcopy(board)
                changed['jobs'].pop(1)
                changed['jobs'][0]['content']+='<p>New artificial responsibility.</p>'
                new_job=copy.deepcopy(board['jobs'][1])
                new_job.update(id=880099,absolute_url='https://job-boards.greenhouse.io/anthropic/jobs/880099')
                changed['jobs'].append(new_job);source.side_effect=None;source.return_value=changed
                now[0]+=601
                # Reload resets form values: explicitly confirm this later query.
                page.locator('#public-search [name=query]').fill('Architect')
                page.locator('#public-search [name=consent]').check()
                page.locator('#public-search-button').click()
                expect(page.locator('#public-changes')).to_contain_text('新增 1 条，修改 1 条，本次未出现 1 条，未变 19 条',timeout=30000)
                expect(page.locator('#public-changes')).to_contain_text('不等于岗位已关闭')
                third=server.public_tasks.state()['task']
                assert third['status']=='completed' and third['report_id']!=second['report_id']
                audit=server.workspace.root/'reports'/third['report_id']/'catalog_changes.json'
                assert json.loads(audit.read_text(encoding='utf-8'))['missing']==['880001']
                assert source.call_count==2
                page.reload()
                expect(page.locator('#public-changes')).to_contain_text('修改 1 条')
                assert source.call_count==2
                result['checks'].append('complete catalog audit: baseline is not additions; explicit later fetch yields 1 added/1 modified/1 missing/19 unchanged; report audit persists and reload does not fetch; missing is not closure')
                page.set_viewport_size({'width':390,'height':844})
                page.screenshot(path=str(out/'local-public-query.png'),full_page=True)
                page.locator('#public-entry').screenshot(path=str(out/'public-lifecycle.png'))
                assert page.evaluate('document.documentElement.scrollWidth<=window.innerWidth'), page.evaluate("""() =>
                    [...document.querySelectorAll('body *')].filter(el => !el.closest('.scroll') &&
                      el.scrollWidth > el.clientWidth && getComputedStyle(el).overflowX === 'visible')
                      .map(el => ({tag:el.tagName,id:el.id,text:el.textContent.slice(0,100),
                        width:el.clientWidth,scroll:el.scrollWidth})).slice(0,20)""")
                assert not result['page_errors']
                result['checks'].append('default local query needs no own server: consent -> one fixed fixture GET -> 20/1 local pages -> separate reports; reload does not fetch; no private query upload')
                browser.close()
        finally:
            release.set()
            server.shutdown();server.server_close();thread.join(timeout=5)


def main():
    from playwright.sync_api import sync_playwright, expect
    out=ROOT/'browser-acceptance'/'recovery';out.mkdir(parents=True,exist_ok=True)
    result={'success':False,'checks':[],'page_errors':[],
            'scope':'Artificial upstream for UI/command only; actual public GET acceptance is separate.'}
    payload={'id':JOB_ID,'absolute_url':SOURCE_URL,'title':'Technical Architect',
             'content':'<p>ARTIFICIAL UI FIXTURE — NOT MARKET DATA.</p><p>Use Claude Code for AI-assisted coding and review generated code. Build dependable systems with comprehensive tests.</p>',
             'location':{'name':'TEST ONLY'}}
    with tempfile.TemporaryDirectory() as tmp:
        command=runpy.run_path(str(ROOT/'scripts/run_real_example.py'))['main']
        with patch.object(SafeHTTP,'json',return_value=payload) as source,contextlib.redirect_stdout(io.StringIO()):
            assert command(['--yes','--no-browser','--workspace',tmp])==0
            assert command(['--yes','--no-browser','--workspace',tmp])==0
            assert source.call_count==1
        result['checks'].append('user command saves one full record/report; repeat command visibly caches without extra upstream GET')
        server=LocalServer(Workspace(tmp))
        server.guided.factory=HandoffFixtureBrowser
        HandoffFixtureBrowser.opened=[]
        thread=threading.Thread(target=server.serve_forever,kwargs={'poll_interval':.01},daemon=True);thread.start()
        try:
            task=server.collector.start({'mode':'urls','platforms':['boss'],'permit_platforms':['boss'],
                'roles':['architect'],'urls':'https://www.zhipin.com/job_detail/test-fixture-a.html\nhttps://www.zhipin.com/job_detail/test-fixture-b.html',
                'detail_budget':1,'rights_note':'UI人工测试','consent':True})
            task['details'][0]['status']='redirect_not_followed';task['details'][1]['status']='budget_skipped'
            task.update(status='needs_attention',phase='report',detail_attempts=1)
            server.collector._save(task)
            with sync_playwright() as pw:
                options={'headless':True}
                if os.environ.get('RADAR_TEST_CHROMIUM'):options['executable_path']=os.environ['RADAR_TEST_CHROMIUM']
                browser=pw.chromium.launch(**options)
                context=browser.new_context(viewport={'width':1360,'height':1000},accept_downloads=True)
                context.route('**/*',lambda r:r.continue_() if r.request.url.startswith(server.origin+'/') else r.abort())
                page=context.new_page();page.on('pageerror',lambda e:result['page_errors'].append(str(e)))
                page.goto(server.entry_url)
                expect(page.locator('#counts')).to_contain_text('真实记录 1')
                page.locator('#runs button').first.click()
                expect(page.locator('#requirements')).to_contain_text('Claude Code')
                with page.expect_download() as pending:
                    page.get_by_role('button',name='requirements_zh.csv',exact=True).click()
                pending.value.save_as(out/'fixture-requirements.csv')
                assert 'Claude Code' in (out/'fixture-requirements.csv').read_text(encoding='utf-8-sig')
                result['checks'].append('saved public-case report is available through unchanged local UI and authenticated CSV download')
                page.screenshot(path=str(out/'example-report.png'),full_page=True)
                page.goto(server.origin+'/advanced')
                expect(page.locator('#collect-history')).to_contain_text('needs_attention')
                page.locator('#collect-load').click()
                expect(page.locator('#collect-result')).to_contain_text('未执行：本批正文尝试预算已用完')
                expect(page.locator('#collect-result')).to_contain_text('历史日志不能判断是否需要登录')
                expect(page.locator('#collect-progress')).to_contain_text('无浏览器登录会话')
                result['checks'].append('historical unknown redirect is distinguished from an unexecuted budget item')
                page.screenshot(path=str(out/'redirect-guidance.png'),full_page=True)
                page.get_by_role('button',name='将未完成链接转交浏览器（先预览，不联网）',exact=True).click()
                expect(page.locator('#collect-result')).to_contain_text('原HTTP正文尝试 1/1，剩余 0')
                assert not server.guided.state()['jobs']
                assert not HandoffFixtureBrowser.opened
                page.once('dialog',lambda dialog:dialog.dismiss())
                page.get_by_role('button',name='确认转交并继续',exact=True).click()
                assert not server.guided.state()['jobs']
                page.once('dialog',lambda dialog:dialog.accept())
                page.get_by_role('button',name='确认转交并继续',exact=True).click()
                page.wait_for_url(server.origin+'/guided?task=*')
                expect(page.locator('#task-status')).to_contain_text('批次结束',timeout=15000)
                child=server.guided.state()['jobs'][0]
                assert page.locator('#task').input_value()==child['id']
                assert child['report_id'] and child['handoff']['parent_id']==task['id']
                assert HandoffFixtureBrowser.opened==[task['details'][0]['url']]
                assert server.collector._load(task['id'])['detail_attempts']==1
                result['checks'].append('HTTP failure preview performs no network; cancel creates nothing; confirmation transfers exact failed URL, preserves budget and roles, produces isolated report in selected browser task')
                page.goto(server.origin+'/advanced')
                # Navigation finishes before the async history request. Wait
                # for the exact saved parent instead of clicking an empty list.
                page.locator('#collect-history').select_option(task['id'])
                page.locator('#collect-load').click()
                page.get_by_role('button',name='将未完成链接转交浏览器（先预览，不联网）',exact=True).click()
                expect(page.get_by_role('link',name='继续已保存的浏览器任务（不重复创建）',exact=True)).to_be_visible()
                assert len(server.guided.state()['jobs'])==1 and len(HandoffFixtureBrowser.opened)==1
                result['checks'].append('repeat preview points to persisted browser task without re-requesting successful records')
                page.screenshot(path=str(out/'handoff-completed.png'),full_page=True)
                page.set_viewport_size({'width':390,'height':844})
                assert page.evaluate('document.documentElement.scrollWidth<=window.innerWidth')
                assert not result['page_errors']
                result['checks'].append('no page-level overflow or JavaScript errors')
                browser.close()
                local_query_journey(pw,options,result,out)
                result['success']=True
        finally:
            server.shutdown();server.server_close();thread.join(timeout=5)
            (out/'results.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(result,ensure_ascii=True,indent=2))


if __name__=='__main__':main()
