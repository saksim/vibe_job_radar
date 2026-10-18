"""Real browser and HTTP UI with authored release bytes, never executed."""
from __future__ import annotations
import argparse
import http.client
import json
from pathlib import Path
import sys
import tempfile
import threading
from unittest.mock import patch, MagicMock

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
            server.releases=ReleaseUpdates(workspace,transport_factory=lambda:fixture,current_version='0.3.0',target_platform='windows-x64')
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
                    expect(page.locator('#release-auto-controls')).to_be_hidden()
                    result['checks'].append('opening the workbench and version panel remains offline')
                    page.locator('#release-check').click()
                    expect(page.locator('#release-download')).to_be_enabled(timeout=10000)
                    expect(page.locator('#release-current')).to_contain_text('0.4.0')
                    expect(page.locator('#release-status')).to_contain_text('此发行未签名（unsigned）')
                    expect(page.locator('#release-link')).to_have_attribute('href','https://github.com/saksim/vibe_job_radar/releases/tag/v0.4.0')
                    assert len(fixture.calls)==8  # One inventory and seven verified small assets.
                    result['checks'].append('explicit check presents verified version, release notes and download confirmation')
                    page.set_viewport_size({'width':390,'height':844})
                    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                    page.locator('#release-panel').screenshot(path=str(out/'updates-available-mobile.png'))
                    offsets=[]
                    def interrupted_download(asset,destination,cancelled,progress):
                        raw=fixture.data[asset['name']];offset=destination.tell();offsets.append(offset)
                        if not offset:
                            destination.write(raw[:137]);progress(137)
                            raise ConnectionResetError('authored interruption')
                        destination.write(raw[offset:]);progress(len(raw))
                    fixture.stream=interrupted_download
                    page.locator('#release-download').click()
                    expect(page.locator('#release-status')).to_contain_text('已保留',timeout=10000)
                    expect(page.locator('#release-download')).to_be_disabled()
                    assert server.releases.state()['retained_bytes']==137
                    assert keep.read_text(encoding='utf-8')=='original workspace'
                    page.locator('#release-panel').screenshot(path=str(out/'updates-interrupted-mobile.png'))
                    server.releases.close()
                    server.releases=ReleaseUpdates(workspace,transport_factory=lambda:fixture,current_version='0.3.0',target_platform='windows-x64')
                    page.reload();page.locator('#release-panel summary').first.click()
                    expect(page.locator('#release-download')).to_be_disabled()
                    page.locator('#release-check').click()
                    expect(page.locator('#release-download')).to_be_enabled(timeout=10000)
                    page.locator('#release-download').click()
                    expect(page.locator('#release-status')).to_contain_text('已校验并解压',timeout=10000)
                    assert offsets==[0,137]
                    result['checks'].append('interrupted prefix survives service restart; fresh check and confirmation precede resume; complete digest precedes staging')
                    state=server.releases.state();assert state['status']=='ready'
                    assert Path(state['directory']).joinpath('VibeJobRadar.exe').read_bytes()==b'AUTHORED TEST DATA, NEVER EXECUTE'
                    assert keep.read_text(encoding='utf-8')=='original workspace' and old.read_bytes()==b'original program'
                    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                    page.locator('#release-panel').screenshot(path=str(out/'updates-ready-mobile.png'))
                    result['checks'].append('download prepares a separate checked directory at 390px and preserves old program/workspace')
                    installer=MagicMock()
                    installer.preflight=server.upgrade_preflight
                    installer.install.return_value={'status':'restarting','message':'真实HTTP已确认；验收不执行人工包'}
                    server.releases.installer=installer
                    page.locator('#release-panel summary').first.click()
                    page.locator('#release-panel summary').first.click()
                    expect(page.locator('#release-auto-controls')).to_be_visible()
                    expect(page.locator('#release-install')).to_be_disabled()
                    page.locator('#release-install-consent').check()
                    with patch.object(server.public_tasks,'busy',return_value=True):
                        page.locator('#release-install').click()
                        expect(page.locator('#release-status')).to_contain_text('请先等待或停止')
                        expect(page.locator('#release-install-consent')).not_to_be_checked()
                    installer.install.assert_not_called()
                    page.locator('#release-install-consent').check()
                    page.locator('#release-install').click()
                    expect(page.locator('#release-status')).to_contain_text('验收不执行人工包')
                    installer.install.assert_called_once()
                    assert keep.read_text(encoding='utf8')=='original workspace'
                    result['checks'].append('automatic upgrade needs fresh separate consent; busy tasks block it; HTTP passes verified ZIP to installer once without executing authored bytes')
                    page.reload();page.locator('#release-panel summary').first.click()
                    # A restarted UI would be a fresh service; reset only this test service.
                    server.releases._save(status='idle',message='test service reset')
                    page.reload();page.locator('#release-panel summary').first.click()
                    expect(page.locator('#release-check')).to_be_enabled()
                    with patch.object(fixture,'latest',side_effect=OSError('private failure detail')):
                        page.locator('#release-check').click()
                        expect(page.locator('#release-status')).to_contain_text('版本操作未完成',timeout=10000)
                        expect(page.locator('#release-status')).not_to_contain_text('private failure detail')
                        expect(page.locator('#release-check')).to_be_enabled()
                        expect(page.locator('#release-download')).to_be_disabled()
                    assert keep.read_text(encoding='utf-8')=='original workspace'
                    result['checks'].append('failed recheck gives bounded retry guidance without exposing details or enabling a stale download')
                    from test_publisher_signing import SignedReleaseFixture
                    from vibe_job_radar import publisher_signing
                    server.releases.close();signed=SignedReleaseFixture()
                    server.releases=ReleaseUpdates(workspace,transport_factory=lambda:signed,
                        current_version='0.3.0',target_platform='windows-x64')
                    page.reload();page.locator('#release-panel summary').first.click()
                    page.locator('#release-check').click()
                    expect(page.locator('#release-download')).to_be_enabled(timeout=10000)
                    expect(page.locator('#release-status')).to_contain_text('ARTIFICIAL PUBLISHER')
                    expect(page.locator('#release-status')).to_contain_text('下载后在本机复核')
                    expect(page.locator('#release-status')).not_to_contain_text('核验通过')
                    with patch.object(publisher_signing,'inspect',return_value=signed.signing):
                        page.locator('#release-download').click()
                        expect(page.locator('#release-status')).to_contain_text('本机签名核验通过',timeout=10000)
                    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                    page.locator('#release-panel').screenshot(path=str(out/'signature-state-mobile.png'))
                    result['checks'].append('signed metadata is displayed as pending until actual staging verifier returns; synthetic signature adapter, no payload execution')
                    from test_release_posix import NativeReleaseFixture
                    for platform in ('linux-x64','macos-arm64'):
                        server.releases.close();native=NativeReleaseFixture();handoff=MagicMock()
                        handoff.preflight=server.upgrade_preflight
                        handoff.install.return_value={'status':'restarting','message':'原生界面已确认；不执行人工程序'}
                        server.releases=ReleaseUpdates(workspace,transport_factory=lambda:native,
                            current_version='0.12.0',target_platform=platform,installer=handoff)
                        page.reload();page.locator('#release-panel summary').first.click()
                        assert not native.calls
                        page.locator('#release-check').click()
                        expect(page.locator('#release-download')).to_be_enabled(timeout=10000)
                        expect(page.locator('#release-link')).to_have_attribute('href',
                            'https://github.com/saksim/vibe_job_radar/releases/tag/desktop-v0.13.0')
                        expect(page.locator('#release-install')).to_be_disabled()
                        page.locator('#release-install-consent').check()
                        page.locator('#release-install').click()
                        expect(page.locator('#release-status')).to_contain_text('不执行人工程序',timeout=10000)
                        handoff.install.assert_called_once()
                        assert handoff.install.call_args.args[1]['platform']==platform
                        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                        page.locator('#release-panel').screenshot(path=str(out/(platform+'-update-mobile.png')))
                        result['checks'].append(platform+' actual UI and HTTP selects native package and distinct consent; inert installer adapter only')
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
