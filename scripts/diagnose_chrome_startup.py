"""One bounded first launch; no site, profile reuse or certificate changes."""
import argparse,json,sys,time
from pathlib import Path
from threading import Event
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'src'))
from vibe_job_radar.guided.cdp_browser import CDPBrowser,chrome_executable
from vibe_job_radar.guided.cdp_connection import CDPConnection
from vibe_job_radar.guided.native_tunnel import NativeTunnel
from vibe_job_radar.network_policy import NetworkPolicy
p=argparse.ArgumentParser();p.add_argument('--variant',choices=['baseline','blank','sdk'],required=True);a=p.parse_args()
result={'variant':a.variant,'scope':'owned first Chrome headless startup only; empty allowlist; no site or TLS trust edits','commands':[]}
call=CDPConnection.call
allowed={'Browser.getVersion','Target.setDiscoverTargets','Target.getTargets','Browser.close'}
def measured(self,session,method,params,**kw):
 row={'method':method if method in allowed else 'other'};result['commands'].append(row);start=time.monotonic()
 try:
  value=call(self,session,method,params,**kw);row['success']=True;return value
 except Exception as exc:
  row.update(success=False,type=type(exc).__name__,code=getattr(exc,'code',''),bridge_running=self.socket.process.poll() is None,transport_failed=self.socket.failed.is_set());raise
 finally:row['elapsed_ms']=round((time.monotonic()-start)*1000)
CDPConnection.call=measured
from vibe_job_radar.guided import cdp_browser
popen=cdp_browser.subprocess.Popen
def launch(command,**kwargs):
 command=list(command)
 if a.variant=='blank':
  args=json.loads(command[-1]);args.remove('--no-startup-window');args.append('about:blank');command[-1]=json.dumps(args)
 return popen(command,**kwargs)
b=None;runtime=None;tunnel=NativeTunnel((),NetworkPolicy.capture(),Event());start=time.monotonic()
args=['--disable-background-networking','--disable-quic','--disable-sync','--force-webrtc-ip-handling-policy=disable_non_proxied_udp','--proxy-bypass-list=<-loopback>','--block-new-web-contents']
try:
 if a.variant=='sdk':
  from playwright.sync_api import sync_playwright
  runtime=sync_playwright().start();b=runtime.chromium.launch(channel='chrome',headless=True,args=args,proxy={'server':tunnel.endpoint},timeout=30000)
 else:
  with patch.object(cdp_browser.subprocess,'Popen',launch):b=CDPBrowser(executable_path=chrome_executable(),headless=True,args=args,proxy={'server':tunnel.endpoint},timeout=30000)
 result.update(success=True,version=b.version)
 if a.variant!='sdk':
  targets=b.connection.root.send('Target.getTargets')['targetInfos']
  result['targets']=[{'type':r.get('type'),'is_blank':r.get('url')=='about:blank','has_context':bool(r.get('browserContextId'))} for r in targets]
except Exception as exc:result.update(success=False,error_type=type(exc).__name__,code=getattr(exc,'code',''),timeout='timeout' in type(exc).__name__.lower())
finally:
 result['startup_elapsed_ms']=round((time.monotonic()-start)*1000)
 try:
  if b:b.close()
 finally:
  if runtime:runtime.stop()
  tunnel.close()
 Path('startup-comparison.json').write_text(json.dumps(result,indent=2),encoding='utf-8');print(json.dumps(result))
