"""CI-only isolated host/guest network namespaces; not a hypervisor certification.

No default route, no external target, no trust-store changes. Only fixture DNS
and proxy-side public-symbol mapping are artificial; guest-to-host TCP is real.
"""
from __future__ import annotations
import json
import os
from pathlib import Path
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
from unittest.mock import patch
import uuid

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'src'),str(ROOT/'tests')]
HOST_IP,GUEST_IP='10.250.241.1','10.250.241.2'
HOST,SYMBOL='network-fixture.invalid','93.184.216.34'


def require_ci():
    if (os.environ.get('CI')!='true' or os.environ.get('GITHUB_ACTIONS')!='true'
            or sys.platform!='linux' or os.geteuid()!=0):
        raise SystemExit('Only ephemeral Linux GitHub Actions may create these network namespaces.')


def host(folder):
    import test_loopback_proxy as http
    import test_loopback_socks as socks
    fixtures={};config={}
    try:
        for key,cls in (('http',http.RealProxyTests),('http_denied',http.RealProxyTests),
                        ('socks5',socks.RealSocksTests),('socks5_denied',socks.RealSocksTests),
                        ('socks5_timeout',socks.RealSocksTests)):
            fixture=cls();fixture.proxy_bind=HOST_IP;fixture.setUp();fixtures[key]=fixture
            if key.endswith('_denied'):fixture.proxy_status=407;fixture.reply=5
            if key.endswith('_timeout'):fixture.pause_handshake=True
            config[key]=fixture.proxy.server_address[1]
        (folder/'peers.json').write_text(json.dumps(config),encoding='utf-8')
        sys.stdin.readline()  # Parent closes the fixture after the guest exits.
    finally:
        evidence={}
        for key,fixture in fixtures.items():
            fixture.tearDown();fixture.doCleanups()
            evidence[key]={'requests':len(fixture.requests),'connects':len(fixture.connects),
                          'sni':fixture.sni,'proxy_peers':fixture.proxy_peers}
        (folder/'host.json').write_text(json.dumps(evidence),encoding='utf-8')


def guest(folder):
    from vibe_job_radar.workspace import Workspace
    from vibe_job_radar.network import SafeHTTP, FetchError
    from vibe_job_radar.loopback_proxy import LocalProxyError
    from vibe_job_radar.guided.transport import PinnedTransport
    from vibe_job_radar.guided.rate import RateLedger, Limits
    ports=json.loads((folder/'peers.json').read_text(encoding='utf-8'))
    pem=ROOT/'tests'/'fixtures'/'connection_test_only.pem'
    context=ssl.create_default_context(cafile=str(pem));real_gai=socket.getaddrinfo
    real_dial=socket.create_connection;dials=[];checks=[];answer=[SYMBOL]
    def dns(name,*args,**kw):
        if name==HOST:return [(socket.AF_INET,socket.SOCK_STREAM,6,'',(answer[0],443))]
        return real_gai(name,*args,**kw)
    def dial(endpoint,*args,**kw):
        assert endpoint[0]==HOST_IP, 'unexpected direct/loopback connection'
        dials.append(endpoint)
        return real_dial(endpoint,*args,**kw)  # No endpoint translation here.
    clean={k:v for k,v in os.environ.items() if not k.startswith('VIBE_RADAR_')}
    with patch.dict(os.environ,clean,clear=True),patch('socket.getaddrinfo',side_effect=dns), \
         patch('socket.create_connection',side_effect=dial),patch('vibe_job_radar.network.create_client_context',return_value=context):
        workspace=Workspace(folder/'guest-workspace')
        def policy(key):
            scheme='http' if key.startswith('http') else 'socks5'
            workspace.network_proxy_preferences({'mode':'vm_'+scheme,'endpoint':f'{scheme}://{HOST_IP}:{ports[key]}',
                'consent':True,'revision':workspace.network_state()['revision']})
            return Workspace(workspace.root).network_policy()
        for scheme in ('http','socks5'):
            selected=policy(scheme)
            request=lambda:SafeHTTP({HOST},interval=0,network_policy=selected).json('https://'+HOST+'/jobs')
            assert request()['ok']
            ledger=RateLedger(folder/(scheme+'-rates.sqlite'),Limits(request_interval=0))
            wire=PinnedTransport(SimpleNamespace(key='fixture',domains=(HOST,),resource_domains=()),ledger,threading.Event())
            wire.bind_policy(selected)
            assert wire.fetch('https://'+HOST+'/detail').status==200
            assert ledger.summary('fixture')['request']['day']==1
            with patch('vibe_job_radar.network.create_client_context',return_value=ssl.create_default_context()):
                try:request()
                except FetchError as error:assert error.code=='tls_verification_failed'
                else:raise AssertionError('untrusted certificate accepted')
            before=len(dials);answer[0]='198.18.1.1'
            try:request()
            except FetchError as error:assert error.code=='non_public_address'
            else:raise AssertionError('Fake-IP accepted')
            finally:answer[0]=SYMBOL
            assert len(dials)==before
            checks.append(scheme+': workspace route reaches remote private peer; SafeHTTP and bridge verify origin TLS; bad TLS/Fake-IP stop')
            selected=policy(scheme+'_denied');before=len(dials)
            try:SafeHTTP({HOST},network_policy=selected).request('https://'+HOST+'/')
            except FetchError as error:assert error.code==('vm_proxy_connection_failed' if scheme=='http' else 'local_socks_request_rejected')
            else:raise AssertionError('proxy rejection ignored')
            assert len(dials)==before+1
        selected=policy('socks5_timeout');before=len(dials);started=time.monotonic()
        try:selected.proxy.open_tunnel(SYMBOL,.05)
        except LocalProxyError as error:assert error.code=='vm_proxy_timeout'
        else:raise AssertionError('deadline ignored')
        assert time.monotonic()-started<2 and len(dials)==before+1
    checks.append('refused HTTP/SOCKS and SOCKS handshake timeout make one attempt, never direct or guest loopback')
    (folder/'guest.json').write_text(json.dumps({'checks':checks,'dials':dials}),encoding='utf-8')


def orchestrate():
    out=ROOT/'browser-acceptance'/'vm-host-network';out.mkdir(parents=True,exist_ok=True)
    result={'success':False,'scope':'Real TCP between isolated Linux network namespaces; artificial DNS/origin, no external route or hypervisor/VPN certification.'}
    suffix=uuid.uuid4().hex[:8];host_ns='radar-host-'+suffix;guest_ns='radar-guest-'+suffix
    host_link='rh'+suffix;guest_link='rg'+suffix;created=[];process=None
    def ip(*args):return subprocess.run(['ip',*args],check=True,capture_output=True,text=True)
    try:
        with tempfile.TemporaryDirectory(prefix='radar-vm-network-') as tmp:
            folder=Path(tmp)
            try:
                for namespace in (host_ns,guest_ns):ip('netns','add',namespace);created.append(namespace)
                # Create directly within our namespace; never alter the runner's interfaces/routes.
                ip('-n',host_ns,'link','add',host_link,'type','veth','peer','name',guest_link)
                ip('-n',host_ns,'link','set',guest_link,'netns',guest_ns)
                for namespace,link,address in ((host_ns,host_link,HOST_IP),(guest_ns,guest_link,GUEST_IP)):
                    ip('-n',namespace,'addr','add',address+'/30','dev',link)
                    ip('-n',namespace,'link','set',link,'up');ip('-n',namespace,'link','set','lo','up')
                    routes=json.loads(ip('-j','-n',namespace,'route').stdout)
                    assert not any(r.get('dst')=='default' for r in routes)
                with (out/'host.log').open('w',encoding='utf-8') as log:
                    process=subprocess.Popen(['ip','netns','exec',host_ns,sys.executable,__file__,'--host',tmp],stdin=subprocess.PIPE,stdout=log,stderr=subprocess.STDOUT,text=True)
                    deadline=time.monotonic()+20
                    while not (folder/'peers.json').exists():
                        if process.poll() is not None or time.monotonic()>deadline:raise RuntimeError('host fixture startup failed')
                        time.sleep(.05)
                    subprocess.run(['ip','netns','exec',guest_ns,sys.executable,__file__,'--guest',tmp],check=True,timeout=45)
                    process.communicate(input='done\n',timeout=10)
                    assert process.returncode==0
                host_result=json.loads((folder/'host.json').read_text(encoding='utf-8'))
                for key,evidence in host_result.items():
                    assert set(evidence['proxy_peers'])=={GUEST_IP}, evidence
                    assert evidence['requests']==(2 if key in ('http','socks5') else 0), evidence
                    assert all(name==HOST for name in evidence['sni'])
                result.update(success=True,host=host_result,guest=json.loads((folder/'guest.json').read_text(encoding='utf-8')))
            finally:
                if process is not None and process.poll() is None:
                    process.terminate()
                    try:process.wait(timeout=5)
                    except subprocess.TimeoutExpired:process.kill();process.wait(timeout=5)
    finally:
        for namespace in reversed(created):ip('netns','delete',namespace)
        (out/'results.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
    print(json.dumps(result,indent=2))


if __name__=='__main__':
    require_ci()
    if len(sys.argv)==3 and sys.argv[1]=='--host':host(Path(sys.argv[2]))
    elif len(sys.argv)==3 and sys.argv[1]=='--guest':guest(Path(sys.argv[2]))
    elif len(sys.argv)==1:orchestrate()
    else:raise SystemExit('invalid fixture arguments')
