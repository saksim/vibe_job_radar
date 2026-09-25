"""Actual browser field-guidance acceptance; all upstream responses are artificial."""
from __future__ import annotations
import argparse
import json
import sys
import tempfile
import threading
import time
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
                    with patch('vibe_job_radar.collection.SiteFetcher') as source:
                        page.get_by_role('link', name='进入猎聘架构师公开分类采集').click()
                        expect(page.locator('#guide-liepin_category')).to_be_visible()
                        page.reload()
                        expect(page.locator('#guide-liepin_category')).to_be_visible()
                        expect(page.locator('#collect-roles input[value=architect]')).to_be_checked()
                        expect(page.locator('#collect-platforms input[value=liepin]')).to_be_checked()
                        expect(page.locator('#collect-permits input[value=liepin]')).not_to_be_checked()
                        expect(page.locator('#collect-form [name=consent]')).not_to_be_checked()
                        expect(page.locator('#collect-form [name=detail_budget]')).to_have_value('5')
                        expect(page.locator('#collect-form [name=api_key]')).to_be_hidden()
                        expect(page.locator('#collect-form [name=urls]')).to_be_hidden()
                        assert server.collector.list()['runs'] == []
                        source.assert_not_called()
                    expect(page.locator('#revision')).to_contain_text('版本 0')
                    result['checks'].append('home category link and reload select the original fixed-scope preset without consent, task creation or upstream requests')
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
                    f.locator('[name=urls]').fill(clean + '\n' + shared)
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
                    assert state['details'][0]['detail_parser'] == 'liepin_public_detail_v1'
                    assert 'ARTIFICIAL-TRACKING' not in json.dumps(state)
                    result['checks'].append('shared Liepin URL is explained, deduplicated and collected into the original report without tracking values')
                    f.locator('[name=urls]').fill(clean)
                    with patch('vibe_job_radar.collection.SiteFetcher') as source:
                        page.locator('#collect-start').click()
                        expect(page.locator('#collect-json')).to_contain_text('fresh_reused', timeout=30000)
                        expect(page.locator('#collect-progress')).to_contain_text('completed', timeout=30000)
                        source.assert_not_called()
                    cached = json.loads(page.locator('#collect-json').text_content())
                    assert cached['id'] != state['id'] and cached['detail_attempts'] == 0 and cached['report_id']
                    assert cached['details'][0]['detail_parser'] == 'liepin_public_detail_v1'
                    assert 'link_normalization' not in cached['details'][0]
                    result['checks'].append('clean Liepin input reuses strictly verified cache in a new report without another upstream request')
                    page.get_by_role('button', name='自动读取猎聘架构师公开分类').click()
                    expect(page.locator('#guide-liepin_category')).to_be_visible()
                    expect(page.locator('#collect-roles input[value=architect]')).to_be_checked()
                    expect(page.locator('#collect-platforms input[value=liepin]')).to_be_checked()
                    expect(page.locator('#collect-permits input[value=liepin]')).not_to_be_checked()
                    expect(f.locator('[name=consent]')).not_to_be_checked()
                    expect(f.locator('[name=detail_budget]')).to_have_value('5')
                    for name in ('api_key', 'urls', 'endpoint', 'search_budget', 'pages'):
                        expect(f.locator(f'[name={name}]')).to_be_hidden()
                        expect(f.locator(f'[name={name}]')).to_be_disabled()
                    before = len(server.collector.list()['runs'])
                    page.locator('#collect-check').click()
                    expect(page.locator('#collect-check-results')).to_contain_text('分类读取最多1次')
                    expect(page.locator('#collect-check-results')).to_contain_text('请确认本次猎聘公开分类页')
                    assert len(server.collector.list()['runs']) == before
                    result['checks'].append('category preset fixes its scope, hides Key and URLs, and requires explicit permission without requests')

                    f.locator('[name=detail_budget]').fill('2')
                    f.locator('[name=rights_note]').fill('人工公开分类回归，响应全部模拟；不是真实岗位或授权。')
                    page.locator('#collect-permits input[value=liepin]').check()
                    f.locator('[name=consent]').check()
                    from vibe_job_radar.public_category import URL as category_url
                    cards = ''.join(f'<div class="job-card-pc-container"><a data-nick="job-detail-job-info" href="https://www.liepin.com/{"a" if i == 201 else "job"}/{i}.shtml">'
                        f'<div class="job-title-box"><div class="ellipsis-1" title="软件架构师人工样本{i}">软件架构师人工样本{i}</div></div></a></div>' for i in (201, 202, 203))
                    category_html = ('<html><head><title>【架构师招聘_招聘架构师人才】-猎聘</title></head><body><div id="main-container">'
                        '<div class="left-job-box"><div class="job-list-box"><div class="left-list-box">'+cards+'</div></div></div></div></body></html>')
                    def category_pager(active):
                        return '<ul class="ant-pagination">'+''.join(
                            f'<li class="ant-pagination-item{" ant-pagination-item-active" if n==active else ""}" title="{n+1}">'
                            f'<a href="{category_url}pn{n}/" data-currentpage="{n}" data-selector="pagintion-item-selector">{n+1}</a></li>'
                            for n in range(3))+'</ul>'
                    category_html=category_html.replace('</title>','</title><link rel="canonical" href="'+category_url+'">')
                    category_html=category_html.replace('</div></body>',category_pager(0)+'</div></body>')
                    def category_response(url):
                        ident = url.rsplit('/', 1)[-1].split('.')[0]
                        content = category_html if url == category_url else (
                            f'<h1>软件架构师人工样本{ident}</h1><dl><dt>职位介绍</dt><dd>'
                            '工作职能：与公司信息部协作，负责系统架构与数据库设计。任职资格：熟悉软件设计，编写文档与自动测试。人工回归材料。'
                            '</dd></dl>')
                        return Response(200, {'content-type': 'text/html'}, content.encode(), url)
                    with patch('vibe_job_radar.collection.SiteFetcher') as source:
                        source.return_value.fetch.side_effect = category_response
                        page.locator('#collect-start').click()
                        expect(page.locator('#collect-progress')).to_contain_text('completed', timeout=30000)
                        expect(page.locator('#collect-start')).to_be_enabled()
                        assert [call.args[0] for call in source.return_value.fetch.call_args_list] == [category_url,
                            'https://www.liepin.com/a/201.shtml', 'https://www.liepin.com/job/202.shtml']
                    category_state = json.loads(page.locator('#collect-json').text_content())
                    assert category_state['category_attempts'] == 1 and category_state['detail_attempts'] == 2
                    assert category_state['category_outcomes'][0]['selected_positions'] == [1, 2]
                    assert category_state['details'][0]['detail_parser'] == 'liepin_public_detail_v1'
                    assert category_state['details'][0]['url'] == 'https://www.liepin.com/a/201.shtml'
                    assert category_state['report_id']
                    manifest = json.loads((workspace.root/'reports'/category_state['report_id']/'run_manifest.json').read_text(encoding='utf-8'))
                    assert manifest['stats']['full_text_job_groups'] == 2
                    expect(page.locator('#collect-result')).to_contain_text('主列表卡片 3 条，已选 2 条')
                    result['checks'].append('category UI selects the first requested cards and generates a batch-only report through the existing API and Store')
                    with patch('vibe_job_radar.collection.SiteFetcher') as source:
                        page.get_by_role('button',name='查看平台下一页（不联网）').click()
                        expect(page.locator('#collect-result')).to_contain_text('本页还有1项未选择，原名单保留')
                        source.assert_not_called()
                    before_next = len(server.collector.list()['runs'])
                    page.get_by_role('button', name='预览这份名单的下一批（不联网）').click()
                    expect(page.locator('#collect-result')).to_contain_text('名单第 3 项')
                    assert len(server.collector.list()['runs']) == before_next
                    with patch('vibe_job_radar.collection.SiteFetcher') as source:
                        source.return_value.fetch.side_effect = category_response
                        page.get_by_role('button', name='确认采集这批职位').click()
                        expect(page.locator('#collect-progress')).to_contain_text('completed', timeout=30000)
                        expect(page.locator('#collect-json')).to_contain_text('"snapshot_reused": true', timeout=30000)
                        expect(page.locator('#collect-start')).to_be_enabled()
                        source.return_value.fetch.assert_called_once_with('https://www.liepin.com/job/203.shtml')
                    next_state = json.loads(page.locator('#collect-json').text_content())
                    assert next_state['id'] != category_state['id'] and next_state['category_attempts'] == 0
                    assert next_state['category_outcomes'][0]['selected_positions'] == [3]
                    assert next_state['report_id'] and next_state['report_id'] != category_state['report_id']
                    assert len(server.collector.list()['runs']) == before_next + 1
                    result['checks'].append('saved category next-batch preview makes no request; confirmation fetches only the remaining detail with observed role-heading synonyms and creates a separate original report')
                    page.get_by_role('button', name='预览这份名单的下一批（不联网）').click()
                    expect(page.locator('#collect-result')).to_contain_text('这份名单已全部选择完毕')
                    page.locator('#collect-history').select_option(category_state['id'])
                    page.locator('#collect-load').click()
                    expect(page.locator('#collect-json')).to_contain_text(category_state['id'])
                    page.get_by_role('button', name='预览这份名单的下一批（不联网）').click()
                    page.get_by_role('button', name='打开已保存的下一批').click()
                    expect(page.locator('#collect-json')).to_contain_text(next_state['id'])
                    assert len(server.collector.list()['runs']) == before_next + 1
                    result['checks'].append('exhaustion is limited to the saved list and reopening an existing continuation never creates or executes another task')
                    parent_bytes=server.collector._path(next_state['id']).read_bytes()
                    before_page=len(server.collector.list()['runs'])
                    with patch('vibe_job_radar.collection.SiteFetcher') as source:
                        page.get_by_role('button',name='查看平台下一页（不联网）').click()
                        expect(page.locator('#collect-result')).to_contain_text('第 1 页 → 第 2 页')
                        expect(page.get_by_role('button',name='确认读取第 2 页并采集最多 2 条')).to_be_visible()
                        assert len(server.collector.list()['runs'])==before_page
                        source.assert_not_called()
                    second_cards=cards.replace('202','204').replace('203','205')
                    second_html=category_html.replace(cards,second_cards).replace('-猎聘</title>','-猎聘-第2页</title>').replace(category_pager(0),category_pager(1))
                    def second_page_response(url):
                        if url==category_url+'pn1/':return Response(200,{'content-type':'text/html'},second_html.encode(),url)
                        return category_response(url)
                    with patch('vibe_job_radar.collection.SiteFetcher') as source:
                        source.return_value.fetch.side_effect=second_page_response
                        page.get_by_role('button',name='确认读取第 2 页并采集最多 2 条').click()
                        expect(page.locator('#collect-progress')).to_contain_text('completed',timeout=30000)
                        expect(page.locator('#collect-start')).to_be_enabled()
                        assert [c.args[0] for c in source.return_value.fetch.call_args_list]==[
                            category_url+'pn1/','https://www.liepin.com/job/204.shtml','https://www.liepin.com/job/205.shtml']
                    page_state=json.loads(page.locator('#collect-json').text_content())
                    assert page_state['category_outcomes'][0]['page_snapshot']['page']==1
                    assert page_state['category_outcomes'][0]['candidates'][0]['status']=='previous_page_duplicate'
                    assert page_state['detail_attempts']==2 and page_state['category_attempts']==1 and page_state['report_id']
                    expect(page.locator('#collect-progress')).to_contain_text('第 2 页')
                    expect(page.locator('#collect-result')).not_to_contain_text('第一页')
                    assert server.collector._path(next_state['id']).read_bytes()==parent_bytes
                    expect(page.locator('#collect-result')).to_contain_text('前页已出现 1 项，未重复请求')
                    result['checks'].append('explicit adjacent-page preview is offline; confirmation reads the observed second page, skips a previous-page duplicate and saves only the two new complete bodies in an independent report')
                    with patch('vibe_job_radar.collection.SiteFetcher') as source:
                        page.reload()
                        page.locator('#collect-history').select_option(next_state['id']);page.locator('#collect-load').click()
                        expect(page.locator('#collect-json')).to_contain_text(next_state['id'])
                        page.get_by_role('button',name='查看平台下一页（不联网）').click()
                        page.get_by_role('button',name='打开已保存的下一页任务').click()
                        expect(page.locator('#collect-json')).to_contain_text(page_state['id'])
                        assert len(server.collector.list()['runs'])==before_page+1
                        source.assert_not_called()
                    result['checks'].append('reloading and opening the existing adjacent-page child preserves its parent, page identity and report without another source request or task')
                    before_algorithm = len(server.collector.list()['runs'])
                    with patch('vibe_job_radar.collection.SiteFetcher') as source:
                        page.goto(server.origin + '/')
                        page.get_by_role('link', name='进入猎聘算法工程师公开分类采集').click()
                        expect(f.locator('[name=category_id]')).to_have_value('algorithm')
                        page.reload()
                        expect(f.locator('[name=category_id]')).to_have_value('algorithm')
                        expect(page.locator('#collect-roles input[value=time_series]')).to_be_checked()
                        expect(page.locator('#collect-roles input[value=domain_algorithm]')).to_be_checked()
                        expect(page.locator('#collect-roles input[value=architect]')).not_to_be_checked()
                        page.locator('#collect-permits input[value=liepin]').check()
                        f.locator('[name=consent]').check()
                        f.locator('[name=category_id]').select_option('architect')
                        expect(f.locator('[name=consent]')).not_to_be_checked()
                        expect(page.locator('#collect-permits input[value=liepin]')).not_to_be_checked()
                        expect(page.locator('#collect-roles input[value=architect]')).to_be_checked()
                        f.locator('[name=category_id]').select_option('algorithm')
                        expect(page.locator('#guide-liepin_category')).to_contain_text('宽分类')
                        assert len(server.collector.list()['runs']) == before_algorithm
                        source.assert_not_called()
                    result['checks'].append('algorithm home link and reload select explicit scope; switching categories clears consent and permission without requests')
                    algorithm_url = 'https://www.liepin.com/career/suanfakaifa/'
                    algorithm_html = category_html.replace('架构师招聘_招聘架构师人才', '算法工程师招聘_招聘算法工程师人才').replace('软件架构师人工样本', '算法工程师人工样本').replace(category_url,algorithm_url)
                    for old, new in (('201','301'), ('202','302'), ('203','303')):
                        algorithm_html = algorithm_html.replace(old, new)
                    def algorithm_response(url):
                        ident = url.rsplit('/', 1)[-1].split('.')[0]
                        body = ('岗位职责：研究图像算法。岗位要求：熟悉计算机视觉，编写图像识别实验与自动测试。人工回归材料。' if ident == '302' else
                                '岗位职责：负责电力负荷预测与时间序列建模。岗位要求：熟悉能源业务，使用Cursor辅助编程并审查生成代码。人工回归材料。')
                        content = algorithm_html if url == algorithm_url else f'<h1>算法工程师人工样本{ident}</h1><dl><dt>职位介绍</dt><dd>{body}</dd></dl>'
                        if ident == '303':
                            body = ('1.本科及以上，计算机相关专业，能够编写接口和自动测试。\n'
                                    '2.掌握时间序列建模，熟练使用Python和数据库。\n'
                                    '3.了解能源业务，具备模型评估和误差分析经验。')
                            content = ('<dl><dt>职位介绍</dt><dd data-selector="job-intro-content">'+body+'</dd></dl>'
                                '<script type="application/ld+json">'+json.dumps(dict(**{'@type':'JobPosting'},
                                    title='算法工程师人工样本303', description=body, url=url))+'</script>')
                        return Response(200, {'content-type': 'text/html'}, content.encode(), url)
                    f.locator('[name=detail_budget]').fill('2')
                    f.locator('[name=rights_note]').fill('人工算法分类回归，未访问真实招聘站点。')
                    page.locator('#collect-permits input[value=liepin]').check()
                    f.locator('[name=consent]').check()
                    with patch('vibe_job_radar.collection.SiteFetcher') as source:
                        source.return_value.fetch.side_effect = algorithm_response
                        page.locator('#collect-start').click()
                        expect(page.locator('#collect-progress')).to_contain_text('completed', timeout=30000)
                        expect(page.locator('#collect-start')).to_be_enabled()
                        assert [c.args[0] for c in source.return_value.fetch.call_args_list] == [algorithm_url,
                            'https://www.liepin.com/a/301.shtml', 'https://www.liepin.com/job/302.shtml']
                    algorithm_state = json.loads(page.locator('#collect-json').text_content())
                    assert algorithm_state['category_id'] == 'algorithm' and algorithm_state['saved_detail_count'] == 2
                    manifest = json.loads((workspace.root/'reports'/algorithm_state['report_id']/'run_manifest.json').read_text(encoding='utf-8'))
                    assert manifest['stats']['full_text_job_groups'] == 1
                    result['checks'].append('algorithm category saves strict full bodies but original role rules exclude an unmatched image-processing job from target statistics')
                    page.get_by_role('button', name='预览这份名单的下一批（不联网）').click()
                    expect(page.locator('#collect-result')).to_contain_text('名单第 3 项')
                    with patch('vibe_job_radar.collection.SiteFetcher') as source:
                        source.return_value.fetch.side_effect = algorithm_response
                        page.get_by_role('button', name='确认采集这批职位').click()
                        expect(page.locator('#collect-progress')).to_contain_text('completed', timeout=30000)
                        expect(page.locator('#collect-json')).to_contain_text('"snapshot_reused": true', timeout=30000)
                        expect(page.locator('#collect-start')).to_be_enabled()
                        source.return_value.fetch.assert_called_once_with('https://www.liepin.com/job/303.shtml')
                    continued = json.loads(page.locator('#collect-json').text_content())
                    assert continued['category_id'] == 'algorithm' and continued['category_attempts'] == 0
                    result['checks'].append('algorithm continuation preserves its category and consumes only the remaining saved-list detail with fully corroborated unheaded numbered qualifications')
                    # Exercise recovery through the original default factory and
                    # real temporary SQLite; only the one-hop source is authored.
                    from vibe_job_radar.guided.rate import Limits, RateLedger
                    rate_clock=[time.time()-2*86400]
                    recovery_ledger=RateLedger(workspace.root/'guided/rates.sqlite',
                        Limits(page_interval=0,request_interval=0,pages_day=2),clock=lambda:rate_clock[0])
                    recovery_html=category_html.replace('201','901').replace('202','902').replace('203','903')
                    recovery_calls=[]
                    def recovery_response(url):
                        recovery_calls.append(url)
                        if url=='https://www.liepin.com/robots.txt':
                            return Response(200,{'content-type':'text/plain'},b'User-agent: *\nDisallow: /*?*\n',url)
                        if url==category_url:return Response(200,{'content-type':'text/html'},recovery_html.encode(),url)
                        assert url in {'https://www.liepin.com/a/901.shtml','https://www.liepin.com/job/902.shtml','https://www.liepin.com/job/903.shtml'}
                        return category_response(url)
                    with patch('vibe_job_radar.collection_rate.RateLedger',return_value=recovery_ledger), patch(
                            'vibe_job_radar.network.SafeHTTP.public_get',side_effect=recovery_response):
                        page.get_by_role('button',name='自动读取猎聘架构师公开分类').click()
                        f.locator('[name=detail_budget]').fill('3')
                        f.locator('[name=rights_note]').fill('人工等待恢复回归，临时工作区，仅上游响应模拟。')
                        page.locator('#collect-permits input[value=liepin]').check();f.locator('[name=consent]').check()
                        page.locator('#collect-start').click()
                        expect(page.locator('#collect-progress')).to_contain_text('needs_attention',timeout=30000)
                        expect(page.locator('#collect-start')).to_be_enabled()
                        rate_parent=json.loads(page.locator('#collect-json').text_content())
                        assert [d['status'] for d in rate_parent['details']]==['ok','daily_limit','host_stopped']
                        parent_bytes=server.collector._path(rate_parent['id']).read_bytes()
                        calls_before=list(recovery_calls)
                        page.get_by_role('button',name='预览因等待未完成的正文（不联网）').click()
                        expect(page.locator('#collect-result')).to_contain_text('保留原成功 1 条；本次最多 2 次')
                        page.get_by_role('button',name='确认保存恢复任务（暂不联网）').click()
                        expect(page.locator('#collect-progress')).to_contain_text('paused')
                        expect(page.locator('#collect-start')).to_be_enabled()
                        rate_child=json.loads(page.locator('#collect-json').text_content())
                        assert rate_child['detail_budget']==2 and rate_child['detail_attempts']==0
                        assert recovery_calls==calls_before and server.collector._path(rate_parent['id']).read_bytes()==parent_bytes
                        result['checks'].append('original default category factory stops at a real temporary daily limit; recovery preview and explicit save preserve the successful body and parent without another request')
                        page.reload();page.locator('#collect-history').select_option(rate_child['id']);page.locator('#collect-load').click()
                        expect(page.locator('#collect-progress')).to_contain_text('paused')
                        assert recovery_calls==calls_before
                        rate_clock[0]+=86401
                        page.locator('#collect-resume').click()
                        expect(page.locator('#collect-progress')).to_contain_text('completed',timeout=30000)
                        expect(page.locator('#collect-start')).to_be_enabled()
                        recovered=json.loads(page.locator('#collect-json').text_content())
                        assert recovered['saved_detail_count']==3 and recovered['detail_attempts']==2
                        assert recovered['category_attempts']==0 and recovered['details'][0]==rate_parent['details'][0]
                        assert recovery_calls[len(calls_before):]==['https://www.liepin.com/robots.txt',
                            'https://www.liepin.com/job/902.shtml','https://www.liepin.com/job/903.shtml']
                        assert server.collector._path(rate_parent['id']).read_bytes()==parent_bytes
                        result['checks'].append('after reload and the controlled quota window, the saved recovery reads only the two remaining details and produces a combined three-body report without re-fetching its list or old success')
                        calls_after=list(recovery_calls)
                        page.locator('#collect-history').select_option(rate_parent['id']);page.locator('#collect-load').click()
                        page.get_by_role('button',name='预览因等待未完成的正文（不联网）').click()
                        page.get_by_role('button',name='打开已保存的恢复任务').click()
                        expect(page.locator('#collect-progress')).to_contain_text('completed')
                        assert json.loads(page.locator('#collect-json').text_content())['id']==recovered['id']
                        assert recovery_calls==calls_after
                        result['checks'].append('reopening a completed recovery from its original failed batch returns the same task and report without network replay')
                    # Use the real default factory here. A shared guided cooldown
                    # must stop advanced HTTP before even the one-hop transport.
                    server.guided.ledger.cool('liepin',3600)
                    before_rate=server.guided.ledger.summary('liepin')
                    with patch('vibe_job_radar.network.SafeHTTP.public_get',side_effect=AssertionError('cooldown must block HTTP')) as upstream:
                        page.get_by_role('button',name='我有职位链接：套用 URL 入门参数').click()
                        f.locator('[name=urls]').fill('https://www.liepin.com/job/90000000000000201.shtml')
                        page.get_by_role('button',name='从链接识别来源平台（不授予权限）').click()
                        f.locator('[name=rights_note]').fill('人工共享冷却回归，不访问网站。')
                        page.locator('#collect-permits input[value=liepin]').check()
                        f.locator('[name=consent]').check()
                        page.locator('#collect-start').click()
                        expect(page.locator('#collect-progress')).to_contain_text('needs_attention',timeout=30000)
                        expect(page.locator('#collect-start')).to_be_enabled()
                        expect(page.locator('#collect-result')).to_contain_text('工作区共享冷却')
                        blocked=json.loads(page.locator('#collect-json').text_content())
                        assert blocked['details'][0]['status']=='cooldown' and not blocked['report_id']
                        assert blocked['details'][0]['fetch_diagnostic']['http_attempts']==0
                        upstream.assert_not_called()
                    result['checks'].append('guided shared cooldown blocks the original default advanced URL factory before HTTP, shows an actionable outcome and never reuses a historical report')
                    with patch('vibe_job_radar.network.SafeHTTP.public_get',side_effect=AssertionError('new task cannot reset cooldown')) as upstream:
                        page.reload()
                        page.get_by_role('button',name='自动读取猎聘架构师公开分类').click()
                        f.locator('[name=rights_note]').fill('人工新任务共享冷却回归，不访问网站。')
                        page.locator('#collect-permits input[value=liepin]').check()
                        f.locator('[name=consent]').check()
                        page.locator('#collect-start').click()
                        expect(page.locator('#collect-progress')).to_contain_text('needs_attention',timeout=30000)
                        expect(page.locator('#collect-start')).to_be_enabled()
                        expect(page.locator('#collect-result')).to_contain_text('工作区共享冷却')
                        blocked_page=json.loads(page.locator('#collect-json').text_content())
                        assert blocked_page['category_outcomes'][0]['status']=='cooldown' and not blocked_page['report_id']
                        assert server.guided.ledger.summary('liepin')==before_rate
                        upstream.assert_not_called()
                    result['checks'].append('reloading and creating a category task retains the same shared cooldown and consumes no HTTP reservation or source request')
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
