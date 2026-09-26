"""Real UI, isolated synthetic ledger and clock, no provider or account access."""
from contextlib import closing
from functools import partial
import argparse,json,sqlite3,sys,tempfile,threading,time
from pathlib import Path
from unittest.mock import Mock,patch
from urllib.parse import urlsplit
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'src'))
from vibe_job_radar import workbench
from vibe_job_radar.guided.rate import RateLedger
from vibe_job_radar.guided.service import GuidedService
from vibe_job_radar.workspace import Workspace


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--channel',choices=['msedge']);args=parser.parse_args()
    out=ROOT/'browser-acceptance/login-quota';out.mkdir(parents=True,exist_ok=True)
    result={'success':False,'checks':[],'external_requests':0,'mutation_requests':0}
    with tempfile.TemporaryDirectory(prefix='radar-quota-ui-') as tmp:
        now=[time.time()-1000];first=now[0];path=Path(tmp)/'rates.sqlite'
        ledger=RateLedger(path,clock=lambda:now[0])
        def rows():
            with closing(sqlite3.connect(path)) as conn:return list(conn.execute('SELECT site,kind,ts FROM visits ORDER BY ts'))
        for offset in (0,301,602):
            now[0]=first+offset;ledger.reserve('liepin','login')
        now[0]=first+903
        original=rows()
        with patch.object(workbench,'GuidedService',partial(GuidedService,ledger=ledger)):
            server=workbench.LocalServer(Workspace(Path(tmp)/'workspace'),port=0)
        server.guided._submit=Mock()
        ident=server.guided.create({'platform':'liepin','keyword':'synthetic quota UI','consent':True,'rights_note':'local synthetic fixture','max_pages':1,'max_jobs':1})['id']
        state=server.guided._load(ident);server.guided._save(state,'ready',status='ready')
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            from playwright.sync_api import sync_playwright,expect
            with sync_playwright() as runtime:
                browser=runtime.chromium.launch(headless=True,**({'channel':args.channel} if args.channel else {}))
                try:
                    context=browser.new_context(service_workers='block',viewport={'width':1100,'height':850})
                    parsed=urlsplit(server.entry_url);origin='http://'+parsed.netloc
                    def route(r):
                        if r.request.url.startswith(origin+'/'):
                            if r.request.method not in {'GET','HEAD'}:result['mutation_requests']+=1
                            r.continue_()
                        else:result['external_requests']+=1;r.abort()
                    context.route('**/*',route)
                    page=context.new_page();page_errors=[];page.on('pageerror',lambda e:page_errors.append(type(e).__name__))
                    page.goto(origin+'/guided?task='+ident+'#'+parsed.fragment)
                    page.locator('#password-login>summary').click()
                    expect(page.locator('#login-availability')).to_contain_text('滚动24小时')
                    expect(page.locator('#submit-password-login')).to_be_disabled()
                    assert '当前没有已打开的采集登录窗口' in page.locator('#login-availability').inner_text()
                    assert '下次允许时间' in page.locator('#login-availability').inner_text()
                    assert page.locator('#login').is_disabled()
                    assert page.locator('#password-login-form input[name=username]').is_disabled()
                    assert page.locator('#password-login-form input[name=password]').is_disabled()
                    assert page.locator('#password-login-form input[name=username]').input_value()==''
                    assert rows()==original
                    result['checks'].append('daily_limit_visible_before_account_input_without_spending')
                    page.locator('#login-availability').scroll_into_view_if_needed();page.screenshot(path=str(out/'daily-limit.png'))
                    now[0]=first+86400
                    expect(page.locator('#submit-password-login')).to_be_enabled(timeout=10000)
                    expect(page.locator('#login-availability')).to_be_hidden()
                    assert page.locator('#login').is_enabled()
                    assert page.locator('#password-login-form input[name=password]').is_enabled()
                    assert rows()==original and result['mutation_requests']==0
                    result['checks'].append('expiry_enables_explicit_action_without_automatic_login')
                    ledger.cool('liepin',900)
                    expect(page.locator('#login-availability')).to_contain_text('本站仍在等待期',timeout=10000)
                    expect(page.locator('#login')).to_be_disabled()
                    assert rows()==original
                    result['checks'].append('shared_cooldown_is_still_enforced')
                    page.set_viewport_size({'width':390,'height':850});page.locator('#login-availability').scroll_into_view_if_needed()
                    assert page.evaluate('document.documentElement.scrollWidth<=window.innerWidth')
                    page.screenshot(path=str(out/'daily-limit-mobile.png'))
                    result['checks'].append('mobile_status_is_readable_without_overflow')
                    assert not page_errors and result['external_requests']==0 and result['mutation_requests']==0
                    assert server.guided._submit.call_count==1
                    result.update(success=True,browser_version=browser.version,page_errors=page_errors,original_attempts=len(original))
                finally:browser.close()
        finally:
            server.shutdown();thread.join(5);server.server_close()
            (out/'results.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(result,ensure_ascii=False))

if __name__=='__main__':main()
