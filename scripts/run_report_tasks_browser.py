"""Narrow real-browser coverage of application-owned reports and queue read races."""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT/'src'), str(ROOT/'tests')]
from vibe_job_radar.local_public import LocalPublicDataClient
from vibe_job_radar.network import SafeHTTP
from vibe_job_radar.workbench import LocalServer
from vibe_job_radar.workspace import Workspace
from test_workbench import capture


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--channel', choices=['msedge'])
    parser.add_argument('--case', choices=['all', 'pageshow', 'deep-link', 'result-return'], default='all')
    parser.add_argument('--out', type=Path, default=ROOT/'browser-acceptance/report-tasks')
    args = parser.parse_args()
    from playwright.sync_api import sync_playwright, expect
    args.out.mkdir(parents=True, exist_ok=True)
    result = {'success':False, 'checks':[], 'page_errors':[], 'external_requests':[],
              'stage':'setup', 'scope':'Actual local browser, application report worker, original reports and queued-query controls; temporary authored inputs only.'}
    clean = {k:v for k,v in os.environ.items() if not k.startswith('VIBE_RADAR_')}
    gates = []
    requests = []
    with tempfile.TemporaryDirectory(prefix='radar-report-ui-') as tmp, patch.dict(os.environ,clean,clear=True), \
            patch('urllib.request.getproxies',return_value={}):
        workspace = Workspace(tmp)
        workspace.add_job(capture())
        wire = Mock()
        wire.json.side_effect = AssertionError('Queue verification must not query upstream')
        server = LocalServer(workspace, public_client=LocalPublicDataClient(workspace,transport=wire))
        server.public_queue.start = lambda: None
        thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval':.01}, daemon=True)
        thread.start()
        browser = None
        try:
            with patch.object(SafeHTTP,'request',side_effect=AssertionError('Unexpected external request')), sync_playwright() as pw:
                browser = pw.chromium.launch(headless=True, **({'channel':args.channel} if args.channel else {}))
                result['browser_version'] = browser.version
                context = browser.new_context(viewport={'width':1280,'height':1000})
                def local_only(route):
                    if route.request.url.startswith(server.origin+'/'): route.continue_()
                    else: result['external_requests'].append('unexpected_nonlocal');route.abort()
                context.route('**/*',local_only)
                context.on('request',lambda request:requests.append((request.method,request.url.removeprefix(server.origin))))
                def new_page(path=None):
                    page = context.new_page()
                    page.on('pageerror',lambda exc:result['page_errors'].append(str(exc)))
                    page.goto(server.entry_url)
                    expect(page.locator('#analyze')).to_be_visible()
                    if path: page.goto(server.origin+path)
                    return page
                def await_condition(page, predicate, message, timeout=8):
                    deadline = time.monotonic()+timeout
                    while not predicate() and time.monotonic()<deadline: page.wait_for_timeout(20)
                    assert predicate(), message
                def controlled(original):
                    entered, release = threading.Event(), threading.Event()
                    gates.append(release)
                    def invoke(*args, **kwargs):
                        entered.set()
                        if not release.wait(10): raise AssertionError('Test gate was not released in time')
                        return original(*args, **kwargs)
                    return entered, release, invoke

                def check_pageshow():
                    result['stage'] = 'persisted_page_restores_observation'
                    page = new_page()
                    expect(page.locator('#report-task-refresh')).to_be_enabled()
                    before_starts = sum(path == '/api/reports/start' for _,path in requests)
                    held_reads = []
                    def hold_read(route):
                        if not held_reads: held_reads.append(route)
                        else: route.continue_()
                    page.route('**/api/reports/state',hold_read)
                    page.locator('#report-task-refresh').click()
                    await_condition(page,lambda:bool(held_reads),'Report read was not held')
                    expect(page.locator('#report-task-refresh')).to_be_disabled()
                    page.evaluate("window.dispatchEvent(new PageTransitionEvent('pagehide',{persisted:true})); window.dispatchEvent(new PageTransitionEvent('pageshow',{persisted:true}));")
                    expect(page.locator('#report-task-refresh')).to_be_enabled()
                    held_reads[0].fulfill(json={'busy':True,'owned_elsewhere':False,'task':{
                        'id':'a'*32,'kind':'analysis','status':'running','report_id':None}})
                    page.wait_for_timeout(150)
                    expect(page.locator('#report-task-refresh')).to_be_enabled()
                    assert 'aaaaaaaa' not in page.locator('#report-task-status').inner_text()
                    assert sum(path == '/api/reports/start' for _,path in requests) == before_starts
                    page.unroute('**/api/reports/state',hold_read)

                    result['stage'] = 'persisted_pending_start_and_status_failure'
                    held_starts = []
                    def hold_start(route):
                        response = route.fetch()
                        assert response.status == 200
                        held_starts.append((route,response.json()))
                    page.route('**/api/reports/start',hold_start)
                    page.route('**/api/status',lambda route:route.fulfill(status=400,json={'error':'Artificial unavailable source state'}))
                    entered, release, invoke = controlled(workspace.analyze)
                    with patch.object(workspace,'analyze',side_effect=invoke) as analyze:
                        page.locator('#analyze').click()
                        await_condition(page,lambda:bool(held_starts),'Start receipt was not held')
                        await_condition(page,entered.is_set,'Restoration worker was not entered')
                        task_id = held_starts[0][1]['task']['id']
                        page.evaluate("window.dispatchEvent(new PageTransitionEvent('pagehide',{persisted:true})); window.dispatchEvent(new PageTransitionEvent('pageshow',{persisted:true}));")
                        expect(page.locator('#report-task-status')).to_contain_text(task_id[:8])
                        expect(page.locator('#analyze')).to_be_disabled()
                        release.set()
                        expect(page.locator('#report-task-status')).to_contain_text('已完成',timeout=10000)
                        # A valid stored result remains useful even though /api/status fails.
                        expect(page.locator('#report')).to_be_visible()
                        expect(page.locator('#notice')).to_contain_text('报告已打开；当前岗位数据状态无法刷新')
                        report_id = server.report_tasks.state()['task']['report_id']
                        expect(page.locator('#report-title')).to_contain_text(report_id[:8])
                        expect(page.locator('#analyze')).to_be_disabled()
                        # The pre-hide running receipt must not replace this completed state.
                        held_starts[0][0].fulfill(json=held_starts[0][1])
                        expect(page.locator('#analyze')).to_be_enabled()
                        expect(page.locator('#report-task-status')).to_contain_text('已完成')
                        expect(page.locator('#report-title')).to_contain_text(report_id[:8])
                        assert page.url.endswith('#report='+report_id)
                        assert analyze.call_count == 1
                        assert sum(path == '/api/reports/start' for _,path in requests) == before_starts+1
                    page.close()
                    result['checks'].append('simulated persisted pagehide/pageshow in actual browser restores an in-flight read without submitting, ignores stale read/start receipts, and displays stored report despite unrelated source-status failure')

                def check_deep_link():
                    result['stage'] = 'historical_deep_link_without_source_state'
                    report_id = workspace.analyze({'dataset':'real'})['id']
                    page = new_page()
                    before_starts = sum(path == '/api/reports/start' for _,path in requests)
                    page.route('**/api/status',lambda route:route.fulfill(status=400,json={'error':'Artificial unavailable source state'}))
                    page.goto(server.origin+'/#report='+report_id)
                    page.reload()  # Hash-only navigation otherwise retains the previous document.
                    expect(page.locator('#report')).to_be_visible()
                    expect(page.locator('#report-title')).to_contain_text(report_id[:8])
                    expect(page.locator('#counts')).to_contain_text('当前岗位数据状态无法刷新')
                    expect(page.locator('#files button')).not_to_have_count(0)
                    assert page.url.endswith('#report='+report_id)
                    assert sum(path == '/api/reports/start' for _,path in requests) == before_starts
                    page.close()
                    result['checks'].append('historical report deep link opens original saved report and downloads despite source-status failure, without generating another report')

                def check_result_return():
                    result['stage'] = 'persisted_page_with_inflight_report_read'
                    page = new_page()
                    expect(page.locator('#report-task-refresh')).to_be_enabled()
                    expect(page.locator('#analyze')).to_be_enabled()
                    before_starts = sum(path == '/api/reports/start' for _,path in requests)
                    held_reports = []
                    def hold_report(route):
                        if not held_reports:
                            response = route.fetch()
                            assert response.status == 200
                            held_reports.append((route,response.json()))
                        else: route.continue_()
                    page.route('**/api/report/*',hold_report)
                    page.locator('#analyze').click()
                    await_condition(page,lambda:bool(held_reports),'Result read was not held')
                    report_id = server.report_tasks.state()['task']['report_id']
                    expect(page.locator('#report-task-open')).to_be_disabled()
                    page.evaluate("window.dispatchEvent(new PageTransitionEvent('pagehide',{persisted:true})); window.dispatchEvent(new PageTransitionEvent('pageshow',{persisted:true}));")
                    expect(page.locator('#report-title')).to_contain_text(report_id[:8])
                    expect(page.locator('#report-task-open')).to_be_enabled()
                    expect(page.locator('#report-task-refresh')).to_be_enabled()
                    held_reports[0][0].fulfill(json=held_reports[0][1])
                    page.wait_for_timeout(150)
                    expect(page.locator('#report-title')).to_contain_text(report_id[:8])
                    expect(page.locator('#report-task-open')).to_be_enabled()
                    assert sum(path == '/api/reports/start' for _,path in requests) == before_starts+1
                    page.close()
                    result['checks'].append('restoration invalidates the old in-flight report opening and independently displays current result before stale response arrives, without another start')

                if args.case == 'all':
                    result['stage'] = 'analysis_survives_page_close'
                    page = new_page()
                    expect(page.locator('#report-task-status')).to_contain_text('尚无后台')
                    entered, release, invoke = controlled(workspace.analyze)
                    with patch.object(workspace,'analyze',side_effect=invoke) as analyze:
                        with page.expect_response(lambda response:response.url.endswith('/api/reports/start')) as accepted:
                            page.locator('#analyze').click()
                        assert accepted.value.status == 200
                        task_id = accepted.value.json()['task']['id']
                        await_condition(page,entered.is_set,'Analysis worker was not entered')
                        expect(page.locator('#analyze')).to_be_disabled()
                        page.close()
                        page = new_page()
                        expect(page.locator('#report-task-status')).to_contain_text(task_id[:8])
                        expect(page.locator('#analyze')).to_be_disabled()
                        release.set()
                        expect(page.locator('#report')).to_be_visible(timeout=10000)
                        expect(page.locator('#report-task-status')).to_contain_text('已完成')
                        assert analyze.call_count == 1
                        receipt = server.report_tasks.state()
                        source_id = receipt['task']['report_id']
                        assert receipt['task']['id'] == task_id and receipt['task']['status'] == 'succeeded'
                        assert page.url.endswith('#report='+source_id)
                        assert len([r for r in requests if r == ('POST','/api/reports/start')]) == 1
                        assert not any(path == '/api/analyze' for _,path in requests)
                    result['checks'].append('one analysis acceptance survives page close; new page observes same task and original report, no replay or old synchronous endpoint')

                    result['stage'] = 'pinned_report_is_not_replaced'
                    pinned = new_page('/#report='+source_id)
                    expect(pinned.locator('#report-title')).to_contain_text(source_id[:8])
                    page.goto(server.origin+'/')
                    expect(page.locator('#demo')).to_be_enabled()
                    with page.expect_response(lambda response:response.url.endswith('/api/reports/start')) as accepted:
                        page.locator('#demo').click()
                    assert accepted.value.status == 200
                    expect(page.locator('#report-title')).to_contain_text('合成演示',timeout=10000)
                    demo_id = server.report_tasks.state()['task']['report_id']
                    pinned.locator('#report-task-refresh').click()
                    expect(pinned.locator('#report-task-status')).to_contain_text('已完成')
                    assert pinned.url.endswith('#report='+source_id)
                    expect(pinned.locator('#report-title')).to_contain_text(source_id[:8])
                    pinned.locator('#report-task-open').click()
                    expect(pinned.locator('#report-title')).to_contain_text(demo_id[:8])
                    result['checks'].append('observing another completed task preserves a pinned historical report until explicit open')

                    result['stage'] = 'personal_report_survives_page_close'
                    advanced = new_page('/advanced')
                    expect(advanced.locator('#generate')).to_be_enabled()
                    advanced.locator('#source-run').select_option(source_id)
                    advanced.locator('#load-requirements').click()
                    expect(advanced.locator('#requirement-list .card')).not_to_have_count(0)
                    entered, release, invoke = controlled(server.evidence.generate)
                    with patch.object(server.evidence,'generate',side_effect=invoke) as generate:
                        with advanced.expect_response(lambda response:response.url.endswith('/api/reports/start')) as accepted:
                            advanced.locator('#generate').click()
                        assert accepted.value.status == 200
                        evidence_task = accepted.value.json()['task']['id']
                        await_condition(advanced,entered.is_set,'Personal report worker was not entered')
                        advanced.close()
                        advanced = new_page('/advanced')
                        expect(advanced.locator('#report-task-status')).to_contain_text(evidence_task[:8])
                        expect(advanced.locator('#generate')).to_be_disabled()
                        # Observation during a report must not fail advanced initialization.
                        expect(advanced.locator('#collection-controls')).to_be_enabled()
                        release.set()
                        expect(advanced.locator('#generated-summary')).to_contain_text('个人报告',timeout=10000)
                        expect(advanced.locator('#generated-coverage table')).to_be_visible()
                        advanced.get_by_text('精确映射状态（预览前 200 行）',exact=True).click()
                        expect(advanced.locator('#generated-matrix table')).to_be_visible()
                        assert generate.call_count == 1
                        personal_id = server.report_tasks.state()['task']['report_id']
                        report = workspace.report(personal_id)
                        assert 'coverage' in report and 'matrix' in report
                        assert not any(path == '/api/evidence/generate' for _,path in requests)
                    result['checks'].append('one personal report survives page close; advanced initialization observes during work; reopened result retains coverage and matrix')

                    result['stage'] = 'evidence_revision_not_silently_refreshed'
                    advanced.locator('#source-run').select_option(source_id)
                    row = server.evidence.catalogue({'run_id':source_id})['rows'][0]
                    server.evidence.review({'run_id':source_id,'requirement_id':row['requirement_id'],
                        'decision':'pending','reviewer':'Artificial browser fixture', 'reason':'Explicit version conflict test',
                        'expected_revision':0})
                    with advanced.expect_response(lambda response:response.url.endswith('/api/reports/start')) as rejected:
                        advanced.locator('#generate').click()
                    assert rejected.value.status == 400
                    expect(advanced.locator('#notice')).to_contain_text('版本已变化')
                    assert server.report_tasks.state()['task']['id'] == evidence_task
                    result['checks'].append('stale personal revision is rejected before task admission; reopening a result does not silently refresh editing revision')

                    result['stage'] = 'queue_late_read_success_and_error'
                    queue = new_page()
                    expect(queue.locator('#queue-add')).to_be_enabled()
                    queue.locator('#public-queue > summary').click()
                    for index, failure in enumerate((False, True), start=1):
                        held = []
                        old = server.public_queue.state()
                        def delayed(route):
                            if not held: held.append(route)
                            else: route.continue_()
                        queue.route('**/api/public/queue/state',delayed)
                        await_condition(queue,lambda:bool(held),'Queue poll was not held',timeout=8)
                        queue.locator('#public-search [name=query]').fill('Architect' if index == 1 else 'Engineer')
                        queue.locator('#queue-add-consent').check()
                        with queue.expect_response(lambda response:response.url.endswith('/queue/enqueue')) as accepted:
                            queue.locator('#queue-add').click()
                        assert accepted.value.status == 200
                        expect(queue.locator('#queue-items li')).to_have_count(index)
                        expect(queue.locator('#queue-controls')).to_have_js_property('disabled',False)
                        queue.locator('#queue-add-consent').check()
                        if failure: held[0].fulfill(status=500,json={'error':'Artificial old read failure'})
                        else: held[0].fulfill(json=old)
                        queue.wait_for_timeout(150)
                        expect(queue.locator('#queue-items li')).to_have_count(index)
                        expect(queue.locator('#queue-controls')).to_have_js_property('disabled',False)
                        expect(queue.locator('#queue-add-consent')).to_be_checked()
                        assert 'Artificial old read failure' not in queue.locator('#queue-status').inner_text()
                        queue.unroute('**/api/public/queue/state',delayed)
                        result['checks'].append('late pre-mutation queue '+('failure' if failure else 'success')+' cannot replace accepted state or revoke newer consent')
                if args.case in ('all', 'pageshow'): check_pageshow()
                if args.case in ('all', 'deep-link'): check_deep_link()
                if args.case in ('all', 'result-return'): check_result_return()
                assert wire.json.call_count == 0
                assert not result['page_errors'] and not result['external_requests']
                result.update(success=True,stage='verified',report_task_starts=sum(path == '/api/reports/start' for _,path in requests),
                              upstream_requests=wire.json.call_count)
                context.close()
        finally:
            for gate in gates: gate.set()
            # sync_playwright has already closed its browser/driver here.
            try:
                server.shutdown();server.server_close();thread.join(5)
            finally:
                (args.out/'results.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(result,ensure_ascii=True,indent=2))


if __name__ == '__main__':main()
