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


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--channel', choices=['msedge'])
    parser.add_argument('--case', choices=['all','draft','areas','collection','linked-recovery'], default='all')
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
