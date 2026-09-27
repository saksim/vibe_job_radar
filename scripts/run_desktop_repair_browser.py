"""Real UI for simulated native crash + fixed-command repair + effective DNS.

Install commands and DNS answers are artificial; the user's Windows heap crash
is not reproduced here. Real headed startup is checked by the companion script.
"""
from __future__ import annotations

import json
import os
import socket
import ssl
import sys
import tempfile
import threading
from pathlib import Path
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from vibe_job_radar.guided.browser_health import environment_report,failed_report
from vibe_job_radar.guided.browser_install import CommandResult
from vibe_job_radar.dns_wire import Answer, ResolutionError
from vibe_job_radar.tls_diagnostic import failure_details
from vibe_job_radar.network_policy import NetworkPolicy
from vibe_job_radar.workspace import Workspace
from vibe_job_radar.workbench import LocalServer


def main():
    from playwright.sync_api import sync_playwright,expect
    out=ROOT/'browser-acceptance'/'browser-health'/'desktop-repair'
    out.mkdir(parents=True,exist_ok=True)
    result={'success':False,'checks':[],'page_errors':[],
            'scope':'Real local UI; injected native exception, install commands and DNS. Not reproduction of the user machine crash or site certification.'}
    calls=[]
    crash=failed_report({**environment_report(),'stage':'launch','executable_exists':True,'launch_tested':True},RuntimeError(
        '<launched> pid=73\n[pid=73] <process did exit: exitCode=3221226356, signal=null>'))
    try:
        with tempfile.TemporaryDirectory() as tmp:
            server=LocalServer(Workspace(tmp))
            worker=threading.Thread(target=server.serve_forever,kwargs={'poll_interval':.01},daemon=True);worker.start()
            server.guided._health_probe=lambda:crash
            server.guided._installer=lambda command,**kw:(calls.append(command) or CommandResult(0,''))
            try:
                with sync_playwright() as pw:
                    options={'headless':True}
                    if os.environ.get('RADAR_TEST_CHROMIUM'):
                        options['executable_path']=os.environ['RADAR_TEST_CHROMIUM']
                    browser=pw.chromium.launch(**options)
                    try:
                        context=browser.new_context(viewport={'width':1280,'height':960})
                        context.route('**/*',lambda route:route.continue_() if route.request.url.startswith(server.origin+'/') else route.abort())
                        page=context.new_page();page.on('pageerror',lambda e:result['page_errors'].append(str(e)))
                        page.on('dialog',lambda d:d.accept())
                        response=page.goto(server.entry_url)
                        assert response is not None
                        csp=response.headers.get('content-security-policy','')
                        assert "script-src 'self'" in csp and "'unsafe-eval'" not in csp
                        result['content_security_policy']=csp
                        result['checks'].append('application script policy remains self-only without unsafe-eval')
                        page.locator('a[href="/guided"]').click()
                        page.locator('#check-browser').click()
                        expect(page.locator('#browser-summary')).to_contain_text('堆损坏异常',timeout=15000)
                        expect(page.locator('#browser-summary')).to_contain_text('并非未安装')
                        page.locator('summary').filter(has_text='浏览器诊断').click()
                        expect(page.locator('#browser-diagnostic')).to_contain_text('0xC0000374')
                        page.locator('#repair-browser').click()
                        # Wait on the rendered text with Playwright's retrying assertion.
                        # String evaluation here fails under the application's strict CSP.
                        expect(page.locator('#environment')).to_contain_text('启动检查失败',timeout=30000)
                        assert calls==[[sys.executable,'-m','playwright','install','--force','chromium']]
                        assert not server.guided.state()['browser_health']['ready']
                        result['checks'].append('native crash is not missing files; re-download uses --force and zero exit alone does not claim launch success')
                        page.locator('#site').select_option('liepin')
                        original_dns=socket.getaddrinfo
                        def resolver(host,*args,**kwargs):
                            if host=='www.liepin.com':return [(socket.AF_INET,socket.SOCK_STREAM,6,'',('198.18.1.251',443))]
                            return original_dns(host,*args,**kwargs)
                        def exchange(host,kind,*args,**kwargs):return Answer(('93.184.216.34',) if kind==1 else (),60,host)
                        with patch('socket.getaddrinfo',side_effect=resolver),patch('vibe_job_radar.network_policy.NetworkPolicy.capture',return_value=NetworkPolicy()),patch.object(server.workspace.dns_resolver,'_exchange',side_effect=exchange) as ex:
                            page.locator('#network').click()
                            expect(page.locator('#diagnostic')).to_contain_text('encrypted_dns_consent_required')
                            ex.assert_not_called()
                            page.locator('#network-preferences summary').click()
                            page.locator('#encrypted-dns-consent').check()
                            page.locator('#save-network-preferences').click()
                            expect(page.locator('#network-dns-status')).to_contain_text('已保存')
                            page.locator('#network').click()
                            expect(page.locator('#diagnostic')).to_contain_text('effective_dns_ok')
                            expect(page.locator('#diagnostic')).to_contain_text('198.18.1.251')
                            expect(page.locator('#diagnostic')).to_contain_text('93.184.216.34')
                            assert ex.call_count==2
                            assert server.guided.ledger.summary('liepin')['request']['day']==0
                            result['checks'].append('raw Fake-IP and effective public DNS displayed separately only after saved consent; no target request or browser readiness claim')
                            # An artificial certificate exception validates visible
                            # diagnostic propagation; no trust or routing is changed.
                            server.workspace.dns_resolver.clear()
                            failure=ssl.SSLCertVerificationError(1,'PRIVATE discarded message')
                            failure.verify_code=20;failure.reason='CERTIFICATE_VERIFY_FAILED'
                            details=failure_details(failure,phase='tls_handshake')
                            ex.side_effect=ResolutionError('encrypted_dns_tls_failed',diagnostic=details)
                            page.locator('#network').click()
                            expect(page.locator('#diagnostic')).to_contain_text('certificate_verification')
                            expect(page.locator('#diagnostic')).to_contain_text('"verify_code": 20')
                            expect(page.locator('#diagnostic')).not_to_contain_text('PRIVATE')
                            page.locator('#network').click()
                            expect(page.locator('#diagnostic')).to_contain_text('上次失败证据')
                            assert ex.call_count==3
                            assert server.guided.ledger.summary('liepin')['request']['day']==0
                            result['checks'].append('TLS category and verify code visible; raw error not exported; repeated diagnostic reuses failure without another exchange')
                        page.screenshot(path=str(out/'desktop-blockers.png'),full_page=True)
                        page.locator('#upgrade-browser').click()
                        expect(page.locator('#browser-summary')).to_contain_text('重新启动工作台',timeout=15000)
                        assert server.guided.state()['installation']=='restart_required'
                        assert '--upgrade' in calls[-2] and '--force' in calls[-1]
                        page.locator('#check-browser').click()
                        expect(page.locator('#browser-summary')).to_contain_text('重新启动工作台')
                        assert not server.guided.state()['browser_health']['ready']
                        page.reload();expect(page.locator('#browser-summary')).to_contain_text('重新启动工作台')
                        page.set_viewport_size({'width':390,'height':844})
                        assert page.evaluate('() => document.documentElement.scrollWidth <= innerWidth')
                        assert not result['page_errors'];assert not server.workspace.db.exists()
                        result['checks'].append('explicit SDK update requires process restart; recheck/reload never reuse stale SDK as success; narrow viewport fits')
                        result['success']=True
                    finally:browser.close()
            finally:server.shutdown();server.server_close();worker.join(timeout=5)
    finally:(out/'results.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(result,ensure_ascii=True,indent=2))


if __name__=='__main__':main()
