"""Explicit local-only native Chromium/Edge acceptance; no recruiting requests.

A fresh test CA is trusted only in an isolated Linux HOME, or temporarily in a
Windows ephemeral CI runner's machine store (removed in finally). This is a developer test,
not production certificate installation or ignore_https_errors. DNS/TCP mapping
exists only in unittest.mock for this local artificial source.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager, ExitStack
from datetime import datetime, timedelta, timezone
from dataclasses import fields, replace
import faulthandler
import gzip
import http.server
import json
import os
from pathlib import Path
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'src'),str(ROOT/'tests')]

from test_native_acquisition import adapter,HOST,URL,SECRET
from test_liepin_recorded_layout import markup as recorded_markup, posting as recorded_posting, BODY as RECORDED_BODY, TITLE as RECORDED_TITLE
from vibe_job_radar.guided.native_browser import NativeBackend
from vibe_job_radar.guided.rate import RateLedger,Limits
from vibe_job_radar.guided.service import GuidedService
from vibe_job_radar.guided.adapters import DOMAdapter, Registry
from vibe_job_radar.guided.liepin import LiepinAdapter
from vibe_job_radar.guided.native_policy import NativeRule
from vibe_job_radar.guided.contracts import CrawlError
from vibe_job_radar.store import Store
from vibe_job_radar.guided.diagnostic_trace import DiagnosticTrace
from vibe_job_radar.network_policy import NetworkPolicy,use_policy
from vibe_job_radar.workspace import Workspace


@contextmanager
def trust_fixture(root, extra_hosts=()):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes,serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID
    key=rsa.generate_private_key(public_exponent=65537,key_size=2048)
    name=x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,'VJR ephemeral native TEST ONLY')])
    now=datetime.now(timezone.utc)
    ca=(x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
        .serial_number(x509.random_serial_number()).not_valid_before(now-timedelta(days=1))
        .not_valid_after(now+timedelta(days=2)).add_extension(x509.BasicConstraints(ca=True,path_length=0),critical=True)
        .sign(key,hashes.SHA256()))
    def leaf(host,file):
        cert=(x509.CertificateBuilder().subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,host)]))
          .issuer_name(name).public_key(key.public_key()).serial_number(x509.random_serial_number())
          .not_valid_before(now-timedelta(days=1)).not_valid_after(now+timedelta(days=1))
          .add_extension(x509.BasicConstraints(ca=False,path_length=None),critical=True)
          .add_extension(x509.SubjectAlternativeName([x509.DNSName(h) for h in (host, *(extra_hosts if host == HOST else ()))]),critical=False)
          .sign(key,hashes.SHA256()))
        file.write_bytes(cert.public_bytes(serialization.Encoding.PEM)+key.private_bytes(
            serialization.Encoding.PEM,serialization.PrivateFormat.TraditionalOpenSSL,serialization.NoEncryption()))
    ca_path=root/'test-ca.pem';ca_path.write_bytes(ca.public_bytes(serialization.Encoding.PEM))
    leaf(HOST,root/'good.pem');leaf('wrong.fixture.test',root/'wrong.pem')
    fingerprint=ca.fingerprint(hashes.SHA1()).hex()
    if sys.platform=='win32':
        if not os.environ.get('CI') or os.environ.get('GITHUB_ACTIONS') != 'true':
            raise RuntimeError('Windows fixture trust is restricted to an explicit GitHub CI runner')
        # Use the Windows tool explicitly, not an NSS namesake on PATH. The
        # fixture is explicitly enabled on an ephemeral CI runner. The machine
        # store avoids an interactive user-root consent dialog in a headless
        # runner. Only our fresh random CA is added, then removed in finally.
        tool=str(Path(os.environ['SystemRoot'])/'System32'/'certutil.exe')
        try:
            subprocess.run([tool,'-f','-addstore','Root',str(ca_path)],
                check=True,capture_output=True,stdin=subprocess.DEVNULL,timeout=30)
            yield
        finally:
            subprocess.run([tool,'-delstore','Root',fingerprint],
                check=True,capture_output=True,stdin=subprocess.DEVNULL,timeout=30)
    elif sys.platform.startswith('linux'):
        home=root/'isolated-home';db=home/'.pki/nssdb';db.mkdir(parents=True)
        tool=shutil.which('certutil')
        if not tool:raise RuntimeError('developer NSS certutil is required for isolated test trust')
        subprocess.run([tool,'-N','-d','sql:'+str(db),'--empty-password'],check=True,capture_output=True)
        subprocess.run([tool,'-A','-d','sql:'+str(db),'-n','radar-ephemeral-fixture','-t','C,,','-i',str(ca_path)],check=True,capture_output=True)
        # Isolate browser trust without moving Playwright's installed binaries.
        cache=os.environ.get('PLAYWRIGHT_BROWSERS_PATH') or str(
            Path(os.environ.get('XDG_CACHE_HOME',str(Path.home()/'.cache')))/'ms-playwright')
        with patch.dict(os.environ,{'HOME':str(home),'PLAYWRIGHT_BROWSERS_PATH':cache}):yield
    else:
        raise RuntimeError('this native trust harness covers Linux/Windows only')


class Fixture:
    def __init__(self,root,certificate):
        self.requests=[];self.sni=[];owner=self
        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version='HTTP/1.1'
            def log_message(self,*_):pass
            def send(self,body,mime='text/html; charset=utf-8',status=200,extra=(),compressed=False):
                if isinstance(body,str):body=body.encode()
                if compressed:body=gzip.compress(body)
                self.send_response(status);self.send_header('Content-Type',mime)
                self.send_header('Content-Length',str(len(body)))
                if compressed:self.send_header('Content-Encoding','gzip')
                for k,v in extra:self.send_header(k,v)
                self.end_headers()
                try:self.wfile.write(body)
                except (OSError,ssl.SSLError):pass
            def record(self,body=b''):
                owner.requests.append({'method':self.command,'path':self.path.split('?')[0],
                    'cookie_ok':'fixture_session=valid' in self.headers.get('Cookie',''),
                    'proxy_secret_absent':not self.headers.get('Proxy-Authorization'),
                    'origin_secret_absent':not self.headers.get('Authorization'),
                    'agent_identified':'VibeJobRadar/0.1' in self.headers.get('User-Agent',''),
                    'body_intact':body==json.dumps({'q':SECRET},separators=(',',':')).encode() if body else True})
            def do_GET(self):
                self.record();path=self.path.split('?')[0]
                if path=='/robots.txt':self.send('User-agent: *\nAllow: /\n','text/plain')
                elif path=='/search':
                    self.send('<!doctype html><meta charset="utf-8"><link rel="stylesheet" href="/fixture.css"><h1>人工列表</h1><div id="jobs"></div><script src="/fixture.js"></script>',
                              extra=[('Set-Cookie','fixture_session=valid; Secure; HttpOnly; SameSite=Lax')])
                elif path=='/fixture.css':self.send('#jobs{padding-left:19px;--native-fixture:yes}','text/css',compressed=True)
                elif path=='/fixture.js':
                    js="fetch('/api/jobs',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({q:"+json.dumps(SECRET)+"})}).then(r=>r.json()).then(j=>{let a=document.createElement('a');a.href=j.jobs[0].url;a.textContent=j.jobs[0].title;document.querySelector('#jobs').append(a);});"
                    self.send(js,'application/javascript',compressed=True)
                elif path=='/recorded-search':
                    self.send('<!doctype html><a href="/job/123.shtml">'+RECORDED_TITLE+'</a>')
                elif path=='/job/123.shtml':
                    self.send(recorded_markup(recorded_posting(url=URL+'/job/123.shtml')))
                elif path=='/job/1':self.send('<h1>时间序列算法工程师</h1><div class="job-description">要求熟练使用 Cursor 进行 AI 辅助编程，编写单元测试与代码审查，负责时间序列预测系统。仅为本地人工测试，不是真实招聘信息。</div>')
                elif path=='/redirect':self.send('',status=302,extra=[('Location','/search')])
                elif path=='/cross':self.send('',status=302,extra=[('Location','https://outside.fixture.test/forbidden')])
                elif path=='/origin-auth':self.send('origin auth',status=401,extra=[('WWW-Authenticate','Basic realm="Radar local native session"')])
                elif path=='/denied':self.send('denied',status=403)
                elif path=='/limited':self.send('wait',status=429,extra=[('Retry-After','301')])
                elif path=='/unknown':self.send("<h1>人工</h1><script>fetch('/apply',{method:'POST',body:'forbidden'})</script>")
                elif path=='/login':self.send('<h1>人工登录</h1><form method="post" action="/login"><button>人工确认</button></form>')
                else:self.send('unknown',status=404)
            def do_POST(self):
                body=self.rfile.read(int(self.headers.get('Content-Length','0')));self.record(body)
                if self.path=='/api/jobs':
                    self.send(json.dumps({'jobs':[{'id':'1','url':'/job/1','title':'时间序列算法工程师'}],'has_more':False},ensure_ascii=False),'application/json',compressed=True)
                elif self.path=='/login':self.send('<h1>人工登录完成</h1>',extra=[('Set-Cookie','login_fixture=valid; Secure; HttpOnly')])
                else:self.send('unexpected write',status=405)
        context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);context.load_cert_chain(str(root/certificate))
        context.set_servername_callback(lambda sock,host,ctx:self.sni.append(host))
        class Server(http.server.ThreadingHTTPServer):
            daemon_threads=True
            def get_request(self):
                # Browser speculative CONNECTs must not block fixture cleanup
                # indefinitely while the server waits for a TLS ClientHello.
                sock,address=super().get_request();sock.settimeout(5)
                try:return context.wrap_socket(sock,server_side=True),address
                except Exception:
                    sock.close();raise
        self.server=Server(('127.0.0.1',0),Handler)
        self.thread=threading.Thread(target=self.server.serve_forever,kwargs={'poll_interval':.05},daemon=True);self.thread.start()
    def close(self):self.server.shutdown();self.server.server_close();self.thread.join(timeout=2)


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--controlled',action='store_true');parser.add_argument('--channel',choices=['msedge']);parser.add_argument('--executable');parser.add_argument('--headed',action='store_true')
    args=parser.parse_args()
    if not args.controlled:
        print('No requests. Use --controlled for explicit developer-only local fixture acceptance.');return
    out=ROOT/'browser-acceptance/native';out.mkdir(parents=True,exist_ok=True)
    result={'success':False,'scope':'Artificial local TLS source; native browser, real production controller/tunnel/service. Not live-site/VPN certification.','checks':[]}
    services=[];backends=[];fixtures=[]
    def checkpoint(stage):
        result['stage']=stage
        (out/'results.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
        print('Native fixture stage: '+stage,flush=True)
    def cleanup_resources():
        for service in services:service.close()
        services.clear()
        for b in backends:
            try:b.close()
            except Exception:pass
        backends.clear()
        for f in fixtures:f.close()
        fixtures.clear()
    faulthandler.dump_traceback_later(75,repeat=True)
    try:
        from playwright._impl._errors import TargetClosedError
        from vibe_job_radar.guided.native_errors import target_closed_by_driver
        result['driver_closed_type_supported'] = target_closed_by_driver(TargetClosedError())
        assert result['driver_closed_type_supported']
        assert not target_closed_by_driver(RuntimeError('Target page, context or browser has been closed'))
        with tempfile.TemporaryDirectory(prefix='vjr-native-fixture-') as d, ExitStack() as cleanup:
            cleanup.callback(cleanup_resources)
            root=Path(d)
            with trust_fixture(root):
                good=Fixture(root,'good.pem');bad=Fixture(root,'wrong.pem');fixtures.extend([good,bad])
                destination=[good.server.server_address]
                real_dns,real_dial=socket.getaddrinfo,socket.create_connection
                def dns(host,*a,**kw):
                    if host==HOST:return [(socket.AF_INET,socket.SOCK_STREAM,6,'',('93.184.216.34',443))]
                    if host in ('127.0.0.1','localhost','::1'):return real_dns(host,*a,**kw)
                    raise AssertionError('test attempted external DNS')
                def dial(address,*a,**kw):
                    if address==('93.184.216.34',443):return real_dial(destination[0],*a,**kw)
                    if address[0] in ('127.0.0.1','localhost','::1'):return real_dial(address,*a,**kw)
                    raise AssertionError('test attempted external network')
                options={'headless':not args.headed}
                if args.channel:options['channel']=args.channel
                if args.executable:options['executable_path']=args.executable
                class TestedBackend(NativeBackend):
                    def _load_robots(self):
                        try:return super()._load_robots()
                        except Exception as exc:
                            chain=[];e=exc
                            while e is not None and len(chain)<5:
                                chain.append(type(e).__name__+': '+str(e));e=e.__cause__
                            result['fixture_failure_chain']=chain
                            raise
                def factory(a,l,c,p,**saved):
                    with use_policy(NetworkPolicy()):b=TestedBackend(a,l,c,p,**options,**saved)
                    backends.append(b);return b
                def backend(name):
                    return factory(adapter(),RateLedger(root/(name+'.sqlite'),Limits(page_interval=0,request_interval=0)),threading.Event(),lambda *_:None)
                def wait(service):
                    deadline=time.monotonic()+50
                    while service.state()['busy']:
                        if time.monotonic()>deadline:raise TimeoutError('native controlled task did not finish')
                        time.sleep(.05)
                    return service.state()['jobs'][0]
                # This harness deliberately maps only public symbols to its own
                # local TLS fixture. Production has no loopback or CA override.
                with patch('socket.getaddrinfo',side_effect=dns),patch('socket.create_connection',side_effect=dial),patch.object(NetworkPolicy,'capture',return_value=NetworkPolicy()):
                    workspace=Workspace(root/'workspace')
                    service=GuidedService(workspace,registry=Registry([adapter()]),
                        ledger=RateLedger(root/'service.sqlite',Limits(page_interval=0,request_interval=0)),native_backend_factory=factory)
                    services.append(service)
                    query={'platform':'fixture','keyword':'时间序列算法工程师','roles':['time_series'],'max_pages':1,'max_jobs':1,
                           'consent':True,'rights_note':'人工上游测试','diagnostics':True,'backend':'native','native_consent':True,'persist_session':True}
                    checkpoint('service-search')
                    service.create(query);task=wait(service)
                    if task['status'] != 'ready':
                        result['fixture_requests'] = good.requests
                        result['fixture_sni'] = good.sni
                        result['initial_diagnostic'] = service.diagnostics({'id':task['id']})
                        result['native_error'] = backends[-1].error if backends else None
                        result['tunnel_error'] = backends[-1].tunnel.last_error if backends and backends[-1].tunnel else None
                    checkpoint('service-search-returned')
                    assert task['status']=='ready', {'code':task['code'],'message':task['message']}
                    assert len(task['cards'])==1
                    b=backends[-1]
                    # Browser APIs belong to the service worker; inspect only
                    # copied counters/trace here, not the Playwright Page.
                    assert b.native_counts['business']==1
                    assert any(r['path']=='/api/jobs' and r['method']=='POST' and r['cookie_ok'] and r['body_intact'] for r in good.requests)
                    assert all(r['proxy_secret_absent'] and r['agent_identified'] for r in good.requests)
                    result['checks'].append('GuidedService selected native; real browser POST, HttpOnly cookie, gzip JS/CSS/JSON and list rendering reached source without HTTP replay')
                    checkpoint('service-collect')
                    service.action({'id':task['id'],'action':'collect','selected':[task['cards'][0]['id']]});task=wait(service)
                    assert task['status']=='completed' and task['outcome']['saved']==1,task.get('code')
                    report=workspace.report(task['report_id']);assert report['manifest']['stats']['full_text_job_groups']==1
                    diagnostic=service.diagnostics({'id':task['id']});assert SECRET not in json.dumps(diagnostic)
                    assert diagnostic['observer_errors']==0
                    (out/'diagnostic.json').write_text(json.dumps(diagnostic,ensure_ascii=False,indent=2),encoding='utf-8')
                    result['checks'].append('same batch detail persisted and existing report generated; opt-in trace has no query/body/cookie secret')
                    service.close();services.remove(service)
                    checkpoint('native-session-restore')
                    previous_report = task['report_id']
                    initial_count = len(good.requests)
                    service=GuidedService(workspace,registry=Registry([adapter()]),
                        ledger=RateLedger(root/'service.sqlite',Limits(page_interval=0,request_interval=0)),native_backend_factory=factory)
                    services.append(service)
                    service.create(query); task=wait(service)
                    assert task['status']=='ready' and task['authentication']=='restored_session_unverified',task.get('code')
                    documents=[r for r in good.requests[initial_count:] if r['path']=='/search']
                    assert documents and documents[0]['cookie_ok'], 'restored cookie missing on first native document'
                    assert task['saved_session_status']=='saved_unverified'
                    service.action({'id':task['id'],'action':'collect','selected':[task['cards'][0]['id']]});task=wait(service)
                    assert task['status']=='completed' and task['outcome']['saved']==1
                    assert workspace.report(previous_report)
                    assert task['report_id'] != previous_report
                    result['checks'].append('opt-in disk snapshot restored before first document in a fresh native browser/service; complete selected JD reaches a new report with old report retained')
                    service.action({'id':task['id'],'action':'forget_session','confirm':True}); task=wait(service)
                    assert task['saved_session_status']=='cleared'
                    assert not (workspace.root/'.radar-sessions/fixture.json').exists()
                    assert not service._backends and not service._session_leases
                    service.close();services.remove(service)
                    checkpoint('native-recorded-liepin-layout')
                    # This artificial origin uses the published representation,
                    # never a real recruiting URL or copied third-party JD. Use
                    # the actual Liepin parser and original service/store/report.
                    template = adapter()
                    layout_contract = replace(template.native_contract,
                        rules=template.native_contract.rules + (
                            NativeRule('recorded_documents', HOST,
                                r'/(?:recorded-search|job/123\.shtml)',
                                resources=('Document',), role='document'),))
                    layout_adapter = LiepinAdapter(**{
                        **{field.name:getattr(template, field.name) for field in fields(DOMAdapter)},
                        'search_base':URL+'/recorded-search',
                        'detail_pattern':r'^/job/[0-9]+\.shtml$',
                        'native_contract':layout_contract})
                    layout_workspace = Workspace(root/'recorded-layout-workspace')
                    service = GuidedService(layout_workspace, registry=Registry([layout_adapter]),
                        ledger=RateLedger(root/'recorded-layout.sqlite',Limits(page_interval=0,request_interval=0)),
                        native_backend_factory=factory)
                    services.append(service)
                    service.create({**query, 'persist_session':False}); task=wait(service)
                    assert task['status']=='ready' and len(task['cards'])==1, task.get('code')
                    service.action({'id':task['id'],'action':'collect','selected':[task['cards'][0]['id']]})
                    task=wait(service)
                    assert task['status']=='completed' and task['outcome']['saved']==1, task.get('code')
                    layout_report=layout_workspace.report(task['report_id'])
                    assert layout_report['manifest']['stats']['full_text_job_groups']==1
                    with Store(layout_workspace.db) as store:
                        records=store.records()
                        assert len(records)==1 and records[0].title==RECORDED_TITLE
                        assert records[0].text==RECORDED_BODY, 'native JD changed or mixed with recommendations'
                    result['checks'].append('actual Liepin parser reads artificial no-h1/raw-newline JSON-LD/dd layout through native search, selected full JD, Store and original report; not a live-site claim')
                    service.close();services.remove(service)
                    checkpoint('native-redirect')
                    b=backend('redirect');b.open(URL+'/redirect');assert b.page.url==URL+'/search'
                    assert b.native_counts['document']==2
                    assert b.page.locator('#jobs').evaluate("el=>getComputedStyle(el).getPropertyValue('--native-fixture').trim()")=='yes'
                    # Headed CI may leave the new tab behind the retired
                    # scratch window. Activate and await an actual paint before
                    # capture; keep screenshot errors fatal, without retries.
                    b.page.bring_to_front()
                    b.page.wait_for_function("document.visibilityState === 'visible'")
                    b.page.evaluate('() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))')
                    b.page.screenshot(path=str(out/'native-rendered.png'))
                    result['checks'].append('same-origin 302 remains browser-native; both document hops counted; gzip stylesheet computed style verified')
                    b.close();backends.remove(b)
                    checkpoint('native-login')
                    b=backend('login');b.open(URL+'/login',authentication=True);b.page.get_by_role('button',name='人工确认').click();b.page.wait_for_timeout(100)
                    assert not b.error,b.error
                    assert any(r['path']=='/login' and r['method']=='POST' for r in good.requests)
                    result['checks'].append('reviewed native login POST executes only in explicit authentication mode')
                    checkpoint('negative-popup')
                    popup_opened = b.page.evaluate("() => {const opened=!!window.open('/apply'); window.open('/apply', '_blank', 'noopener'); return opened;}")
                    deadline = time.monotonic() + 5
                    while time.monotonic() < deadline:
                        # CDP target events can arrive after evaluate returns.
                        # Await the refusal while the owner drains both popups.
                        try:
                            b.pump()
                        except CrawlError as exc:
                            assert exc.code == 'native_surface_unsupported', exc.code
                        if (b.error and len(b.context.pages) == 1 and not b._rejected_pages
                                and not b._pending_rejected_targets):
                            break
                    # Chromium may refuse the popup before creating any CDP
                    # target. Otherwise the controller must report its refusal.
                    assert b.error == 'native_surface_unsupported' or (b.error is None and not popup_opened), b.error
                    assert len(b.context.pages)==1, 'uncontrolled popup escaped the owned-page boundary'
                    assert not any(r['path']=='/apply' for r in good.requests)
                    result['popup_refusal'] = b.error or 'browser_open_returned_null'
                    result['popup_abort_observations'] = dict(b.__dict__.get('_ownership_abort_counts', {}))
                    result['checks'].append('script popups, including noopener, do not create uncontrolled requests')
                    b.close();backends.remove(b)
                    for name,path,expected in [('cross','/cross','redirect_requires_attention'),('unknown','/unknown','native_operation_unreviewed'),('origin-auth','/origin-auth','http_401'),('denied','/denied','http_403'),('limited','/limited','http_429')]:
                        checkpoint('negative-'+name)
                        b=backend(name)
                        try:b.open(URL+path)
                        except Exception as exc:
                            assert getattr(exc,'code',None)==expected,(name,type(exc).__name__,getattr(exc,'code',None),b.error)
                        else:raise AssertionError(name+' was not stopped')
                        b.close();backends.remove(b);result['checks'].append(name+' stops with '+expected)
                    assert not any(r['path']=='/apply' for r in good.requests)
                    checkpoint('negative-proxy-auth')
                    before=len(good.requests)
                    b=backend('bad-proxy-auth')
                    b.tunnel.password += '-wrong-fixture-only'
                    try:b.open(URL+'/search')
                    except Exception as exc:
                        assert getattr(exc,'code',None)=='native_proxy_auth_failed', getattr(exc,'code',None)
                    else:raise AssertionError('wrong local proxy credentials accepted')
                    assert len(good.requests)==before, 'wrong proxy secret reached the TLS source'
                    b.close();backends.remove(b)
                    result['checks'].append('incorrect proxy credentials stop with exact local error before target HTTP')
                    checkpoint('negative-certificate')
                    destination[0]=bad.server.server_address
                    b=backend('bad-cert')
                    try:b.open(URL+'/search')
                    except Exception as exc:
                        result['bad_certificate_code']=getattr(exc,'code',type(exc).__name__)
                        assert result['bad_certificate_code']=='tls_verification_failed', result['bad_certificate_code']
                    else:raise AssertionError('wrong-host certificate accepted')
                    assert not bad.requests,'browser sent HTTP despite wrong-host certificate'
                    b.close();backends.remove(b)
                    result['checks'].append('browser itself rejects wrong-host certificate before any HTTP; no ignore_https_errors')
                    result['requests']=good.requests
                    assert all(r['agent_identified'] and r['proxy_secret_absent'] and r['origin_secret_absent'] for r in good.requests), 'all redirects and operations must preserve application identity without proxy secrets'
                    assert all(host==HOST for host in good.sni+bad.sni)
                    result['success']=True
                    checkpoint('passed')
    except Exception as exc:
        # Artificial test data only; production trace never prints exceptions.
        result['error_type']=type(exc).__name__;result['error']=str(exc)[:1500]
        raise
    finally:
        cleanup_resources()
        faulthandler.cancel_dump_traceback_later()
        (out/'results.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps(result,ensure_ascii=True))


if __name__=='__main__':main()
