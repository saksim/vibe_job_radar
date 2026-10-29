"""Local-only duplicate submit while the publisher's post-login read is pending."""
import argparse
import json
from pathlib import Path
import tempfile
import threading
import time

import verify_native_login_landing as fixture


class TransitionBackend(fixture.LandingBackend):
    login_html = '''<!doctype html><meta charset="utf-8"><body>
<h1>LOCAL TRANSITION FIXTURE ONLY</h1><input data-nick="login-smscode">
<p id="result"></p><button id="submit">Synthetic login</button>
<script>document.querySelector('#submit').onclick=async()=>{
 const headers={'Content-Type':'application/json','X-Client-Type':'web'};
 const submit=()=>fetch('''+json.dumps(fixture.LOGIN)+''',
 {method:'POST',headers,body:'{}'}).then(r=>r.json());
 const first=await submit();
 if(first.flag===1) fetch('''+json.dumps(fixture.FLAG)+''',
 {headers:{'X-Client-Type':'web'}}).then(r=>r.json()).then(()=>location.assign('''+json.dumps(fixture.ROOT)+'''));
 setTimeout(()=>submit().then(second=>{
  if(second.flag!==1) document.querySelector('#result').textContent=second.msg;
  document.body.dataset.second='replied';
 }).catch(()=>document.body.dataset.second='cancelled'),200);
};</script></body>'''

    def __init__(self, *args, **kwargs):
        self.login_posts = 0
        self.held_flag = None
        super().__init__(*args, **kwargs)

    def fixture_content(self, url, method):
        if url == fixture.LOGIN and method == 'POST':
            self.login_posts += 1
            payload = ({'flag':1, 'data':{'fixture':True}} if self.login_posts == 1
                       else {'flag':0, 'msg':'验证码错误或过期'})
            return json.dumps(payload, ensure_ascii=False), 'application/json'
        return super().fixture_content(url, method)

    def _send(self, session, method, params=None, callback=None):
        if method == 'Fetch.continueRequest':
            event = self.fixture_requests.get((session, params['requestId']))
            if event and event['request']['url'] == fixture.FLAG and event['request']['method'] == 'GET':
                self.held_flag = (session, params, callback)
                return  # Fixture response only; nothing is released to the network.
        return super()._send(session, method, params, callback)

    def release_flag(self):
        session, params, callback = self.held_flag
        self.held_flag = None
        super()._send(session, 'Fetch.continueRequest', params, callback)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--controlled', action='store_true', required=True)
    parser.add_argument('--channel', choices=['msedge','chrome'], default='msedge')
    parser.add_argument('--expect-unguarded', action='store_true')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    result = {'controlled_only':True, 'real_login_verified':False,
              'expect_unguarded':args.expect_unguarded}
    with tempfile.TemporaryDirectory() as folder:
        b = TransitionBackend(fixture.builtins().get('liepin'),
            fixture.RateLedger(Path(folder)/'rates.sqlite', fixture.Limits(page_interval=0,request_interval=0)),
            threading.Event(), headless=True, channel=args.channel, human_login=True)
        try:
            b.start_manual_login()
            try: b.open(fixture.MAIN, authentication=True)
            except fixture.CrawlError as exc:
                if exc.code not in fixture.MANUAL_LOGIN_CODES: raise
            b.page.locator('#submit').click(timeout=5000)
            stages = []
            until = time.monotonic()+12
            while time.monotonic()<until:
                b.page.wait_for_timeout(50)
                if b.error: break
                progress = b.manual_login_status()
                if not stages or stages[-1] != progress['stage']: stages.append(progress['stage'])
                second = b.page.evaluate('document.body.dataset.second || ""')
                if second and b.held_flag:
                    result['second_result'] = second
                    b.release_flag()
                if fixture.peek(b): break
            ticket = fixture.peek(b)
            result.update(browser_version=b.browser.version, login_posts=b.login_posts,
                stages=stages, return_ready=ticket is not None,
                backend_error=b.error, account_documents_delivered=b.delivered.count(fixture.ROOT),
                manual_login=b._manual_login_progress.snapshot())
            if args.expect_unguarded:
                assert b.login_posts == 2 and 'code_rejected' in stages
                assert ticket is None and b.error == 'login_return_unverified'
            else:
                assert b.login_posts == 1 and result['second_result'] == 'cancelled'
                assert 'code_rejected' not in stages and ticket is not None and not b.error
                fixture.claim(b,ticket); b.collection_mode(); b.open(fixture.ENTRY)
                result['original_entry_reached'] = b.page.url == fixture.ENTRY
                assert result['original_entry_reached'] and not b.error
            assert fixture.ROOT not in b.delivered
            result['expected_behavior_observed'] = True
        except Exception as exc:
            result.update(exception=type(exc).__name__, error_code=getattr(exc,'code',None))
        finally:
            result['trace'] = b._diagnostics.snapshot()
            try: b.close(); result['closed'] = True
            except Exception as exc: result['close_error'] = type(exc).__name__
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({k:v for k,v in result.items() if k!='trace'},ensure_ascii=False))
    return 0 if result.get('expected_behavior_observed') and result.get('closed') else 1


if __name__ == '__main__':
    raise SystemExit(main())
