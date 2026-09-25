"""Explicit, local-only CORS search -> actual Liepin adapter -> report test.

Five artificial HTTPS hosts use a fresh isolated test CA and the existing
native tunnel. No real platform traffic, credentials or request replay.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack, closing
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
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from run_native_browser_acceptance import (ROOT, HOST, URL, NativeBackend, RateLedger,
    Limits, GuidedService, Registry, Store, NetworkPolicy, use_policy, Workspace,
    trust_fixture, RECORDED_BODY, RECORDED_TITLE, recorded_markup, recorded_posting)
from vibe_job_radar.guided.adapters import builtins
from vibe_job_radar.guided.contracts import CrawlError
from vibe_job_radar.guided.login_return import LoginReturnManager
from vibe_job_radar.guided.native_policy import contract_for, NativeRule

API_HOST = 'api.' + HOST
CDN_HOST = 'static.' + HOST
LOGIN_HOST = 'login.' + HOST
REGION_HOST = 'regions.' + HOST
OPTIONAL_HOST = 'optional.' + HOST
PATH = '/api/com.liepin.searchfront4c.pc-search-job'
ASSET = '/fe-www-pc/v6/js/search-fixture.js'
REGION_PATH = '/api/com.liepin.bd.p.v4.get-all-dq'
SUGGEST_PATH = '/api/com.liepin.searchfront4c.pc-search-suggest-list'
HOTWORD_PATH = '/api/com.liepin.searchfront4c.pc-hot-search-word-list'
LOGIN_CONFIG_PATH = '/api/com.liepin.pupa.get-pc-login-scan-config'


class SearchFixture:
    def __init__(self, root):
        self.requests = []
        self.search_shapes = []
        self.deny_cors = False
        self.deny_regions = False
        self.deny_login_config = False
        self.deny_query_documents = True
        self.pacing_burst = 0
        owner = self
        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'
            def log_message(self, *_): pass
            def send(self, content, mime='text/html; charset=utf-8', status=200, extra=()):
                # A 204 ends at its headers. Even gzip of an empty string has
                # bytes, which poison the next response on a reused connection.
                no_body = status in {204, 205}
                raw = b'' if no_body else content.encode('utf-8')
                compressed = mime.startswith('text/html') and not no_body
                if compressed: raw = gzip.compress(raw)
                self.send_response(status)
                self.send_header('Content-Type', mime)
                if status != 204: self.send_header('Content-Length', str(len(raw)))
                if compressed: self.send_header('Content-Encoding', 'gzip')
                for key, value in extra: self.send_header(key, value)
                if mime.startswith('text/html'):
                    # The publisher permits popups; the added restriction must
                    # independently prohibit them and retain this policy.
                    self.send_header('Content-Security-Policy',
                        "object-src 'none'; sandbox allow-scripts allow-same-origin allow-forms allow-popups")
                self.send_header('Access-Control-Allow-Origin', URL)
                self.send_header('Access-Control-Allow-Methods', 'GET, POST')
                self.send_header('Access-Control-Allow-Credentials', 'true')
                self.send_header('Access-Control-Allow-Headers', 'content-type,x-client-type')
                self.end_headers()
                try: self.wfile.write(raw)
                except (OSError, ssl.SSLError): pass
            def record(self):
                owner.requests.append({'method': self.command, 'host': self.headers.get('Host'),
                    'path': urlsplit(self.path).path, 'anonymous': not self.headers.get('Cookie'),
                    'query_keys': sorted(parse_qs(urlsplit(self.path).query, keep_blank_values=True)),
                    'no_credentials': not self.headers.get('Authorization') and not self.headers.get('Proxy-Authorization'),
                    'identified': 'VibeJobRadar/0.1' in self.headers.get('User-Agent', '')})
            def do_GET(self):
                self.record(); path = urlsplit(self.path).path
                if path == '/robots.txt':
                    if self.headers.get('Host') in (API_HOST, LOGIN_HOST, REGION_HOST):
                        # Match the observed missing API robots file. HTML error
                        # content must remain inert while its status is read.
                        self.send('<script>fetch("/robots-error-must-not-run")</script>'
                                  '<img src="/robots-error-must-not-run">Not Found', status=404)
                    else:
                        self.send('User-agent: *\n'+('Disallow: /*?*\n' if owner.deny_query_documents else 'Allow: /\n'), 'text/plain')
                elif path == '/zhaopin/':
                    # Intentionally no anchors: only the browser response can
                    # produce the candidate; a DOM-only implementation fails.
                    early = ('<script>window.initialPopupBlocked = '
                             '(window.open("/apply") === null);</script>'
                             if parse_qs(urlsplit(self.path).query).get('key') == ['窗口隔离'] else '')
                    account = ('<div id="header-quick-menu-user-info">合成账号区域</div>'
                               if 'local_login_fixture=valid' in self.headers.get('Cookie','') else '')
                    styles = ''.join('<link rel="stylesheet" href="https://' + CDN_HOST +
                        '/fe-www-pc/v6/css/pacing-' + str(i) + '.css">' for i in range(owner.pacing_burst))
                    self.send('<!doctype html><meta charset="utf-8">' + styles + early + account + '<h1>合成搜索页</h1>'
                              '<iframe id="common-footer" src="https://' + CDN_HOST + '/footer"></iframe>'
                              '<div id="loaded"></div><script src="https://' + CDN_HOST + ASSET + '"></script>')
                elif path == REGION_PATH and self.headers.get('Host') == REGION_HOST:
                    self.send(json.dumps({'flag':1,'data':{'syntheticRegion':True}}), 'application/json')
                elif path == SUGGEST_PATH and self.headers.get('Host') == API_HOST:
                    self.send(json.dumps({'flag':1,'data':{'suggestList':[{'word':'不能替换输入的其他词'}]}}), 'application/json')
                elif path == ASSET:
                    self.send("""window.optionalBlocked = 0;
const optionalPaths=['/api/com.liepin.cbp.baizhong.op.v2-show-4pc',
 '/statisticPlatform/standardFLog.json', '/statisticPlatform/standardTLog.json',
 ...Array.from({length:80}, (_,i) => '/api/com.liepin.cbp.baizhong.op.v2-show-4pc?fixtureBurst='+i)];
const optionalRequests=optionalPaths.map(path =>
 fetch('https://""" + OPTIONAL_HOST + """' + path, {method:'POST',
 headers:{'Content-Type':'application/json'},body:'{}'}).catch(() => window.optionalBlocked++));
window.hotWordBlocked=0;
window.loginConfigRead=false;
const hotWordRequests=[{}, {headers:{'X-Client-Type':'web'}}].map(options =>
 fetch('https://""" + API_HOST + HOTWORD_PATH + """', options)
 .catch(() => window.hotWordBlocked++));
const key = new URL(location.href).searchParams.get('key') || '';
window.searchSubmissions=0;window.initializationResponses=0;
function search(keyword) {
 const main={city:'410',dq:'410',pubTime:'7',currentPage:0,pageSize:40,key:keyword,
  suggestTag:'fixture-tag',workYearCode:'',compId:'',compName:'',compTag:'',industry:'',
  salaryCode:'10$30',jobKind:'',compScale:'',compKind:'',compStage:'',eduLevel:''};
 const through={scene:'fixture-search',skId:'',fkId:'',ckId:'a'.repeat(32),suggest:null};
 const form={...main,salaryCode:'',salaryLow:'10',salaryHigh:'30'};
 let passThroughForm=through;
 if(keyword) {
  history.replaceState({},'',location.pathname+'?'+new URLSearchParams(
   {...main,...through,suggest:'null',suggestId:''}));
  delete form.pubTime;form.hrActiveTimeCode=main.pubTime;
  passThroughForm={...through,ckId:'b'.repeat(32)};
 }
 return fetch('https://""" + API_HOST + PATH + """', {
  method:'POST', headers:{'Content-Type':'application/json','X-Client-Type':'web'},
  body: JSON.stringify({data:{mainSearchPcConditionForm:form,passThroughForm}})
 }).then(r => r.json()).then(j => {
  document.querySelector('#loaded').textContent='response received';
  document.querySelectorAll('[data-fixture-recommendation]').forEach(a => a.remove());
  if (!keyword) {
   window.initializationResponses++;
   const a=document.createElement('a');a.href=j.data.data.jobCardList[0].job.link;
   a.textContent='默认推荐';a.setAttribute('data-fixture-recommendation','true');document.body.append(a);
  }
 });
}
Promise.all([Promise.all(optionalRequests), fetch('https://""" + REGION_HOST + REGION_PATH + """?from=component', {
 method:'GET', credentials:'include', headers:{'X-Client-Type':'web'}
}).then(r => r.json()), Promise.all(hotWordRequests),
 fetch('https://""" + API_HOST + LOGIN_CONFIG_PATH + """', {
  method:'POST', credentials:'include', headers:{'X-Client-Type':'web'}
 }).then(r => r.json()).then(j => {window.loginConfigRead=j.data.graySwitch===true;})
]).then(([,regions]) => {
 const field=document.createElement('input');field.type='text';field.placeholder='搜索职位、公司';
 document.body.append(field);
 field.addEventListener('input', () => {
  fetch('https://""" + API_HOST + SUGGEST_PATH + """?keyword='+encodeURIComponent(field.value),
   {headers:{'X-Client-Type':'web'}}).then(r => r.json()).then(() => {window.suggestionsArrived=true;});
 });
 field.addEventListener('keydown',event => {
  if(event.key==='Enter') {
   event.preventDefault();window.searchSubmissions++;
   search(field.value);
  }
 });
 return search(key);
});""", 'application/javascript')
                elif path.startswith('/fe-www-pc/v6/css/pacing-') and path.endswith('.css'):
                    self.send('body { color: #123; }', 'text/css')
                elif path == '/fixture-login':
                    self.send('<h1>人工登录</h1><form method="post" action="/fixture-login">'
                              '<button>人工确认</button></form>')
                elif path == '/job/123.shtml':
                    self.send(recorded_markup(recorded_posting(url=URL + path)))
                else: self.send('unknown', status=404)
            def do_OPTIONS(self):
                self.record()
                if urlsplit(self.path).path == LOGIN_CONFIG_PATH and self.headers.get('Host') == API_HOST:
                    self.send('', status=403 if owner.deny_login_config else 204)
                elif urlsplit(self.path).path == REGION_PATH and self.headers.get('Host') == REGION_HOST:
                    self.send('', status=403 if owner.deny_regions else 204)
                elif urlsplit(self.path).path not in (PATH,SUGGEST_PATH): self.send('unknown', status=405)
                else: self.send('', status=403 if owner.deny_cors else 204)
            def do_POST(self):
                self.record()
                if self.path == LOGIN_CONFIG_PATH and self.headers.get('Host') == API_HOST:
                    assert not self.rfile.read(int(self.headers.get('Content-Length','0')))
                    self.send(json.dumps({'flag':1,'data':{'graySwitch':True}}),'application/json')
                    return
                if self.path == '/fixture-login':
                    self.rfile.read(int(self.headers.get('Content-Length', '0')))
                    self.send('<h1>人工登录完成</h1>', extra=[
                        ('Set-Cookie', 'local_login_fixture=valid; HttpOnly; Secure; SameSite=Lax')])
                    return
                if self.path != PATH: self.send('unknown', status=405); return
                raw = self.rfile.read(int(self.headers.get('Content-Length', '0')))
                data = json.loads(raw)['data']
                form = data['mainSearchPcConditionForm']
                owner.search_shapes.append(dict(keyword_empty=form['key']=='',
                    date_alias='pubTime' not in form and form.get('hrActiveTimeCode')=='7',
                    salary_split=form.get('salaryCode')=='' and form.get('salaryLow')=='10' and form.get('salaryHigh')=='30',
                    rotated_id=data.get('passThroughForm',{}).get('ckId')=='b'*32))
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
    result = {'success':False,'scope':'Five artificial TLS hosts, actual native backend and Liepin adapter; not live certification.', 'checks':[]}
    services = []; server = None
    try:
        with tempfile.TemporaryDirectory(prefix='radar-search-fixture-') as tmp, ExitStack() as cleanup:
            root = Path(tmp)
            cleanup.callback(lambda: [s.close() for s in services])
            with trust_fixture(root, (API_HOST, CDN_HOST, LOGIN_HOST, REGION_HOST)):
                server = SearchFixture(root)
                cleanup.callback(server.close)
                template = builtins().get('liepin')
                contract = contract_for(template)
                mapping = {'www.liepin.com':HOST, 'api-c.liepin.com':API_HOST,
                           'concat.lietou-static.com':CDN_HOST, 'image0.lietou-static.com':CDN_HOST,
                           'api-passport.liepin.com':LOGIN_HOST, 'feim.liepin.com':CDN_HOST,
                           'api-dok.liepin.com':REGION_HOST}
                rules = tuple(replace(r, host=mapping[r.host], cors_origin=URL if r.cors_origin else '') for r in contract.rules)
                local_contract = replace(contract, hosts=(HOST,API_HOST,CDN_HOST,LOGIN_HOST,REGION_HOST), rules=rules,
                    ignored_rules=tuple(replace(r,host=mapping.get(r.host,OPTIONAL_HOST)) for r in contract.ignored_rules))
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
                class FixtureBackend(NativeBackend):
                    def snapshot(self):
                        page = super().snapshot()
                        if urlsplit(page.url).path == '/zhaopin/':
                            # GuidedService owns its browser on the worker. Read
                            # fixture-only counters there and publish plain data;
                            # the test's main thread must not issue CDP commands.
                            self.form_observation = self.page.evaluate('''() => ({
                                url: location.href,
                                submissions: window.searchSubmissions,
                                initializations: window.initializationResponses,
                                suggestions: window.suggestionsArrived === true,
                                optionalBlocked: window.optionalBlocked,
                                hotWordBlocked: window.hotWordBlocked,
                                loginConfigRead: window.loginConfigRead
                            })''')
                        return page
                def factory(a,l,c,p,**saved):
                    with use_policy(NetworkPolicy()):
                        return FixtureBackend(a,l,c,p,headless=not args.headed,channel=args.channel,executable_path=args.executable,**saved)
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
                    first_request = len(server.requests)
                    service.create(query); task=wait(service)
                    result['first_task_code'] = task['code']
                    if task['status'] != 'ready':
                        result['diagnostics'] = service.diagnostics({'id':task['id']})
                    assert task['status']=='ready' and len(task['cards'])==1, task.get('code')
                    assert any(r['method']=='OPTIONS' and r['host']==API_HOST for r in server.requests), 'preflight not observed at upstream'
                    assert any(r['method']=='POST' and r['host']==API_HOST for r in server.requests)
                    assert any(r['host']==CDN_HOST and r['path']==ASSET for r in server.requests)
                    native = service._backends[task['id']]
                    first_business = [r for r in server.requests[first_request:] if r['host'] in (API_HOST,REGION_HOST) and r['path'] in (PATH,REGION_PATH,SUGGEST_PATH)]
                    assert native.native_counts['business']==len(first_business), 'every search/region request and actual preflight must be accounted'
                    assert sum(r['method']=='POST' and r['path']==PATH for r in first_business)==2
                    assert sum(r['method']=='GET' and r['path']==REGION_PATH for r in first_business)==1
                    assert sum(r['method']=='GET' and r['path']==SUGGEST_PATH for r in first_business)==1
                    result['checks'].append('native CDN script and cross-origin preflight/search POST supply a candidate without DOM links or login')
                    assert {r['method'] for r in server.requests if r['host']==REGION_HOST and r['path']==REGION_PATH}=={'OPTIONS','GET'}
                    result['checks'].append('reviewed city catalogue GET and GET preflight use their own origin robots, native credential-aware CORS and request accounting; they cannot supply job cards')
                    assert native.page.document_url == local.search_base
                    observed_form = native.form_observation
                    assert observed_form['url'] == native.page.url
                    assert observed_form['submissions'] == 1
                    assert observed_form['initializations'] == 1
                    assert observed_form['suggestions'] is True
                    assert observed_form['loginConfigRead'] is True
                    assert {r['method'] for r in server.requests[first_request:] if r['path']==LOGIN_CONFIG_PATH}=={'OPTIONS','POST'}
                    assert native.native_counts['login']==2
                    assert native.wire.ledger.summary(local.key)['login']['day']==0
                    assert not any(o.operation in {'liepin_login_ui_config','liepin_login_ui_preflight'} for o in native.observations())
                    result['checks'].append('publisher login display configuration uses its real POST preflight, reply and request quota before authentication; no credential submission, login budget or job-body observation')
                    assert observed_form['hotWordBlocked'] == 2
                    assert not any(r['path']==HOTWORD_PATH for r in server.requests)
                    optional_events = service.diagnostics({'id':task['id']})['events']
                    assert any(e['code']=='native_optional_request_blocked'
                               and e['host']==API_HOST and e['method']=='GET'
                               and e['local_block'] and e['impact']=='optional' for e in optional_events)
                    assert any(e['code']=='native_optional_request_blocked'
                               and e['host']==API_HOST and e['method']=='OPTIONS'
                               and e['local_block'] and e['impact']=='optional' for e in optional_events)
                    result['checks'].append('optional hotword GET and real preflight are aborted locally without reaching the server, failing the task, consuming business quota or replacing its single explicit keyword submission')
                    published_url = parse_qs(urlsplit(native.page.url).query, keep_blank_values=True)
                    assert published_url['pubTime'] == ['7'] and published_url['salaryCode'] == ['10$30']
                    assert published_url['ckId'] == ['a'*32] and published_url['suggest'] == ['null']
                    submitted_shapes = [s for s in server.search_shapes if not s['keyword_empty']]
                    assert len(submitted_shapes) == 1 and all(submitted_shapes[0][k] for k in ('date_alias','salary_split','rotated_id'))
                    assert not any('key' in r['query_keys'] for r in server.requests[:])
                    try: native.wire.ensure_robots(native.page.url)
                    except CrawlError as exc: assert exc.code=='robots_denied',exc.code
                    else: raise AssertionError('fixture must refuse an actual keyword document URL')
                    result['checks'].append('default service task uses the visible field and one Enter, reads suggestions without changing the keyword, pairs its response after history update and never fetches the forbidden keyword document or collects initial recommendations')
                    result['checks'].append('publisher-shaped URL filters pair with the date alias, split salary and rotated pass-through ID; full current-response digest still selects the same complete JD')
                    # All outside DNS/dials above raise; ignored preflights must
                    # terminate locally while the real search/report succeeds.
                    assert not any(r['host']==OPTIONAL_HOST for r in server.requests)
                    assert any(e['code']=='native_optional_request_blocked' and e['impact']=='optional'
                               for e in service.diagnostics({'id':task['id']})['events'])
                    result['checks'].append('known marketing, standardFLog and standardTLog requests are aborted before network access without aborting search or report')
                    assert observed_form['optionalBlocked'] == 83
                    assert native.native_counts['blocked'] >= 83
                    assert not native._halted
                    result['checks'].append('80 additional simultaneous optional requests finish with native local refusals before the visible form is offered; no recursive command-budget exhaustion or optional upstream request')
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
                    assert not any(('login' in r['path'] and r['path']!=LOGIN_CONFIG_PATH) or 'apply' in r['path'] for r in server.requests)
                    result['checks'].append('anonymous read sends only the exact login display configuration, no authentication request or credentials; application identity retained')
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
                    assert not any(('login' in r['path'] and r['path']!=LOGIN_CONFIG_PATH) or 'apply' in r['path'] for r in server.requests)
                    result['checks'].append('one opted-in create action performs native API-only search, bounded selection, complete JD and original report without login or manual Collect')
                    service.create({**query, 'keyword': '明确无结果', 'auto_collect': True})
                    automatic_empty = wait(service)
                    result['automatic_empty_state'] = {k:automatic_empty[k] for k in ('code','status')}
                    if automatic_empty['code'] != 'no_matching_jobs':
                        result['automatic_empty_diagnostics'] = service.diagnostics({'id':automatic_empty['id']})
                    assert automatic_empty['code'] == 'no_matching_jobs', automatic_empty['code']
                    assert not automatic_empty['selection'] and not automatic_empty['report_id']
                    result['checks'].append('automatic mode keeps confirmed empty results empty without selecting stale jobs or creating a report')
                    service.close(); services.clear()
                    before = len(server.requests)
                    b = factory(local, RateLedger(root/'query-document-denied.sqlite', Limits(page_interval=0,request_interval=0)), threading.Event(), lambda *_: None)
                    try:
                        try: b.open(local.search_url('时间序列'))
                        except CrawlError as exc: assert exc.code=='robots_denied',exc.code
                        else: raise AssertionError('query document request bypassed robots')
                    finally: b.close()
                    assert not any(r['path']=='/zhaopin/' for r in server.requests[before:])
                    result['checks'].append('a real keyword document navigation remains refused before network even after the form route succeeds')
                    server.deny_query_documents = False
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
                        # Exercise the passive return gate on this real native
                        # page. The synthetic login above is not an account
                        # verifier; this only enqueues the original capture.
                        now = [0.0]; queued = []
                        state = dict(id='local-return', platform='liepin',
                            search_url=local.search_url('时间序列'), keyword='时间序列',
                            query_scope_version=1, status='waiting_manual',
                            authentication='manual_pending', phase='search',
                            auto_continue_after_login=True)
                        watcher = LoginReturnManager(clock=lambda:now[0])
                        owner = SimpleNamespace(_lock=threading.RLock(), _busy=False,
                            _shutdown=threading.Event(), _cancel=threading.Event(),
                            _backends={state['id']:b}, registry=Registry([login_adapter]),
                            _load=lambda _:state, _save=lambda value,**kw:value.update(kw),
                            _submit=lambda *args:queued.append(args))
                        before_return = len(server.requests)
                        watcher.arm(state,b); watcher.tick(owner)
                        assert not queued
                        now[0] = 1; watcher.tick(owner)
                        now[0] = 2; watcher.tick(owner)
                        assert queued == [('capture','local-return')]
                        assert len(server.requests) == before_return
                        assert state['authentication']=='manual_pending'
                        assert 'effective_search' not in state
                        # Return to the supported keyword-free login entry,
                        # then exercise the one-shot normal form handoff.
                        from vibe_job_radar.guided.liepin_form import matching_search_entry_signature, submit_search
                        b.open_search(state['search_url'],keyword=state['keyword'],authentication=True)
                        b.page.wait_for_function('window.initializationResponses === 1',timeout=15000)
                        state.update(backend='native',cards=[],selection=[],status='waiting_manual')
                        queued.clear();now[0]=3
                        before_entry = len(server.requests)
                        watcher.arm(state,b);watcher.tick(owner)
                        assert not queued
                        now[0]=4;watcher.tick(owner)
                        assert len(queued)==1 and queued[0][:2]==('resume_returned_search','local-return')
                        assert len(server.requests)==before_entry
                        returned=queued[0][2]
                        assert returned.backend is b and returned.keyword==state['keyword']
                        assert matching_search_entry_signature(b,state,b.snapshot())==returned.signature
                        b.collection_mode();submit_search(b,returned.keyword)
                        assert b.page.evaluate('window.searchSubmissions')==1
                        assert b.adapter.cards(b.snapshot())
                        assert not any(r['path']=='/fixture-login' for r in server.requests[before_entry:])
                        assert not any(r['path']=='/zhaopin/' for r in server.requests[before_entry:])
                    finally: b.close()
                    result['checks'].append('sandboxed gzip documents preserve normal same-tab form POST, HttpOnly Cookie and subsequent API-only search; artificial login only')
                    result['checks'].append('two passive native reads accept publisher-expanded original query scope after the artificial form, enqueue exactly one capture and send no new request; account authentication remains unverified')
                    result['checks'].append('query-free artificial account/search controls require two stable observations and a fresh owner check, then submit the original keyword once through the visible form without another login or document navigation')
                    # A real publisher rejection must prevent the POST rather
                    # than getting replaced by a driver-generated success.
                    before = sum(r['method']=='POST' and r['path']==PATH for r in server.requests)
                    server.deny_cors = True
                    b = factory(local, RateLedger(root/'cors-denied.sqlite', Limits(page_interval=0,request_interval=0)), threading.Event(), lambda *_: None)
                    try:
                        try: b.open(local.search_url('时间序列'))
                        except Exception as exc:
                            assert getattr(exc, 'code', None)=='http_403', getattr(exc, 'code', None)
                        else: raise AssertionError('publisher preflight denial accepted')
                    finally: b.close()
                    assert sum(r['method']=='POST' and r['path']==PATH for r in server.requests)==before
                    result['checks'].append('publisher OPTIONS denial prevents search POST and is not fabricated as success')
                    server.deny_cors = False
                    before_get = sum(r['method']=='GET' and r['path']==REGION_PATH for r in server.requests)
                    before_post = sum(r['method']=='POST' and r['path']==PATH for r in server.requests)
                    server.deny_regions = True
                    b = factory(local, RateLedger(root/'regions-denied.sqlite', Limits(page_interval=0,request_interval=0)), threading.Event(), lambda *_: None)
                    try:
                        try: b.open(local.search_url('时间序列'))
                        except Exception as exc:
                            assert getattr(exc, 'code', None)=='http_403', getattr(exc, 'code', None)
                        else: raise AssertionError('publisher region preflight denial accepted')
                    finally: b.close()
                    assert sum(r['method']=='GET' and r['path']==REGION_PATH for r in server.requests)==before_get
                    assert sum(r['method']=='POST' and r['path']==PATH for r in server.requests)==before_post
                    result['checks'].append('publisher region OPTIONS refusal prevents its GET and the dependent search without replacing either response')
                    server.deny_regions = False
                    before_config = sum(r['method']=='POST' and r['path']==LOGIN_CONFIG_PATH for r in server.requests)
                    before_search = sum(r['method']=='POST' and r['path']==PATH for r in server.requests)
                    server.deny_login_config = True
                    b = factory(local, RateLedger(root/'login-config-denied.sqlite', Limits(page_interval=0,request_interval=0)), threading.Event(), lambda *_: None)
                    try:
                        try: b.open(local.search_url('时间序列'))
                        except Exception as exc:
                            assert getattr(exc,'code',None)=='http_403',getattr(exc,'code',None)
                        else: raise AssertionError('publisher login config refusal accepted')
                    finally: b.close()
                    assert sum(r['method']=='POST' and r['path']==LOGIN_CONFIG_PATH for r in server.requests)==before_config
                    assert sum(r['method']=='POST' and r['path']==PATH for r in server.requests)==before_search
                    result['checks'].append('publisher login configuration OPTIONS refusal prevents that POST and dependent search; no synthetic default, QR action or credential retry')
                    server.deny_login_config = False
                    # Real, default request pacing must coexist with a burst of
                    # permitted assets and the ordinary automatic report path.
                    # No clock/transport/controller wait is mocked here.
                    server.pacing_burst = 20
                    paced_workspace = Workspace(root/'paced-workspace')
                    paced_ledger = RateLedger(root/'paced-rate.sqlite', Limits(page_interval=0))
                    paced = GuidedService(paced_workspace, registry=Registry([local]),
                        ledger=paced_ledger, native_backend_factory=factory)
                    services.append(paced)
                    before_paced = len(server.requests)
                    try:
                        paced.create({**query, 'max_pages':1, 'auto_collect':True})
                        paced_task = wait(paced)
                        assert paced_task['status']=='completed', paced_task.get('code')
                        assert paced_task['outcome']['saved']==1
                        with Store(paced_workspace.db) as store:
                            paced_records = store.records()
                            assert len(paced_records)==1 and paced_records[0].text==RECORDED_BODY
                        assert paced_workspace.report(paced_task['report_id'])['manifest']['stats']['full_text_job_groups']==1
                        requests = server.requests[before_paced:]
                        assert sum('/css/pacing-' in r['path'] for r in requests)==20
                        assert paced_ledger.summary('liepin')['request']['day']==len(requests)
                        import sqlite3
                        with closing(sqlite3.connect(paced_ledger.path)) as db:
                            stamps = [r[0] for r in db.execute(
                                "SELECT ts FROM visits WHERE site='liepin' AND kind='request' ORDER BY ts")]
                        assert len(stamps)>20 and all(b-a>=.499 for a,b in zip(stamps, stamps[1:]))
                        result['paced_request_count'] = len(stamps)
                        result['paced_min_interval'] = min(b-a for a,b in zip(stamps, stamps[1:]))
                    finally:
                        server.pacing_burst = 0
                        paced.close()
                    result['checks'].append('20 permitted stylesheet requests retain the default 0.5s durable spacing while the visible keyword form, full JD and original report complete automatically without blocking CDP replies')
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
