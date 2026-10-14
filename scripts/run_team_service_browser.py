"""Actual Waitress process, two client processes and browser; authored data only.

The test-only transport models a local TLS-terminating proxy. Production clients
still require public HTTPS; this is not a deployed domain/TLS or live-site test.
"""
from __future__ import annotations
import argparse
import http.client
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import asdict
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'src'),str(ROOT/'tests')]
import test_public_hybrid as fixtures
from vibe_job_radar.team_store import TeamStore
from vibe_job_radar.team_catalog import TeamCatalog
from vibe_job_radar.team_service import private_json
from vibe_job_radar.team_client import TeamConnection,install_connection
from vibe_job_radar.guided.rate import Limits,RateLimit
from vibe_job_radar.guided.contracts import CrawlError
from vibe_job_radar.network import FetchError
from vibe_job_radar.workspace import Workspace
from vibe_job_radar.workbench import LocalServer


class ProxyFixtureTransport:
    def __init__(self,port):self.port=port
    def json(self,url,*,method='GET',headers=None,payload=None):
        from urllib.parse import urlsplit
        assert urlsplit(url).netloc=='team.fixture.test'
        conn=http.client.HTTPConnection('127.0.0.1',self.port,timeout=10)
        try:
            raw=json.dumps(payload).encode() if payload is not None else None
            conn.request(method,urlsplit(url).path,body=raw,headers={'Host':'team.fixture.test','X-Forwarded-Proto':'https',
                         **(headers or {}),**({'Content-Type':'application/json'} if raw is not None else {})})
            response=conn.getresponse();value=json.loads(response.read())
            if response.status!=200:raise FetchError('http_'+str(response.status),retry_after=float(response.getheader('Retry-After') or 0))
            return value
        finally:conn.close()


def child(args):
    connection=TeamConnection(args.workspace,transport=ProxyFixtureTransport(args.port))
    try:
        if args.action=='reserve':connection.reserve('liepin','page');result={'outcome':'accepted'}
        else:result={'outcome':'authorized','device':connection.request('/v1/team/state')['device']}
    except RateLimit:result={'outcome':'limited'}
    except CrawlError as error:result={'outcome':error.code}
    print(json.dumps(result));return 0


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--browser-executable')
    parser.add_argument('--out',type=Path,help='Fresh output directory for an independent evidence run')
    parser.add_argument('--child',action='store_true');parser.add_argument('--port',type=int)
    parser.add_argument('--workspace',type=Path);parser.add_argument('--action')
    args=parser.parse_args()
    if args.child:return child(args)
    from playwright.sync_api import sync_playwright,expect
    from vibe_job_radar.qualification import output_directory
    out=output_directory(ROOT,args.out or ROOT/'browser-acceptance/team');out.mkdir(parents=True,exist_ok=True)
    result={'success':False,'checks':[],'page_errors':[],'external_requests':[],
            'scope':'Actual Waitress, two client processes, Edge/Chromium UI and original reports; artificial JDs, loopback proxy envelope; not real TLS/domain/site acceptance.'}
    process=None;server=None;worker=None
    try:
        with tempfile.TemporaryDirectory(prefix='team-service-browser-') as tmp:
            root=Path(tmp);data=root/'service';store=TeamStore(data);store.initialize();catalog=TeamCatalog(data);catalog.initialize()
            store.resource('test-team','account','liepin',['https://www.liepin.com'],limits=Limits(page_interval=60))
            config={'schema_version':1,'origin':'https://team.fixture.test','quota_resources':{'liepin':'account'},
                    'public_sources':[asdict(fixtures.SOURCE)],'use_public_service':True}
            private_json(root/'client.json',config)
            for name in ('alice','bob'):
                store.member('test-team',name,'collector');store.grant('test-team',name,'account');store.grant('test-team',name,'fixture',source=True)
                token=store.issue('test-team',name,name+'-pc')
                private_json(root/(name+'-credential.json'),{'schema_version':1,'device_token':token})
                (root/name).mkdir();install_connection(root/name,root/'client.json',root/(name+'-credential.json'))
            catalog.publish(fixtures.REGISTRY,[fixtures.job('one'),fixtures.job('two')],observed_at={'fixture':time.time()})
            with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
            private_json(root/'service.json',{'schema_version':1,'host':'team.fixture.test','port':port,'threads':4,'connection_limit':16})
            env=dict(os.environ);env['PYTHONPATH']=os.pathsep.join([str(ROOT/'src'),str(ROOT/'tests'),env.get('PYTHONPATH','')]);env['PYTHONDONTWRITEBYTECODE']='1'
            def start():
                p=subprocess.Popen([sys.executable,'-B','-m','vibe_job_radar.team_service','--data',str(data),'serve','--config',str(root/'service.json')],
                                   env=env,stdout=subprocess.PIPE,stderr=subprocess.PIPE,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
                deadline=time.monotonic()+20
                while time.monotonic()<deadline:
                    if p.poll() is not None:raise AssertionError('Waitress exited before readiness: '+p.communicate()[1].decode(errors='replace')[-2000:])
                    try:
                        if ProxyFixtureTransport(port).json('https://team.fixture.test/healthz')['status']=='ready':return p
                    except (OSError,FetchError):pass
                    time.sleep(.1)
                p.terminate();p.wait(10);raise AssertionError('Waitress did not become ready')
            def client(name,action):
                done=subprocess.run([sys.executable,'-B',str(Path(__file__)),'--child','--port',str(port),'--workspace',str(root/name),'--action',action],
                                    env=env,capture_output=True,text=True,timeout=20,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
                if done.returncode:raise AssertionError('Client process failed: '+done.stderr[-1000:])
                return json.loads(done.stdout)
            process=start()
            assert client('alice','reserve')['outcome']=='accepted'
            assert client('bob','reserve')['outcome']=='limited'
            result['checks'].append('actual Waitress and two independent client processes share one account limit')
            process.terminate();process.communicate(timeout=10);process=start()
            assert client('bob','reserve')['outcome']=='limited'
            result['checks'].append('service restart preserves central consumed quota')
            with patch('vibe_job_radar.team_client.SafeHTTP',side_effect=lambda *a,**k:ProxyFixtureTransport(port)):
                workspace=Workspace(root/'alice');server=LocalServer(workspace)
                worker=threading.Thread(target=server.serve_forever,kwargs={'poll_interval':.01},daemon=True);worker.start()
                with sync_playwright() as pw:
                    launch={'headless':True}
                    if args.browser_executable:launch['executable_path']=args.browser_executable
                    browser=pw.chromium.launch(**launch)
                    try:
                        context=browser.new_context(viewport={'width':1100,'height':900})
                        def local_only(route):
                            if route.request.url.startswith(server.origin+'/'):route.continue_()
                            else:result['external_requests'].append('unexpected_external_browser_request');route.abort()
                        context.route('**/*',local_only)
                        page=context.new_page();page.on('pageerror',lambda error:result['page_errors'].append(str(error)))
                        page.goto(server.entry_url)
                        page.locator('#team-panel summary').click()
                        expect(page.locator('#team-status')).to_contain_text('状态页未联网')
                        page.locator('#team-check').click();expect(page.locator('#team-result')).to_contain_text('alice-pc')
                        page.locator('#team-sync').click();expect(page.locator('#team-status')).to_contain_text('同步完成',timeout=20000)
                        expect(page.locator('#team-result')).to_contain_text('已确认')
                        result['checks'].append('workbench checks device permission and commits a complete synchronized version')
                        page.set_viewport_size({'width':390,'height':844})
                        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                        page.locator('#team-panel').screenshot(path=str(out/'team-mobile.png'))
                        form=page.locator('#public-search');form.locator('[name=query]').fill('AI')
                        form.locator('[name=consent]').check();form.locator('button').click()
                        expect(page.locator('#report')).to_be_visible(timeout=20000)
                        state=server.public_tasks.snapshot();assert state['status']=='completed'
                        assert workspace.report(state['report_id']) and workspace.status()['counts']['full_text']==2
                        page.locator('#report').screenshot(path=str(out/'team-report-mobile.png'))
                        result['checks'].append('authenticated service query produces two authored full JDs and the original report at 390px')
                        store.revoke('test-team','alice-pc');page.locator('#team-check').click()
                        expect(page.locator('#team-status')).to_contain_text('到期或撤销')
                        assert workspace.report(state['report_id'])
                        result['checks'].append('revoked device is rejected while its existing local report remains readable')
                    finally:browser.close()
                server.shutdown();server.server_close();worker.join(10);server=None
            assert not result['page_errors'] and not result['external_requests']
            process.terminate();process.communicate(timeout=10);process=None
        result['success']=True
    except Exception as error:
        result['success']=False;result['error']=str(error)
    finally:
        if server is not None:server.shutdown();server.server_close()
        if worker is not None:worker.join(10)
        if process is not None and process.poll() is None:process.terminate();process.communicate(timeout=10)
        try:(out/'result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
        except OSError:
            result['success']=False;result['evidence_error']='result_file_unwritable'
        print(json.dumps(result,ensure_ascii=False,indent=2))
    return 0 if result['success'] else 1


if __name__=='__main__':raise SystemExit(main())
