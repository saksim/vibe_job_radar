"""Explicit, local-only CORS search -> actual Liepin adapter -> report test.

Four artificial HTTPS hosts use a fresh isolated test CA and the existing
native tunnel. No real platform traffic, credentials or request replay.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack
from dataclasses import replace
import http.server
import gzip
import json
from pathlib import Path
import socket
import ssl
import tempfile
import threading
import time
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from run_native_browser_acceptance import (ROOT, HOST, URL, NativeBackend, RateLedger,
    Limits, GuidedService, Registry, Store, NetworkPolicy, use_policy, Workspace,
    trust_fixture, RECORDED_BODY, RECORDED_TITLE, recorded_markup, recorded_posting)
from vibe_job_radar.guided.adapters import builtins
from vibe_job_radar.guided.contracts import CrawlError
from vibe_job_radar.guided.native_policy import contract_for, NativeRule

API_HOST = 'api.' + HOST
CDN_HOST = 'static.' + HOST
LOGIN_HOST = 'login.' + HOST
OPTIONAL_HOST = 'optional.' + HOST
PATH = '/api/com.liepin.searchfront4c.pc-search-job'
ASSET = '/fe-www-pc/v6/js/search-fixture.js'


class SearchFixture:
    def __init__(self, root):
        self.requests = []
        self.deny_cors = False
        owner = self
        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'
            def log_message(self, *_): pass
            def send(self, content, mime='text/html; charset=utf-8', status=200, extra=()):
                raw = content.encode('utf-8')
                compressed = mime.startswith('text/html')
                if compressed: raw = gzip.compress(raw)
                self.send_response(status)
                self.send_header('Content-Type', mime)
                self.send_header('Content-Length', str(len(raw)))
                if compressed: self.send_header('Content-Encoding', 'gzip')
                for key, value in extra: self.send_header(key, value)
                if mime.startswith('text/html'):
                    # The publisher permits popups; the added restriction must
                    # independently prohibit them and retain this policy.
                    self.send_header('Content-Security-Policy',
                        "object-src 'none'; sandbox allow-scripts allow-same-origin allow-forms allow-popups")
                self.send_header('Access-Control-Allow-Origin', URL)
                self.send_header('Access-Control-Allow-Methods', 'POST')
                self.send_header('Access-Control-Allow-Headers', 'content-type,x-client-type')
                self.end_headers()
                try: self.wfile.write(raw)
                except (OSError, ssl.SSLError): pass
            def record(self):
                owner.requests.append({'method': self.command, 'host': self.headers.get('Host'),
                    'path': urlsplit(self.path).path, 'anonymous': not self.headers.get('Cookie'),
                    'no_credentials': not self.headers.get('Authorization') and not self.headers.get('Proxy-Authorization'),
                    'identified': 'VibeJobRadar/0.1' in self.headers.get('User-Agent', '')})
            def do_GET(self):
                self.record(); path = urlsplit(self.path).path
                if path == '/robots.txt':
                    if self.headers.get('Host') in (API_HOST, LOGIN_HOST):
                        # Match the observed missing API robots file. HTML error
                        # content must remain inert while its status is read.
                        self.send('<script>fetch("/robots-error-must-not-run")</script>'
                                  '<img src="/robots-error-must-not-run">Not Found', status=404)
                    else:
                        self.send('User-agent: *\nAllow: /\n', 'text/plain')
                elif path == '/zhaopin/':
                    # Intentionally no anchors: only the browser response can
                    # produce the candidate; a DOM-only implementation fails.
                    early = ('<script>window.initialPopupBlocked = '
                             '(window.open("/apply") === null);</script>'
                             if parse_qs(urlsplit(self.path).query).get('key') == ['窗口隔离'] else '')
                    self.send('<!doctype html><meta charset="utf-8">' + early + '<h1>合成搜索页</h1>'
                              '<iframe id="common-footer" src="https://' + CDN_HOST + '/footer"></iframe>'
                              '<div id="loaded"></div><script src="https://' + CDN_HOST + ASSET + '"></script>')
                elif path == ASSET:
                    self.send("""window.optionalBlocked = 0;
for (const path of ['/api/com.liepin.cbp.baizhong.op.v2-show-4pc',
 '/statisticPlatform/standardFLog.json', '/statisticPlatform/standardTLog.json']) {
 fetch('https://""" + OPTIONAL_HOST + """' + path, {method:'POST',
 headers:{'Content-Type':'application/json'},body:'{}'}).catch(() => window.optionalBlocked++);
}
const key = new URL(location.href).searchParams.get('key') || '';
fetch('https://""" + API_HOST + PATH + """', {
 method:'POST', headers:{'Content-Type':'application/json','X-Client-Type':'web'},
 body: JSON.stringify({data:{mainSearchPcConditionForm:{key,currentPage:0,pageSize:40}}})
}).then(r => r.json()).then(j => {
 document.querySelector('#loaded').textContent='response received';
 if (!key) {
  const a=document.createElement('a');a.href=j.data.data.jobCardList[0].job.link;
  a.textContent='默认推荐';a.setAttribute('data-fixture-recommendation','true');document.body.append(a);
 }
});""", 'application/javascript')
                elif path == '/fixture-login':
                    self.send('<h1>人工登录</h1><form method="post" action="/fixture-login">'
                              '<button>人工确认</button></form>')
                elif path == '/job/123.shtml':
                    self.send(recorded_markup(recorded_posting(url=URL + path)))
                else: self.send('unknown', status=404)
            def do_OPTIONS(self):
                self.record()
                if urlsplit(self.path).path != PATH: self.send('unknown', status=405)
                else: self.send('', status=403 if owner.deny_cors else 204)
            def do_POST(self):
                self.record()
                if self.path == '/fixture-login':
                    self.rfile.read(int(self.headers.get('Content-Length', '0')))
                    self.send('<h1>人工登录完成</h1>', extra=[
                        ('Set-Cookie', 'local_login_fixture=valid; HttpOnly; Secure; SameSite=Lax')])
                    return
                if self.path != PATH: self.send('unknown', status=405); return
                raw = self.rfile.read(int(self.headers.get('Content-Length', '0')))
                form = json.loads(raw)['data']['mainSearchPcConditionForm']
                jobs = [] if form['key'] == '明确无结果' else [
                    {'job': {'jobId': 'internal-not-url', 'title': RECORDED_TITLE, 'link': URL + '/job/123.shtml'}}]
                self.send(json.dumps({'flag':1,'data':{'data':{'jobCardList':jobs},
                    'pagination':{'currentPage':0,'pageSize':40}}}), 'application/json')
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(str(root / 'good.pem'))
        class Server(http.server.ThreadingHTTPServer):
            daemon_threads = True
            def get_request(self):
                conn, address = super().get_request(); conn.settimeout(5)
                try: return context.wrap_socket(conn, server_side=True), address
                except Exception: conn.close(); raise
        self.server = Server(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval':.05}, daemon=True)
        self.thread.start()
    def close(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join(timeout=2)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--controlled', action='store_true')
    parser.add_argument('--headed', action='store_true')
    parser.add_argument('--channel', choices=['msedge'])
    parser.add_argument('--executable')
    args = parser.parse_args()
    if not args.controlled:
        print('No requests; use --controlled for the local artificial-source test.'); return
    out = ROOT / 'browser-acceptance/native'; out.mkdir(parents=True, exist_ok=True)
    result = {'success':False,'scope':'Four artificial TLS hosts, actual native backend and Liepin adapter; not live certification.', 'checks':[]}
    services = []; server = None
    try:
        with tempfile.TemporaryDirectory(prefix='radar-search-fixture-') as tmp, ExitStack() as cleanup:
            root = Path(tmp)
            cleanup.callback(lambda: [s.close() for s in services])
            with trust_fixture(root, (API_HOST, CDN_HOST, LOGIN_HOST)):
                server = SearchFixture(root)
                cleanup.callback(server.close)
                template = builtins().get('liepin')
                contract = contract_for(template)
                mapping = {'www.liepin.com':HOST, 'api-c.liepin.com':API_HOST,
                           'concat.lietou-static.com':CDN_HOST, 'image0.lietou-static.com':CDN_HOST,
                           'api-passport.liepin.com':LOGIN_HOST, 'feim.liepin.com':CDN_HOST}
                rules = tuple(replace(r, host=mapping[r.host], cors_origin=URL if r.cors_origin else '') for r in contract.rules)
                local_contract = replace(contract, hosts=(HOST,API_HOST,CDN_HOST,LOGIN_HOST), rules=rules,
                    ignored_rules=tuple(replace(r,host=OPTIONAL_HOST) for r in contract.ignored_rules))
                local = replace(template, domains=(HOST,), resource_domains=(HOST,),
                    search_base=URL+'/zhaopin/', login_url=URL+'/', native_contract=local_contract)
                real_dns, real_dial = socket.getaddrinfo, socket.create_connection
                def dns(host,*a,**kw):
                    if host in local_contract.hosts: return [(socket.AF_INET,socket.SOCK_STREAM,6,'',('93.184.216.34',443))]
                    if host in ('127.0.0.1','localhost','::1'): return real_dns(host,*a,**kw)
                    raise AssertionError('external DNS attempted')
                def dial(address,*a,**kw):
                    if address == ('93.184.216.34',443): return real_dial(server.server.server_address,*a,**kw)
                    if address[0] in ('127.0.0.1','localhost','::1'): return real_dial(address,*a,**kw)
                    raise AssertionError('external connection attempted')
                def factory(a,l,c,p,**saved):
                    with use_policy(NetworkPolicy()):
                        return NativeBackend(a,l,c,p,headless=not args.headed,channel=args.channel,executable_path=args.executable,**saved)
                def wait(service):
                    until=time.monotonic()+45
                    while service.state()['busy']:
                        if time.monotonic()>until: raise TimeoutError('native search did not finish')
                        time.sleep(.05)
                    return service.state()['jobs'][0]
                with patch('socket.getaddrinfo', side_effect=dns), patch('socket.create_connection', side_effect=dial), patch.object(NetworkPolicy,'capture',return_value=NetworkPolicy()):
                    entry = factory(local, RateLedger(root/'entry.sqlite', Limits(page_interval=0,request_interval=0)), threading.Event(), lambda *_: None)
                    try:
                        entry.open(local.search_base)
                        entry.page.locator('a[data-fixture-recommendation]').wait_for(state='visible',timeout=5000)
                        page = entry.snapshot()
                        assert not entry.auth_mode and not entry.error and not entry._halted
                        assert any(o.context.get('entry_bootstrap') for o in page.business)
                        try: local.cards(page)
                        except CrawlError as exc: assert exc.code=='not_job_list', exc.code
                        else: raise AssertionError('default recommendations accepted as a keyword list')
                        assert not local.confirmed_empty(page)
                    finally: entry.close()
                    result['checks'].append('query-free initialization reaches the publisher UI but its API and DOM recommendations are neither keyword results nor confirmed empty search; no login')
                    workspace = Workspace(root/'workspace')
                    service = GuidedService(workspace,registry=Registry([local]),
                        ledger=RateLedger(root/'rate.sqlite', Limits(page_interval=0,request_interval=0)), native_backend_factory=factory)
                    services.append(service)
                    query={'platform':'liepin','keyword':'时间序列','roles':['time_series'],'max_pages':2,'max_jobs':1,
                           'consent':True,'rights_note':'仅合成测试','backend':'native','native_consent':True,'diagnostics':True}
                    service.create(query); task=wait(service)
                    result['first_task_code'] = task['code']
                    if task['status'] != 'ready':
                        result['diagnostics'] = service.diagnostics({'id':task['id']})
                    assert task['status']=='ready' and len(task['cards'])==1, task.get('code')
                    assert any(r['method']=='OPTIONS' and r['host']==API_HOST for r in server.requests), 'preflight not observed at upstream'
                    assert any(r['method']=='POST' and r['host']==API_HOST for r in server.requests)
                    assert any(r['host']==CDN_HOST and r['path']==ASSET for r in server.requests)
                    native = service._backends[task['id']]
                    assert native.native_counts['business']==2, 'POST and preflight must both be accounted'
                    result['checks'].append('native CDN script and cross-origin preflight/search POST supply a candidate without DOM links or login')
                    # All outside DNS/dials above raise; ignored preflights must
                    # terminate locally while the real search/report succeeds.
                    assert not any(r['host']==OPTIONAL_HOST for r in server.requests)
                    assert any(e['code']=='native_optional_request_blocked' and e['impact']=='optional'
                               for e in service.diagnostics({'id':task['id']})['events'])
                    result['checks'].append('known marketing, standardFLog and standardTLog requests are aborted before network access without aborting search or report')
                    assert not any(r['path']=='/robots-error-must-not-run' for r in server.requests)
                    result['checks'].append('API robots 404 is distinguished from refusal; its HTML error body cannot execute scripts or fetch resources')
                    assert not any(r['path']=='/footer' for r in server.requests)
                    result['checks'].append('unsupported embedded footer is blocked before loading without aborting the main search document')
                    # With a two-page budget, the first gather has already
                    # looked for a next button. This source has none. A plain
                    # reread must still use the obtained API response, without
                    # repeating search/login or turning the result into empty.
                    cards_before = task['cards']
                    requests_before = len(server.requests)
                    count_before = dict(native.native_counts)
                    service.action({'id': task['id'], 'action': 'capture'})
                    task = wait(service)
                    assert task['status'] == 'ready' and task['code'] == 'ready', task.get('code')
                    assert task['cards'] == cards_before, 'no-next discarded existing API candidates'
                    assert len(server.requests) == requests_before, 'reread caused an extra source request'
                    assert native.native_counts == count_before, 'reread consumed request/page budget'
                    assert service._backends[task['id']] is native, 'reread replaced the browser session'
                    result['checks'].append('absent next button preserves API-only candidates for reread and collection with zero additional requests')
                    service.action({'id':task['id'],'action':'collect','selected':[task['cards'][0]['id']]}); task=wait(service)
                    assert task['status']=='completed' and task['outcome']['saved']==1,task.get('code')
                    with Store(workspace.db) as store:
                        records=store.records()
                        assert len(records)==1 and records[0].text==RECORDED_BODY and records[0].title==RECORDED_TITLE
                    assert workspace.report(task['report_id'])['manifest']['stats']['full_text_job_groups']==1
                    result['checks'].append('actual Liepin parser, Store and original report preserve the selected full JD')
                    previous_report=task['report_id']
                    service.create({**query,'keyword':'明确无结果'}); empty=wait(service)
                    assert empty['status']=='ready' and empty['code']=='no_matching_jobs' and not empty['cards'],empty.get('code')
                    assert workspace.report(previous_report)
                    result['checks'].append('valid empty response means no matches, not required login; previous report preserved')
                    assert all(r['anonymous'] and r['no_credentials'] and r['identified'] for r in server.requests)
                    assert not any('login' in r['path'] or 'apply' in r['path'] for r in server.requests)
                    result['checks'].append('anonymous read sends no login request or credentials; application identity retained')
                    # Explicit query-order opt-in must complete the same
                    # pipeline with no UI/manual collect action or login request.
                    service.create({**query, 'auto_collect': True})
                    automatic = wait(service)
                    assert automatic['status'] == 'completed', automatic.get('code')
                    assert automatic['selection_source'] == 'query_order'
                    assert automatic['auto_selection_applied'] is True
                    assert automatic['outcome']['saved'] == 1
                    assert len(automatic['selection']) == 1
                    assert automatic['authentication'] == 'not_checked'
                    assert workspace.report(automatic['report_id'])['manifest']['stats']['full_text_job_groups'] == 1
                    assert workspace.report(previous_report)
                    assert not any('login' in r['path'] or 'apply' in r['path'] for r in server.requests)
                    result['checks'].append('one opted-in create action performs native API-only search, bounded selection, complete JD and original report without login or manual Collect')
                    service.create({**query, 'keyword': '明确无结果', 'auto_collect': True})
                    automatic_empty = wait(service)
                    assert automatic_empty['code'] == 'no_matching_jobs'
                    assert not automatic_empty['selection'] and not automatic_empty['report_id']
                    result['checks'].append('automatic mode keeps confirmed empty results empty without selecting stale jobs or creating a report')
                    service.close(); services.clear()
                    # Independent owned thread and fresh native context: a
                    # browser-generated popup must not leak even its first HTTP.
                    b = factory(local, RateLedger(root/'popup.sqlite', Limits(page_interval=0,request_interval=0)), threading.Event(), lambda *_: None)
                    try:
                        b.open(local.search_url('窗口隔离'))
                        assert b.page.evaluate('window.initialPopupBlocked') is True, 'early script opened a popup'
                        for _ in range(5):
                            blocked = b.page.evaluate("""() => {
                                const first = window.open('/apply');
                                const second = window.open('/apply', '_blank', 'noopener');
                                const a = document.createElement('a');
                                a.href='/apply'; a.target='_blank'; document.body.append(a); a.click(); a.remove();
                                const f = document.createElement('form');
                                f.action='/apply'; f.method='POST'; f.target='_blank';
                                document.body.append(f); f.submit(); f.remove();
                                return first === null && second === null;
                            }""")
                            assert blocked, 'renderer allowed an auxiliary window'
                            b.pump()
                        assert not b.page.is_closed(), 'popup refusal destroyed the main job page'
                        assert b.adapter.cards(b.snapshot()), 'search data lost after popup refusal'
                        b.pump()
                        assert len(b.context.pages)==1, 'uncontrolled popup remains'
                        assert not any(r['path']=='/apply' for r in server.requests), 'popup first request escaped'
                    finally: b.close()
                    result['checks'].append('CORS document sandbox blocks initial-script, window.open, noopener, link and form popups before HTTP; main search remains usable')
                    # Same-tab normal forms and native Cookie processing must
                    # still work under the extra policy. These login endpoints
                    # exist ONLY on this artificial server/contract.
                    login_rules = (*local_contract.rules,
                        NativeRule('fixture_login_page', HOST, r'/fixture-login',
                                   resources=('Document',), role='document'),
                        NativeRule('fixture_login_submit', HOST, r'/fixture-login',
                                   methods=('POST',), resources=('Document',),
                                   role='login', authentication=True))
                    login_adapter = replace(local, native_contract=replace(local_contract, rules=login_rules))
                    b = factory(login_adapter, RateLedger(root/'form.sqlite', Limits(page_interval=0,request_interval=0)), threading.Event(), lambda *_: None)
                    try:
                        b.open(URL+'/fixture-login', authentication=True)
                        with b.page.expect_navigation(wait_until='domcontentloaded'):
                            b.page.get_by_role('button', name='人工确认').click()
                        b._check_error()
                        assert b.page.locator('h1').inner_text() == '人工登录完成'
                        cookie = next(c for c in b.context.cookies([URL]) if c['name']=='local_login_fixture')
                        assert cookie['value']=='valid' and cookie['httpOnly'] and cookie['secure']
                        b.open(local.search_url('时间序列'))
                        assert b.adapter.cards(b.snapshot())
                        assert any(r['path']=='/zhaopin/' and not r['anonymous'] for r in server.requests)
                        assert not any(r['path']=='/apply' for r in server.requests)
                    finally: b.close()
                    result['checks'].append('sandboxed gzip documents preserve normal same-tab form POST, HttpOnly Cookie and subsequent API-only search; artificial login only')
                    # A real publisher rejection must prevent the POST rather
                    # than getting replaced by a driver-generated success.
                    before = sum(r['method']=='POST' for r in server.requests)
                    server.deny_cors = True
                    b = factory(local, RateLedger(root/'cors-denied.sqlite', Limits(page_interval=0,request_interval=0)), threading.Event(), lambda *_: None)
                    try:
                        try: b.open(local.search_url('时间序列'))
                        except Exception as exc:
                            assert getattr(exc, 'code', None)=='http_403', getattr(exc, 'code', None)
                        else: raise AssertionError('publisher preflight denial accepted')
                    finally: b.close()
                    assert sum(r['method']=='POST' for r in server.requests)==before
                    result['checks'].append('publisher OPTIONS denial prevents search POST and is not fabricated as success')
                    server.deny_cors = False
                    b = factory(local, RateLedger(root/'blank.sqlite', Limits(page_interval=0,request_interval=0)), threading.Event(), lambda *_: None)
                    try:
                        b.open(local.search_url('时间序列'))
                        assert b.observations()
                        before = len(server.requests)
                        b.page.evaluate("setTimeout(() => location.replace('about:blank'), 0)")
                        b.page.wait_for_url('about:blank',timeout=5000)
                        try: b.snapshot()
                        except Exception as exc:
                            assert getattr(exc,'code',None)=='native_page_cleared',getattr(exc,'code',None)
                        else: raise AssertionError('blank navigation returned stale content')
                        assert not b.observations() and len(server.requests)==before
                    finally: b.close()
                    result['checks'].append('a published main document leaving for blank stops with an explicit cause, discards old results and never retries')
                    result['success']=True
    finally:
        for service in services: service.close()
        result['requests']=server.requests if server else []
        (out/'liepin-search-results.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps(result,ensure_ascii=True))

if __name__ == '__main__': main()
