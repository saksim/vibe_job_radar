"""Actual browser field-guidance acceptance; all upstream responses are artificial."""
from __future__ import annotations
import argparse
import json
import sys
import tempfile
import threading
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from vibe_job_radar.workbench import LocalServer
from vibe_job_radar.workspace import Workspace
from vibe_job_radar.network import Response
from vibe_job_radar.utils import utc_now


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--channel', choices=['msedge'])
    args = parser.parse_args()
    from playwright.sync_api import sync_playwright, expect
    output = ROOT / 'browser-acceptance' / 'field-guidance'
    output.mkdir(parents=True, exist_ok=True)
    result = {'created_at': utc_now(), 'success': False, 'checks': [], 'page_errors': [],
              'external_browser_requests': [], 'scope': 'Artificial URLs and provider responses, real UI/HTTP/storage. Not live platform certification.'}
    with tempfile.TemporaryDirectory() as tmp:
        workspace = Workspace(tmp); server = LocalServer(workspace)
        thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': 0.01}, daemon=True); thread.start()
        try:
            with sync_playwright() as pw:
                browser = pw.chromium.launch(headless=True, **({'channel': args.channel} if args.channel else {}))
                result['browser_version'] = browser.version
                context = browser.new_context(viewport={'width': 1440, 'height': 1050})
                def local_only(route):
                    if route.request.url.startswith(server.origin + '/'):
                        route.continue_()
                    else:
                        result['external_browser_requests'].append(route.request.url); route.abort()
                context.route('**/*', local_only)
                page = context.new_page(); page.on('pageerror', lambda e: result['page_errors'].append(str(e)))
                try:
                    page.goto(server.entry_url)
                    expect(page.locator('#counts')).to_contain_text('真实记录 0')
                    page.get_by_role('link', name='进入自动采集 / 原文复核 / 附件与量化指标工作台').click()
                    expect(page.locator('#revision')).to_contain_text('版本 0')
                    page.get_by_role('button', name='我有职位链接：套用 URL 入门参数').click()
                    f = page.locator('#collect-form')
                    expect(page.locator('#guide-urls')).to_be_visible()
                    for name in ('api_key', 'endpoint', 'contract_ref', 'search_budget', 'feed_budget'):
                        expect(f.locator(f'[name={name}]')).to_be_hidden()
                        expect(f.locator(f'[name={name}]')).to_be_disabled()
                    expect(f.locator('[name=urls]')).to_have_value('')
                    expect(f.locator('[name=consent]')).not_to_be_checked()
                    assert server.collector.list()['runs'] == []
                    result['checks'].append('URL preset hides unrelated fields and creates no task or invented data')

                    page.locator('#collect-check').click()
                    expect(page.locator('#collect-check-results')).to_contain_text('先在招聘网站打开一个具体职位')
                    expect(page.locator('#collect-check-results')).to_contain_text('外部网络请求0')
                    assert server.collector.list()['runs'] == []
                    f.locator('[name=urls]').fill('https://www.zhipin.com/job_detail/REPLACE_WITH_REAL_JOB_ID.html')
                    page.locator('#collect-start').click()
                    expect(page.locator('#collect-check-results')).to_contain_text('这是格式示例')
                    assert server.collector.list()['runs'] == []
                    result['checks'].append('missing fields and literal sample URL block start with actionable messages')

                    actual = 'https://www.zhipin.com/job_detail/field-guide-artificial.html'
                    f.locator('[name=urls]').fill(actual)
                    page.get_by_role('button', name='从链接识别来源平台（不授予权限）').click()
                    expect(page.locator('#collect-check-results')).to_contain_text('已识别 1 个不同职位链接')
                    expect(page.locator('#collect-platforms input[value=boss]')).to_be_checked()
                    expect(page.locator('#collect-permits input[value=boss]')).not_to_be_checked()
                    result['checks'].append('local URL recognition selects platform but never grants permission')

                    f.locator('[name=rights_note]').fill('人工UI验收样本，不是真实职位授权或市场数据。')
                    page.locator('#collect-permits input[value=boss]').check()
                    f.locator('[name=consent]').check()
                    page.locator('#collect-check').click()
                    expect(page.locator('#collect-check-results')).to_contain_text('填写检查通过')
                    assert server.collector.list()['runs'] == []
                    page.locator('section').first.screenshot(path=str(output / 'url-case.png'))
                    result['checks'].append('complete URL example passes local preflight without creating a collection')
                    html = '<h1>时间序列算法工程师</h1><div class="job-sec-text">要求熟练使用 Cursor 进行 AI 辅助编程并编写单元测试。</div>'
                    with patch('vibe_job_radar.collection.SiteFetcher') as source:
                        source.return_value.fetch.return_value = Response(200, {'content-type': 'text/html'}, html.encode(), actual)
                        page.locator('#collect-start').click()
                        expect(page.locator('#collect-progress')).to_contain_text('completed', timeout=30000)
                        expect(page.locator('#collect-start')).to_be_enabled()
                        assert source.return_value.fetch.call_count == 1
                    result['checks'].append('explicit execution after preflight still completes real pipeline with mocked upstream')

                    page.get_by_role('button', name='我想先搜索：套用搜索入门参数').click()
                    expect(page.locator('#guide-search')).to_be_visible()
                    expect(f.locator('[name=api_key]')).to_be_visible()
                    for name in ('urls', 'endpoint', 'contract_ref', 'feed_budget'):
                        expect(f.locator(f'[name={name}]')).to_be_hidden()
                    expect(f.locator('[name=detail_budget]')).to_have_value('0')
                    expect(page.locator('#collect-permission-fields')).to_be_hidden()
                    page.locator('#collect-check').click()
                    expect(page.locator('#collect-check-results')).to_contain_text('尚无 Brave Search API Key')
                    expect(page.locator('#collect-check-results')).to_contain_text('将如何检索')
                    assert len(server.collector.list()['runs']) == 1
                    result['checks'].append('search-only preset previews query plan without Key or upstream calls')

                    f.locator('[name=api_key]').fill('ARTIFICIAL-SEARCH-KEY-NOT-REAL')
                    f.locator('[name=search_storage_rights]').check(); f.locator('[name=consent]').check()
                    page.locator('#collect-check').click()
                    expect(page.locator('#collect-check-results')).to_contain_text('填写检查通过')
                    assert len(server.collector.list()['runs']) == 1
                    page.locator('section').first.screenshot(path=str(output / 'search-case.png'))
                    payload = {'web': {'results': [{'title': '时间序列算法工程师', 'description': '熟练使用Cursor',
                        'url': 'https://www.zhipin.com/job_detail/search-guide-artificial.html'}]}, 'query': {'more_results_available': False}}
                    with patch('vibe_job_radar.collection.SafeHTTP') as search, patch('vibe_job_radar.collection.SiteFetcher') as details:
                        search.return_value.json.return_value = payload
                        page.locator('#collect-start').click()
                        expect(page.locator('#collect-progress')).to_contain_text('needs_attention', timeout=30000)
                        expect(page.locator('#collect-start')).to_be_enabled()
                        assert search.return_value.json.call_count == 1
                        assert details.return_value.fetch.call_count == 0
                    state = json.loads(page.locator('#collect-json').text_content())
                    assert state['search_requests'] == 1 and state['detail_attempts'] == 0
                    assert workspace.status()['counts']['snippet'] == 1
                    result['checks'].append('search-only example runs one mocked API request and no detail request')

                    f.locator('[name=api_key]').fill('TEST-KEY-MUST-NOT-CROSS-PROVIDERS')
                    f.locator('[name=mode]').select_option('feed')
                    expect(f.locator('[name=api_key]')).to_have_value('')
                    expect(f.locator('[name=endpoint]')).to_be_visible()
                    expect(f.locator('[name=urls]')).to_be_hidden()
                    page.locator('#collect-check').click()
                    expect(page.locator('#collect-check-results')).to_contain_text('向实际的数据提供方索取')
                    result['checks'].append('mode changes clear credentials and feed route explains missing supplier inputs')
                    page.get_by_role('button', name='我有职位链接：套用 URL 入门参数').click()
                    shared = 'https://www.liepin.com/job/123.shtml?pgRef=artificial&skId=ARTIFICIAL-TRACKING'
                    clean = 'https://www.liepin.com/job/123.shtml'
                    f.locator('[name=urls]').fill(shared + '\n' + clean)
                    page.get_by_role('button', name='从链接识别来源平台（不授予权限）').click()
                    expect(page.locator('#collect-check-results')).to_contain_text('已识别 1 个不同职位链接')
                    expect(page.locator('#collect-check-results')).to_contain_text('同一职位编号的公开地址')
                    expect(page.locator('#collect-permits input[value=liepin]')).not_to_be_checked()
                    page.locator('#collect-permits input[value=liepin]').check()
                    f.locator('[name=rights_note]').fill('人工分享链接回归，不是真实岗位或授权。')
                    f.locator('[name=consent]').check()
                    share_html = ('<h1>时间序列算法工程师</h1><dl><dt>职位介绍</dt><dd>'
                        '岗位职责：负责时间序列预测系统与模型设计。任职要求：熟悉统计学和数据库，编写测试和设计文档。人工回归材料。'
                        '</dd></dl>')
                    with patch('vibe_job_radar.collection.SiteFetcher') as source:
                        source.return_value.fetch.return_value = Response(200, {'content-type': 'text/html'}, share_html.encode(), clean)
                        page.locator('#collect-start').click()
                        expect(page.locator('#collect-progress')).to_contain_text('completed', timeout=30000)
                        expect(page.locator('#collect-start')).to_be_enabled()
                        source.return_value.fetch.assert_called_once_with(clean)
                    state = json.loads(page.locator('#collect-json').text_content())
                    assert state['detail_attempts'] == 1 and state['report_id']
                    assert state['details'][0]['link_normalization']['policy'] == 'liepin_share_v1'
                    assert 'ARTIFICIAL-TRACKING' not in json.dumps(state)
                    result['checks'].append('shared Liepin URL is explained, deduplicated and collected into the original report without tracking values')
                    page.set_viewport_size({'width': 390, 'height': 844})
                    page.locator('section').first.screenshot(path=str(output / 'mobile-case.png'))
                    assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
                    assert result['page_errors'] == [] and result['external_browser_requests'] == []
                    result['checks'].append('390px layout fits without horizontal overflow or JS errors')
                    result['success'] = True
                finally:
                    (output / 'results.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
                    browser.close()
        finally:
            server.shutdown(); server.server_close(); thread.join(timeout=5)
    print(json.dumps(result, ensure_ascii=True, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
