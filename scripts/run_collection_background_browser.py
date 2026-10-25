"""Actual browser ownership/credential checks for non-category background batches."""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
from unittest.mock import patch
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT/'src'), str(ROOT/'tests')]
from vibe_job_radar.workbench import LocalServer
from vibe_job_radar.workspace import Workspace
from vibe_job_radar.guided.rate import Limits, RateLedger
from vibe_job_radar.network import SafeHTTP
from test_public_category import Wire, listing, card, job_url


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--channel', choices=['msedge'])
    parser.add_argument('--case', choices=['all','paused-startup'], default='all')
    parser.add_argument('--out', type=Path, default=ROOT/'browser-acceptance/collection-background')
    args = parser.parse_args()
    from playwright.sync_api import sync_playwright, expect
    args.out.mkdir(parents=True, exist_ok=True)
    result = dict(success=False, checks=[], page_errors=[], external_browser_requests=[], unmocked_upstream_requests=0,
                  phase='starting', scope='Actual browser and original background owner/collector/report; temporary workspace and authored upstream only.')
    releases = []
    requests = []
    credential_polling_requests = []
    secrets = ('ARTIFICIAL-FEED-FIRST', 'ARTIFICIAL-FEED-RESUME')
    with tempfile.TemporaryDirectory() as tmp:
        workspace = Workspace(tmp)
        ledger = RateLedger(workspace.root/'guided/rates.sqlite', Limits(page_interval=0, request_interval=0))
        server = LocalServer(workspace)
        thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .01}, daemon=True)
        def reject_unmocked_request(*args, **kwargs):
            result['unmocked_upstream_requests'] += 1
            raise AssertionError('Unmocked upstream request is forbidden, including failure cleanup.')
        upstream_guard = patch.object(SafeHTTP, 'request', side_effect=reject_unmocked_request)
        upstream_guard.start()
        thread.start()
        browser = None
        try:
            with patch('vibe_job_radar.collection_rate.RateLedger', return_value=ledger), sync_playwright() as pw:
                browser = pw.chromium.launch(headless=True, **({'channel': args.channel} if args.channel else {}))
                result['browser_version'] = browser.version
                context = browser.new_context(viewport={'width': 1440, 'height': 1000})
                def local_only(route):
                    if route.request.url.startswith(server.origin+'/'):
                        route.continue_()
                    else:
                        result['external_browser_requests'].append(route.request.url)
                        route.abort()
                context.route('**/*', local_only)
                def observed_request(request):
                    path=urlsplit(request.url).path
                    requests.append(path)
                    if path in {'/api/collection/background/state', '/api/collection/background/pause', '/api/collection/status', '/api/collection/list'} and request.post_data:
                        if 'api_key' in json.loads(request.post_data):
                            credential_polling_requests.append(path)
                context.on('request', observed_request)
                def execution_requests():
                    return sum(path in {'/api/collection/background/start', '/api/collection/create_and_start'} for path in requests)
                def open_page(*, active=False):
                    page = context.new_page()
                    page.on('pageerror', lambda error: result['page_errors'].append(str(error)))
                    page.goto(server.entry_url)
                    page.get_by_role('link', name='进入自动采集 / 原文复核 / 附件与量化指标工作台').click()
                    expect(page.locator('#collection-controls')).to_be_enabled()
                    if not active:
                        expect(page.locator('#collect-start')).to_be_enabled()
                    return page
                def await_event(page, event):
                    deadline = time.monotonic()+8
                    while not event.is_set() and time.monotonic()<deadline:
                        page.wait_for_timeout(20)
                    assert event.is_set(), 'authored upstream was not reached in the original observer window'
                def no_secret_saved(page=None):
                    state = json.dumps(server.collection_runner.state(), ensure_ascii=False)
                    assert all(secret not in state for secret in secrets)
                    if page is not None:
                        assert all(secret not in page.locator('body').inner_text() for secret in secrets)
                        assert page.locator('#collect-form [name=api_key]').input_value()==''
                    for path in workspace.root.rglob('*'):
                        if path.is_file():
                            if path.name=='.writer.lock':
                                # Windows deliberately denies reading held lock bytes;
                                # these fixed one-byte markers cannot contain a token.
                                assert path.stat().st_size==1
                                continue
                            raw = path.read_bytes()
                            assert all(secret.encode() not in raw for secret in secrets), 'credential persisted in '+path.name
                def check_paused_startup_order():
                    result['phase'] = 'paused_owner_precedes_bootstrap'
                    task = server.collector.start({'mode':'feed','roles':['time_series'],
                        'platforms':['liepin'],'permit_platforms':[], 'feed_budget':2,
                        'endpoint':'https://www.liepin.com/artificial-startup-feed',
                        'contract_ref':'https://www.liepin.com/artificial-contract',
                        'rights_note':'Authored paused-owner startup order; no real upstream.', 'consent':True})
                    entered, release = threading.Event(), threading.Event(); releases.append(release)
                    calls = []
                    def startup_feed(url, *, headers=None, **kwargs):
                        assert url=='https://www.liepin.com/artificial-startup-feed'
                        calls.append(dict(headers or {})); entered.set()
                        assert release.wait(30), 'authored startup response release missing'
                        return {'jobs':[], 'next_cursor':'remaining'}
                    page = context.new_page()
                    page.on('pageerror', lambda error:result['page_errors'].append(str(error)))
                    try:
                        with patch.object(SafeHTTP,'json',side_effect=startup_feed):
                            server.collection_runner.start({'id':task['id'],'consent':True,'api_key':secrets[0]})
                            await_event(page, entered)
                            server.collection_runner.pause({'id':task['id']}); release.set()
                            deadline=time.monotonic()+10
                            while time.monotonic()<deadline:
                                paused=server.collection_runner.state()
                                if not paused['active'] and paused['status']=='paused':break
                                time.sleep(.02)
                            assert not paused['active'] and paused['status']=='paused'
                        assert calls==[{'Authorization':'Bearer '+secrets[0]}]
                        starts=execution_requests()
                        held=[]
                        page.route('**/api/bootstrap',lambda route:held.append(route))
                        page.add_init_script('if(location.origin === '+json.dumps(server.origin)+') sessionStorage.setItem("radar-session",'+json.dumps(server.token)+');')
                        page.goto(server.origin+'/advanced')
                        expect(page.locator('#collect-progress')).to_contain_text('paused',timeout=10000)
                        expect(page.locator('#collect-json')).to_contain_text(task['id'])
                        expect(page.locator('#collect-resume')).to_be_disabled()
                        assert len(held)==1
                        result['startup_order']={'paused_task_id':task['id'],'bootstrap_held_until_paused':True,
                            'mode_before_bootstrap':page.locator('#collect-form [name=mode]').input_value()}
                        held[0].continue_()
                        expect(page.locator('#collect-resume')).to_be_enabled()
                        result['startup_order']['mode_after_bootstrap']=page.locator('#collect-form [name=mode]').input_value()
                        expect(page.locator('#collect-form [name=mode]')).to_have_value('feed')
                        page.once('dialog',lambda dialog:dialog.dismiss())
                        page.locator('#collect-resume').click()
                        expect(page.locator('#notice')).to_contain_text('已取消继续')
                        assert execution_requests()==starts and len(calls)==1
                        assert server.collector.status({'id':task['id']})['feed_requests']==1
                        no_secret_saved(page)
                        result['checks'].append('a paused feed observed before delayed bootstrap keeps its mode when configuration arrives; cancelling tokenless resume sends no start and consumes no additional budget')
                    finally:
                        page.close()

                if args.case=='paused-startup':
                    check_paused_startup_order()
                    assert not result['page_errors'] and not result['external_browser_requests']
                    result['phase']='completed';result['success']=True
                    browser.close();browser=None
                    print(json.dumps(result,ensure_ascii=True,indent=2))
                    return 0
                page = open_page()
                form = page.locator('#collect-form')
                result['phase'] = 'url_close_without_ui_steps'
                page.get_by_role('button', name='我有职位链接：套用 URL 入门参数').click()
                form.locator('[name=urls]').fill(job_url(71)+'\n'+job_url(72))
                page.get_by_role('button', name='从链接识别来源平台（不授予权限）').click()
                form.locator('[name=detail_budget]').fill('2')
                page.locator('#collect-roles input[value=time_series]').uncheck()
                page.locator('#collect-roles input[value=architect]').check()
                form.locator('[name=rights_note]').fill('人工 URL 后台离页验收，临时工作区，不访问真实站点。')
                page.locator('#collect-permits input[value=liepin]').check()
                form.locator('[name=consent]').check()
                entered, release = threading.Event(), threading.Event(); releases.append(release)
                wire = Wire(listing(card(71)+card(72)))
                def url_response(url, **kwargs):
                    if url==job_url(71):
                        entered.set()
                        assert release.wait(30), 'authored response release missing'
                    return wire.public_get(url, **kwargs)
                with patch.object(SafeHTTP, 'public_get', side_effect=url_response):
                    page.locator('#collect-start').click(); await_event(page, entered)
                    ident = server.collection_runner.state()['id']
                    page.close()
                    release.set()
                    deadline = time.monotonic()+10
                    while time.monotonic()<deadline:
                        saved = server.collector.status({'id': ident})
                        if saved['status'] in {'completed','needs_attention','empty'}: break
                        time.sleep(.03)
                    assert saved['status']=='completed' and saved['saved_detail_count']==2 and saved['report_id']
                    assert wire.calls==['https://www.liepin.com/robots.txt', job_url(71), job_url(72)]
                report = workspace.root/'reports'/saved['report_id']
                report_hashes = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in report.iterdir() if p.is_file()}
                assert 'run_manifest.json' in report_hashes
                assert json.loads((report/'run_manifest.json').read_text(encoding='utf-8'))['stats']['full_text_job_groups']==2
                result['checks'].append('closing the real URL page during the first body still completes both bodies and the original report, with zero UI step calls')
                page = open_page(); form = page.locator('#collect-form')
                starts = execution_requests()
                page.locator('#collect-history').select_option(ident); page.locator('#collect-load').click()
                expect(page.locator('#collect-progress')).to_contain_text('completed')
                assert json.loads(page.locator('#collect-json').text_content())['report_id']==saved['report_id']
                assert execution_requests()==starts
                result['checks'].append('a newly opened page reads the completed URL task/report without authorizing another start')

                result['phase'] = 'feed_rejected_start_preserves_input'
                form.locator('[name=mode]').select_option('feed')
                page.locator('#collect-roles input[value=architect]').uncheck()
                page.locator('#collect-roles input[value=time_series]').check()
                form.locator('[name=endpoint]').fill('https://www.liepin.com/artificial-background-feed')
                form.locator('[name=contract_ref]').fill('https://www.liepin.com/artificial-contract')
                form.locator('[name=feed_budget]').fill('2')
                form.locator('[name=rights_note]').fill('人工 feed 后台与密钥生命周期验收。')
                form.locator('[name=api_key]').fill(secrets[0]); form.locator('[name=consent]').check()
                # This case verifies the retained explicit-resume endpoint. New
                # create+execute is one application command and cannot be split by
                # intercepting its internal worker call from the browser.
                feed_data = page.evaluate('() => collectionGuide.data()')
                feed_data.pop('api_key', None)
                feed_id = server.collector.start(feed_data)['id']
                page.locator('#collect-refresh').click()
                expect(page.locator('#collect-history')).to_have_value(ident)
                expect(page.locator('#collect-history option[value="'+feed_id+'"]')).to_have_count(1)
                page.locator('#collect-history').select_option(feed_id)
                page.locator('#collect-load').click()
                expect(page.locator('#collect-json')).to_contain_text(feed_id)
                rejected_path = server.origin+'/api/collection/background/start'
                context.route(rejected_path, lambda route: route.fulfill(status=400, content_type='application/json', body=json.dumps({'error':'人工拒绝：请核对启动条件。'})))
                page.locator('#collect-resume').click()
                expect(page.locator('#notice')).to_contain_text('人工拒绝')
                expect(form.locator('[name=api_key]')).to_have_value(secrets[0])
                expect(page.locator('#collect-resume')).to_be_enabled()
                feed_id = json.loads(page.locator('#collect-json').text_content())['id']
                assert server.collector.status({'id': feed_id})['feed_requests']==0
                context.unroute(rejected_path)
                result['checks'].append('a rejected background start retains the entered token for correction and performs no provider request')
                entered, release = threading.Event(), threading.Event(); releases.append(release)
                feed_calls = []
                def feed_response(url, *, headers=None, **kwargs):
                    assert url.startswith('https://www.liepin.com/artificial-background-feed')
                    feed_calls.append((url, dict(headers or {})))
                    if len(feed_calls)==1:
                        entered.set()
                        assert release.wait(30), 'authored feed response release missing'
                    number = len(feed_calls)
                    return {'jobs':[{'title':'时间序列算法工程师', 'text':'负责时间序列预测与模型评估，熟练使用 Cursor 辅助编程并完成代码审查和单元测试。人工验收材料。',
                                     'url':job_url(80+number), 'evidence_level':'full_text'}],
                            'next_cursor':'second' if number==1 else ''}
                with patch.object(SafeHTTP, 'json', side_effect=feed_response):
                    result['phase'] = 'feed_accept_reopen_pause'
                    page.locator('#collect-resume').click(); await_event(page, entered)
                    expect(form.locator('[name=api_key]')).to_have_value('')
                    starts = execution_requests()
                    reopened = open_page(active=True)
                    expect(reopened.locator('#collect-json')).to_contain_text(feed_id)
                    expect(reopened.locator('#collect-background-note')).to_contain_text('可以关闭或刷新网页')
                    assert execution_requests()==starts
                    page.close(); page = reopened; form = page.locator('#collect-form')
                    no_secret_saved(page)
                    page.locator('#collect-pause').click()
                    expect(page.locator('#notice')).to_contain_text('已请求暂停')
                    release.set()
                    expect(page.locator('#collect-progress')).to_contain_text('paused', timeout=10000)
                    expect(page.locator('#collect-resume')).to_be_enabled()
                    paused = server.collector.status({'id': feed_id})
                    assert paused['feed_requests']==1 and len(feed_calls)==1
                    page.reload()
                    expect(page.locator('#collect-progress')).to_contain_text('paused', timeout=10000)
                    expect(page.locator('#collect-resume')).to_be_enabled()
                    assert execution_requests()==starts and len(feed_calls)==1
                    no_secret_saved(page)
                    page.once('dialog', lambda dialog: dialog.dismiss())
                    page.locator('#collect-resume').click()
                    expect(page.locator('#notice')).to_contain_text('已取消继续')
                    assert execution_requests()==starts and len(feed_calls)==1
                    result['checks'].append('accepted feed clears its form token immediately; another page observes then pauses it, reload stays paused and cancelling anonymous resume sends no request')
                    result['phase'] = 'feed_explicit_resume_with_new_token'
                    form.locator('[name=api_key]').fill(secrets[1])
                    page.locator('#collect-resume').click()
                    expect(form.locator('[name=api_key]')).to_have_value('')
                    expect(page.locator('#collect-progress')).to_contain_text('completed', timeout=10000)
                    expect(page.locator('#collect-resume')).to_be_enabled()
                    done = server.collector.status({'id': feed_id})
                    assert done['feed_requests']==2 and done['report_id']
                    assert len(done['feed_outcomes'])==2 and all(row['status']=='ok' and row['full_text']==1 for row in done['feed_outcomes'])
                    feed_report=workspace.root/'reports'/done['report_id']
                    assert json.loads((feed_report/'run_manifest.json').read_text(encoding='utf-8'))['stats']['full_text_job_groups']==2
                    assert len(feed_calls)==2 and 'cursor=second' in feed_calls[1][0]
                    assert feed_calls[0][1]=={'Authorization':'Bearer '+secrets[0]}
                    assert feed_calls[1][1]=={'Authorization':'Bearer '+secrets[1]}
                    no_secret_saved(page)
                result['checks'].append('explicit resume uses only the new memory token for the remaining feed cursor and produces the combined original report without persisting either credential')
                assert report_hashes=={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in report.iterdir() if p.is_file()}
                assert '/api/collection/step' not in requests
                assert not credential_polling_requests
                assert not result['page_errors'] and not result['external_browser_requests']
                result['background_start_requests'] = requests.count('/api/collection/background/start')
                result['create_and_start_requests'] = requests.count('/api/collection/create_and_start')
                result['ui_step_requests'] = 0
                result['credential_polling_requests'] = 0
                result['checks'].append('no original report changes, no direct step requests, no browser external network requests or JavaScript errors')
                check_paused_startup_order()
                assert not result['page_errors'] and not result['external_browser_requests']
                result['phase'] = 'completed'; result['success'] = True
                browser.close(); browser = None
        finally:
            for release in releases: release.set()
            if browser is not None:
                try: browser.close()
                except Exception: pass
            server.shutdown(); server.server_close(); thread.join(timeout=5)
            upstream_guard.stop()
            if result['unmocked_upstream_requests']:
                result['success'] = False
            (args.out/'results.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    assert not result['unmocked_upstream_requests']
    print(json.dumps(result, ensure_ascii=True, indent=2))
    return 0


if __name__=='__main__':
    raise SystemExit(main())
