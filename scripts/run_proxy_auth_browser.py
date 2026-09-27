"""Actual Chromium -> bridge -> authenticated proxy -> verified artificial TLS.

Uses a per-client fixture CA; never modifies OS/browser trust or user profiles.
The native guard's separate credentials and encrypted tunnel have real socket
tests in test_proxy_auth. This script exercises the browser bridge specifically.
"""
from dataclasses import replace
import json
import os
from pathlib import Path
import sys
import threading

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'src'),str(ROOT/'tests')]
from test_proxy_auth import HTTPProxyAuthenticationTests, SocksProxyAuthenticationTests, HOST
from vibe_job_radar.guided.adapters import builtins
from vibe_job_radar.guided.browser import PlaywrightBackend
from vibe_job_radar.guided.contracts import CrawlError
from vibe_job_radar.guided.rate import RateLedger, Limits


def main():
    out=ROOT/'browser-acceptance'/'proxy-auth';out.mkdir(parents=True,exist_ok=True)
    result={'success':False,'checks':[],'source_requests':0,
            'scope':'Real browser, local HTTP/SOCKS authentication and verified fixture TLS; artificial credentials and targets only.'}
    try:
        for protocol,cls in (('http',HTTPProxyAuthenticationTests),('socks5',SocksProxyAuthenticationTests)):
            fixture=cls();fixture.setUp()
            fixture.browser_fixture=True;fixture.content_type='text/html; charset=utf-8'
            fixture.payload='<h1>人工代理验收</h1><p>本页面只来自本地测试TLS服务器。</p>'.encode('utf-8')
            adapter=replace(builtins().get('liepin'),domains=(HOST,),resource_domains=(HOST,),login_hosts=(HOST,),
                login_url='https://'+HOST+'/',search_base='https://'+HOST+'/zhaopin/')
            ledger=RateLedger(Path(fixture.tmp.name)/'browser-rates.sqlite',Limits(page_interval=0,request_interval=0))
            def exercise():
                backend=PlaywrightBackend(adapter,ledger,threading.Event(),lambda *_:None,headless=True,
                    executable_path=os.environ.get('RADAR_TEST_CHROMIUM'))
                try:
                    page=backend.open(adapter.search_url('人工'))
                    assert '人工代理验收' in page.html
                    assert len(fixture.requests)>=2  # robots and the requested document.
                    for request in fixture.requests:
                        headers=request['headers'] if protocol=='http' else request[2]
                        assert 'proxy-authorization' not in {key.lower() for key in headers}
                    attempts=len(fixture.connects) if protocol=='http' else len(fixture.authentication)
                    delivered=len(fixture.requests)
                    if protocol=='http':fixture.expected_proxy_auth='Basic rejected'
                    else:fixture.expected_credentials=(b'rejected',b'rejected')
                    try:backend.open('https://'+HOST+'/job/2.shtml')
                    except CrawlError as error:assert error.code=='local_proxy_auth_failed',error.code
                    else:raise AssertionError('authentication rejection was not preserved')
                    after=len(fixture.connects) if protocol=='http' else len(fixture.authentication)
                    assert after==attempts+1 and len(fixture.requests)==delivered
                    result['checks'].append({'protocol':protocol,'browser_version':backend.browser.version,
                        'verified_target_requests':delivered,'rejected_auth_attempts':after-attempts,
                        'origin_received_proxy_auth':False,'fallback_or_replay':False})
                finally:backend.close()
            try:fixture.perform(exercise)
            finally:
                fixture.tearDown();fixture.doCleanups()
        result['success']=True
    finally:
        (out/'results.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(result,ensure_ascii=False,indent=2))


if __name__=='__main__':main()
