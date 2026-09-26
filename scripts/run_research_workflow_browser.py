"""Real Chromium, real local application, artificial JDs and personal evidence.

Exercises the product journey, not third-party site or VPN certification.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from vibe_job_radar.workspace import Workspace
from vibe_job_radar.workbench import LocalServer
from vibe_job_radar.utils import utc_now


def main() -> int:
    from playwright.sync_api import expect, sync_playwright
    parser = argparse.ArgumentParser()
    parser.add_argument('--browser-executable', help='Developer test only; otherwise use Playwright Chromium.')
    args = parser.parse_args()
    out = ROOT / 'browser-acceptance' / 'research'
    out.mkdir(parents=True, exist_ok=True)
    result = {'created_at': utc_now(), 'success': False, 'checks': [], 'page_errors': [],
              'external_browser_requests': [],
              'scope': 'Artificial JD and candidate data; actual local browser, original analysis and evidence APIs. Not live site acceptance.'}
    try:
        with tempfile.TemporaryDirectory(prefix='research-journey-') as tmp:
            workspace = Workspace(tmp)
            server = LocalServer(workspace)
            worker = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .01}, daemon=True)
            worker.start()
            try:
                with sync_playwright() as pw:
                    browser = pw.chromium.launch(headless=True, **({'executable_path': args.browser_executable} if args.browser_executable else {}))
                    try:
                        context = browser.new_context(viewport={'width':1280, 'height':960}, accept_downloads=True)
                        def local_only(route):
                            if route.request.url.startswith(server.origin + '/'):
                                route.continue_()
                            else:
                                result['external_browser_requests'].append(route.request.url)
                                route.abort()
                        context.route('**/*', local_only)
                        page = context.new_page()
                        page.on('pageerror', lambda e: result['page_errors'].append(str(e)))
                        page.goto(server.entry_url)
                        expect(page.locator('#research-go')).to_be_enabled()
                        assert page.locator('main').evaluate("e=>e.firstElementChild.tagName") == 'HEADER'
                        goal = page.locator('#research-goal')
                        goal.locator('[name=role]').select_option('time_series')
                        goal.locator('[name=platform]').select_option('liepin')
                        goal.locator('[name=keyword]').fill('时间序列算法工程师')
                        goal.locator('button').click()
                        expect(page.locator('#site')).to_have_value('liepin')
                        expect(page.locator('#role')).to_have_value('time_series')
                        expect(page.locator('#search-form [name=keyword]')).to_have_value('时间序列算法工程师')
                        expect(page.locator('#search-form [name=consent]')).not_to_be_checked()
                        expect(page.locator('#search-form [name=rights_note]')).to_have_value('')
                        assert server.guided.state()['jobs'] == []
                        result['checks'].append('goal first; validated role/platform/keyword transfer without auto-consent or site access')
                        page.goto(server.origin + '/')
                        expect(page.locator('#counts')).to_contain_text('真实记录 0')
                        form = page.locator('#job-form')
                        for key, value in {
                            'title':'时间序列算法工程师', 'company':'人工研究夹具（非招聘事实）',
                            'source_ref':'research-browser:approved-fixture',
                            'text':'要求熟练使用 Cursor 进行 AI 辅助编程，编写单元测试并进行代码审查。',
                            'rights_note':'人工测试输入，仅验证界面；不是平台授权或本人工作。'
                        }.items():
                            form.locator(f'[name={key}]').fill(value)
                        form.locator('[name=platform]').select_option('manual')
                        form.locator('[name=full_text_confirmed]').check()
                        form.locator('button[type=submit]').click()
                        expect(page.locator('#counts')).to_contain_text('真实记录 1')
                        page.locator('#analyze').click()
                        expect(page.locator('#brief-conclusion')).to_contain_text('已形成本批')
                        expect(page.locator('#brief-capabilities')).to_contain_text('Cursor')
                        expect(page.locator('#brief-common')).to_contain_text('不足以归纳')
                        expect(page.locator('#brief-evidence')).to_contain_text('不等于本人不具备')
                        source_id = page.url.split('#report=')[1]
                        cards = page.locator('#brief-roles .brief-card')
                        expect(cards).to_have_count(3)
                        expect(cards.filter(has_text='时间序列算法工程师')).to_contain_text('完整正文 1 个去重岗位，其中有AI编程证据 1 个')
                        expect(cards.filter(has_text='垂直领域算法工程师')).to_contain_text('完整正文 0 个去重岗位')
                        expect(cards.filter(has_text='架构师')).to_contain_text('人工确认 0 行')
                        page.locator('#brief-roles').screenshot(path=str(out / 'role-sample-coverage.png'))
                        result['checks'].append('per-role complete-text and accepted-AI denominators are separate; zero directions and unconfirmed rule rows stay explicit')

                        source_raw = workspace.report_file(source_id, 'requirements.jsonl').read_bytes()
                        before_hash = hashlib.sha256(source_raw).hexdigest()
                        decoy = workspace.analyze({'roles':['architect']})
                        assert decoy['id'] != source_id
                        page.screenshot(path=str(out / 'research-conclusion.png'), full_page=True)
                        result['checks'].append('JD to readable requirements and evidence gaps; one job is not a common market baseline')
                        page.locator('#evidence-next').click()
                        expect(page.locator('#source-run')).to_have_value(source_id)
                        expect(page.locator('#requirement-list')).to_contain_text('Cursor')
                        expect(page.locator('#revision')).to_contain_text('版本 0')
                        assert server.evidence.state()['revision'] == 0
                        result['checks'].append('same report opened automatically even with a newer decoy; no review or profile write on navigation')
                        page.locator('#requirement-list input[type=checkbox]').first.check()
                        ef = page.locator('#evidence-form')
                        for key,value in {'name':'人工测试人物','project':'人工研究项目','contribution':'人工测试：本人负责验收规格和代码审查。',
                                          'reviewer':'人工测试复核人','external_url':'https://example.com/artificial-proof'}.items():
                            ef.locator(f'[name={key}]').fill(value)
                        ef.locator('[name=review_status]').select_option('approved')
                        ef.locator('[name=attested]').check()
                        page.get_by_text('添加可观测指标', exact=True).click()
                        mf = page.locator('#metric-form')
                        mf.locator('[name=metric_id]').select_option('cycle_time_hours')
                        for key,value in {'current':'6','baseline':'10','sample_size':'20','baseline_sample_size':'20',
                                          'window':'after fixture','baseline_window':'before fixture','comparison_basis':'same artificial tasks'}.items():
                            mf.locator(f'[name={key}]').fill(value)
                        mf.locator('button[type=submit]').click()
                        expect(page.locator('#metric-list')).to_contain_text('cycle_time_hours')
                        ef.locator('button[type=submit]').click()
                        expect(page.locator('#revision')).to_contain_text('版本 1')
                        page.locator('#generate').click()
                        expect(page.locator('#generated-descriptions')).to_contain_text('40.00%')
                        expect(page.locator('#generated-descriptions')).to_contain_text('本人负责范围')
                        expect(page.locator('#generated-matrix')).to_contain_text('user_attested_exact')
                        assert hashlib.sha256(workspace.report_file(source_id, 'requirements.jsonl').read_bytes()).hexdigest() == before_hash
                        result['checks'].append('user-entered project and comparable metric produce personal wording via existing evidence flow; source remains immutable')
                        page.locator('#research-return').click()
                        expect(page.locator('#report-title')).to_contain_text(source_id[:8])
                        page.reload()
                        expect(page.locator('#report-title')).to_contain_text(source_id[:8])
                        result['checks'].append('return and reload retain original batch instead of selecting latest report')
                        page.set_viewport_size({'width':390,'height':844})
                        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                        page.screenshot(path=str(out / 'research-mobile.png'), full_page=True)

                        page.locator('#brief-roles').screenshot(path=str(out / 'role-sample-mobile.png'))
                        with page.expect_download() as download:
                            page.get_by_role('button', name='research_brief.md', exact=True).click()
                        download.value.save_as(str(out / 'fixture-research-brief.md'))
                        assert 'Cursor' in (out / 'fixture-research-brief.md').read_text(encoding='utf-8')
                        # An artificial legacy manifest exercises the original read path.
                        manifest_path = workspace.report_file(source_id, 'run_manifest.json')
                        current_manifest = manifest_path.read_bytes()
                        legacy = json.loads(current_manifest)
                        legacy['research_brief'].pop('role_sample_note')
                        for role in legacy['research_brief']['roles']:
                            for key in ('sample_counts', 'sample_status', 'sample_note'):
                                role.pop(key)
                        legacy_raw = json.dumps(legacy, ensure_ascii=False).encode('utf-8')
                        try:
                            manifest_path.write_bytes(legacy_raw)
                            page.reload()
                            expect(page.locator('#brief-roles')).to_contain_text('未记录方向样本计数')
                            expect(page.locator('#brief-roles')).not_to_contain_text('完整正文 0')
                            assert manifest_path.read_bytes() == legacy_raw
                            assert hashlib.sha256(workspace.report_file(source_id, 'requirements.jsonl').read_bytes()).hexdigest() == before_hash
                            result['checks'].append('legacy report view leaves unknown role counts unknown and does not rewrite saved report files')
                        finally:
                            manifest_path.write_bytes(current_manifest)
                        page.goto(server.origin + '/advanced#report=invalid')
                        expect(page.locator('#notice')).to_contain_text('未自动加载其他报告')
                        expect(page.locator('#source-run')).to_have_value('')
                        result['checks'].append('readable guide downloads, narrow-screen layout holds, invalid report never falls back to another batch')
                        assert not result['page_errors'], result['page_errors']
                        assert not result['external_browser_requests'], result['external_browser_requests']
                        result['success'] = True
                    finally:
                        browser.close()
            finally:
                server.shutdown(); server.server_close(); worker.join(timeout=5)
    finally:
        (out / 'results.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result['success'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
