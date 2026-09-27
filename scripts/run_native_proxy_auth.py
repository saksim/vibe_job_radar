"""Ephemeral CI only: native browser through authenticated upstream proxies.

Reuses the existing temporary fixture CA workflow. Never run its trust-store
setup on a user's Windows host; non-CI invocations stop before creating a CA.
"""
import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import threading
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'src'),str(ROOT/'tests')]
from run_native_browser_acceptance import trust_fixture, Fixture
from test_native_acquisition import adapter, HOST, URL
from test_proxy_auth import HTTPProxyAuthenticationTests, SocksProxyAuthenticationTests
from vibe_job_radar.guided.native_browser import NativeBackend
from vibe_job_radar.guided.contracts import CrawlError
from vibe_job_radar.guided.rate import RateLedger, Limits
from vibe_job_radar.network_policy import NetworkPolicy, use_policy
from native_proxy_evidence import update_proxy_evidence


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--controlled',action='store_true')
    parser.add_argument('--channel',choices=['msedge'])
    parser.add_argument('--headed',action='store_true')
    args=parser.parse_args()
    if not args.controlled:return
    if os.environ.get('CI')!='true':
        raise SystemExit('This temporary trust-store acceptance runs only on ephemeral CI runners.')
    out=ROOT/'browser-acceptance'/'native-proxy-auth';out.mkdir(parents=True,exist_ok=True)
    result={'success':False,'source_requests':0,'checks':[],
            'scope':'Ephemeral CI; real native browser and authenticated local proxy with artificial TLS source and credentials.'}
    source = None
    def checkpoint(stage, **facts):
        update_proxy_evidence(result, stage, source=source, **facts)
        (out/'results.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
        print('Native proxy fixture: '+stage,flush=True)
    try:
        with tempfile.TemporaryDirectory(prefix='radar-native-proxy-auth-') as tmp:
            root=Path(tmp)
            with trust_fixture(root):
                source=Fixture(root,'good.pem')
                try:
                    for protocol,cls in (('http',HTTPProxyAuthenticationTests),('socks5',SocksProxyAuthenticationTests)):
                        fixture=cls();fixture.setUp();fixture.upstream_address=source.server.server_address
                        real_dns=socket.getaddrinfo
                        def dns(host,*args,**kwargs):
                            if host==HOST:return [(socket.AF_INET,socket.SOCK_STREAM,6,'',(fixture.symbol,443))]
                            if host in ('127.0.0.1','localhost','::1'):return real_dns(host,*args,**kwargs)
                            raise AssertionError('fixture forbids external DNS')
                        options={'headless':not args.headed}
                        if args.channel:options['channel']=args.channel
                        def exercise():
                            policy=NetworkPolicy.capture()
                            def browser(name):
                                with use_policy(policy):
                                    return NativeBackend(adapter(),RateLedger(root/(protocol+name+'.sqlite'),
                                        Limits(page_interval=0,request_interval=0)),threading.Event(),lambda *_:None,**options)
                            @contextmanager
                            def observed(name, source_start):
                                backend = None
                                action = ['create']
                                stage = protocol + '-' + name
                                try:
                                    backend = browser(name)
                                    yield backend, action
                                except Exception as exc:
                                    checkpoint(stage, backend=backend, fixture=fixture,
                                        source_start=source_start, error=exc, action=action[0])
                                    raise
                                finally:
                                    if backend is not None:
                                        try:
                                            checkpoint(stage, backend=backend, fixture=fixture,
                                                source_start=source_start, action=action[0])
                                        finally:
                                            backend.close()
                            with patch('socket.getaddrinfo',side_effect=dns):
                                checkpoint(protocol+'-normal')
                                before=len(source.requests)
                                with observed('normal', before) as (backend, action):
                                    action[0] = 'search'
                                    page=backend.open(adapter().search_url('人工'))
                                    assert len(adapter().cards(page))==1
                                    action[0] = 'detail'
                                    detail=backend.open(URL+'/job/1')
                                    assert 'Cursor' in adapter().detail(detail)['text']
                                    action[0] = 'verify'
                                    version=backend.browser.version
                                received=source.requests[before:]
                                assert received and all(row['proxy_secret_absent'] and row['origin_secret_absent'] for row in received)
                                assert all(host==HOST for host in source.sni)
                                checkpoint(protocol+'-rejected')
                                if protocol=='http':fixture.expected_proxy_auth='Basic rejected'
                                else:fixture.expected_credentials=(b'rejected',b'rejected')
                                attempts=len(fixture.connects) if protocol=='http' else len(fixture.authentication)
                                delivered=len(source.requests)
                                with observed('rejected', delivered) as (backend, action):
                                    action[0] = 'rejected_search'
                                    try:backend.open(adapter().search_url('人工'))
                                    except CrawlError as error:assert error.code=='local_proxy_auth_failed',error.code
                                    else:raise AssertionError('native browser ignored upstream authentication rejection')
                                    action[0] = 'verify'
                                after=len(fixture.connects) if protocol=='http' else len(fixture.authentication)
                                assert after-attempts==1 and len(source.requests)==delivered
                                result['checks'].append({'protocol':protocol,'browser_version':version,
                                    'native_list_and_full_artificial_detail':True,'target_received_proxy_credentials':False,
                                    'rejected_auth_attempts':after-attempts,'rejected_target_requests':0})
                        try:fixture.perform(exercise)
                        finally:fixture.tearDown();fixture.doCleanups()
                finally:source.close()
        result['success']=True
    except Exception as exc:
        checkpoint('failed', error=exc, action='setup_or_cleanup')
        raise
    finally:checkpoint('finished' if result['success'] else 'failed')


if __name__=='__main__':main()
