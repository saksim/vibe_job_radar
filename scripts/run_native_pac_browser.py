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
from vibe_job_radar import system_pac, wpad_native, pac_url_worker
from vibe_job_radar.workspace import Workspace
from test_system_pac import SourceServer


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--controlled',action='store_true')
    parser.add_argument('--headed',action='store_true')
    modes=parser.add_mutually_exclusive_group()
    modes.add_argument('--system',action='store_true');modes.add_argument('--wpad',action='store_true')
    modes.add_argument('--portable-domain',action='store_true')
    parser.add_argument('--channel',choices=['msedge','chrome'])
    args=parser.parse_args()
    if not args.controlled:return
    if os.environ.get('CI')!='true' or os.environ.get('GITHUB_ACTIONS')!='true' or (os.name!='nt' and not args.portable_domain):
        raise SystemExit('Native PAC trust-store acceptance requires ephemeral GitHub CI; Windows for legacy modes.')
    if args.portable_domain and not pac_url_worker.available():
        raise SystemExit('Portable domain PAC needs the actual optional engine.')
    out=ROOT/'browser-acceptance'/('native-portable-domain-pac' if args.portable_domain else 'native-wpad' if args.wpad else 'native-system-pac' if args.system else 'native-pac');out.mkdir(parents=True,exist_ok=True)
    result={'success':False,'checks':[],
        'scope':'Ephemeral Linux/Windows CI; authored PAC, actual browser and temporary artificial TLS source. Portable domain modes use the isolated engine with separate DNS scope. Legacy system/WPAD modes substitute OS source discovery. No real enterprise PAC, WPAD discovery, accounts or recruiting requests.'}
    pac_server=SourceServer()
    clean={k:v for k,v in os.environ.items() if not k.startswith('VIBE_RADAR_')}
    def checkpoint(stage):
        result['stage']=stage
        (out/'results.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
        print('Native PAC: '+stage,flush=True)
    try:
        with tempfile.TemporaryDirectory(prefix='radar-native-pac-') as temp:
            root=Path(temp)
            with patch.dict(os.environ,clean,clear=True),trust_fixture(root),patch.object(system_pac,'current_source',return_value=pac_server.source),patch.object(wpad_native,'discover',return_value=pac_server.source) as discovery:
                browser_env={k:v for k,v in os.environ.items() if not k.startswith('VIBE_RADAR_')}
                source=Fixture(root,'good.pem')
                try:
                    for keyword,cls,dns_scope in ((key,cls,dns) for key,cls in (('PROXY',RealProxyTests),('SOCKS5',RealSocksTests))
                            for dns in ((False,True) if args.portable_domain else (False,))):
                        fixture=cls();fixture.setUp();fixture.upstream_address=source.server.server_address
                        real_dns=socket.getaddrinfo
                        def dns(host,*args,**kwargs):
                            if host==HOST:return [(socket.AF_INET,socket.SOCK_STREAM,6,'',(fixture.symbol,443))]
                            if host in ('127.0.0.1','localhost','::1'):return real_dns(host,*args,**kwargs)
                            raise AssertionError('external DNS forbidden')
                        raw=keyword+' '+fixture.setting.split('://',1)[1]+'; DIRECT'
                        script='function FindProxyForURL(url,host){return '+json.dumps(raw)+';}'
                        if args.portable_domain:
                            condition='url==='+json.dumps('https://'+HOST+'/')+' && host==='+json.dumps(HOST)
                            condition+=(' && isInNet(dnsResolve("localhost"),"127.0.0.0","255.0.0.0")' if dns_scope else ' && typeof dnsResolve==="undefined"')
                            script='function FindProxyForURL(url,host){return ('+condition+')?'+json.dumps(raw)+':"INVALID domain-input";}'
                        pac_server.body=script.encode()
                        workspace=Workspace(root/('workspace-'+keyword+('-dns' if dns_scope else '')))
                        if args.portable_domain:
                            save=workspace.network_domain_pac_dns_preferences if dns_scope else workspace.network_domain_pac_preferences
                            save(dict(name='authored-domain.pac',script=script,revision=0,consent=True))
                        if args.system:
                            workspace.network_system_pac_preferences(dict(config_id=pac_server.source.config_id,revision=0,consent=True))
                        if args.wpad:
                            workspace.network_wpad_discover(dict(revision=0,consent=True))
                            workspace.wpad_discovery._thread.join(2)
                            candidate=workspace.network_state()['wpad']['candidate']
                            workspace.network_wpad_preferences(dict(revision=0,candidate=candidate,consent=True))
                        before_sources=len(pac_server.requests)
                        before_discovery=discovery.call_count
                        def browser(name):
                            policy=(workspace.network_policy() if args.system or args.wpad or args.portable_domain else
                                NetworkPolicy('explicit_workspace',pac=PacSnapshot(script,lambda:True),pac_id='fixture'))
                            with use_policy(policy):
                                return NativeBackend(adapter(),RateLedger(root/(keyword+name+('-dns' if dns_scope else '')+'.sqlite'),
                                    Limits(page_interval=0,request_interval=0)),threading.Event(),lambda *_:None,
                                    headless=not args.headed,channel=args.channel or ('msedge' if os.name=='nt' else None))
                        def exercise():
                            result['active_case']={'protocol':keyword,'dns_helpers':dns_scope}
                            with patch.dict(os.environ,browser_env,clear=True),patch('socket.getaddrinfo',side_effect=dns):
                                checkpoint(keyword+'-normal');backend=browser('normal')
                                try:
                                    page=backend.open(adapter().search_url('人工'))
                                    assert len(adapter().cards(page))==1
                                    detail=backend.open(URL+'/job/1')
                                    assert 'Cursor' in adapter().detail(detail)['text']
                                    version=backend.browser.version
                                finally:backend.close()
                                delivered=len(source.requests);before=len(fixture.dials)
                                before_connects=len(fixture.connects)

                                if keyword=='PROXY':fixture.proxy_status=503
                                else:fixture.reply=5
                                checkpoint(keyword+'-refusal');backend=browser('refusal')
                                refusal_code=''
                                try:
                                    try:backend.open(adapter().search_url('人工'))
                                    except CrawlError as exc:
                                        refusal_code=exc.code
                                        assert exc.code in {'local_proxy_connection_failed','local_socks_request_rejected'},exc.code
                                    else:raise AssertionError('refused PAC route was bypassed')
                                finally:
                                    # Keep bounded counts/fixed codes before close. No raw
                                    # browser messages, URLs, credentials or response bodies.
                                    result['refusal_observation']={
                                        'protocol':keyword,'dns_helpers':dns_scope,
                                        'code':refusal_code,'controller_code':backend.error,
                                        'tunnel_code':backend.tunnel.last_error,
                                        'authenticated_challenges':len(backend._auth_attempts),
                                        'tunnel_connections':backend.tunnel.connections,
                                        'proxy_dials':len(fixture.dials)-before,
                                        'proxy_connects':len(fixture.connects)-before_connects,
                                        'target_requests':len(source.requests)-delivered}
                                    checkpoint(keyword+'-refusal-observed')
                                    backend.close()
                                assert len(source.requests)==delivered
                                assert len(fixture.dials)>before
                                assert all(address==fixture.proxy.server_address for address in fixture.dials)
                                assert all(name==HOST for name in source.sni)
                                downloads=len(pac_server.requests)-before_sources
                                assert downloads==(2 if args.system or args.wpad else 0),downloads
                                assert discovery.call_count-before_discovery==(2 if args.wpad else 0)
                                result['checks'].append(dict(protocol=keyword,browser_version=version,portable_domain=args.portable_domain,dns_helpers=dns_scope,
                                    canonical_root_enforced=args.portable_domain,
                                    original_list_and_full_artificial_detail=True,refused_target_requests=0,
                                    direct_fallback=False,pac_return_preserved=True,system_source_downloads=downloads,
                                    wpad_fixture_discoveries=discovery.call_count-before_discovery,real_wpad_discovery=False))
                        try:fixture.perform(exercise)
                        finally:workspace.close_network();fixture.tearDown();fixture.doCleanups()
                finally:source.close()
        result['success']=True
    finally:
        pac_server.close();checkpoint('finished' if result['success'] else 'failed')


if __name__=='__main__':main()
