"""Ephemeral CI only: actual native TLS GET failure, finite recovery and stop."""
import argparse
from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import socket
import tempfile
import time
from unittest.mock import patch

from run_native_browser_acceptance import (ROOT, Fixture, trust_fixture, HOST,
    adapter, NativeBackend, GuidedService, Registry, Workspace, RateLedger, Limits,
    NetworkPolicy, use_policy)


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--controlled',action='store_true')
    parser.add_argument('--channel',choices=['msedge']);parser.add_argument('--headed',action='store_true')
    args=parser.parse_args()
    if not args.controlled or not os.environ.get('CI') or os.environ.get('GITHUB_ACTIONS')!='true':
        raise SystemExit('No requests or trust changes: run only in an explicitly enabled ephemeral GitHub CI runner.')
    out=ROOT/'browser-acceptance'/'native-read-retry';out.mkdir(parents=True,exist_ok=True)
    result={'success':False,'checks':[],'source_requests':0,
        'scope':'Native browser/controller and real artificial TLS source; isolated CI fixture trust and quota clock. No live account/platform.'}
    now=[1000.0];remaining=[1];instances=[]
    try:
        with tempfile.TemporaryDirectory(prefix='radar-native-read-retry-') as tmp:
            root=Path(tmp)
            with trust_fixture(root),ExitStack() as stack:
                fixture=Fixture(root,'good.pem');stack.callback(fixture.close)
                handler=fixture.server.RequestHandlerClass;original_get=handler.do_GET
                def get(request):
                    if request.path.split('?')[0]=='/job/1' and remaining[0]:
                        remaining[0]-=1;request.record()
                        request.send('artificial unavailable',status=503,extra=[('Retry-After','30')])
                    else:original_get(request)
                stack.enter_context(patch.object(handler,'do_GET',get))
                real_dns,real_dial=socket.getaddrinfo,socket.create_connection
                def dns(host,*a,**kw):
                    if host==HOST:return [(socket.AF_INET,socket.SOCK_STREAM,6,'',('93.184.216.34',443))]
                    if host in ('127.0.0.1','localhost','::1'):return real_dns(host,*a,**kw)
                    raise AssertionError('fixture attempted external DNS')
                def dial(address,*a,**kw):
                    if address==('93.184.216.34',443):return real_dial(fixture.server.server_address,*a,**kw)
                    if address[0] in ('127.0.0.1','localhost','::1'):return real_dial(address,*a,**kw)
                    raise AssertionError('fixture attempted external connection')
                stack.enter_context(patch('socket.getaddrinfo',side_effect=dns))
                stack.enter_context(patch('socket.create_connection',side_effect=dial))
                stack.enter_context(patch.object(NetworkPolicy,'capture',return_value=NetworkPolicy()))
                def factory(a,l,c,p,**saved):
                    options={'headless':not args.headed}
                    if args.channel:options['channel']=args.channel
                    with use_policy(NetworkPolicy()):b=NativeBackend(a,l,c,p,**options,**saved)
                    instances.append(b);return b
                workspace=Workspace(root/'workspace')
                ledger=RateLedger(root/'rates.sqlite',Limits(page_interval=0,request_interval=0),clock=lambda:now[0])
                service=GuidedService(workspace,registry=Registry([adapter()]),ledger=ledger,native_backend_factory=factory)
                stack.callback(service.close)
                def wait(ident,predicate):
                    deadline=time.monotonic()+45
                    while time.monotonic()<deadline:
                        state=service._load(ident)
                        if not service.state()['busy'] and predicate(state):return state
                        time.sleep(.03)
                    raise AssertionError('native fixture state: '+state['code'])
                def create():
                    ident=service.create({'platform':'fixture','keyword':'时间序列算法工程师','roles':['time_series'],
                        'max_pages':1,'max_jobs':1,'consent':True,'rights_note':'ARTIFICIAL NATIVE RETRY',
                        'backend':'native','native_consent':True})['id']
                    state=wait(ident,lambda s:s['status']=='ready')
                    service.action({'id':ident,'action':'collect','selected':[state['cards'][0]['id']]})
                    return ident
                ident=create();waiting=wait(ident,lambda s:s['code']=='read_retry_wait')
                assert waiting['next_allowed_at']==1030 and waiting['read_retry']['used']==1
                now[0]=1031;finished=wait(ident,lambda s:s['status']=='completed')
                assert finished['outcome']['saved']==1 and finished['read_retry']['used']==1
                assert len(instances)==1
                assert sum(r['path']=='/job/1' for r in fixture.requests)==2
                assert sum(r['method']=='POST' and r['path']=='/api/jobs' for r in fixture.requests)==1
                report=workspace.root/'reports'/finished['report_id']/'run_manifest.json'
                digest=hashlib.sha256(report.read_bytes()).hexdigest()
                result['checks'].append('actual native 503 and Retry-After recover the interrupted JD in the same browser; business POST not replayed')
                remaining[0]=9;ident=create()
                for used in (1,2):
                    waiting=wait(ident,lambda s:s['code']=='read_retry_wait' and s['read_retry']['used']==used)
                    now[0]=waiting['next_allowed_at']+1
                stopped=wait(ident,lambda s:s['code']=='read_retry_exhausted')
                assert not stopped['auto_resume'] and stopped['outcome']['failed']==1
                assert stopped['outcome']['pending']==0 and stopped['read_retry']['used']==2
                assert sum(r['path']=='/job/1' for r in fixture.requests)==5
                assert sum(r['method']=='POST' and r['path']=='/api/jobs' for r in fixture.requests)==2
                count=len(fixture.requests);time.sleep(.3);assert len(fixture.requests)==count
                assert hashlib.sha256(report.read_bytes()).hexdigest()==digest
                result['checks'].append('three failed native GETs exhaust two retry slots, mark the JD failed and preserve previous report without extra requests')
                result.update(success=True,browser_versions=[b.browser.version for b in instances],
                    native_requests=len(fixture.requests))
    finally:
        (out/'results.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(result,ensure_ascii=False,indent=2))


if __name__=='__main__':main()
