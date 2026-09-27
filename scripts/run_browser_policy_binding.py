"""Real Playwright callback -> production bridge -> local TLS/DoH -> batch report.

All upstream pages/answers are artificial. Reuse the existing test-only public-IP
mapping and certificate context; no production URL/route/verification exceptions.
This closes the test gap between direct transport tests and fixture-only browsers.
"""
from __future__ import annotations

import argparse
import json
from contextlib import ExitStack
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT/'src'), str(ROOT/'tests')]

from test_encrypted_dns_transport import EncryptedRoundTripTests
from test_encrypted_dns import HOST
from vibe_job_radar.guided.adapters import DOMAdapter, Registry
from vibe_job_radar.guided.browser import PlaywrightBackend
from vibe_job_radar.guided.rate import RateLedger, Limits
from vibe_job_radar.guided.service import GuidedService
from vibe_job_radar.network_policy import current_policy
from vibe_job_radar.network import USER_AGENT
from vibe_job_radar.network_settings import save as save_settings
from vibe_job_radar.workspace import Workspace


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--channel', choices=['msedge'])
    args = parser.parse_args()
    out = ROOT/'browser-acceptance'/'policy-binding'; out.mkdir(parents=True, exist_ok=True)
    result = {'success':False, 'checks':[], 'callback_contexts':[], 'page_errors':[],
              'header_checks': [], 'stylesheet_rendered': False,
              'scope':'Real collector and callback dispatch, production TLS bridge and report; local artificial DoH/pages only. Not user-network or site certification.'}
    fixture = EncryptedRoundTripTests(); fixture.setUp()
    original_get = fixture.origin.RequestHandlerClass.do_GET
    observed = result['callback_contexts']

    def get(handler):
        # Inspect serialized HTTP fields, not only the Python input dictionary.
        agents = handler.headers.get_all('User-Agent', [])
        encodings = handler.headers.get_all('Accept-Encoding', [])
        result['header_checks'].append({
            'path': urlsplit(handler.path).path, 'agent_count': len(agents),
            'crawler_identified': len(agents) == 1 and USER_AGENT in agents[0].split(),
            'identity_once': encodings == ['identity'],
        })
        fixture.targets.append((handler.path, dict(handler.headers)))
        path = urlsplit(handler.path).path
        if path == '/robots.txt':
            content = 'User-agent: *\nAllow: /\n'; mime = 'text/plain'
        elif path == '/search':
            content = '<!doctype html><meta charset="utf-8"><link rel="stylesheet" href="/fixture.css"><h1>人工测试岗位列表</h1><div id="jobs"></div><script src="/fixture.js"></script>'
            mime = 'text/html; charset=utf-8'
        elif path == '/fixture.css':
            content = '#jobs { --radar-fixture-loaded: yes; padding-left: 17px; }'
            mime = 'text/css'
        elif path == '/fixture.js':
            content = "fetch('/fixture-list.json').then(r=>r.json()).then(j=>{let a=document.createElement('a');a.href=j.url;a.textContent=j.title;document.querySelector('#jobs').append(a);})"
            mime = 'application/javascript'
        elif path == '/fixture-list.json':
            content = json.dumps({'url':'/job/1','title':'时间序列算法工程师'}, ensure_ascii=False)
            mime = 'application/json'
        elif path == '/job/1':
            content = '<!doctype html><meta charset="utf-8"><h1>时间序列算法工程师</h1><div class="job-description">要求熟练使用 Cursor 进行 AI 辅助编程，编写单元测试与代码审查，负责时间序列预测系统。此页面仅为人工验收材料，不是真实招聘信息。</div>'
            mime = 'text/html; charset=utf-8'
        else:
            content = ''; mime = 'text/plain'
        raw = content.encode('utf-8')
        handler.send_response(200); handler.send_header('Content-Type',mime)
        handler.send_header('Content-Length',str(len(raw))); handler.end_headers(); handler.wfile.write(raw)

    fixture.origin.RequestHandlerClass.do_GET = get
    class ObservedBackend(PlaywrightBackend):
        def __init__(self,*a,**kw):
            super().__init__(*a,**kw)
            self.page.on('pageerror', lambda exc: result['page_errors'].append(type(exc).__name__))
        def snapshot(self):
            snapshot = super().snapshot()
            if urlsplit(snapshot.url).path == '/search':
                value = self.page.locator('#jobs').evaluate(
                    "el => getComputedStyle(el).getPropertyValue('--radar-fixture-loaded').trim()")
                assert value == 'yes', 'stylesheet did not render through the production bridge'
                result['stylesheet_rendered'] = True
            return snapshot
        def _route(self,route):
            # Observation only: do NOT set/reset context or patch the dispatcher.
            bound = self.wire.network_policy
            observed.append({'kind':route.request.resource_type,
                'ambient_encrypted_dns':current_policy().encrypted_dns,
                'bound_encrypted_dns':bound.encrypted_dns if bound else None,
                'policy_id':bound.fingerprint if bound else None})
            super()._route(route)

    def wait(service):
        deadline = time.monotonic()+25
        while service.state()['busy']:
            if time.monotonic() > deadline: raise TimeoutError('controlled task did not finish')
            time.sleep(.05)
        return service.state()['jobs'][0]

    options = {'headless':True}
    if args.channel: options['channel'] = args.channel
    elif os.environ.get('RADAR_TEST_CHROMIUM'): options['executable_path'] = os.environ['RADAR_TEST_CHROMIUM']
    adapter = DOMAdapter('fixture','人工策略验收',(HOST,),f'https://{HOST}/search','q',
        r'^/job/[0-9]+$', f'https://{HOST}/login', (HOST,))
    try:
        with tempfile.TemporaryDirectory() as folder, ExitStack() as cleanup:
            root = Path(folder)
            def make(name,consent):
                workspace = Workspace(root/name)
                if consent:save_settings(workspace,{'mode':'fake_ip_doh','revision':0,'consent':True})
                ledger = RateLedger(workspace.root/'fixture-rates.sqlite',Limits(page_interval=0,request_interval=0))
                service = GuidedService(workspace, registry=Registry([adapter]), ledger=ledger,
                    backend_factory=lambda a,l,c,p:ObservedBackend(a,l,c,p,**options))
                cleanup.callback(service.close)
                return workspace,service
            workspace, service = make('consented',True)
            query = {'platform':'fixture','keyword':'时间序列算法工程师','roles':['time_series'],
                     'max_pages':1,'max_jobs':1,'consent':True,'diagnostics':True,'rights_note':'人工上游，仅验证本地流程；非平台许可。'}

            def exercise():
                diagnostic = service.diagnose({'platform':'fixture'})
                assert diagnostic['host'] == HOST and diagnostic['code'] == 'effective_dns_ok'
                assert diagnostic['passed'] and len(fixture.posts) == 2
                expected = diagnostic['policy']['policy_id']
                result['diagnostic_policy_id'] = expected
                result['checks'].append('same-host workspace diagnostic genuinely resolves synthetic Fake-IP over verified local DoH TLS')
                service.create(query); task = wait(service)
                assert task['status']=='ready', {'code':task['code'],'message':task['message']}
                assert len(task['cards'])==1
                assert {r['kind'] for r in observed} >= {'document','stylesheet','script','fetch'}
                assert any(r['ambient_encrypted_dns'] is False for r in observed)
                assert all(r['bound_encrypted_dns'] is True and r['policy_id']==expected for r in observed)
                assert len(fixture.posts)==2  # Same workspace resolver cache, not a second resolver.
                paths = {urlsplit(p).path for p,_ in fixture.targets}
                assert {'/robots.txt','/search','/fixture.css','/fixture.js','/fixture-list.json'} <= paths
                result['checks'].append('actual Playwright route callbacks lose ambient consent but retain the owner-bound policy and shared resolver for robots, page, script and fetch')
                service.action({'id':task['id'],'action':'collect','selected':[task['cards'][0]['id']]})
                task = wait(service)
                assert task['status']=='completed' and task['outcome']['saved']==1
                report = workspace.report(task['report_id'])
                assert report['manifest']['stats']['full_text_job_groups']==1
                assert report['manifest']['research_brief']['status']=='research_ready'
                result['checks'].append('selected detail crosses the same production HTTPS bridge and becomes an original full-text research report')
                trace = service.diagnostics({'id':task['id']})
                stages = {event['stage'] for event in trace['events']}
                assert {'route','http_request','robots','list_parse','detail_parse','persist','report'} <= stages
                assert trace['trace_id'] == task['id'] and trace['observer_errors'] == 0
                assert '时间序列算法工程师' not in json.dumps(trace,ensure_ascii=False)
                result['checks'].append('opt-in D01 trace binds across the real callback context through transport, parser, store and report without retaining query or JD text')
                (out/'acquisition-diagnostic.json').write_text(json.dumps(trace,ensure_ascii=False,indent=2),encoding='utf-8')
                # Revoke on the original workspace; old bound object must NOT override it.
                save_settings(workspace,{'mode':'system','revision':1,'consent':False})
                before = (len(fixture.posts),len(fixture.targets))
                service.action({'id':task['id'],'action':'search'}); denied = wait(service)
                assert denied['code']=='encrypted_dns_disabled', denied['code']
                assert before==(len(fixture.posts),len(fixture.targets))
                assert workspace.report(task['report_id'])['id']==task['report_id']
                result['checks'].append('revoked permission stops an existing bound session before any further DoH/target exchange and preserves the earlier report')
                service.action({'id':task['id'],'action':'stop'});wait(service)
                _, other = make('not-consented',False)
                before = (len(fixture.posts),len(fixture.targets))
                other.create(query); denied = wait(other)
                assert denied['code']=='non_public_address',denied['code']
                assert before==(len(fixture.posts),len(fixture.targets))
                result['checks'].append('separate non-consenting workspace never borrows consent, cache or network settings from the successful workspace')
                assert set(fixture.sni)=={'cloudflare-dns.com',HOST}
                assert result['stylesheet_rendered']
                assert result['header_checks']
                assert all(r['agent_count'] == 1 and r['crawler_identified'] and r['identity_once']
                           for r in result['header_checks']), result['header_checks']
                result['checks'].append('robots, CSS, document, script, data and detail each arrive with one explicit crawler User-Agent and one identity encoding; stylesheet actually renders')
                assert not result['page_errors']
                result['tls_requests']={'doh_posts':len(fixture.posts),'target_requests':len(fixture.targets)}
                result['success']=True
            fixture.perform(exercise)
    finally:
        fixture.origin.RequestHandlerClass.do_GET = original_get
        fixture.tearDown()
        (out/'results.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(result,ensure_ascii=True,indent=2))

if __name__=='__main__':main()
