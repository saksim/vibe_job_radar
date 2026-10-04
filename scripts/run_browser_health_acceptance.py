"""Real headed startup recovery; missing cache/download failure are controlled fixtures.

Run under a desktop (Linux: xvfb-run -a). No job URL is visited and no package is
installed by this script. CI explicitly installs the selected Playwright/browser.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'src'))
from vibe_job_radar.guided.browser_health import probe_browser
from vibe_job_radar.guided.browser_install import CommandResult
from vibe_job_radar.workbench import LocalServer
from vibe_job_radar.workspace import Workspace


def main():
    from playwright.sync_api import sync_playwright, expect
    output=ROOT/'browser-acceptance'/'browser-health'
    output.mkdir(parents=True,exist_ok=True)
    result={'success':False,'checks':[],'page_errors':[], 'unexpected_ui_requests':[],
            'scope':'Real headed collector initialization with no site visits. Missing cache and download failure are injected, no remote platform authentication.'}
    override=os.environ.get('RADAR_TEST_CHROMIUM')  # developer-only, not an API input
    with tempfile.TemporaryDirectory() as tmp:
        server=LocalServer(Workspace(tmp))
        thread=threading.Thread(target=server.serve_forever,kwargs={'poll_interval':.01},daemon=True)
        thread.start()
        try:
            with sync_playwright() as pw:
                browser=pw.chromium.launch(headless=True,**({'executable_path':override} if override else {}))
                context=browser.new_context(viewport={'width':1320,'height':980})
                def local_only(route):
                    if route.request.url.startswith(server.origin+'/'):route.continue_()
                    else:result['unexpected_ui_requests'].append(route.request.url);route.abort()
                context.route('**/*',local_only)
                page=context.new_page();page.on('pageerror',lambda error:result['page_errors'].append(str(error)))
                page.goto(server.entry_url);page.locator('a[href="/guided"]').click()
                expect(page.locator('#check-browser')).to_be_visible()
                result['checks'].append('browser check accessible from authenticated beginner interface')
                with patch.dict(os.environ, {'PLAYWRIGHT_BROWSERS_PATH':str(Path(tmp)/'empty-browser-cache')}):
                    page.locator('#check-browser').click()
                    expect(page.locator('#browser-summary')).to_contain_text('配套 Chromium 可执行文件不存在',timeout=45000)
                    deadline=time.monotonic()+10
                    while server.guided.state()['busy'] and time.monotonic()<deadline:page.wait_for_timeout(100)
                    first=server.guided.state()['browser_health']
                    assert first['code']=='browser_executable_missing' and not first['launch_tested']
                    assert first['playwright_version']
                    result['checks'].append('installed Python package plus missing actual binary produces precise missing-executable diagnosis')
                    page.locator('summary').filter(has_text='浏览器诊断').click()
                    expect(page.locator('#browser-diagnostic')).to_contain_text('playwright install chromium')
                    page.screenshot(path=str(output/'missing-binary.png'),full_page=True)
                # Returning to the proper cache models the result of installing the
                # matching browser in the user's current interpreter environment.
                if override:
                    server.guided._health_probe=lambda:probe_browser(executable_path=override)
                page.locator('#check-browser').click()
                expect(page.locator('#browser-summary')).to_contain_text('已通过空白页启动检查',timeout=45000)
                second=server.guided.state()['browser_health']
                assert second['ready'] and second['mode']=='headed' and second['launch_tested']
                detail=second['blank_page_check']
                assert detail['step']=='verified' and set(detail['elapsed_ms'])=={'set_content','read_title'}
                assert detail['page_closed'] is False and detail['browser_connected'] is True
                assert not any(detail['events_before_cleanup'][name] for name in ('crash','close','disconnected'))
                result['blank_page_check']=detail
                result['checks'].append('same-process recheck starts actual headed collection backend and clears stale not-ready state')
                result['playwright_version']=second['playwright_version']
                result['python_version']=second['python_version']
                assert not server.workspace.db.exists()
                assert server.guided.ledger.summary('boss')['request']['day']==0
                result['checks'].append('blank startup probe creates no jobs and consumes no collection budget')
                page.screenshot(path=str(output/'browser-ready.png'),full_page=True)
                # Do not install or contact a package server in a browser test.
                server.guided._installer=lambda *a,**kw:CommandResult(1,'simulated download refusal: https://u:secret@host/?token=secret')
                page.on('dialog',lambda dialog:dialog.accept())
                page.locator('#install').click()
                expect(page.locator('#browser-summary')).to_contain_text('组件安装命令失败',timeout=15000)
                expect(page.locator('#browser-diagnostic')).to_contain_text('simulated download refusal')
                assert 'secret' not in page.locator('#browser-diagnostic').inner_text()
                result['checks'].append('install button shows failing phase and sanitized output instead of false installed status')
                page.reload();expect(page.locator('#browser-summary')).to_contain_text('组件安装命令失败')
                result['checks'].append('reload preserves current-process diagnosis and does not rerun installation')
                page.set_viewport_size({'width':390,'height':844})
                assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
                assert result['page_errors']==[] and result['unexpected_ui_requests']==[]
                result['checks'].append('narrow viewport does not overflow; no JavaScript errors or external UI requests')
                result['success']=True
                browser.close()
        finally:
            server.shutdown();server.server_close();thread.join(timeout=5)
            (output/'results.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(result,ensure_ascii=True,indent=2))


if __name__=='__main__':main()
