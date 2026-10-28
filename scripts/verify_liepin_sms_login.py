"""Real browser, synthetic publisher: SMS handoff -> original JD/report -> reuse.

All HTTP is fulfilled by the fixture. No recruitment site, phone, SMS service,
real CAPTCHA or user account is contacted. Test actors operate fixture controls.
"""
import argparse
import json
from pathlib import Path
import queue
import sys
import tempfile
import time
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'src'), str(ROOT / 'tests')]
from vibe_job_radar.guided.adapters import builtins
from vibe_job_radar.guided.browser import PlaywrightBackend
from vibe_job_radar.guided.contracts import CrawlError
from vibe_job_radar.guided.rate import Limits, RateLedger
from vibe_job_radar.guided.service import GuidedService
from vibe_job_radar.guided.transport import WireResponse
from vibe_job_radar.store import Store
from vibe_job_radar.workspace import Workspace
from test_liepin_recorded_layout import markup, BODY, URL
from test_liepin_native_search import payload

ENTRY = 'https://www.liepin.com/zhaopin/'
LOGIN = 'https://www.liepin.com/login?backUrl=https%3A%2F%2Fwww.liepin.com%2Fzhaopin%2F'
PASSPORT = 'https://api-passport.liepin.com/api/com.liepin.passport.'
SEARCH = 'https://api-c.liepin.com/api/com.liepin.searchfront4c.pc-search-job'
FRAME = 'https://turing.captcha.qcloud.com/template/drag_ele.html'
VERIFY = 'https://turing.captcha.qcloud.com/cap_union_new_verify'


def entry(logged):
    controls = ('<div id="header-quick-menu-user-info">Synthetic account</div>' if logged else '''
    <div id="login-panel"><p>登录后查看职位</p><button id="header-quick-menu-login">登录</button>
    <input data-nick="login-smscode" placeholder="Synthetic SMS code"><button id="send">Synthetic send</button>
    <button id="login-submit">Synthetic login</button></div>''')
    script = '''
    const passport = PASSPORT, search = SEARCH;
    const post = (url, body={}) => fetch(url, {method:'POST', credentials:'include',
      headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)}).then(r=>r.json());
    const send = document.querySelector('#send');
    if(send) send.onclick=async()=>{
      await post(passport+'captcha.get-smart-captcha');
      const iframe=document.createElement('iframe'); iframe.src=FRAME; document.body.append(iframe);
    };
    window.addEventListener('message',async e=>{
      if(e.origin!=='https://turing.captcha.qcloud.com'||e.data!=='synthetic-human-complete')return;
      document.querySelector('iframe').remove(); await post(passport+'captcha.send-sms');
      document.body.dataset.sent='yes';
    });
    const login=document.querySelector('#login-submit');
    if(login) login.onclick=async()=>{
      await post(passport+'account.tel-sms-login'); location.href='/zhaopin/';
    };
    document.querySelector('#query').onkeydown=async e=>{
      if(e.key!=='Enter')return; e.preventDefault();
      const key=e.target.value; history.pushState(null,'','?key='+encodeURIComponent(key));
      const result=await post(search,{data:{mainSearchPcConditionForm:{key,currentPage:0,pageSize:40}}});
      for(const item of result.data.data.jobCardList){const a=document.createElement('a');
        a.href=item.job.link; a.textContent=item.job.title; document.body.append(a);}
    };
    '''.replace('PASSPORT', json.dumps(PASSPORT)).replace('SEARCH', json.dumps(SEARCH)).replace('FRAME', json.dumps(FRAME))
    return '<html><body>'+controls+'<input id="query" type="text" placeholder="搜索职位、公司"><script>'+script+'</script></body></html>'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--channel', choices=['msedge', 'chrome'])
    parser.add_argument('--out', type=Path, default=ROOT / 'browser-acceptance' / 'liepin-sms-login')
    args = parser.parse_args()
    out = args.out
    out.mkdir(parents=True, exist_ok=True)
    # Keep the isolated workspace as reproducible evidence; do not let platform
    # temp-directory cleanup errors turn a successful scenario into false output.
    workspace = Workspace(Path(tempfile.mkdtemp(prefix='sms-fixture-', dir=out)))
    commands, calls = queue.Queue(), []
    account = {'valid': True}
    result = {'success': False, 'scope': 'Real browser and production bridge/service; synthetic HTTP and accounts only.', 'checks': []}
    services = []

    class Wire:
        def __init__(self, adapter, ledger, cancelled, progress):
            self.ledger, self.cancelled, self.blocked = ledger, cancelled, set()
        def allowed_resource(self, url):
            return urlsplit(url).hostname in {'www.liepin.com', 'api-c.liepin.com',
                'api-passport.liepin.com', 'turing.captcha.qcloud.com'}
        def reserve(self, kind):
            if self.cancelled.is_set(): raise CrawlError('paused')
            self.ledger.reserve('liepin', kind)
        def ensure_robots(self, url):
            if urlsplit(url).hostname == 'www.liepin.com' and urlsplit(url).query:
                raise CrawlError('robots_denied')
        def fetch(self, url, method='GET', headers=None, body=None, *, required=True):
            self.reserve('request')
            calls.append((method, urlsplit(url).hostname, urlsplit(url).path))
            logged = account['valid'] and 'synthetic_session=fixture' in (headers or {}).get('cookie', '')
            cors = {'access-control-allow-origin': (headers or {}).get('origin', 'https://www.liepin.com'),
                    'access-control-allow-credentials': 'true', 'access-control-allow-methods': 'POST',
                    'access-control-allow-headers': 'content-type,x-requested-with'}
            if method == 'OPTIONS': return WireResponse(204, cors, b'')
            cookies, content_type = (), 'application/json'
            if url.startswith(PASSPORT):
                response = '{}'
                if url.endswith('account.tel-sms-login'):
                    cookies = ('synthetic_session=fixture; Domain=.liepin.com; Path=/; Secure; HttpOnly; SameSite=Lax',)
            elif url == VERIFY:
                response = '{}'
            elif url == FRAME:
                content_type = 'text/html; charset=utf-8'
                response = '<button id="human">Synthetic human verification</button><script>document.querySelector("#human").onclick=async()=>{await fetch('+json.dumps(VERIFY)+', {method:"POST",body:"fixture"});parent.postMessage("synthetic-human-complete","https://www.liepin.com");}</script>'
            elif url in (ENTRY, LOGIN):
                content_type, response = 'text/html; charset=utf-8', entry(logged)
            elif url == SEARCH:
                response = json.dumps(payload())
            elif url == URL and logged:
                content_type, response = 'text/html; charset=utf-8', markup(raw_breaks=False)
            else:
                raise CrawlError('fixture_unexpected_request')
            return WireResponse(200, {**cors, 'content-type': content_type}, response.encode(), cookies)

    class Actor(PlaywrightBackend):
        def pump(self):
            try: action = commands.get_nowait()
            except queue.Empty: action = None
            if action == 'sms': self.page.locator('#send').click()
            elif action == 'captcha': self.page.frame_locator('iframe').locator('#human').click()
            elif action == 'login':
                self.page.locator('[data-nick="login-smscode"]').fill('synthetic-code')
                self.page.locator('#login-submit').click()
            super().pump()

    def service():
        ledger = RateLedger(workspace.root / 'guided' / 'rates.sqlite',
                            Limits(page_interval=0, request_interval=0, login_interval=0))
        def factory(a, l, c, p, **options):
            options['channel'] = args.channel
            return Actor(a, l, c, p, headless=True, transport_factory=Wire, **options)
        value = GuidedService(workspace, ledger=ledger, backend_factory=factory)
        value._login_return.interval = .05
        services.append(value)
        return value

    def create(value):
        return value.create({'platform':'liepin', 'keyword':'时间序列算法工程师', 'max_pages':1,
            'max_jobs':1, 'consent':True, 'rights_note':'Synthetic offline acceptance only',
            'auto_collect':True, 'persist_session':True, 'diagnostics':True})['id']

    def wait(value, ident, predicate):
        deadline = time.monotonic() + 40
        while time.monotonic() < deadline:
            view = value.state(); state = next(x for x in view['jobs'] if x['id'] == ident)
            if not view['busy'] and predicate(state): return state
            time.sleep(.05)
        result['last_state'] = {k:state.get(k) for k in ('status','code','login_continuation','saved_session_status')}
        raise AssertionError('fixture did not reach expected state')

    def posts(suffix): return sum(m=='POST' and path.endswith(suffix) for m, _, path in calls)

    try:
        first = service(); ident = create(first)
        wait(first, ident, lambda s:s['status']=='waiting_manual')
        first.action({'id':ident,'action':'login','auto_continue':True})
        wait(first, ident, lambda s:s['code']=='login_sms_required')
        assert ('GET', 'www.liepin.com', '/login') in calls
        deadline = first._login_return._watches[ident].expires
        commands.put('sms')
        wait(first, ident, lambda s:s['code']=='login_captcha_required')
        assert first._login_return._watches[ident].expires == deadline
        commands.put('captcha')
        wait(first, ident, lambda s:s['code']=='login_sms_required')
        assert first._login_return._watches[ident].expires == deadline
        commands.put('login')
        done = wait(first, ident, lambda s:s['status']=='completed')
        assert done['id']==ident and done['report_id'] and done['outcome']['saved']==1
        assert done['login_continuation']=='resumed_search'
        assert done['saved_session_status']=='saved_unverified'
        with Store(workspace.db) as store: assert store.records()[0].text == BODY
        assert posts('send-sms')==posts('tel-sms-login')==posts('cap_union_new_verify')==1
        result['checks'].append('SMS -> human widget -> SMS -> original query -> complete JD and report, one submission each')
        result['checks'].append('human steps keep one task and original watcher deadline; actual query document GET remains denied')
        first.close()
        second = service(); next_id = create(second)
        resumed = wait(second, next_id, lambda s:s['status']=='completed')
        assert resumed['report_id'] and posts('tel-sms-login')==1
        result['checks'].append('new service/browser restores saved Cookie before first page and produces report without another login')
        second.close()
        account['valid'] = False
        third = service(); expired_id = create(third)
        expired = wait(third, expired_id, lambda s:s['status']=='waiting_manual')
        assert expired['code']=='manual_required' and not expired['report_id']
        assert posts('send-sms')==posts('tel-sms-login')==1
        result['checks'].append('publisher-expired session returns to manual login without sending SMS, retrying login or inventing a report')
        result['success'] = True
    except Exception as error:
        result['success'], result['error_type'] = False, type(error).__name__
    finally:
        for value in services: value.close()
        result['login_posts'], result['sms_posts'] = posts('tel-sms-login'), posts('send-sms')
        (out/'synthetic-sms-results.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result['success'] else 1


if __name__ == '__main__': raise SystemExit(main())
