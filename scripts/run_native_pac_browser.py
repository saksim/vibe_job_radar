"""Ephemeral Windows CI: native Edge through real PAC-selected local proxies."""
import argparse
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
from run_native_browser_acceptance import trust_fixture,Fixture
from test_native_acquisition import adapter,HOST,URL
from test_loopback_proxy import RealProxyTests
from test_loopback_socks import RealSocksTests
from vibe_job_radar.guided.native_browser import NativeBackend
from vibe_job_radar.guided.contracts import CrawlError
from vibe_job_radar.guided.rate import RateLedger,Limits
from vibe_job_radar.network_policy import NetworkPolicy,use_policy
from vibe_job_radar.pac import PacSnapshot
from vibe_job_radar import system_pac
from vibe_job_radar.workspace import Workspace
from test_system_pac import SourceServer


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--controlled',action='store_true')
    parser.add_argument('--headed',action='store_true');parser.add_argument('--system',action='store_true');args=parser.parse_args()
    if not args.controlled:return
    if os.name!='nt' or os.environ.get('CI')!='true':
        raise SystemExit('Native PAC trust-store acceptance requires ephemeral Windows CI.')
    out=ROOT/'browser-acceptance'/('native-system-pac' if args.system else 'native-pac');out.mkdir(parents=True,exist_ok=True)
    result={'success':False,'checks':[],
        'scope':'Ephemeral Windows CI only; authored PAC, actual Edge and temporary artificial TLS source. System mode fakes only OS configuration and performs real source downloads. No personal PAC, accounts or recruiting requests.'}
    pac_server=SourceServer()
    clean={k:v for k,v in os.environ.items() if not k.startswith('VIBE_RADAR_')}
    def checkpoint(stage):
        result['stage']=stage
        (out/'results.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
        print('Native PAC: '+stage,flush=True)
    try:
        with tempfile.TemporaryDirectory(prefix='radar-native-pac-') as temp:
            root=Path(temp)
            with trust_fixture(root),patch.object(system_pac,'current_source',return_value=pac_server.source),patch.dict(os.environ,clean,clear=True):
                source=Fixture(root,'good.pem')
                try:
                    for keyword,cls in (('PROXY',RealProxyTests),('SOCKS5',RealSocksTests)):
                        fixture=cls();fixture.setUp();fixture.upstream_address=source.server.server_address
                        real_dns=socket.getaddrinfo
                        def dns(host,*args,**kwargs):
                            if host==HOST:return [(socket.AF_INET,socket.SOCK_STREAM,6,'',(fixture.symbol,443))]
                            if host in ('127.0.0.1','localhost','::1'):return real_dns(host,*args,**kwargs)
                            raise AssertionError('external DNS forbidden')
                        raw=keyword+' '+fixture.setting.split('://',1)[1]+'; DIRECT'
                        script='function FindProxyForURL(url,host){return '+json.dumps(raw)+';}'
                        pac_server.body=script.encode()
                        workspace=Workspace(root/('workspace-'+keyword))
                        if args.system:
                            workspace.network_system_pac_preferences(dict(config_id=pac_server.source.config_id,revision=0,consent=True))
                        before_sources=len(pac_server.requests)
                        def browser(name):
                            policy=(workspace.network_policy() if args.system else
                                NetworkPolicy('explicit_workspace',pac=PacSnapshot(script,lambda:True),pac_id='fixture'))
                            with use_policy(policy):
                                return NativeBackend(adapter(),RateLedger(root/(keyword+name+'.sqlite'),
                                    Limits(page_interval=0,request_interval=0)),threading.Event(),lambda *_:None,
                                    headless=not args.headed,channel='msedge')
                        def exercise():
                            with patch.dict(os.environ,clean,clear=True),patch('socket.getaddrinfo',side_effect=dns):
                                checkpoint(keyword+'-normal');backend=browser('normal')
                                try:
                                    page=backend.open(adapter().search_url('人工'))
                                    assert len(adapter().cards(page))==1
                                    detail=backend.open(URL+'/job/1')
                                    assert 'Cursor' in adapter().detail(detail)['text']
                                    version=backend.browser.version
                                finally:backend.close()
                                delivered=len(source.requests);before=len(fixture.dials)
                                if keyword=='PROXY':fixture.proxy_status=503
                                else:fixture.reply=5
                                checkpoint(keyword+'-refusal');backend=browser('refusal')
                                try:
                                    try:backend.open(adapter().search_url('人工'))
                                    except CrawlError as exc:
                                        assert exc.code in {'local_proxy_connection_failed','local_socks_request_rejected'},exc.code
                                    else:raise AssertionError('refused PAC route was bypassed')
                                finally:backend.close()
                                assert len(source.requests)==delivered
                                assert len(fixture.dials)>before
                                assert all(address==fixture.proxy.server_address for address in fixture.dials)
                                assert all(name==HOST for name in source.sni)
                                downloads=len(pac_server.requests)-before_sources
                                assert downloads==(2 if args.system else 0),downloads
                                result['checks'].append(dict(protocol=keyword,browser_version=version,
                                    original_list_and_full_artificial_detail=True,refused_target_requests=0,
                                    direct_fallback=False,pac_return_preserved=True,system_source_downloads=downloads))
                        try:fixture.perform(exercise)
                        finally:fixture.tearDown();fixture.doCleanups()
                finally:source.close()
        result['success']=True
    finally:
        pac_server.close();checkpoint('finished' if result['success'] else 'failed')


if __name__=='__main__':main()
