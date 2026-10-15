"""Real browser and HTTP UI with authored release bytes, never executed."""
from __future__ import annotations
import argparse
import http.client
import json
from pathlib import Path
import sys
import tempfile
import threading
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'src'),str(ROOT/'tests')]
from test_release_updates import ReleaseFixture
from vibe_job_radar.release_updates import ReleaseUpdates
from vibe_job_radar.workspace import Workspace
from vibe_job_radar.workbench import LocalServer


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--browser-executable')
    parser.add_argument('--out',type=Path)
    args=parser.parse_args()
    from playwright.sync_api import sync_playwright,expect
    from vibe_job_radar.qualification import output_directory
    out=output_directory(ROOT,args.out or ROOT/'browser-acceptance/updates');out.mkdir(parents=True,exist_ok=True)
    result={'success':False,'checks':[],'page_errors':[],'external_requests':[],
            'scope':'Actual Edge/Chromium and authenticated workbench HTTP; authored GitHub metadata/archive. No public release download or downloaded executable run.'}
    server=None;worker=None
    try:
        with tempfile.TemporaryDirectory(prefix='release-updates-browser-') as temporary:
            root=Path(temporary);workspace=Workspace(root/'workspace');fixture=ReleaseFixture()
            keep=workspace.root/'private-original.txt';keep.write_text('original workspace',encoding='utf-8')
            old=root/'old-program.exe';old.write_bytes(b'original program')
            server=LocalServer(workspace);server.releases.close()
            server.releases=ReleaseUpdates(workspace,transport_factory=lambda:fixture,current_version='0.3.0')
            worker=threading.Thread(target=server.serve_forever,kwargs={'poll_interval':.01},daemon=True);worker.start()
            connection=http.client.HTTPConnection('127.0.0.1',server.server_address[1],timeout=10)
            for path in ('/api/releases/state','/api/releases/check'):
                data='{"consent":true}' if path.endswith('check') else None
                connection.request('POST' if data else 'GET',path,body=data,
                                   headers={'Content-Type':'application/json'} if data else {})
                response=connection.getresponse();response.read();assert response.status==403
            connection.close();assert not fixture.calls
            result['checks'].append('unauthenticated state and update actions rejected without any release request')
            with sync_playwright() as pw:
                launch={'headless':True}
                if args.browser_executable:launch['executable_path']=args.browser_executable
                browser=pw.chromium.launch(**launch)
                try:
                    context=browser.new_context(viewport={'width':1100,'height':900})
                    def local_only(route):
                        if route.request.url.startswith(server.origin+'/'):route.continue_()
                        else:result['external_requests'].append('unexpected_external_browser_request');route.abort()
                    context.route('**/*',local_only)
                    page=context.new_page();page.on('pageerror',lambda error:result['page_errors'].append(str(error)))
                    page.goto(server.entry_url);page.locator('#release-panel summary').first.click()
                    expect(page.locator('#release-current')).to_contain_text('0.3.0')
                    assert not fixture.calls
                    expect(page.locator('#release-download')).to_be_disabled()
                    result['checks'].append('opening the workbench and version panel remains offline')
                    page.locator('#release-check').click()
                    expect(page.locator('#release-download')).to_be_enabled(timeout=10000)
                    expect(page.locator('#release-current')).to_contain_text('0.4.0')
                    expect(page.locator('#release-link')).to_have_attribute('href','https://github.com/saksim/vibe_job_radar/releases/tag/v0.4.0')
                    assert len(fixture.calls)==8  # One inventory and seven verified small assets.
                    result['checks'].append('explicit check presents verified version, release notes and download confirmation')
                    page.set_viewport_size({'width':390,'height':844})
                    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                    page.locator('#release-panel').screenshot(path=str(out/'updates-available-mobile.png'))
                    page.locator('#release-download').click()
                    expect(page.locator('#release-status')).to_contain_text('已校验并解压',timeout=10000)
                    state=server.releases.state();assert state['status']=='ready'
                    assert Path(state['directory']).joinpath('VibeJobRadar.exe').read_bytes()==b'AUTHORED TEST DATA, NEVER EXECUTE'
                    assert keep.read_text(encoding='utf-8')=='original workspace' and old.read_bytes()==b'original program'
                    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                    page.locator('#release-panel').screenshot(path=str(out/'updates-ready-mobile.png'))
                    result['checks'].append('download prepares a separate checked directory at 390px and preserves old program/workspace')
                    with patch.object(fixture,'latest',side_effect=OSError('private failure detail')):
                        page.locator('#release-check').click()
                        expect(page.locator('#release-status')).to_contain_text('版本操作未完成',timeout=10000)
                        expect(page.locator('#release-status')).not_to_contain_text('private failure detail')
                        expect(page.locator('#release-check')).to_be_enabled()
                        expect(page.locator('#release-download')).to_be_disabled()
                    assert keep.read_text(encoding='utf-8')=='original workspace'
                    result['checks'].append('failed recheck gives bounded retry guidance without exposing details or enabling a stale download')
                finally:browser.close()
            server.shutdown();server.server_close();worker.join(10);server=None
            assert not result['page_errors'] and not result['external_requests']
        result['success']=True
    except Exception as error:result['error']=str(error)
    finally:
        if server is not None:server.shutdown();server.server_close()
        if worker is not None:worker.join(10)
        (out/'result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
        print(json.dumps(result,ensure_ascii=False,indent=2))
    return 0 if result['success'] else 1


if __name__=='__main__':raise SystemExit(main())
