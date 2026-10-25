"""Real browser acceptance of acquisition and evidence UI; remote responses are artificial."""
from __future__ import annotations
import argparse
import io
import json
import sys
import tempfile
import threading
import zipfile
from pathlib import Path
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from vibe_job_radar.workbench import LocalServer
from vibe_job_radar.workspace import Workspace
from vibe_job_radar.network import Response
from vibe_job_radar.utils import utc_now


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--channel',choices=['msedge'])
    parser.add_argument('--out',type=Path,default=ROOT/'browser-acceptance'/'advanced')
    args=parser.parse_args()
    from playwright.sync_api import sync_playwright, expect
    output=args.out
    output.mkdir(parents=True,exist_ok=True)
    results={'created_at':utc_now(),'success':False,'checks':[],'page_errors':[], 'external_browser_requests':[],
             'scope':'人工编写数据与模拟上游；真实浏览器、采集状态机、SQLite、证据与报告，不是实站采集验收。'}
    with tempfile.TemporaryDirectory() as tmp:
        workspace=Workspace(tmp)
        server=LocalServer(workspace)
        worker=threading.Thread(target=server.serve_forever,kwargs={'poll_interval':0.01},daemon=True)
        worker.start()
        try:
            with sync_playwright() as pw:
                browser=pw.chromium.launch(headless=True,**({'channel':args.channel} if args.channel else {}))
                context=browser.new_context(viewport={'width':1440,'height':1000},accept_downloads=True)
                def local(route):
                    if route.request.url.startswith(server.origin+'/'):route.continue_()
                    else:results['external_browser_requests'].append(route.request.url);route.abort()
                context.route('**/*',local)
                page=context.new_page()
                page.on('pageerror',lambda error:results['page_errors'].append(str(error)))
                try:
                    page.goto(server.entry_url)
                    expect(page.locator('#counts')).to_contain_text('真实记录 0')
                    page.get_by_role('link',name='进入自动采集 / 原文复核 / 附件与量化指标工作台').click()
                    expect(page.locator('#revision')).to_contain_text('版本 0')
                    results['checks'].append('advanced navigation authenticates without exposing token in requests')
                    f=page.locator('#collect-form')
                    f.locator('[name=mode]').select_option('urls')
                    f.locator('[name=urls]').fill('https://www.zhipin.com/job_detail/browser-artificial.html')
                    f.locator('[name=rights_note]').fill('人工浏览器测试数据，不是真实职位或平台授权。')
                    for locator in page.locator('#collect-platforms input').all():locator.set_checked(locator.input_value()=='boss')
                    page.locator('#collect-permits input[value=boss]').check()
                    f.locator('[name=consent]').check()
                    html='<h1>架构师</h1><div class="job-sec-text">🧪 测试说明。要求熟练使用 Cursor 进行 AI 辅助编程，并完成单元测试和代码审查。</div>'
                    with patch('vibe_job_radar.collection.SiteFetcher') as remote:
                        remote.return_value.fetch.return_value=Response(200,{'content-type':'text/html'},html.encode(),'https://www.zhipin.com/job_detail/browser-artificial.html')
                        page.locator('#collect-start').click()
                        expect(page.locator('#collect-progress')).to_contain_text('completed',timeout=30000)
                        self_state=json.loads(page.locator('#collect-json').text_content())
                        expect(page.locator('#collect-start')).to_be_enabled()
                        assert remote.return_value.fetch.call_count==1
                    results['checks'].append('browser automatically completes URL acquisition, ingestion and report generation with mocked remote HTML')
                    page.locator('#source-run').select_option(self_state['report_id'])
                    page.locator('#load-requirements').click()
                    expect(page.locator('#requirement-list input[type=checkbox]').first).to_be_visible()
                    page.locator('#requirement-list input[type=checkbox]').first.check()
                    page.get_by_role('button',name='复核此条',exact=True).first.click()
                    rf=page.locator('#review-form')
                    rf.locator('[name=reviewer]').fill('Browser fixture reviewer')
                    rf.locator('[name=reason]').fill('已核对人工测试原文，仅用于验收。')
                    rf.locator('button[type=submit]').click()
                    expect(page.locator('#revision')).to_contain_text('版本 1')
                    page.locator('#requirement-list .card').first.locator('details summary').click()
                    assert page.locator('#requirement-list .card').first.locator('pre').inner_text() == '🧪 测试说明。要求熟练使用 Cursor 进行 AI 辅助编程，并完成单元测试和代码审查。'
                    results['checks'].append('original JD, including non-BMP text, is displayed with exact source-span highlighting and a versioned review')
                    ef=page.locator('#evidence-form')
                    ef.locator('[name=name]').fill('人工测试人物')
                    ef.locator('[name=project]').fill('Browser fixture project')
                    ef.locator('[name=contribution]').fill('人工测试：本人负责规格设计、代码审查和自动回归。')
                    ef.locator('[name=review_status]').select_option('approved')
                    ef.locator('[name=reviewer]').fill('Browser fixture reviewer')
                    ef.locator('[name=attested]').check()
                    page.get_by_text('上传证据附件（1 MB / 文件，工作区最多 50 MB / 100 个）',exact=True).click()
                    page.locator('#evidence-file').set_input_files({'name':'proof.txt','mimeType':'text/plain','buffer':b'Artificial evidence only, not a real CV.'})
                    page.locator('#upload-form [name=rights_confirmed]').check()
                    page.locator('#upload-form button[type=submit]').click()
                    expect(page.locator('#upload-result')).to_contain_text('SHA-256')
                    results['checks'].append('attachment upload computes and selects its SHA-256 without manual file paths')
                    page.get_by_text('添加可观测指标',exact=True).click()
                    mf=page.locator('#metric-form')
                    mf.locator('[name=metric_id]').select_option('cycle_time_hours')
                    for key,value in {'current':'6','baseline':'10','sample_size':'20','baseline_sample_size':'20','window':'After fixture','baseline_window':'Before fixture','comparison_basis':'Same artificial tasks'}.items():
                        mf.locator(f'[name={key}]').fill(value)
                    page.locator('#metric-preview').click()
                    expect(page.locator('#metric-preview-text')).to_contain_text('40.00%')
                    mf.locator('button[type=submit]').click()
                    expect(page.locator('#metric-list')).to_contain_text('cycle_time_hours')
                    ef.locator('button[type=submit]').click()
                    expect(page.locator('#revision')).to_contain_text('版本 2')
                    results['checks'].append('metric preview, exact requirement mapping and attested personal evidence persist through forms')
                    page.locator('#generate').click()
                    expect(page.locator('#generated-descriptions')).to_contain_text('40.00%')
                    expect(page.locator('#generated-descriptions')).to_contain_text('本人负责范围')
                    expect(page.locator('#generated-matrix')).to_contain_text('user_attested_exact')
                    results['checks'].append('personal report includes observations and exact-mapping matrix from the frozen source report')
                    with page.expect_download() as pending:page.locator('#export-evidence').click()
                    pending.value.save_as(str(output/'fixture-evidence.zip'))
                    with zipfile.ZipFile(output/'fixture-evidence.zip') as z:
                        candidate=json.loads(z.read('candidate.json'))
                        assert len(candidate['evidence'])==1
                        assert z.read(candidate['evidence'][0]['evidence_ref']).startswith(b'Artificial evidence')
                    results['checks'].append('portable evidence ZIP contains the candidate, reviews and registered proof bytes')
                    page.screenshot(path=str(output/'desktop-personal-report.png'),full_page=True)
                    page.reload()
                    expect(page.locator('#saved-evidence')).to_contain_text('Browser fixture project')
                    page.get_by_role('button',name='编辑证据',exact=True).click()
                    expect(ef.locator('[name=attested]')).not_to_be_checked()
                    ef.locator('[name=review_status]').select_option('draft')
                    ef.locator('button[type=submit]').click()
                    expect(page.locator('#revision')).to_contain_text('版本 3')
                    page.once('dialog',lambda dialog:dialog.accept())
                    page.get_by_role('button',name='撤回证据',exact=True).click()
                    expect(page.locator('#revision')).to_contain_text('版本 4')
                    expect(page.locator('#saved-evidence .card')).to_have_count(0)
                    results['checks'].append('reload, edit-to-draft and revoke preserve revision history without duplicating records')
                    page.set_viewport_size({'width':390,'height':844})
                    page.screenshot(path=str(output/'mobile-workflows.png'),full_page=True)
                    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                    results['checks'].append('390px viewport does not overflow horizontally')
                    assert results['page_errors']==[]
                    assert results['external_browser_requests']==[]
                    results['success']=True
                finally:
                    if not results['success']:
                        page.screenshot(path=str(output/'failure.png'),full_page=True)
                    browser.close()
        finally:
            server.shutdown();server.server_close();worker.join(timeout=5)
            (output/'results.json').write_text(json.dumps(results,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(results,ensure_ascii=True,indent=2))
    return 0

if __name__=='__main__':raise SystemExit(main())
