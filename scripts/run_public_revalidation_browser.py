"""Real local UI and reports with artificial conditional responses; no site requests."""
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'src'),str(ROOT/'tests')]
from test_public_revalidation import FixtureTransport
from vibe_job_radar.local_public import LocalPublicDataClient
from vibe_job_radar.network import FetchError,JSONRepresentation
from vibe_job_radar.workspace import Workspace
from vibe_job_radar.workbench import LocalServer


def main():
    from playwright.sync_api import sync_playwright,expect
    out=ROOT/'browser-acceptance/public-revalidation';out.mkdir(parents=True,exist_ok=True)
    result={'success':False,'checks':[],'page_errors':[],'external_requests':[],
        'scope':'Real local HTTP/UI/report pipeline with artificial 200/304/503 and injected clock; not live source evidence.'}
    clean={k:v for k,v in os.environ.items() if not k.startswith('VIBE_RADAR_')}
    with tempfile.TemporaryDirectory(prefix='radar-validation-ui-') as tmp,\
         patch.dict(os.environ,clean,clear=True),patch('urllib.request.getproxies',return_value={}):
        workspace=Workspace(tmp);now=[time.time()];wire=FixtureTransport()
        client=LocalPublicDataClient(workspace,transport=wire,clock=lambda:now[0])
        server=LocalServer(workspace,public_client=client)
        thread=threading.Thread(target=server.serve_forever,kwargs={'poll_interval':.01},daemon=True);thread.start()
        try:
            with sync_playwright() as pw:
                options={'headless':True}
                if os.environ.get('RADAR_TEST_CHROMIUM'):options['executable_path']=os.environ['RADAR_TEST_CHROMIUM']
                browser=pw.chromium.launch(**options)
                try:
                    result['browser_version']=browser.version
                    context=browser.new_context(viewport={'width':1280,'height':900})
                    def local_only(route):
                        if route.request.url.startswith(server.origin+'/'):route.continue_()
                        else:result['external_requests'].append('unexpected_nonlocal');route.abort()
                    context.route('**/*',local_only)
                    page=context.new_page();page.on('pageerror',lambda exc:result['page_errors'].append(type(exc).__name__))
                    page.goto(server.entry_url);expect(page.locator('#public-search-button')).to_be_enabled()
                    assert wire.calls==[]
                    page.locator('#public-search [name=query]').fill('Architect')
                    page.locator('#public-search [name=consent]').check()
                    page.locator('#public-search-button').click()
                    expect(page.locator('#public-status')).to_contain_text('已取得所选公开来源',timeout=15000)
                    first=server.public_tasks.snapshot();original=client.path.read_bytes()
                    expect(page.locator('#brief-capabilities')).to_contain_text('Cursor')
                    result['checks'].append('explicit first 200 produces original full-text report; page startup makes no source request')

                    now[0]+=601;wire.answer=JSONRepresentation(304,None,'W/"fixture-v1"')
                    page.locator('#public-search-button').click()
                    expect(page.locator('#public-status')).to_contain_text('确认目录未变化',timeout=15000)
                    second=server.public_tasks.snapshot()
                    assert second['not_modified'] and not second['stale'] and second['collected_at']==first['collected_at']
                    assert second['source_checked_at']!=first['collected_at'] and len(wire.calls)==2
                    assert client.path.read_bytes()==original
                    folder=workspace.root/'reports'/second['report_id']
                    audit=json.loads((folder/'public_source.json').read_text(encoding='utf-8'))
                    assert audit['not_modified'] and audit['collected_at']==first['collected_at']
                    assert not (folder/'catalog_changes.json').exists()
                    expect(page.locator('#public-changes')).to_be_hidden()
                    page.reload();expect(page.locator('#public-status')).to_contain_text('确认目录未变化')
                    assert len(wire.calls)==2
                    result['checks'].append('304/reload retains old body time, records later server confirmation, keeps v2 bytes and creates original audited report without replaying old change counts')

                    now[0]+=601;wire.answer=FetchError('http_503')
                    page.locator('#public-search [name=query]').fill('Architect')
                    page.locator('#public-search [name=consent]').check()
                    page.locator('#public-search-button').click()
                    expect(page.locator('#public-status')).to_contain_text('过期缓存',timeout=15000)
                    third=server.public_tasks.snapshot()
                    assert third['stale'] and third['source_checked_at']==second['source_checked_at']
                    assert third['collected_at']==first['collected_at'] and len(wire.calls)==3
                    for task in (first,second,third):assert workspace.report(task['report_id'])
                    page.set_viewport_size({'width':390,'height':844})
                    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                    page.locator('#public-entry').screenshot(path=str(out/'public-revalidation-mobile.png'))
                    result['checks'].append('later 503 shows stale cache and preserves both prior timestamps/reports; 390px has no overflow')
                    assert not result['page_errors'] and not result['external_requests']
                    result.update(success=True,fixture_requests=len(wire.calls),source_requests=0)
                finally:browser.close()
        finally:
            server.shutdown();server.server_close();thread.join(5)
            (out/'results.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(result,ensure_ascii=True,indent=2))


if __name__=='__main__':main()
