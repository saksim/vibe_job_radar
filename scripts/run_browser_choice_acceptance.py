"""Windows installed browser: real headed collector, local UI and persistence.

The initial bundled crash is a fixture. The selected browser is launched, not installed
by this script. No real account, profile, site navigation or automated fallback.
"""
from __future__ import annotations
import argparse
import json
import sys
import tempfile
import threading
from urllib.parse import urlsplit
from pathlib import Path
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from vibe_job_radar.guided.browser_health import environment_report, failed_report, probe_browser
from vibe_job_radar.workspace import Workspace
from vibe_job_radar.workbench import LocalServer


def open_workbench(page, server, expect):
    """Check application readiness, not a late browser-wide load event.

    One navigation, no retry/fallback. A 200 alone cannot pass: the real JS must
    authenticate, populate counts and remove the fragment before proceeding.
    """
    response = page.goto(server.entry_url, wait_until='domcontentloaded', timeout=30000)
    if response is None or response.status != 200:
        raise AssertionError('workbench document did not return HTTP 200')
    expect(page.locator('#counts')).to_contain_text('真实记录 0', timeout=30000)
    if page.url != server.origin + '/':
        raise AssertionError('workbench authentication initialization incomplete')
    expect(page.locator('a[href="/guided"]')).to_be_visible()


def main():
    from playwright.sync_api import sync_playwright, expect
    parser=argparse.ArgumentParser();parser.add_argument('--channel',choices=['msedge','chrome'],default='msedge')
    parser.add_argument('--backend',choices=['bridge','native'],default='bridge')
    parser.add_argument('--ui-channel',choices=['msedge','chrome'])
    args=parser.parse_args();channel=args.channel
    native=args.backend=='native'
    label='Microsoft Edge' if channel=='msedge' else 'Google Chrome'
    folder=('browser-choice' if channel=='msedge' else 'browser-choice-chrome')+('-native' if native else '')
    out=ROOT/'browser-acceptance'/folder;out.mkdir(parents=True,exist_ok=True)
    ready_text='已通过原生实验的空白页' if native else '已通过空白页启动检查'
    choose_button='#use-native-browser-choice' if native else '#use-browser-choice'
    result={'success':False,'stage':'startup','ui_events':[],'checks':[],'page_errors':[], 'external_ui_requests':[],
            'channel':channel,'backend':args.backend,'scope':'Real selected installed browser headed collector and local UI; bundled native exception is artificial. Not user-PC crash reproduction or site certification.'}
    fixture=failed_report({**environment_report(),'stage':'launch'},RuntimeError(
        '<launched> pid=123\n[pid=123] <process did exit: exitCode=3221226356, signal=null>'))
    try:
        with tempfile.TemporaryDirectory() as tmp:
            workspace=Workspace(tmp);server=LocalServer(workspace)
            thread=threading.Thread(target=server.serve_forever,kwargs={'poll_interval':.01},daemon=True);thread.start()
            def probe(**kw):
                return probe_browser(**kw) if kw.get('channel')==channel else fixture
            server.guided._health_probe=probe
            server.guided._installer=lambda *a,**k:(_ for _ in ()).throw(AssertionError('unexpected install'))
            try:
                with sync_playwright() as pw:
                    # The workbench UI browser is separate from the headed collector.
                    browser=pw.chromium.launch(channel=args.ui_channel or channel,headless=True)
                    try:
                        context=browser.new_context(viewport={'width':1280,'height':960})
                        def route(r):
                            if r.request.url.startswith(server.origin+'/'):r.continue_()
                            else:result['external_ui_requests'].append(r.request.url);r.abort()
                        context.route('**/*',route)
                        page=context.new_page();page.on('pageerror',lambda e:result['page_errors'].append(str(e)))
                        page.on('dialog',lambda d:d.accept())
                        # Fixed path classifications only: no token, query,
                        # response body or exception text in developer evidence.
                        def observe_response(response):
                            if len(result['ui_events']) >= 32:
                                return
                            parsed = urlsplit(response.url)
                            if parsed.scheme + '://' + parsed.netloc == server.origin:
                                route_name = parsed.path if parsed.path in {'/', '/app.js', '/api/status', '/guided', '/guided.js'} else 'other_local'
                                result['ui_events'].append({'route':route_name,'status':response.status})
                        page.on('response', observe_response)
                        result['stage'] = 'workbench-readiness'
                        open_workbench(page, server, expect)
                        result['checks'].append('local document 200, JS authenticated status and token-free URL are ready before navigation')
                        result['stage'] = 'second-cold-workbench-readiness'
                        second = browser.new_context(viewport={'width':1280,'height':960})
                        try:
                            second.route('**/*', route)
                            second_page = second.new_page()
                            second_page.on('pageerror', lambda e: result['page_errors'].append(str(e)))
                            second_page.on('response', observe_response)
                            open_workbench(second_page, server, expect)
                            result['checks'].append('second independent cold UI context authenticates without reload or retry')
                        finally:
                            second.close()
                        result['stage'] = 'collector-choice'
                        page.locator('a[href="/guided"]').click()
                        expect(page.locator('#environment')).to_contain_text('不表示缺少组件')
                        page.locator('#check-browser').click()
                        expect(page.locator('#browser-summary')).to_contain_text('停止循环重装')
                        expect(page.locator('#browser-history')).to_contain_text('0xC0000374')
                        result['checks'].append('operation log is not installation detection; failed check records minimal history')
                        page.locator('#browser-alternative summary').click()
                        page.locator('#browser-choice').select_option(channel)
                        page.locator(choose_button).click()
                        expect(page.locator('#browser-selected')).to_contain_text('当前采集浏览器：本机 '+label,timeout=45000)
                        expect(page.locator('#browser-summary')).to_contain_text(ready_text)
                        health=server.guided.state()['browser_health']
                        assert health['selection_applied'] and health['mode']=='headed'
                        assert health['browser_channel']==channel and health['launch_tested']
                        assert health['network_backend']==args.backend
                        if native:
                            assert health['native_component']['external_connections']==0
                            assert health['native_component']['cleanup_verified']
                        result['browser_version']=health['browser_version']
                        result['edge_version' if channel=='msedge' else 'chrome_version']=health['browser_version']
                        result['playwright_version']=health['playwright_version']
                        assert server.guided.state()['jobs']==[]
                        assert server.guided.ledger.summary('liepin')['request']['day']==0
                        result['checks'].append('explicit selected browser choice runs the real headed collector on a blank page before persisting; no install, jobs or source quota')
                        page.screenshot(path=str(out/(('edge' if channel=='msedge' else 'chrome')+'-ready.png')),full_page=True)
                        # Service restart exercises the persisted product state, not a browser reload.
                        server.guided.close()
                        from vibe_job_radar.guided.service import GuidedService
                        server.guided=GuidedService(workspace)
                        page.reload(wait_until="domcontentloaded")
                        expect(page.locator('#browser-selected')).to_contain_text('本机 '+label)
                        expect(page.locator('#browser-choice')).to_have_value(channel)
                        expect(page.locator('#browser-summary')).to_contain_text('尚未验证')
                        expect(page.locator('#browser-history')).to_contain_text('历史，不代表本次就绪')
                        assert not server.guided.state()['browser_health']['ready']
                        result['checks'].append('restart keeps the selected channel and history but never calls a historical green check current readiness')
                        result['stage']='restart-recheck'
                        if native:
                            page.locator('#browser-alternative summary').click()
                        page.locator(choose_button if native else '#check-browser').click()
                        expect(page.locator('#browser-summary')).to_contain_text(ready_text,timeout=45000)
                        assert server.guided.state()['browser_health']['browser_channel']==channel
                        page.set_viewport_size({'width':390,'height':844})
                        assert page.evaluate('() => document.documentElement.scrollWidth <= innerWidth')
                        page.screenshot(path=str(out/(('edge' if channel=='msedge' else 'chrome')+'-mobile.png')),full_page=True)
                        assert not result['page_errors'] and not result['external_ui_requests']
                        assert not workspace.db.exists()
                        result['checks'].append('regular recheck uses the saved channel, no source requests or JS errors; narrow layout fits')
                        result['stage']='passed'
                        result['success']=True
                    finally:browser.close()
            finally:server.shutdown();server.server_close();thread.join(timeout=5)
    except Exception as exc:
        result['error_type'] = type(exc).__name__
        # Playwright's raw navigation exception embeds the local #token.
        # Preserve failure and fixed stage without printing that secret.
        raise RuntimeError('browser choice acceptance failed; see fixed stage and local response metadata') from None
    finally:(out/'results.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(result,ensure_ascii=True,indent=2))


if __name__=='__main__':main()
