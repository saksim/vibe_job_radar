"""Actual Chromium -> bridge -> authenticated proxy -> verified artificial TLS.

Uses a per-client fixture CA; never modifies OS/browser trust or user profiles.
The native guard's separate credentials and encrypted tunnel have real socket
tests in test_proxy_auth. This script exercises the browser bridge specifically.
"""
from dataclasses import replace
import argparse
import json
import os
from pathlib import Path
import sys
import subprocess
import threading

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'src'),str(ROOT/'tests')]
from test_proxy_auth import HTTPProxyAuthenticationTests, SocksProxyAuthenticationTests, HOST
from test_digest_proxy import HTTPDigestProxyTests
from test_ntlm_proxy import HTTPNtlmProxyTests
from vibe_job_radar.guided.adapters import builtins
from vibe_job_radar.guided.browser import PlaywrightBackend
from vibe_job_radar.guided.contracts import CrawlError
from vibe_job_radar.guided.rate import RateLedger, Limits
from vibe_job_radar.guided.saved_session import SavedSession
from vibe_job_radar.network_policy import NetworkPolicy


def fixture_adapter():
    return replace(builtins().get('liepin'), domains=(HOST,), resource_domains=(HOST,), login_hosts=(HOST,),
                   login_url='https://' + HOST + '/', search_base='https://' + HOST + '/zhaopin/')


def restore_in_child(workspace, browser):
    """Test-only child; stdout carries only this fixture's artificial cookies."""
    selected = NetworkPolicy.capture()
    lease = SavedSession.for_policy(workspace, fixture_adapter(), backend='bridge', browser=browser,
                                    policy=selected)
    try:
        result = {'policy_id': selected.fingerprint}
        try:
            result['storage_state'] = lease.restore()
            result['status'] = lease.status
        except CrawlError as exc:
            result['error'] = exc.code
        print(json.dumps(result))
    finally:
        lease.close()


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--channel',choices=['msedge','chrome'])
    parser.add_argument('--protocol',choices=['http','socks5','http-digest','http-ntlmv2'])
    args=parser.parse_args()
    options={'channel':args.channel} if args.channel else {'executable_path':os.environ.get('RADAR_TEST_CHROMIUM')}
    browser_binding=args.channel or 'bundled'
    out=ROOT/'browser-acceptance'/'proxy-auth';out.mkdir(parents=True,exist_ok=True)
    result={'success':False,'checks':[],'source_requests':0,'browser_channel':browser_binding,
            'scope':'Real browser, local HTTP Basic/Digest/NTLMv2 and SOCKS5 authentication and verified fixture TLS; artificial credentials and targets only.'}
    try:
        for protocol,cls in (('http',HTTPProxyAuthenticationTests),('socks5',SocksProxyAuthenticationTests),('http-digest',HTTPDigestProxyTests),('http-ntlmv2',HTTPNtlmProxyTests)):
            if args.protocol and protocol!=args.protocol:continue
            fixture=cls();fixture.setUp()
            fixture.browser_fixture=True;fixture.content_type='text/html; charset=utf-8'
            fixture.payload='<h1>人工代理验收</h1><p>本页面只来自本地测试TLS服务器。</p>'.encode('utf-8')
            adapter=fixture_adapter()
            ledger=RateLedger(Path(fixture.tmp.name)/'browser-rates.sqlite',Limits(page_interval=0,request_interval=0))
            def exercise():
                backend=PlaywrightBackend(adapter,ledger,threading.Event(),lambda *_:None,headless=True,
                    **options)
                try:
                    page=backend.open(adapter.search_url('人工'))
                    assert '人工代理验收' in page.html
                    assert len(fixture.requests)>=2  # robots and the requested document.
                    cookie = dict(name='continuity_fixture', value='ARTIFICIAL-BROWSER-COOKIE',
                                  domain=HOST, path='/', expires=-1, httpOnly=True, secure=True, sameSite='Lax')
                    backend.context.add_cookies([cookie])
                    selected = NetworkPolicy.capture()
                    saved = SavedSession.for_policy(fixture.tmp.name, adapter, backend='bridge',
                                                     browser=browser_binding, policy=selected)
                    try:
                        saved.save(backend.export_session_cookies())
                        snapshot = saved.path.read_bytes()
                    finally:
                        saved.close()
                    child = subprocess.run([sys.executable, str(Path(__file__).resolve()),
                                            '--restore-session-fixture', fixture.tmp.name, browser_binding],
                                           capture_output=True, text=True, encoding='utf-8', timeout=20, check=True)
                    restored = json.loads(child.stdout)
                    assert restored['policy_id'] != selected.fingerprint
                    assert saved.path.read_bytes() == snapshot
                    cookie_continuity = False
                    if os.name == 'nt':
                        assert restored['status'] == 'restored_unverified'
                        assert restored['storage_state']['cookies'] == [cookie]
                        backend.close()
                        backend = PlaywrightBackend(adapter, ledger, threading.Event(), lambda *_:None,
                            headless=True, **options,
                            storage_state=restored['storage_state'])
                        offset = len(fixture.requests)
                        assert '人工代理验收' in backend.open(adapter.search_url('人工恢复')).html
                        received = fixture.requests[offset:]
                        # Fresh backend performs robots then its first document;
                        # the real TLS origin must see the restored cookie there.
                        assert len(received) == 2
                        headers = received[-1]['headers'] if protocol != 'socks5' else received[-1][2]
                        assert {k.lower():v for k,v in headers.items()}.get('cookie') == (
                            cookie['name'] + '=' + cookie['value'])
                        cookie_continuity = True
                    else:
                        assert restored['error'] == 'saved_session_incompatible'
                    for request in fixture.requests:
                        headers=request['headers'] if protocol!='socks5' else request[2]
                        assert 'proxy-authorization' not in {key.lower() for key in headers}
                    attempts=len(fixture.connects) if protocol=='http' else len(fixture.authentication)
                    delivered=len(fixture.requests)
                    if protocol=='http':fixture.expected_proxy_auth='Basic rejected'
                    elif protocol in ('http-digest','http-ntlmv2'):fixture.reject_auth=True
                    else:fixture.expected_credentials=(b'rejected',b'rejected')
                    try:backend.open('https://'+HOST+'/job/2.shtml')
                    except CrawlError as error:assert error.code=='local_proxy_auth_failed',error.code
                    else:raise AssertionError('authentication rejection was not preserved')
                    after=len(fixture.connects) if protocol=='http' else len(fixture.authentication)
                    assert after==attempts+1 and len(fixture.requests)==delivered
                    result['checks'].append({'protocol':protocol,'browser_version':backend.browser.version,
                        'verified_target_requests':delivered,'rejected_auth_attempts':after-attempts,
                        'origin_received_proxy_auth':False,'fallback_or_replay':False,
                        'authenticated_cookie_restart':cookie_continuity,
                        'saved_session_version':2 if os.name=='nt' else 1,
                        'session_scope':'real DPAPI child and first new-browser document' if os.name=='nt'
                                        else 'owner-only v1 rejects new-process credential binding'})
                finally:backend.close()
            try:fixture.perform(exercise)
            finally:
                result['source_requests'] += len(fixture.requests)
                fixture.tearDown();fixture.doCleanups()
        result['success']=True
    finally:
        (out/'results.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(result,ensure_ascii=False,indent=2))


if __name__=='__main__':
    if len(sys.argv)==4 and sys.argv[1]=='--restore-session-fixture':
        restore_in_child(sys.argv[2],sys.argv[3])
    else:
        main()
