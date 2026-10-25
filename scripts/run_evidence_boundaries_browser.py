"""Actual browser checks for draft ownership and independent advanced work areas."""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
from unittest.mock import patch
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT/'src'), str(ROOT/'tests')]
from vibe_job_radar.network import SafeHTTP
from vibe_job_radar.workbench import LocalServer
from vibe_job_radar.workspace import Workspace
from test_workbench import capture


def verify_category_history_boundaries(browser, result):
    """Keep optional history observations outside confirmed category execution."""
    import hashlib
    import time
    from playwright.sync_api import expect
    from vibe_job_radar.collection import TERMINAL
    from vibe_job_radar.guided.rate import Limits, RateLedger
    from vibe_job_radar.public_category import URL
    from test_public_category import card, data, job_url
    from test_public_category_pages import PageWire, page_html

    def hashes(directory):
        return {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                for p in directory.iterdir() if p.is_file()}

    cases = result.setdefault('category_history_results', [])
    for kind in ('next', 'page', 'existing', 'recovery'):
        result['phase'] = 'category_history_' + kind
        observed = dict(case=kind, requests=[], receipts=[], history_failures=[],
                        state_failures=0, phase='setup', success=False)
        cases.append(observed)
        with tempfile.TemporaryDirectory(prefix='radar-category-history-') as tmp:
            workspace = Workspace(tmp)
            server = LocalServer(workspace)
            worker = None
            context = None
            held_history = []
            detail_entered, release_detail = threading.Event(), threading.Event()
            now = time.time()
            limits = Limits(page_interval=0, request_interval=0, **({'pages_day': 2} if kind == 'recovery' else {}))
            ledger = RateLedger(workspace.root/'guided/rates.sqlite', limits, clock=lambda: now)
            wire = PageWire(first=page_html(cards=''.join(card(i) for i in (601,602,603))),
                            second=page_html(1, cards=''.join(card(i) for i in (601,604,605))))
            allowed = {'https://www.liepin.com/robots.txt', URL, URL+'pn1/',
                       *(job_url(i) for i in range(601,606))}
            blocked_detail = job_url(602 if kind == 'next' else 604)
            history_fault = {'enabled': False, 'held_once': False}

            def authored_get(url):
                assert url in allowed, 'Unexpected authored-source URL'
                if kind in {'next', 'page'} and url == blocked_detail:
                    detail_entered.set()
                    assert release_detail.wait(30), 'Authored detail was not released inside its cleanup bound'
                return wire.public_get(url)

            try:
                with patch('vibe_job_radar.collection_rate.RateLedger', return_value=ledger), patch.object(SafeHTTP, 'public_get', side_effect=authored_get):
                    parent = server.collector.start(data(detail_budget=3 if kind == 'recovery' else 1))
                    for _ in range(12):
                        parent = server.collector.step({'id': parent['id']})
                        if parent['status'] in TERMINAL:
                            break
                    if kind == 'recovery':
                        assert [row['status'] for row in parent['details']] == ['ok','daily_limit','host_stopped']
                    else:
                        assert parent['status'] == 'completed' and parent['detail_attempts'] == 1
                    assert parent['report_id'] and parent['category_attempts'] == 1
                    parent_raw = server.collector._path(parent['id']).read_bytes()
                    parent_report = workspace.root/'reports'/parent['report_id']
                    report_hashes = hashes(parent_report)
                    before_calls = list(wire.calls)
                    before_ledger = ledger.summary('liepin')
                    observed.update(parent_id=parent['id'], parent_report_id=parent['report_id'],
                                    parent_sha256=hashlib.sha256(parent_raw).hexdigest(),
                                    parent_report_sha256=report_hashes, setup_calls=before_calls)
                    worker = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .01}, daemon=True)
                    worker.start()
                    context = browser.new_context(viewport={'width':1440, 'height':1000})
                    context.add_init_script('if(location.origin === '+json.dumps(server.origin)+') sessionStorage.setItem("radar-session",'+json.dumps(server.token)+');')

                    def local_only(route):
                        if route.request.url.startswith(server.origin+'/'):
                            route.continue_()
                        else:
                            result['external_requests'].append('unexpected_category_nonlocal')
                            route.abort()

                    def record_request(request):
                        path = urlsplit(request.url).path
                        if path.startswith('/api/'):
                            observed['requests'].append(dict(path=path,
                                body=json.loads(request.post_data) if request.post_data else None))

                    context.route('**/*', local_only)
                    context.on('request', record_request)
                    page = context.new_page()
                    page.on('pageerror', lambda error: result['page_errors'].append(str(error)))
                    page.goto(server.origin+'/advanced')
                    expect(page.locator('#collect-load')).to_be_enabled(timeout=5000)
                    page.locator('#collect-history').select_option(parent['id'])
                    page.locator('#collect-load').click()
                    expect(page.locator('#collect-json')).to_contain_text(parent['id'], timeout=5000)
                    if kind == 'page':
                        preview_label = '查看平台下一页（不联网）'
                        launch_label = '确认读取第 2 页并采集最多 1 条'
                        operation = 'category_page'
                    elif kind == 'recovery':
                        preview_label = '预览因等待未完成的正文（不联网）'
                        launch_label = '确认保存恢复任务（暂不联网）'
                        operation = 'category_recovery'
                    else:
                        preview_label = '预览这份名单的下一批（不联网）'
                        launch_label = '确认采集这批职位'
                        operation = 'category_next'
                    with page.expect_response('**/api/collection/'+operation+'_preview', timeout=5000) as response:
                        page.get_by_role('button', name=preview_label, exact=True).click()
                    plan = response.value.json()
                    assert response.value.status == 200 and plan['external_network_requests'] == 0
                    launch = page.get_by_role('button', name=launch_label, exact=True)
                    expect(launch).to_be_enabled(timeout=5000)
                    endpoint = '/api/collection/'+operation+'_start'
                    payload = dict(id=plan['id'], fingerprint=plan['fingerprint'], consent=True)
                    if kind == 'existing':
                        independently_saved = server.collector.category_next_start(payload)
                        assert independently_saved['created'] is True
                        # Observe completion of the actual UI read promise after a
                        # held response; the wrapper neither repeats nor alters it.
                        page.evaluate('''() => {const observe=observeCollectionHistory; window.categoryHistoryReads=[];
                            observeCollectionHistory=(...args)=>{const pending=observe(...args);window.categoryHistoryReads.push(pending);return pending;};}''')

                    def capture_creation(route):
                        actual = route.fetch(max_retries=0)
                        receipt = actual.json()
                        observed['receipts'].append(dict(status=actual.status, receipt=receipt,
                            request=json.loads(route.request.post_data)))
                        history_fault['enabled'] = actual.status == 200
                        route.fulfill(response=actual)

                    def history_read(route):
                        if not history_fault['enabled']:
                            route.continue_()
                        elif kind == 'existing' and not history_fault['held_once']:
                            history_fault['held_once'] = True
                            held_history.append(route)
                        elif kind == 'existing':
                            route.continue_()
                        else:
                            observed['history_failures'].append({'status':500})
                            route.fulfill(status=500, json={'error':'Artificial category history outage'})

                    def state_read(route):
                        if kind == 'page' and history_fault['enabled'] and observed['state_failures'] == 0:
                            observed['state_failures'] += 1
                            route.fulfill(status=500, json={'error':'Artificial category owner observation failure'})
                        else:
                            route.continue_()

                    page.route('**'+endpoint, capture_creation)
                    page.route('**/api/collection/list', history_read)
                    page.route('**/api/collection/background/state', state_read)
                    observed['phase'] = 'confirm_create'
                    if kind in {'next', 'page'}:
                        with page.expect_response('**/api/collection/background/start', timeout=5000) as started:
                            launch.click()
                        owner = started.value.json()
                        assert started.value.status == 200 and owner['active'] is True
                        observed['background_receipt'] = owner
                    else:
                        with page.expect_response('**'+endpoint, timeout=5000) as created:
                            launch.click()
                        assert created.value.status == 200
                    assert len(observed['receipts']) == 1
                    receipt = observed['receipts'][0]
                    child_id = receipt['receipt']['task']['id']
                    observed['child_id'] = child_id
                    assert receipt['status'] == 200 and receipt['request'] == payload
                    assert receipt['receipt']['created'] is (kind != 'existing')
                    expect(page.locator('#collect-json')).to_contain_text(child_id, timeout=5000)
                    starts = lambda: [r for r in observed['requests'] if r['path']=='/api/collection/background/start']

                    if kind in {'next', 'page'}:
                        assert owner['id'] == owner['task']['id'] == child_id
                        assert [r['body'] for r in starts()] == [dict(id=child_id, consent=True)]
                        expect(page.locator('#collection-history-state')).to_contain_text('Artificial category history outage', timeout=5000)
                        assert 'Artificial category history outage' not in page.locator('#notice').inner_text()
                        if kind == 'page':
                            expect(page.locator('#collection-state')).to_contain_text('后台状态未知', timeout=5000)
                            expect(page.locator('#collect-start')).to_be_disabled(timeout=5000)
                            expect(page.locator('#collection-history-state')).to_contain_text('Artificial category history outage', timeout=5000)
                            assert server.collection_runner.state()['active'] and observed['state_failures'] == 1
                            observed['unknown_state_preserved'] = True
                        release_detail.set()
                        if kind == 'page':
                            page.locator('#collection-reload').click()
                        expect(page.locator('#collect-progress')).to_contain_text('completed', timeout=30000)
                        expect(page.locator('#collect-start')).to_be_enabled(timeout=5000)
                        child = server.collector.status({'id':child_id})
                        assert child['report_id'] and child['report_id'] != parent['report_id']
                        assert child['detail_budget'] == child['detail_attempts'] == 1
                        assert child['category_attempts'] == (1 if kind == 'page' else 0)
                        expected_calls = ['https://www.liepin.com/robots.txt'] + ([URL+'pn1/'] if kind == 'page' else []) + [job_url(604 if kind == 'page' else 602)]
                        assert wire.calls[len(before_calls):] == expected_calls
                        if kind == 'page':
                            assert child['category_outcomes'][0]['candidates'][0]['status'] == 'previous_page_duplicate'
                        assert len(starts()) == 1
                        observed['completed_child'] = child
                        observed['additional_upstream_calls'] = wire.calls[len(before_calls):]
                        observed['history_warning'] = page.locator('#collection-history-state').inner_text()
                        assert 'Artificial category history outage' in observed['history_warning']
                        history_fault['enabled'] = False
                        page.locator('#collect-refresh').click()
                        expect(page.locator('#collection-history-state')).to_have_text('', timeout=5000)
                        assert len(starts()) == 1
                        result['checks'].append(kind+' category confirms and admits the exact child despite history failure, preserves the original report and budget, and isolates observation errors without replay')
                    elif kind == 'existing':
                        assert child_id == independently_saved['task']['id'] and not starts()
                        expect(page.locator('#collect-refresh')).to_be_enabled(timeout=5000)
                        page.locator('#collect-refresh').click()
                        expect(page.locator('#collect-history')).to_have_value(child_id, timeout=5000)
                        assert len(held_history) == 1 and wire.calls == before_calls
                        server.collection_runner.close()
                        with page.expect_response('**/api/collection/background/start', timeout=5000) as rejected:
                            page.locator('#collect-resume').click()
                        rejection = rejected.value.json()
                        assert rejected.value.status == 400
                        expect(page.locator('#notice')).to_contain_text('本工作台已关闭', timeout=5000)
                        assert [r['body'] for r in starts()] == [dict(id=child_id, consent=True)]
                        page.locator('#collect-history').select_option(parent['id'])
                        page.locator('#collect-load').click()
                        expect(page.locator('#collect-json')).to_contain_text(parent['id'], timeout=5000)
                        route = held_history.pop()
                        observed['history_failures'].append({'status':500, 'delayed':True})
                        route.fulfill(status=500, json={'error':'Artificial stale history outage'})
                        page.evaluate('() => Promise.all(window.categoryHistoryReads)')
                        expect(page.locator('#collect-history')).to_have_value(parent['id'], timeout=5000)
                        expect(page.locator('#collect-json')).to_contain_text(parent['id'], timeout=5000)
                        expect(page.locator('#notice')).to_contain_text(rejection['error'], timeout=5000)
                        expect(page.locator('#collection-history-state')).to_have_text('', timeout=5000)
                        child = server.collector.status({'id':child_id})
                        assert child['status']=='paused' and child['detail_attempts']==0 and not child['report_id']
                        assert not server.collection_runner.state()['active'] and wire.calls == before_calls
                        assert ledger.summary('liepin') == before_ledger
                        observed['explicit_rejection'] = rejection
                        observed['no_automatic_start'] = True
                        observed['stale_observation_ignored'] = True
                        result['checks'].append('an existing child is only displayed; explicit continuation retains real shutdown rejection, and a delayed history error cannot replace that rejection or another selection')
                    else:
                        expect(page.locator('#notice')).to_contain_text('Artificial category history outage', timeout=5000)
                        expect(page.locator('#collect-start')).to_be_enabled(timeout=5000)
                        child = server.collector.status({'id':child_id})
                        assert child['status']=='paused' and child['detail_attempts']==child['category_attempts']==0
                        assert child['detail_budget']==2 and child['details'][0]==parent['details'][0]
                        assert not child['report_id'] and not starts() and not server.collection_runner.state()['active']
                        assert wire.calls == before_calls and ledger.summary('liepin') == before_ledger
                        observed['saved_only_child'] = child
                        result['checks'].append('recovery confirmation still saves only, even when history fails: no runner, request or quota reservation, and inherited success remains unchanged')
                    assert server.collector._path(parent['id']).read_bytes() == parent_raw
                    assert hashes(parent_report) == report_hashes
                    assert len(server.collector.list()['runs']) == 2
                    assert not any(r['path'] == '/api/collection/step' for r in observed['requests'])
                    observed['parent_and_report_unchanged'] = True
                    observed['phase'] = 'completed'
                    observed['success'] = True
            finally:
                release_detail.set()
                for route in held_history:
                    try: route.abort()
                    except Exception: pass
                if context is not None:
                    context.close()
                if worker is not None:
                    server.shutdown()
                server.server_close()
                if worker is not None:
                    worker.join(timeout=5)



def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--channel', choices=['msedge'])
    parser.add_argument('--case', choices=['all','draft','areas','collection','linked-recovery','category-history'], default='all')
    parser.add_argument('--out', type=Path, default=ROOT/'browser-acceptance/evidence-boundaries')
    args = parser.parse_args()
    from playwright.sync_api import sync_playwright, expect
    args.out.mkdir(parents=True, exist_ok=True)
    result = dict(success=False, checks=[], page_errors=[], external_requests=[], phase='setup',
                  scope='Real browser, original evidence store and collector; temporary authored material and mocked upstream only.')
    requests = []
    clean = {k:v for k,v in os.environ.items() if not k.startswith('VIBE_RADAR_')}
    with tempfile.TemporaryDirectory(prefix='radar-boundaries-') as tmp, patch.dict(os.environ, clean, clear=True):
        workspace = Workspace(tmp)
        workspace.add_job(capture())
        source = workspace.analyze({})['id']
        server = LocalServer(workspace)
        row = server.evidence.catalogue({'run_id':source})['rows'][0]
        def save(project, revision, ident='', contribution='Artificial original contribution'):
            return server.evidence.save(dict(expected_revision=revision, name='Artificial person', evidence_id=ident,
                project=project, contribution=contribution, scope='offline', run_id=source,
                capabilities=[row['capability']], requirement_ids=[row['requirement_id']], metrics=[], review_status='draft'))
        first = save('First fixture project', 0)['evidence'][0]['evidence_id']
        second = save('Second fixture project', 1)['evidence'][-1]['evidence_id']
        def review(reason, revision):
            return server.evidence.review(dict(run_id=source, requirement_id=row['requirement_id'],
                decision='reject', reviewer='Artificial second editor', reason=reason, expected_revision=revision))
        worker = threading.Thread(target=server.serve_forever, kwargs={'poll_interval':.01}, daemon=True)
        worker.start()
        browser = None
        try:
            with patch.object(SafeHTTP, 'request', side_effect=AssertionError('Unexpected upstream')), sync_playwright() as pw:
                browser = pw.chromium.launch(headless=True, **({'channel':args.channel} if args.channel else {}))
                result['browser_version'] = browser.version
                context = browser.new_context(viewport={'width':1440,'height':1000})
                context.add_init_script('if(location.origin === '+json.dumps(server.origin)+') sessionStorage.setItem("radar-session",'+json.dumps(server.token)+');')
                def local_only(route):
                    if route.request.url.startswith(server.origin+'/'): route.continue_()
                    else: result['external_requests'].append('unexpected_nonlocal');route.abort()
                context.route('**/*', local_only)
                context.on('request', lambda request:requests.append((urlsplit(request.url).path, request.post_data)))
                def rejected_read(message):
                    return lambda route:route.fulfill(status=400,json={'error':message})
                def new_page(routes=(), *, linked=True):
                    page = context.new_page()
                    page.on('pageerror', lambda error:result['page_errors'].append(str(error)))
                    for path, message in routes:
                        page.route('**'+path, rejected_read(message))
                    page.goto(server.origin+'/advanced'+('#report='+(source if linked is True else str(linked)) if linked else ''))
                    return page
                def upload(page):
                    page.get_by_text('上传证据附件（1 MB / 文件，工作区最多 50 MB / 100 个）',exact=True).click()
                    page.locator('#evidence-file').set_input_files({'name':'authored-proof.txt','mimeType':'text/plain','buffer':b'Authored local fixture; no real personal material.'})
                    page.locator('#upload-form [name=rights_confirmed]').check()
                    with page.expect_response('**/api/evidence/upload') as response:
                        page.locator('#upload-form button[type=submit]').click()
                    assert response.value.status == 200
                    expect(page.locator('#upload-result')).to_contain_text('SHA-256')
                    expect(page.locator('#evidence-form button[type=submit]')).to_be_enabled()
                def post_click(page, selector, endpoint, expected):
                    with page.expect_response('**'+endpoint) as response: page.locator(selector).click()
                    assert response.value.status == expected, (endpoint, response.value.status, expected)
                    return response.value.json()
                def execution_count():
                    return sum(path in {'/api/collection/create_and_start','/api/collection/background/start'} for path,_ in requests)

                if args.case in {'all','draft'}:
                    result['phase'] = 'stale_evidence_upload_observation'
                    page = new_page()
                    expect(page.locator('#new-evidence')).to_be_enabled()
                    page.locator('#saved-evidence .card').filter(has_text='First fixture project').get_by_role('button',name='编辑证据').click()
                    form = page.locator('#evidence-form')
                    expect(form.locator('[name=evidence_id]')).to_have_value(first)
                    form.locator('[name=contribution]').fill('Unsaved first-window draft')
                    save('First fixture project', 2, first, 'Saved in second window')
                    upload(page)
                    expect(page.locator('#draft-status')).to_contain_text('基于版本 2，资料已更新至 3')
                    post_click(page, '#evidence-form button[type=submit]', '/api/evidence/save', 409)
                    expect(form.locator('[name=contribution]')).to_have_value('Unsaved first-window draft')
                    assert next(e for e in server.evidence.state()['evidence'] if e['evidence_id']==first)['contribution']=='Saved in second window'
                    result['checks'].append('attachment observation keeps the old draft revision; a stale save is rejected without replacing another window or losing local input')

                    result['phase'] = 'explicit_reload_own_review_identity'
                    page.once('dialog',lambda dialog:dialog.accept())
                    page.locator('#reload-state').click()
                    expect(form.locator('[name=contribution]')).to_have_value('Saved in second window')
                    expect(page.locator('#new-evidence')).to_be_enabled()
                    page.get_by_role('button',name='复核此条',exact=True).first.click()
                    rf = page.locator('#review-form')
                    rf.locator('[name=reviewer]').fill('Artificial same-page reviewer')
                    rf.locator('[name=reason]').fill('Explicit same-page original review')
                    post_click(page, '#review-form button[type=submit]', '/api/evidence/review', 200)
                    expect(page.locator('#draft-status')).to_contain_text('基于版本 4')
                    form.locator('[name=contribution]').fill('Explicit reloaded edit')
                    post_click(page, '#evidence-form button[type=submit]', '/api/evidence/save', 200)
                    expect(form.locator('[name=evidence_id]')).to_have_value(first)
                    assert {e['evidence_id'] for e in server.evidence.state()['evidence']}=={first,second}
                    assert next(e for e in server.evidence.state()['evidence'] if e['evidence_id']==second)['contribution']=='Artificial original contribution'
                    result['checks'].append('explicit reload restores saved values; a same-page review advances only the matching draft base and saving preserves the selected evidence identity')

                    result['phase'] = 'stale_review_upload_observation'
                    page.locator('#load-requirements').click()
                    expect(page.locator('#new-evidence')).to_be_enabled()
                    page.get_by_role('button',name='复核此条',exact=True).first.click()
                    rf.locator('[name=reason]').fill('Unsaved first-window review')
                    review('Saved second-window review',5)
                    page.get_by_text('上传证据附件（1 MB / 文件，工作区最多 50 MB / 100 个）',exact=True).click()
                    upload(page)
                    expect(page.locator('#review-draft-status')).to_contain_text('基于版本 5，资料已更新至 6')
                    post_click(page, '#review-form button[type=submit]', '/api/evidence/review', 409)
                    expect(rf.locator('[name=reason]')).to_have_value('Unsaved first-window review')
                    assert server.evidence.state()['reviews'][source+':'+row['requirement_id']]['reason']=='Saved second-window review'
                    result['checks'].append('review form owns its original catalogue revision and report; refreshing attachments cannot authorize overwriting another review')

                    result['phase'] = 'post_commit_newer_revision_receipt'
                    page.once('dialog',lambda dialog:dialog.accept())
                    page.locator('#reload-state').click()
                    expect(page.locator('#draft-status')).to_contain_text('基于版本 6')
                    expect(page.locator('#new-evidence')).to_be_enabled()
                    form.locator('[name=contribution]').fill('Committed before concurrent update')
                    original_save = server.evidence.save
                    def committed_then_changed(data):
                        own = original_save(data)
                        concurrent = dict(data, expected_revision=own['revision'], contribution='Concurrent after commit')
                        return original_save(concurrent)
                    with patch.object(server.evidence, 'save', side_effect=committed_then_changed):
                        receipt = post_click(page, '#evidence-form button[type=submit]', '/api/evidence/save', 200)
                    assert receipt['revision']==8
                    expect(page.locator('#notice')).to_contain_text('期间资料又有修改')
                    expect(page.locator('#draft-status')).to_contain_text('基于版本 6，资料已更新至 8')
                    post_click(page, '#evidence-form button[type=submit]', '/api/evidence/save', 409)
                    assert next(e for e in server.evidence.state()['evidence'] if e['evidence_id']==first)['contribution']=='Concurrent after commit'
                    result['checks'].append('a successful save receipt already advanced by another writer preserves the old draft base and requires explicit reload instead of silently rebasing')
                    page.close()

                if args.case in {'all','areas'}:
                    result['phase'] = 'independent_initialization'
                    before = execution_count()
                    page = new_page([('/api/bootstrap','Artificial configuration failure')])
                    expect(page.locator('#collection-state')).to_contain_text('Artificial configuration failure')
                    expect(page.locator('#new-evidence')).to_be_enabled()
                    expect(page.locator('#research-return')).to_have_attribute('href','/#report='+source)
                    expect(page.locator('#collect-start')).to_be_disabled()
                    page.close()
                    page = new_page([('/api/evidence/state','Artificial personal-store failure')])
                    expect(page.locator('#evidence-state')).to_contain_text('Artificial personal-store failure')
                    expect(page.locator('#collect-start')).to_be_enabled()
                    expect(page.locator('#new-evidence')).to_be_disabled()
                    expect(page.locator('#research-return')).to_have_attribute('href','/#report='+source)
                    page.close()
                    page = new_page([('/api/collection/background/state','Artificial unknown owner')])
                    expect(page.locator('#collection-state')).to_contain_text('Artificial unknown owner')
                    expect(page.locator('#new-evidence')).to_be_enabled()
                    expect(page.locator('#collect-start')).to_be_disabled()
                    page.unroute('**/api/collection/background/state')
                    page.locator('#collection-reload').click()
                    expect(page.locator('#collect-start')).to_be_enabled()
                    assert execution_count()==before
                    page.close()
                    page=new_page(linked='invalid-link')
                    expect(page.locator('#new-evidence')).to_be_enabled()
                    expect(page.locator('#source-run')).to_have_value('')
                    page.locator('#reload-state').click()
                    expect(page.locator('#new-evidence')).to_be_enabled()
                    expect(page.locator('#source-run')).to_have_value('')
                    page.close()
                    assert not any(path=='/api/status' for path,_ in requests)
                    result['checks'].append('configuration, personal-store and historical-report reads have independent failure boundaries; an unknown collection owner blocks execution until an explicit read, with no auto-start')

                if args.case in {'all','linked-recovery'}:
                    result['phase']='linked_report_recovers_after_personal_store_failure'
                    workspace.analyze({})
                    history=server.evidence.state()['runs']
                    assert len(history)>=2
                    decoy, linked=history[0]['id'],history[1]['id']
                    assert decoy!=linked
                    writes=lambda:sum(path in {'/api/evidence/save','/api/evidence/review','/api/evidence/remove','/api/reports/start'} for path,_ in requests)
                    before_writes=writes()
                    page=new_page([('/api/evidence/state','Artificial initial personal-store outage')],linked=linked)
                    expect(page.locator('#evidence-state')).to_contain_text('Artificial initial personal-store outage')
                    expect(page.locator('#research-return')).to_have_attribute('href','/#report='+linked)
                    expect(page.locator('#source-run')).to_have_value('')
                    expect(page.locator('#new-evidence')).to_be_disabled()
                    page.unroute('**/api/evidence/state')
                    page.locator('#reload-state').click()
                    expect(page.locator('#new-evidence')).to_be_enabled()
                    result['linked_recovery']={'linked_report':linked,'first_history_report':decoy,
                        'selection_after_recovery':page.locator('#source-run').input_value(),
                        'loaded_run_after_recovery':page.evaluate('runId'),
                        'return_href':page.locator('#research-return').get_attribute('href')}
                    expect(page.locator('#source-run')).to_have_value(linked)
                    assert page.evaluate('runId')==linked
                    expect(page.locator('#requirement-list input[type=checkbox]').first).to_be_visible()
                    catalogue=[json.loads(body)['run_id'] for path,body in requests if path=='/api/evidence/catalogue']
                    assert catalogue[-1]==linked
                    # The pending initial selection is consumed when applied, not
                    # on each later explicit reload or ordinary profile observation.
                    page.locator('#source-run').select_option(decoy)
                    page.locator('#load-requirements').click()
                    expect(page.locator('#new-evidence')).to_be_enabled()
                    assert page.evaluate('runId')==decoy
                    page.locator('#reload-state').click()
                    expect(page.locator('#new-evidence')).to_be_enabled()
                    expect(page.locator('#source-run')).to_have_value(decoy)
                    assert page.evaluate('runId')==decoy and writes()==before_writes
                    result['checks'].append('a verified nonfirst historical report survives initial personal-store failure and explicit recovery, loads the same source, then preserves a later explicit different report without automatic profile writes')
                    page.close()

                if args.case in {'all','collection'}:
                    result['phase'] = 'combined_feed_preflight_and_acceptance'
                    page = new_page(linked=False)
                    expect(page.locator('#collect-start')).to_be_enabled()
                    form = page.locator('#collect-form')
                    form.locator('[name=mode]').select_option('feed')
                    form.locator('[name=endpoint]').fill('https://www.zhipin.com/artificial-boundary-feed')
                    form.locator('[name=contract_ref]').fill('https://www.zhipin.com/artificial-contract')
                    form.locator('[name=feed_budget]').fill('1')
                    form.locator('[name=api_key]').fill('ARTIFICIAL-MEMORY-ONLY')
                    form.locator('[name=consent]').check()
                    before_tasks = len(server.collector.list()['runs'])
                    post_click(page, '#collect-start', '/api/collection/create_and_start', 400)
                    expect(page.locator('#collect-check-results')).to_contain_text('还缺什么')
                    expect(form.locator('[name=api_key]')).to_have_value('ARTIFICIAL-MEMORY-ONLY')
                    assert len(server.collector.list()['runs'])==before_tasks
                    form.locator('[name=rights_note]').fill('Artificial local feed acceptance; no real upstream or authorization claim.')
                    page.route('**/api/collection/list',lambda route:route.fulfill(status=400,json={'error':'Artificial history outage'}))
                    feed_calls=[]
                    def feed(url, *, headers=None, **kwargs):
                        feed_calls.append(dict(headers or {}))
                        return {'jobs':[dict(title='时间序列算法工程师', text=capture()['text'], url=capture()['url'], evidence_level='full_text')], 'next_cursor':''}
                    with patch.object(SafeHTTP,'json',side_effect=feed):
                        accepted=post_click(page, '#collect-start', '/api/collection/create_and_start', 200)
                        assert accepted['accepted'] is True and accepted['created'] is True
                        expect(page.locator('#collect-progress')).to_contain_text('completed',timeout=10000)
                        expect(page.locator('#collect-start')).to_be_enabled()
                    assert feed_calls==[{'Authorization':'Bearer ARTIFICIAL-MEMORY-ONLY'}]
                    assert server.collector.status({'id':accepted['task']['id']})['report_id']
                    expect(form.locator('[name=api_key]')).to_have_value('')
                    assert not any(path in {'/api/collection/start','/api/collection/background/start','/api/collection/step'} for path,_ in requests)
                    result['checks'].append('combined preflight rejects without creating a task or clearing the token; accepted feed completes and reports despite a history-read outage, with no UI start/step chain')
                    page.unroute('**/api/collection/list')
                    result['phase'] = 'created_but_start_unconfirmed'
                    form.locator('[name=api_key]').fill('ARTIFICIAL-MEMORY-ONLY')
                    with patch.object(server.collection_runner,'start',side_effect=RuntimeError('Artificial lost acceptance')):
                        uncertain=post_click(page, '#collect-start', '/api/collection/create_and_start', 200)
                    assert uncertain['accepted'] is None and uncertain['created'] is True
                    expect(page.locator('#collect-start')).to_be_disabled()
                    expect(page.locator('#collect-json')).to_contain_text(uncertain['task']['id'])
                    starts=execution_count()
                    page.locator('#collection-reload').click()
                    expect(page.locator('#collect-start')).to_be_enabled()
                    assert execution_count()==starts and server.collector.status({'id':uncertain['task']['id']})['feed_requests']==0
                    page.route('**/api/collection/create_and_start',lambda route:route.fulfill(status=500,json={'error':'Artificial transport-side server error'}))
                    form.locator('[name=api_key]').fill('ARTIFICIAL-MEMORY-ONLY')
                    post_click(page, '#collect-start', '/api/collection/create_and_start', 500)
                    expect(page.locator('#collect-start')).to_be_disabled()
                    expect(page.locator('#collection-state')).to_contain_text('结果未知')
                    result['checks'].append('created-but-unconfirmed and JSON server-error receipts block duplicate execution; explicit recovery only observes the original task and never replays it')
                    page.close()

                if args.case in {'all','category-history'}:
                    verify_category_history_boundaries(browser, result)

                assert not result['page_errors'] and not result['external_requests']
                result['success']=True;result['phase']='completed'
                browser.close();browser=None
        finally:
            if browser is not None:
                try: browser.close()
                except Exception: pass
            server.shutdown();server.server_close();worker.join(timeout=5)
            (args.out/'results.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(result,ensure_ascii=True,indent=2))
    return 0


if __name__=='__main__':
    raise SystemExit(main())
