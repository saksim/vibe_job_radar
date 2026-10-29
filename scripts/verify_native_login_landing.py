"""Controlled Edge login-return exercise; no publisher egress or real account."""
import argparse
import base64
import json
from pathlib import Path
import tempfile
import threading
import time
from urllib.parse import urlsplit

from verify_native_human_login import ControlledBackend
from vibe_job_radar.guided.adapters import builtins
from vibe_job_radar.guided.contracts import CrawlError
from vibe_job_radar.guided.diagnostic_trace import DiagnosticTrace
from vibe_job_radar.guided.login_landing import peek, claim
from vibe_job_radar.guided.login_surface import MANUAL_LOGIN_CODES
from vibe_job_radar.guided.native_browser import NativeBackend
from vibe_job_radar.guided.native_documents import document_response_params
from vibe_job_radar.guided.rate import RateLedger, Limits

MAIN = 'https://www.liepin.com/login'
ENTRY = 'https://www.liepin.com/zhaopin/'
ROOT = 'https://c.liepin.com/'
LOGIN = 'https://api-passport.liepin.com/api/com.liepin.passport.account.tel-sms-login'
FLAG = 'https://api-c.liepin.com/api/com.liepin.cresume.resume-complete-flag'
HEADER = 'https://api-c.liepin.com/api/com.liepin.usercx.pc.user.base-property'
HTML = '''<!doctype html><meta charset="utf-8"><body><h1>LOCAL FIXTURE ONLY</h1>
<input data-nick="login-smscode"><button id="submit">Synthetic return</button>
<script>document.querySelector('#submit').onclick=async()=>{
 const headers={'Content-Type':'application/json','X-Client-Type':'web'};
 await fetch('''+json.dumps(LOGIN)+''',{method:'POST',headers,body:'{}'}).then(r=>r.json());
 await fetch('''+json.dumps(FLAG)+''',{headers:{'X-Client-Type':'web'}}).then(r=>r.json());
 location.assign('''+json.dumps(ROOT)+''');};</script></body>'''
SEARCH = '''<!doctype html><meta charset="utf-8"><body><h1>LOCAL ORIGINAL SEARCH ENTRY</h1>
<input type="text" placeholder="搜索职位、公司"><script>
fetch('''+json.dumps(HEADER)+''',{method:'POST',headers:{'Content-Type':'application/json','X-Client-Type':'web'},body:'{}'});
</script></body>'''


class LandingBackend(ControlledBackend):
    def __init__(self, *args, **kwargs):
        self.fixture_replies = {}
        super().__init__(*args, **kwargs)
        self._diagnostics = DiagnosticTrace('0'*32, 'liepin', browser=kwargs['channel'])

    def _load_robots(self):
        for host in self.contract.hosts:
            body = b'User-agent: *\nDisallow: /\n' if host=='c.liepin.com' else b'User-agent: *\nAllow: /\n'
            self.wire.install_robots('https://'+host,200,'text/plain',body)

    def _send(self, session, method, params=None, callback=None):
        key = (session, (params or {}).get('requestId'))
        reply = self.fixture_replies.get(key)
        if reply and method == 'Fetch.getResponseBody':
            return callback({'body': reply['body'], 'base64Encoded': True})
        if reply and method == 'Fetch.continueResponse':
            self.fixture_replies.pop(key)
            return NativeBackend._send(self, session, 'Fetch.fulfillRequest', reply, callback)
        if method == 'Fetch.fulfillRequest':
            self.fixture_replies.pop(key, None)
        if method != 'Fetch.continueRequest':
            return NativeBackend._send(self,session,method,params,callback)
        event = self.fixture_requests.pop((session,params['requestId']))
        request = event['request']; url = request['url']
        if url==MAIN: content,mime=HTML,'text/html'
        elif url==ENTRY: content,mime=SEARCH,'text/html'
        elif url in (LOGIN,FLAG,HEADER): content,mime='{"fixture":true,"data":{"intercepted":false}}','application/json'
        elif urlsplit(url).path=='/favicon.ico': content,mime='','image/x-icon'
        else: raise AssertionError('Unexpected controlled request')
        headers = [{'name':'Content-Type','value':mime},
            {'name':'Access-Control-Allow-Origin','value':'https://www.liepin.com'},
            {'name':'Access-Control-Allow-Headers','value':'content-type,x-client-type'},
            {'name':'Access-Control-Allow-Methods','value':'GET,POST,OPTIONS'}]
        response_event={**event,'responseStatusCode':200,'responseStatusText':'OK','responseHeaders':headers}
        response=document_response_params(response_event,enabled=True)
        response.setdefault('responseCode',200); response.setdefault('responseHeaders',headers)
        response['body']=base64.b64encode(content.encode()).decode()
        self.delivered.append(url)
        # Every admitted fixture request is fulfilled locally; none can reach
        # the tunnel. A missing response raises rather than using the network.
        # Fulfillment at Request stage does not emit a Response-stage pause.
        # Feed the synthetic response through the normal response checks first;
        # loadingFinished is still emitted by the actual browser, not this test.
        self.fixture_replies[key] = response
        return self._response_paused(session, response_event)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--controlled',action='store_true',required=True)
    parser.add_argument('--channel',choices=['msedge','chrome'],default='msedge')
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    result={'controlled_only':True,'real_login_verified':False}
    with tempfile.TemporaryDirectory() as folder:
        b=LandingBackend(builtins().get('liepin'),
            RateLedger(Path(folder)/'rates.sqlite',Limits(page_interval=0,request_interval=0)),
            threading.Event(),headless=True,channel=args.channel,human_login=True)
        try:
            b.start_manual_login()
            try: b.open(MAIN,authentication=True)
            except CrawlError as exc:
                if exc.code not in MANUAL_LOGIN_CODES: raise
            b.page.locator('#submit').click(timeout=5000)
            deadline=time.monotonic()+12
            ticket=None
            while time.monotonic()<deadline:
                b.page.wait_for_timeout(50)
                ticket=peek(b)
                if ticket is not None or b.error: break
            if ticket is None: raise AssertionError('No owned return ticket: '+str(b.error))
            result.update(browser_version=b.browser.version,return_ready=True,
                main_document_preserved=b.page.url==MAIN,
                login_submissions=b._manual_login_progress.submits,
                account_documents_delivered=b.delivered.count(ROOT))
            claim(b,ticket); b.collection_mode()
            before = b.delivered.count(LOGIN)
            # The old login document can issue another preflight while the
            # task departs. It must be canceled, never admitted or fatal.
            b.page.evaluate("void fetch("+json.dumps(LOGIN)+",{method:'POST',headers:{'Content-Type':'application/json','X-Client-Type':'web'},body:'{}'}).catch(()=>{})")
            deadline=time.monotonic()+5
            while time.monotonic()<deadline:
                b.page.wait_for_timeout(50)
                cancelled=[e for e in b._diagnostics.snapshot()['events']
                           if e.get('code')=='login_departure_request_cancelled']
                if cancelled or b.error: break
            result['late_login_cancelled']=bool(cancelled)
            assert cancelled and b.delivered.count(LOGIN)==before and not b.error
            b.open(ENTRY)
            deadline=time.monotonic()+5
            while HEADER not in b.delivered and time.monotonic()<deadline:
                b.page.wait_for_timeout(50)
            result.update(original_entry_reached=b.page.url==ENTRY,
                header_read_completed=HEADER in b.delivered,backend_error=b.error)
            assert result['main_document_preserved'] and result['original_entry_reached']
            assert result['header_read_completed'] and not b.error and ROOT not in b.delivered
        except Exception as exc:
            result.update(exception=type(exc).__name__, backend_error=b.error,
                          error_code=getattr(exc, 'code', None),
                          page_url=b.page.url if b.page else None)
        finally:
            result.update(trace=b._diagnostics.snapshot(), delivered=b.delivered,
                          manual_login=b._manual_login_progress.snapshot())
            try:
                b.close()
                result['closed'] = True
            except Exception as exc:
                result['close_error'] = type(exc).__name__
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({k:v for k,v in result.items() if k not in {'trace','delivered'}},ensure_ascii=False))
    return 0 if result.get('original_entry_reached') and result.get('closed') and not result.get('exception') else 1


if __name__=='__main__': raise SystemExit(main())
