"""One hosted source control before enabling actual frozen DNS WPAD acceptance."""
import json
import multiprocessing
import os
from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from wpad_dns_acceptance import DnsWpadFixture,verify_app
from wpad_dns_protocol import require_hosted
from system_pac_request_trace import FixtureRequestTrace
from verify_windows_portable import RunningApp
from vibe_job_radar.workspace import Workspace
from vibe_job_radar.workbench import LocalServer

def main():
    require_hosted()
    out=ROOT/'release-verification/wpad-dns-source-control'
    out.mkdir(parents=True,exist_ok=True)
    path=out/'result.json'
    if path.exists():raise ValueError('fresh source-control output required')
    result={'success':False,'kind':'hosted-source-dns-wpad-control','frozen_executable':False,
            'product_qualification':False,'scope':'Real source API/native DNS discovery and native PAC evaluation; controlled loopback source. No job target or login.'}
    trace=FixtureRequestTrace();trace.register('workbench',os.getpid())
    fixture=DnsWpadFixture(trace.observe)
    try:
        with tempfile.TemporaryDirectory(prefix='workspace-',dir=out) as directory:
            workspace=Workspace(directory);server=LocalServer(workspace,public_client=None)
            thread=threading.Thread(target=server.serve_forever,kwargs={'poll_interval':.01},daemon=True)
            thread.start()
            api=SimpleNamespace(port=server.server_port,origin=server.origin,token=server.token)
            api.call=lambda path,data=None:RunningApp.call(api,path,data)
            api.json=lambda path,data=None:RunningApp.json(api,path,data)
            try:
                with fixture:
                    verify_app(api,fixture,trace,result.setdefault('api',{}))
            finally:
                server.shutdown();server.server_close();thread.join(5)
                result['server_closed']=not thread.is_alive()
        result['success']=fixture.restored() and result['api']['success'] and result['server_closed']
    except Exception as error:
        result['error_type']=type(error).__name__
    finally:
        fixture.close()
        result['fixture']=fixture.snapshot();result['request_trace']=trace.snapshot()
        if not fixture.restored():result['success']=False
        path.write_text(json.dumps(result,indent=2),encoding='utf8')
        print(json.dumps(result))
    return 0 if result['success'] else 1

if __name__=='__main__':
    multiprocessing.freeze_support()
    raise SystemExit(main())
