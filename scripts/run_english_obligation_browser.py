"""Authored English JD through the original form, reports, CSV and history."""
import json
import os
from pathlib import Path
import sys
import tempfile
import threading

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from vibe_job_radar.workspace import Workspace
from vibe_job_radar.workbench import LocalServer


def main():
    from playwright.sync_api import sync_playwright,expect
    out=ROOT/'browser-acceptance/english-obligation';out.mkdir(parents=True,exist_ok=True)
    result={'success':False,'checks':[],'page_errors':[],'external_requests':[],
        'scope':'Artificial authored English input in the real local form/report/CSV flow; no market claim or source request.'}
    with tempfile.TemporaryDirectory(prefix='radar-english-ui-') as tmp:
        workspace=Workspace(tmp);server=LocalServer(workspace)
        thread=threading.Thread(target=server.serve_forever,kwargs={'poll_interval':.01},daemon=True);thread.start()
        try:
            with sync_playwright() as pw:
                options={'headless':True}
                if os.environ.get('RADAR_TEST_CHROMIUM'):options['executable_path']=os.environ['RADAR_TEST_CHROMIUM']
                browser=pw.chromium.launch(**options)
                try:
                    result['browser_version']=browser.version
                    context=browser.new_context(viewport={'width':1280,'height':900},accept_downloads=True)
                    def local_only(route):
                        if route.request.url.startswith(server.origin+'/'):route.continue_()
                        else:result['external_requests'].append('unexpected_nonlocal');route.abort()
                    context.route('**/*',local_only)
                    page=context.new_page();page.on('pageerror',lambda exc:result['page_errors'].append(type(exc).__name__))
                    page.goto(server.entry_url);expect(page.locator('#counts')).to_contain_text('真实记录 0')
                    def add(text,slug,company):
                        form=page.locator('#job-form')
                        form.locator('[name=title]').fill('Software Architect')
                        form.locator('[name=company]').fill(company)
                        form.locator('[name=platform]').select_option('boss')
                        form.locator('[name=url]').fill('https://www.zhipin.com/job_detail/'+slug+'.html')
                        form.locator('[name=text]').fill(text)
                        form.locator('[name=rights_note]').fill('Authored local test fixture, not a real job or market evidence.')
                        form.locator('[name=full_text_confirmed]').check()
                        form.locator('button[type=submit]').click()
                    add('About us:\nWe build Claude Code for software teams.\nBenefits:\nWe provide a Cursor subscription.\n'
                        'Qualifications:\nYou must use Cursor. You must not upload customer secrets.', 'english-positive-fixture','FICTIONAL FIXTURE ONE')
                    expect(page.locator('#counts')).to_contain_text('真实记录 1');page.locator('#analyze').click()
                    expect(page.locator('#report-stats')).to_contain_text('已接受正向要求 2')
                    expect(page.locator('#requirements')).to_contain_text('You must use Cursor.')
                    expect(page.locator('#requirements')).to_contain_text('You must not upload customer secrets.')
                    expect(page.locator('#requirements')).not_to_contain_text('subscription')
                    expect(page.locator('#requirements')).not_to_contain_text('software teams')
                    expect(page.locator('#manifest')).to_contain_text('rules-0.2.0')
                    first=next((workspace.root/'reports').iterdir())
                    original={p.name:p.read_bytes() for p in first.iterdir() if p.is_file()}
                    with page.expect_download() as pending:page.get_by_role('button',name='requirements_zh.csv',exact=True).click()
                    pending.value.save_as(str(out/'authored-requirements.csv'))
                    csv=(out/'authored-requirements.csv').read_text(encoding='utf-8-sig')
                    assert 'You must use Cursor.' in csv and 'customer secrets' in csv and 'subscription' not in csv
                    result['checks'].append('authored English form: company/benefits excluded, two positive tool capabilities and a separate prohibition reach original report/CSV with exact quotes')

                    add('Our platform integrates Codex. This is an authored ambiguous mention, with no candidate-use obligation.',
                        'english-ambiguous-fixture','FICTIONAL FIXTURE TWO')
                    expect(page.locator('#counts')).to_contain_text('真实记录 2');page.locator('#analyze').click()
                    expect(page.locator('#report-stats')).to_contain_text('正文岗位组 2')
                    expect(page.locator('#report-stats')).to_contain_text('已接受正向要求 2')
                    expect(page.locator('#requirements')).to_contain_text('Our platform integrates Codex.')
                    expect(page.locator('#requirements')).to_contain_text('needs_review')
                    assert original=={p.name:p.read_bytes() for p in first.iterdir() if p.is_file()}
                    page.reload();expect(page.locator('#runs button')).to_have_count(2)
                    page.locator('#runs button').last.click()
                    expect(page.locator('#report')).to_be_visible()
                    page.set_viewport_size({'width':390,'height':844})
                    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                    page.locator('#research-brief').screenshot(path=str(out/'english-report-mobile.png'))
                    result['checks'].append('ambiguous product mention remains review-only without inflating positive count; old report bytes/history survive reload and 390px layout')
                    assert server.public_tasks.snapshot()['status']=='idle'
                    assert not result['page_errors'] and not result['external_requests']
                    result.update(success=True,source_requests=0)
                finally:browser.close()
        finally:
            server.shutdown();server.server_close();thread.join(5)
            (out/'results.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(result,ensure_ascii=True,indent=2))


if __name__=='__main__':main()
