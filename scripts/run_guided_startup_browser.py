"""Delay actual script delivery: initial controls cannot swallow a first action."""
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from vibe_job_radar.workbench import LocalServer, Handler
from vibe_job_radar.workspace import Workspace
from vibe_job_radar.guided.browser_health import environment_report, failed_report


def main():
    from playwright.sync_api import sync_playwright, expect
    out=ROOT/'browser-acceptance'/'guided-startup';out.mkdir(parents=True,exist_ok=True)
    result={'success':False,'checks':[],'source_requests':0,'page_errors':[],
        'scope':'Actual browser and HTTP UI; delayed local JavaScript and artificial missing-browser health response; no platform visits or installs.'}
    waiting,release=threading.Event(),threading.Event();actions=[];probes=[]
    original_get,original_post=Handler.do_GET,Handler.do_POST
    with tempfile.TemporaryDirectory(prefix='radar-guided-startup-') as tmp:
        server=LocalServer(Workspace(tmp))
        def delayed(handler):
            if handler.server is server and handler.path=='/guided.js':
                waiting.set()
                if not release.wait(15):raise AssertionError('fixture script was not released')
            return original_get(handler)
        def post(handler):
            if handler.server is server:actions.append(handler.path)
            return original_post(handler)
        def health():
            probes.append(True)
            return failed_report(environment_report(),FileNotFoundError('artificial missing binary'),code='browser_executable_missing')
        server.guided._health_probe=health
        thread=threading.Thread(target=server.serve_forever,kwargs={'poll_interval':.01},daemon=True);thread.start()
        try:
            with patch.object(Handler,'do_GET',delayed),patch.object(Handler,'do_POST',post),sync_playwright() as pw:
                options={'headless':True}
                if os.environ.get('RADAR_TEST_CHROMIUM'):options['executable_path']=os.environ['RADAR_TEST_CHROMIUM']
                browser=pw.chromium.launch(**options)
                try:
                    page=browser.new_page(viewport={'width':1320,'height':980})
                    page.on('pageerror',lambda error:result['page_errors'].append(str(error)))
                    page.route('**/*',lambda route:route.continue_() if route.request.url.startswith(server.origin+'/') else route.abort())
                    page.goto(server.entry_url)
                    page.goto(server.origin+'/guided',wait_until='commit')
                    deadline=time.monotonic()+5
                    while not waiting.is_set() and time.monotonic()<deadline:
                        page.wait_for_timeout(20)  # Dispatch the intercepted script request.
                    assert waiting.is_set(), 'fixture did not hold the actual script response'
                    expect(page.locator('#check-browser')).to_be_visible()
                    assert page.locator('#check-browser').is_disabled(), 'initial check action is enabled before its script is available'
                    assert page.locator('#search-form input[name=keyword]').is_disabled()
                    assert actions==[] and probes==[]
                    assert page.locator('a[href="/"]').first.is_enabled()
                    result['checks'].append('while script response is held, visible actions and form fields stay disabled; navigation remains available and no action is sent')
                    release.set()
                    expect(page.locator('#check-browser')).to_be_enabled(timeout=15000)
                    page.locator('#check-browser').click()
                    expect(page.locator('#browser-summary')).to_contain_text('配套 Chromium 可执行文件不存在',timeout=15000)
                    assert actions==['/api/guided/check_browser'] and len(probes)==1
                    result['checks'].append('after actual initialization the first click sends exactly one check and displays its result')
                    page.set_viewport_size({'width':390,'height':844})
                    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                    assert not result['page_errors'] and not server.workspace.db.exists()
                    assert server.guided.ledger.summary('liepin')['request']['day']==0
                    result.update(success=True,browser_version=browser.version)
                    result['checks'].append('mobile layout, zero job records and zero collection quota preserved')
                finally:
                    release.set();browser.close()
        finally:
            release.set();server.shutdown();server.server_close();thread.join(5)
            (out/'results.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(result,ensure_ascii=False,indent=2))


if __name__=='__main__':main()
