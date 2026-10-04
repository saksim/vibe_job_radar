"""Explicit, local-only CORS search -> actual Liepin adapter -> report test.

Five artificial HTTPS hosts use a fresh isolated test CA and the existing
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
from urllib.parse import parse_qs, urlencode, urlsplit

from run_native_browser_acceptance import (ROOT, HOST, URL, NativeBackend, RateLedger,
    Limits, GuidedService, Registry, Store, NetworkPolicy, use_policy, Workspace,
    trust_fixture, RECORDED_BODY, RECORDED_TITLE, recorded_markup, recorded_posting)
from vibe_job_radar.guided.adapters import builtins
from vibe_job_radar.guided.native_policy import contract_for, NativeRule

API_HOST = 'api.' + HOST
CDN_HOST = 'static.' + HOST
LOGIN_HOST = 'login.' + HOST
OPTIONAL_HOST = 'optional.' + HOST
REGION_HOST = 'regions.' + HOST
REGION_PATH = '/api/com.liepin.bd.p.v4.get-all-dq'
SUGGEST_PATH = '/api/com.liepin.searchfront4c.pc-search-suggest-list'
CONFIG_PATH = '/api/com.liepin.pupa.get-pc-login-scan-config'
HOTWORDS_PATH = '/api/com.liepin.searchfront4c.pc-hot-search-word-list'
TLOG_PATH = '/statisticPlatform/standardTLog.json'
DEPENDENCY_PATHS = (REGION_PATH, SUGGEST_PATH, CONFIG_PATH)
PATH = '/api/com.liepin.searchfront4c.pc-search-job'
ASSET = '/fe-www-pc/v6/js/search-fixture.js'


class SearchFixture:
    def __init__(self, root):
        self.requests = []
        self.deny_cors = False
        self.login_late_pacing = False
        self.dependency_mode = False
        self.binding_mode = ''
        self.form_mode = ''
        self.search_shapes = []
        self.denied_dependency = None
        owner = self
        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'
            def log_message(self, *_): pass
            def send(self, content, mime='text/html; charset=utf-8', status=200, extra=()):
                no_body = status in {204,205}
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
                dependency_path = urlsplit(self.path).path
                self.send_header('Access-Control-Allow-Methods', 'GET' if dependency_path in (REGION_PATH, SUGGEST_PATH) else 'POST')
                if dependency_path in DEPENDENCY_PATHS:
                    self.send_header('Access-Control-Allow-Credentials', 'true')
                self.send_header('Access-Control-Allow-Headers', 'content-type,x-client-type')
                self.end_headers()
                try: self.wfile.write(raw)
                except (OSError, ssl.SSLError): pass
            def record(self):
                owner.requests.append({'method': self.command, 'host': self.headers.get('Host'),
                    'path': urlsplit(self.path).path, 'anonymous': not self.headers.get('Cookie'),
                    'query_keys':sorted(parse_qs(urlsplit(self.path).query,keep_blank_values=True)),
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
                        rule='Disallow: /*?*' if owner.form_mode and self.headers.get('Host')==HOST else 'Allow: /'
                        self.send('User-agent: *\n'+rule+'\n', 'text/plain')
                elif path == '/zhaopin/':
                    # Intentionally no anchors: only the browser response can
                    # produce the candidate; a DOM-only implementation fails.
                    early = ('<script>window.initialPopupBlocked = '
                             '(window.open("/apply") === null);</script>'
                             if parse_qs(urlsplit(self.path).query).get('key') == ['窗口隔离'] else '')
                    stale = ('<a href="/job/999.shtml">过期推荐职位</a>' if owner.binding_mode or owner.form_mode else '')
                    if owner.form_mode:
                        stale += '<div id="header-quick-menu-user-info">合成账号区域</div>' if 'local_login_fixture=valid' in self.headers.get('Cookie','') else ''
                        if owner.form_mode=='challenge':stale += '<p>请完成安全验证</p>'
                    self.send('<!doctype html><meta charset="utf-8">' + early + stale + '<h1>合成搜索页</h1>'
                              '<iframe id="common-footer" src="https://' + CDN_HOST + '/footer"></iframe>'
                              '<div id="loaded"></div><script src="https://' + CDN_HOST + ASSET + '"></script>')
                elif path in (REGION_PATH, SUGGEST_PATH):
                    self.send(json.dumps({'flag':1,'data':[]}), 'application/json')
                elif path == ASSET and owner.form_mode:
                    self.send(visible_form_script(owner.form_mode), 'application/javascript')
                elif path == ASSET and owner.binding_mode:
                    self.send(binding_script(owner.binding_mode), 'application/javascript')
                elif path == ASSET and owner.dependency_mode:
                    self.send(dependency_script(), 'application/javascript')
                elif path == ASSET:
                    self.send("""window.optionalBlocked = 0;
for (const path of ['/api/com.liepin.cbp.baizhong.op.v2-show-4pc', '/statisticPlatform/standardFLog.json']) {
 fetch('https://""" + OPTIONAL_HOST + """' + path, {method:'POST',
 headers:{'Content-Type':'application/json'},body:'{}'}).catch(() => window.optionalBlocked++);
}
const key = new URL(location.href).searchParams.get('key');
fetch('https://""" + API_HOST + PATH + """', {
 method:'POST', headers:{'Content-Type':'application/json','X-Client-Type':'web'},
 body: JSON.stringify({data:{mainSearchPcConditionForm:{key,currentPage:0,pageSize:40}}})
}).then(r => r.json()).then(j => {document.querySelector('#loaded').textContent='response received';});""", 'application/javascript')
                elif path.startswith('/fe-www-pc/v6/css/pacing-') and path.endswith('.css'):
                    self.send('body { color: #123; }', 'text/css')
                elif path == '/fixture-login':
                    late = ('<script>setTimeout(() => {for (let i=0;i<3;i++) {'
                            'const link=document.createElement("link");link.rel="stylesheet";'
                            'link.href="https://' + CDN_HOST + '/fe-www-pc/v6/css/pacing-"+i+".css";'
                            'document.head.appendChild(link);}},250);</script>'
                            if owner.login_late_pacing else '')
                    self.send('<h1>人工登录</h1><form method="post" action="/fixture-login">'
                              '<button>人工确认</button></form>' + late)
                elif path == '/job/123.shtml':
                    self.send(recorded_markup(recorded_posting(url=URL + path)))
                else: self.send('unknown', status=404)
            def do_OPTIONS(self):
                self.record()
                path = urlsplit(self.path).path
                if path in DEPENDENCY_PATHS:
                    self.send('', status=403 if owner.denied_dependency == path else 204)
                elif path != PATH: self.send('unknown', status=405)
                else: self.send('', status=403 if owner.deny_cors else 204)
            def do_POST(self):
                self.record()
                if urlsplit(self.path).path == CONFIG_PATH:
                    self.rfile.read(int(self.headers.get('Content-Length', '0')))
                    self.send(json.dumps({'flag':1,'data':{'graySwitch':True}}), 'application/json')
                    return
                if self.path == '/fixture-login':
                    self.rfile.read(int(self.headers.get('Content-Length', '0')))
                    self.send('<h1>人工登录完成</h1>', extra=[
                        ('Set-Cookie', 'local_login_fixture=valid; HttpOnly; Secure; SameSite=Lax')])
                    return
                if self.path != PATH: self.send('unknown', status=405); return
                raw = self.rfile.read(int(self.headers.get('Content-Length', '0')))
                data = json.loads(raw)['data'];form = data['mainSearchPcConditionForm']
                owner.search_shapes.append({'keyword_empty':form['key']=='',
                    'date_alias':'pubTime' not in form and form.get('hrActiveTimeCode')=='7',
                    'salary_split':form.get('salaryCode')=='' and form.get('salaryLow')=='20' and form.get('salaryHigh')=='40',
                    'rotated_id':data.get('passThroughForm',{}).get('ckId')=='b'*32})
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



def dependency_script():
    endpoints = {'region':'https://'+REGION_HOST+REGION_PATH,
                 'suggest':'https://'+API_HOST+SUGGEST_PATH,
                 'config':'https://'+API_HOST+CONFIG_PATH,
                 'hotwords':'https://'+API_HOST+HOTWORDS_PATH,
                 'tlog':'https://'+OPTIONAL_HOST+TLOG_PATH,
                 'search':'https://'+API_HOST+PATH}
    return 'const endpoints = '+json.dumps(endpoints)+';\n'+'''
(async () => {
  await Promise.all([
    fetch(endpoints.hotwords, {headers:{'X-Client-Type':'web'}}).catch(() => null),
    fetch(endpoints.tlog, {method:'POST',headers:{'Content-Type':'application/json'},body:'{}'}).catch(() => null)
  ]);
  for (const name of ['region','suggest','config']) {
    const response = await fetch(endpoints[name], {
      method:name==='config' ? 'POST' : 'GET',credentials:'include',
      headers:{'X-Client-Type':'web'}
    });
    if (!response.ok) throw new Error('dependency refused');
    await response.json();
  }
  const key = new URL(location.href).searchParams.get('key');
  const response = await fetch(endpoints.search, {
    method:'POST',headers:{'Content-Type':'application/json','X-Client-Type':'web'},
    body:JSON.stringify({data:{mainSearchPcConditionForm:{key,currentPage:0,pageSize:40}}})
  });
  await response.json();
  document.querySelector('#loaded').textContent='dependencies and response received';
})().catch(() => {document.querySelector('#loaded').textContent='dependency refused';});
'''


def verify_dependency_chain(root, server, local, factory, wait, query, result):
    sequence=[('OPTIONS',REGION_PATH),('GET',REGION_PATH),
              ('OPTIONS',SUGGEST_PATH),('GET',SUGGEST_PATH),
              ('OPTIONS',CONFIG_PATH),('POST',CONFIG_PATH),
              ('OPTIONS',PATH),('POST',PATH)]
    result['dependency_results']=[]
    try:
        for label, denied in [('allowed',None),('region-refused',REGION_PATH),
                              ('suggest-refused',SUGGEST_PATH),('config-refused',CONFIG_PATH)]:
            server.dependency_mode=True;server.denied_dependency=denied
            before=len(server.requests)
            workspace=Workspace(root/('dependency-'+label))
            ledger=RateLedger(root/('dependency-'+label+'.sqlite'),Limits(page_interval=0,request_interval=0))
            service=GuidedService(workspace,registry=Registry([local]),ledger=ledger,native_backend_factory=factory)
            try:
                service.create(explicit_seed({**query,'max_pages':1},local))
                task=wait(service)
                actual=[(r['method'],r['path']) for r in server.requests[before:] if r['path'] in (*DEPENDENCY_PATHS,PATH)]
                assert not any(r['path'] in (HOTWORDS_PATH,TLOG_PATH) for r in server.requests[before:])
                assert ledger.summary('liepin')['login']['day']==0
                if denied is None:
                    assert task['status']=='ready' and len(task['cards'])==1,task.get('code')
                    assert actual==sequence,actual
                    native=service._backends[task['id']]
                    assert native.native_counts['business']==6 and native.native_counts['login']==2
                    assert all(o.operation!='liepin_login_ui_config' for o in native._observations)
                    blocked=[e for e in service.diagnostics({'id':task['id']})['events'] if e['code']=='native_optional_request_blocked']
                    assert len(blocked)==2
                    service.action({'id':task['id'],'action':'collect','selected':[task['cards'][0]['id']]})
                    task=wait(service)
                    assert task['status']=='completed' and task['outcome']['saved']==1
                    with Store(workspace.db) as store:
                        records=store.records();assert len(records)==1 and records[0].text==RECORDED_BODY
                    assert workspace.report(task['report_id'])['manifest']['stats']['full_text_job_groups']==1
                    assert task['authentication']=='not_checked'
                    result['dependency_results'].append({'case':label,'required_sequence':actual,'optional_requests_forwarded':0,
                        'full_body_preserved':True,'report_full_text_job_groups':1,'login_attempts':0,'authentication':'not_checked'})
                    result['checks'].append('exact native region and suggestion GET/preflights plus login display POST precede current search/full JD/report; TLog and same-host hotwords stay local; no password action')
                else:
                    stop=sequence.index(('OPTIONS',denied))+1
                    assert task['code']=='http_403' and task['status']!='ready',task.get('code')
                    assert not task['cards'] and not task['report_id']
                    assert actual==sequence[:stop],actual
                    result['dependency_results'].append({'case':label,'required_sequence':actual,'code':'http_403',
                        'denied_operation_not_sent':True,'later_search_not_sent':True,'login_attempts':0})
                    result['checks'].append(label+': publisher preflight403 keeps original refusal, sends no denied read or later search, and produces no report')
            finally:
                service.close()
    finally:
        server.dependency_mode=False;server.denied_dependency=None


def binding_script(mode):
    config=json.dumps({'mode':mode,'endpoint':'https://'+API_HOST+PATH})
    return 'const fixture='+config+';'+r'''
const q=Object.fromEntries(new URL(location.href).searchParams.entries());
const fields=['key','city','otherCity','dq','workYearCode','compId','compName','compTag',
 'industry','salary','jobKind','compScale','compKind','compStage','eduLevel','suggestTag'];
const form=Object.fromEntries(fields.filter(k=>k in q).map(k=>[k,q[k]]));
form.currentPage=Number(q.currentPage);form.pageSize=Number(q.pageSize);
form.hrActiveTimeCode=q.pubTime;
const bounds=q.salaryCode.split('$');form.salaryCode='';
form.salaryLow=bounds[0];form.salaryHigh=bounds[1];
const through={scene:q.scene,skId:q.skId,fkId:q.fkId,ckId:'b'.repeat(32),suggest:null,sfrom:q.sfrom};
if(fixture.mode==='mismatch')form.otherCity='different-city';
if(fixture.mode!=='missing')fetch(fixture.endpoint,{method:'POST',
 headers:{'Content-Type':'application/json','X-Client-Type':'web'},
 body:JSON.stringify({data:{mainSearchPcConditionForm:form,passThroughForm:through}})
}).then(r=>r.json()).then(()=>{document.querySelector('#loaded').textContent='response received';})
 .catch(()=>{document.querySelector('#loaded').textContent='original request rejected';});
'''


def verify_published_binding(root, server, local, factory, wait, query, result):
    result['published_binding_results']=[]
    try:
        for label in ('matched','empty','mismatch','missing'):
            server.binding_mode=label;before=len(server.requests)
            key='明确无结果' if label=='empty' else query['keyword']
            params=dict(key=key,city='410',otherCity='fixture-other-city',dq='410',pubTime='7',
                currentPage=0,pageSize=40,workYearCode='',compId='',compName='',compTag='',industry='',
                salaryCode='20$40',jobKind='',compScale='',compKind='',compStage='',eduLevel='',suggestTag='',
                scene='fixture-search',skId='',fkId='',ckId='a'*32,suggest='null',suggestId='',sfrom='fixture-field')
            workspace=Workspace(root/('binding-'+label))
            ledger=RateLedger(root/('binding-'+label+'.sqlite'),Limits(page_interval=0,request_interval=0))
            service=GuidedService(workspace,registry=Registry([local]),ledger=ledger,native_backend_factory=factory)
            try:
                service.create({**query,'keyword':key,'max_pages':1,'list_url':local.search_base+'?'+urlencode(params)})
                task=wait(service)
                posts=sum(r['method']=='POST' and r['path']==PATH for r in server.requests[before:])
                assert not any(r['path']=='/job/999.shtml' for r in server.requests[before:])
                assert ledger.summary('liepin')['login']['day']==0
                if label=='matched':
                    assert task['status']=='ready' and len(task['cards'])==1 and task['cards'][0]['url']==URL+'/job/123.shtml',task.get('code')
                    assert posts==1
                    service.action({'id':task['id'],'action':'collect','selected':[task['cards'][0]['id']]})
                    task=wait(service);assert task['status']=='completed' and task['outcome']['saved']==1
                    with Store(workspace.db) as store:
                        records=store.records();assert len(records)==1 and records[0].text==RECORDED_BODY
                    assert workspace.report(task['report_id'])['manifest']['stats']['full_text_job_groups']==1
                    row={'case':label,'search_posts':posts,'full_body_preserved':True,'report_full_text_job_groups':1,'stale_dom_used':False}
                    result['checks'].append('native publisher date/salary/filter/interaction mapping binds its own response over stale DOM and preserves full JD/original report')
                else:
                    expected={'empty':'no_matching_jobs','mismatch':'liepin_search_query_mismatch','missing':'page_not_ready'}[label]
                    assert task['code']==expected and not task['cards'] and not task['report_id'],task.get('code')
                    assert posts==(1 if label=='empty' else 0)
                    assert (task['status']=='ready')==(label=='empty')
                    row={'case':label,'search_posts':posts,'code':expected,'stale_dom_used':False,'report_created':False}
                    result['checks'].append(label+': current empty/mismatched/missing response cannot fall back to stale DOM, unrelated request, login or report')
                row['login_attempts']=0;result['published_binding_results'].append(row)
            finally:
                service.close()
    finally:
        server.binding_mode=''

def explicit_seed(query, local):
    # Preserve the earlier exact-navigation cases. Default keyword-only form
    # submission is exercised separately below through real browser controls.
    if 'list_url' in query:
        return query
    return {**query,'list_url':local.search_url(query['keyword'])+'&currentPage=0'}


def visible_form_script(mode):
    config=json.dumps({'mode':mode,'endpoint':'https://'+API_HOST+PATH})
    return 'const fixture='+config+';'+r'''
window.searchSubmissions=0;window.initializationResponses=0;
const field=document.createElement('input');field.type='text';
field.placeholder='搜索职位、公司';document.body.append(field);
function search(keyword) {
 const main={key:keyword,city:'410',otherCity:'fixture-other-city',dq:'410',pubTime:'7',
  currentPage:0,pageSize:40,workYearCode:'',compId:'',compName:'',compTag:'',industry:'',
  salaryCode:'20$40',jobKind:'',compScale:'',compKind:'',compStage:'',eduLevel:'',suggestTag:''};
 const through={scene:'fixture-form',skId:'',fkId:'',ckId:'a'.repeat(32),suggest:null,sfrom:'fixture-field'};
 const form={...main,salaryCode:'',salaryLow:'20',salaryHigh:'40'};
 let passThroughForm=through;
 if(keyword) {
  history.replaceState({},'',location.pathname+'?'+new URLSearchParams({...main,...through,suggest:'null',suggestId:''}));
  delete form.pubTime;form.hrActiveTimeCode=main.pubTime;
  passThroughForm={...through,ckId:'b'.repeat(32)};
  if(fixture.mode==='missing')return Promise.resolve();
 }
 return fetch(fixture.endpoint,{method:'POST',headers:{'Content-Type':'application/json','X-Client-Type':'web'},
  body:JSON.stringify({data:{mainSearchPcConditionForm:form,passThroughForm}})
 }).then(r=>r.json()).then(()=>{
  document.querySelector('#loaded').textContent='response received';
  if(!keyword)window.initializationResponses++;
 });
}
field.addEventListener('keydown',event=>{
 if(event.key==='Enter') {event.preventDefault();window.searchSubmissions++;search(field.value);}
});
search('');
'''


def verify_visible_forms(root, server, local, factory, wait, query, result):
    result['visible_form_results']=[]
    def observed_factory(*args,**kwargs):
        backend=factory(*args,**kwargs);settle=backend._settle
        def observed_settle(*a,**kw):
            answer=settle(*a,**kw)
            if kw.get('search'):
                backend.fixture_form_observation=backend.page.evaluate('''() => ({
                    submissions:window.searchSubmissions,initializations:window.initializationResponses})''')
            return answer
        backend._settle=observed_settle
        return backend
    try:
        for label in ('matched','empty','missing','challenge','query-document-refused'):
            server.form_mode=label;before=len(server.requests);shape_before=len(server.search_shapes)
            key='明确无结果' if label=='empty' else query['keyword']
            workspace=Workspace(root/('visible-form-'+label))
            ledger=RateLedger(root/('visible-form-'+label+'.sqlite'),Limits(page_interval=0,request_interval=0))
            service=GuidedService(workspace,registry=Registry([local]),ledger=ledger,native_backend_factory=observed_factory)
            try:
                requested={**query,'keyword':key,'max_pages':1}
                if label=='query-document-refused':requested=explicit_seed(requested,local)
                service.create(requested);task=wait(service)
                requests=server.requests[before:];shapes=server.search_shapes[shape_before:]
                posts=sum(not row['keyword_empty'] for row in shapes)
                assert not any(r['path']=='/job/999.shtml' for r in requests)
                assert not any(r['path']=='/zhaopin/' and r['query_keys'] for r in requests)
                assert ledger.summary('liepin')['login']['day']==0
                row={'case':label,'keyword_posts':posts,'query_document_requests':0,'stale_recommendation_used':False,'login_attempts':0}
                if label in ('matched','empty'):
                    native=service._backends[task['id']]
                    observed=native.fixture_form_observation
                    assert observed=={'submissions':1,'initializations':1},observed
                    assert posts==1 and len(shapes)==2 and shapes[0]['keyword_empty']
                    assert all(shapes[1][k] for k in ('date_alias','salary_split','rotated_id'))
                    row.update(visible_submissions=1,initializations=1)
                    if label=='matched':
                        assert task['status']=='ready' and len(task['cards'])==1,task.get('code')
                        assert task['cards'][0]['url']==URL+'/job/123.shtml'
                        service.action({'id':task['id'],'action':'collect','selected':[task['cards'][0]['id']]})
                        task=wait(service);assert task['status']=='completed' and task['outcome']['saved']==1
                        with Store(workspace.db) as store:
                            records=store.records();assert len(records)==1 and records[0].text==RECORDED_BODY
                        assert workspace.report(task['report_id'])['manifest']['stats']['full_text_job_groups']==1
                        row.update(full_body_preserved=True,report_full_text_job_groups=1)
                    else:
                        assert task['status']=='ready' and task['code']=='no_matching_jobs' and not task['cards'] and not task['report_id'],task.get('code')
                        row.update(code=task['code'],report_created=False)
                else:
                    expected={'missing':'page_not_ready','challenge':'manual_required','query-document-refused':'robots_denied'}[label]
                    assert task['code']==expected and task['status']!='ready' and not task['cards'] and not task['report_id'],task.get('code')
                    assert posts==0
                    if label=='query-document-refused':assert not shapes and not any(r['path']=='/zhaopin/' for r in requests)
                    row.update(code=task['code'],report_created=False)
                result['visible_form_results'].append(row)
                result['checks'].append('visible form '+label+': exact owned keyword input and current response; no query document, stale recommendation or login request')
            finally:service.close()
    finally:server.form_mode=''


def verify_visible_login_return(root, server, local, factory, query, result):
    from types import SimpleNamespace
    from vibe_job_radar.guided.login_return import LoginReturnManager
    from vibe_job_radar.guided.liepin_form import matching_search_entry_signature, submit_search
    server.form_mode='login'
    contract=local.native_contract
    rules=(*contract.rules,
        NativeRule('fixture_login_page',HOST,r'/fixture-login',resources=('Document',),role='document'),
        NativeRule('fixture_login_submit',HOST,r'/fixture-login',methods=('POST',),resources=('Document',),role='login',authentication=True))
    adapter=replace(local,native_contract=replace(contract,rules=rules))
    b=None
    try:
        b=factory(adapter,RateLedger(root/'visible-login-return.sqlite',Limits(page_interval=0,request_interval=0)),threading.Event(),lambda *_:None)
        b.open(URL+'/fixture-login',authentication=True)
        with b.page.expect_navigation(wait_until='domcontentloaded'):
            b.page.get_by_role('button',name='人工确认').click()
        b._check_error()
        assert b.page.locator('h1').inner_text()=='人工登录完成'
        cookie=next(c for c in b.context.cookies([URL]) if c['name']=='local_login_fixture')
        assert cookie['value']=='valid' and cookie['httpOnly'] and cookie['secure']
        state=dict(id='form-return',platform='liepin',backend='native',keyword=query['keyword'],
            search_url=local.search_url(query['keyword']),query_scope_version=1,status='waiting_manual',
            authentication='manual_pending',phase='search',cards=[],selection=[],auto_continue_after_login=True)
        b.open_search(state['search_url'],keyword=state['keyword'],authentication=True)
        b.page.wait_for_function('window.initializationResponses === 1',timeout=15000)
        now=[0];queued=[];watcher=LoginReturnManager(clock=lambda:now[0])
        owner=SimpleNamespace(_lock=threading.RLock(),_busy=False,_shutdown=threading.Event(),_cancel=threading.Event(),
            _backends={state['id']:b},registry=Registry([adapter]),_load=lambda _:state,
            _save=lambda value,**kw:value.update(kw),_submit=lambda *args:queued.append(args))
        before=len(server.requests);shape_before=len(server.search_shapes)
        watcher.arm(state,b);watcher.tick(owner);assert not queued
        now[0]=1;watcher.tick(owner);now[0]=2;watcher.tick(owner)
        assert len(queued)==1 and queued[0][:2]==('resume_returned_search',state['id'])
        assert len(server.requests)==before and state['authentication']=='manual_pending'
        returned=queued[0][2];assert returned.backend is b and returned.keyword==state['keyword']
        assert matching_search_entry_signature(b,state,b.snapshot())==returned.signature
        b.collection_mode();submit_search(b,returned.keyword)
        assert b.page.evaluate('window.searchSubmissions')==1
        cards=b.adapter.cards(b.snapshot());assert len(cards)==1 and cards[0].url==URL+'/job/123.shtml'
        assert len(server.search_shapes[shape_before:])==1 and not server.search_shapes[-1]['keyword_empty']
        assert not any(r['path'] in ('/fixture-login','/zhaopin/') for r in server.requests[before:])
        result['visible_login_return']={'artificial_login_only':True,'queued_actions':1,'visible_submissions':1,
            'keyword_posts':1,'new_login_requests':0,'new_document_requests':0,'authentication':'manual_pending',
            'third_owner_observation_matched':True,'current_response_bound':True}
        result['checks'].append('artificial login returns to the owned query-free entry; two stable reads and a third owner check submit the original visible keyword once without another document or login')
    finally:
        if b is not None:b.close()
        server.form_mode=''


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--controlled', action='store_true')
    parser.add_argument('--headed', action='store_true')
    parser.add_argument('--channel', choices=['msedge','chrome'])
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
                    ignored_rules=tuple(replace(r,host=API_HOST if r.key=='liepin_hotwords' else OPTIONAL_HOST) for r in contract.ignored_rules))
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
                    workspace = Workspace(root/'workspace')
                    service = GuidedService(workspace,registry=Registry([local]),
                        ledger=RateLedger(root/'rate.sqlite', Limits(page_interval=0,request_interval=0)), native_backend_factory=factory)
                    services.append(service)
                    query={'platform':'liepin','keyword':'时间序列','roles':['time_series'],'max_pages':2,'max_jobs':1,
                           'consent':True,'rights_note':'仅合成测试','backend':'native','native_consent':True,'diagnostics':True}
                    service.create(explicit_seed(query,local)); task=wait(service)
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
                    result['checks'].append('known marketing/statistics requests are aborted before network access without aborting search or report')
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
                    service.create(explicit_seed({**query,'keyword':'明确无结果'},local)); empty=wait(service)
                    assert empty['status']=='ready' and empty['code']=='no_matching_jobs' and not empty['cards'],empty.get('code')
                    assert workspace.report(previous_report)
                    result['checks'].append('valid empty response means no matches, not required login; previous report preserved')
                    assert all(r['anonymous'] and r['no_credentials'] and r['identified'] for r in server.requests)
                    assert not any('login' in r['path'] or 'apply' in r['path'] for r in server.requests)
                    result['checks'].append('anonymous read sends no login request or credentials; application identity retained')
                    # Explicit query-order opt-in must complete the same
                    # pipeline with no UI/manual collect action or login request.
                    service.create(explicit_seed({**query, 'auto_collect': True},local))
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
                    service.create(explicit_seed({**query, 'keyword': '明确无结果', 'auto_collect': True},local))
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
                    # The same native browser outlives search, manual collection
                    # and the explicit login action. Delayed stylesheet reads
                    # exercise real progress callbacks after login has returned.
                    manual_workspace = Workspace(root/'manual-progress-workspace')
                    manual_adapter = replace(login_adapter, login_url=URL+'/fixture-login')
                    manual = GuidedService(manual_workspace, registry=Registry([manual_adapter]),
                        ledger=RateLedger(root/'manual-progress-rate.sqlite', Limits(page_interval=0)),
                        native_backend_factory=factory)
                    services.append(manual)
                    try:
                        manual.create(explicit_seed({**query, 'max_pages':1},local))
                        manual_task = wait(manual)
                        assert manual_task['status']=='ready', manual_task.get('code')
                        selected = [manual_task['cards'][0]['id']]
                        manual.action({'id':manual_task['id'],'action':'collect','selected':selected})
                        manual_task = wait(manual)
                        assert manual_task['status']=='completed' and manual_task['outcome']['saved']==1
                        manual_report = manual_task['report_id']
                        before_login = len(server.requests)
                        server.login_late_pacing = True
                        manual.action({'id':manual_task['id'],'action':'login','auto_continue':False})
                        pending = wait(manual)
                        deadline = time.monotonic()+5
                        while sum('/css/pacing-' in r['path'] for r in server.requests[before_login:])<3 and time.monotonic()<deadline:
                            time.sleep(.05)
                        assert sum('/css/pacing-' in r['path'] for r in server.requests[before_login:])==3
                        pending = manual.state()['jobs'][0]
                        assert pending['status']=='waiting_manual', pending.get('code')
                        assert pending['code']=='manual_browser_open'
                        assert pending['authentication']=='manual_pending'
                        assert pending['selection']==selected and pending['report_id']==manual_report
                        assert pending['cards'][0]['status']=='ok' and pending['cards'][0]['record_id']
                        assert manual_workspace.report(manual_report)['manifest']['stats']['full_text_job_groups']==1
                        with Store(manual_workspace.db) as store:
                            assert len(store.records())==1 and store.records()[0].text==RECORDED_BODY
                        assert not any(r['method']=='POST' and r['path']=='/fixture-login' for r in server.requests[before_login:])
                        result['manual_login_pending_state'] = {k:pending[k] for k in ('status','code','authentication')}
                    finally:
                        server.login_late_pacing = False
                        manual.close()
                    result['checks'].append('default-paced search, separate selected JD/report and later login retain selection, saved card, report and manual_pending after three late stylesheet requests; no password or login POST')
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
                    verify_dependency_chain(root,server,local,factory,wait,query,result)
                    verify_published_binding(root,server,local,factory,wait,query,result)
                    verify_visible_forms(root, server, local, factory, wait, query, result)
                    verify_visible_login_return(root, server, local, factory, query, result)
                    result['success']=True
    finally:
        for service in services: service.close()
        result['requests']=server.requests if server else []
        (out/'liepin-search-results.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps(result,ensure_ascii=True))

if __name__ == '__main__': main()
