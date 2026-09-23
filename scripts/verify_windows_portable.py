"""Exercise the built executable with no Python on its PATH; no recruiting requests."""
from __future__ import annotations
import argparse
import http.client
import json
import os
from pathlib import Path
import queue
import re
import subprocess
import sys
import tempfile
import threading
import time
from urllib.parse import urlsplit

from build_windows_portable import inventory


class RunningApp:
    def __init__(self,exe,workspace,cwd,env):
        self.proc=subprocess.Popen([str(exe),'--workspace',str(workspace),'--no-browser'],cwd=cwd,env=env,
            stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NO_WINDOW)
        self.lines=queue.Queue(maxsize=16)
        def read():
            # Session tokens exist only in memory and are never written into
            # build logs/artifacts. Retain only the loopback entry for requests.
            for raw in self.proc.stdout:
                match=re.search(rb'http://127\.0\.0\.1:\d+/#token=([A-Za-z0-9_-]+)',raw)
                if match:
                    try:self.lines.put_nowait(match.group().decode('ascii'))
                    except queue.Full:pass
        self.reader=threading.Thread(target=read,daemon=True);self.reader.start()
        try:
            self.entry=self.lines.get(timeout=30);parts=urlsplit(self.entry)
            self.port=parts.port;self.origin=f'http://127.0.0.1:{self.port}';self.token=parts.fragment.removeprefix('token=')
        except Exception:
            self.close();raise RuntimeError('portable local server did not start') from None

    def call(self,path,data=None):
        conn=http.client.HTTPConnection('127.0.0.1',self.port,timeout=30)
        headers={'X-Radar-Token':self.token,'Origin':self.origin}
        if data is not None:headers['Content-Type']='application/json'
        try:
            conn.request('POST' if data is not None else 'GET',path,
                body=None if data is None else json.dumps(data).encode('utf-8'),headers=headers)
            response=conn.getresponse();raw=response.read()
            value=json.loads(raw) if 'application/json' in response.getheader('Content-Type','') else raw
            return response.status,value
        finally:conn.close()

    def json(self,path,data=None):
        status,value=self.call(path,data)
        if status!=200:raise AssertionError(f'portable local API failed: {path} [{status}]')
        return value

    def close(self):
        if self.proc.poll() is None:
            self.proc.terminate()
            try:self.proc.wait(10)
            except subprocess.TimeoutExpired:self.proc.kill();self.proc.wait(5)
        if hasattr(self,'reader'):self.reader.join(5)
        self.proc.stdout.close()


def verify(bundle,report_path):
    if sys.platform!='win32':raise ValueError('portable executable verification requires Windows')
    if bundle.is_symlink():raise ValueError('portable bundle is a symlink')
    bundle=bundle.resolve();exe=bundle/'VibeJobRadar.exe'
    if not exe.is_file():raise ValueError('portable executable absent')
    before=inventory(bundle)
    result={'success':False,'stage':'doctor','checks':[],'page_errors':[],'external_browser_requests':[],
        'scope':'Built Windows executable with Python PATH/environment removed, artificial manual JD, original report, bundled-browser blank-page check. No live recruiting certification.'}
    env={k:v for k,v in os.environ.items() if k not in {'PYTHONPATH','PYTHONHOME','VIRTUAL_ENV','CONDA_PREFIX','PLAYWRIGHT_BROWSERS_PATH'} and not k.startswith('VIBE_RADAR_')}
    env['PATH']=str(Path(os.environ['SystemRoot'])/'System32')
    env['PYTHONUTF8']='1'
    try:
        with tempfile.TemporaryDirectory(prefix='portable-runtime-') as temp:
            root=Path(temp).resolve()
            if root.parent!=Path(tempfile.gettempdir()).resolve():raise ValueError('invalid verification temporary directory')
            workspace=root/'用户工作区 with spaces';cwd=root/'different-working-directory';cwd.mkdir()
            doctor=subprocess.run([str(exe),'--doctor','--workspace',str(workspace)],cwd=cwd,env=env,
                capture_output=True,timeout=30,creationflags=subprocess.CREATE_NO_WINDOW)
            result['doctor_returncode']=doctor.returncode
            result['doctor_error_types']=sorted({item.decode('ascii') for item in
                re.findall(rb'\b([A-Z][A-Za-z]*(?:Error|Exception))\b',doctor.stderr)})
            if doctor.returncode or json.loads(doctor.stdout.decode('utf-8')).get('workspace_writable') is not True:
                raise AssertionError('portable doctor failed')
            result['checks'].append('exe runs from a different directory with no Python PATH, Unicode/spaced workspace and working SQLite')
            result['stage']='server_start'
            app=RunningApp(exe,workspace,cwd,env)
            try:
                result['stage']='resources_and_runtime'
                for path in ('/','/app.js','/guided','/guided.js','/advanced','/advanced.js','/network-settings.js','/public-schedule.js'):
                    if app.call(path)[0]!=200:raise AssertionError('packaged static resource absent')
                status=app.json('/api/status')
                if status['counts']['records']!=0:raise AssertionError('packaged application did not use empty test workspace')
                state=app.json('/api/guided/state')
                if state['runtime']['kind']!='portable' or Path(state['python']).resolve()!=exe:raise AssertionError('not executing bundled application')
                for mode in ('ensure','reinstall','upgrade','tls'):
                    if app.call('/api/guided/install',{'consent':True,'mode':mode})[0]!=400:
                        raise AssertionError('portable component mutation was not refused')
                result['stage']='bundled_browser_check'
                app.json('/api/guided/check_browser',{})
                deadline=time.monotonic()+60
                while time.monotonic()<deadline:
                    state=app.json('/api/guided/state')
                    if not state['busy'] and state['browser_health']['launch_tested']:break
                    time.sleep(.1)
                health=state['browser_health']
                if not health['ready'] or not health['launch_tested']:raise AssertionError('bundled browser did not pass real blank-page check')
                browser_exe=Path(health['executable_path']).resolve()
                if bundle/'browsers' not in browser_exe.parents or not browser_exe.is_file():raise AssertionError('browser is outside portable browser directory')
                result['checks'].append('all static resources load; bundled runtime/metadata work; pip/repair mutations refused; real packaged Chromium starts and closes with original offline backend')
                result['stage']='original_report'
                app.json('/api/job',{'title':'时间序列算法工程师','company':'便携包人工测试（非招聘事实）',
                    'platform':'manual','source_ref':'portable-acceptance:artificial',
                    'text':'人工夹具，不是市场岗位。要求熟练使用 Cursor 进行 AI 辅助编程，必须编写单元测试并进行代码审查。',
                    'rights_note':'独立编写的便携产物测试输入，不是外部招聘或本人业绩。',
                    'evidence_level':'full_text','full_text_confirmed':True})
                report=app.json('/api/analyze',{'dataset':'real','roles':['time_series']});ident=report['id']
                status,data=app.call(f'/api/download/{ident}/requirements_zh.csv')
                if status!=200 or b'Cursor' not in data:raise AssertionError('portable original report/download failed')
                result['stage']='real_browser_ui'
                from playwright.sync_api import sync_playwright,expect
                with sync_playwright() as pw:
                    browser=pw.chromium.launch(headless=True,executable_path=str(browser_exe))
                    try:
                        context=browser.new_context(viewport={'width':390,'height':844})
                        def local_only(route):
                            if route.request.url.startswith(app.origin+'/'):route.continue_()
                            else:result['external_browser_requests'].append('unexpected_nonlocal_request');route.abort()
                        context.route('**/*',local_only)
                        page=context.new_page();page.on('pageerror',lambda exc:result['page_errors'].append(type(exc).__name__))
                        page.goto(app.entry+'&report='+ident)
                        expect(page.locator('#report')).to_be_visible();expect(page.locator('#brief-capabilities')).to_contain_text('Cursor')
                        page.screenshot(path=str(report_path.parent/'portable-report-mobile.png'),full_page=True)
                        page.goto(app.origin+'/guided')
                        expect(page.locator('#environment')).to_contain_text('便携运行包')
                        expect(page.locator('#portable-runtime-note')).to_be_visible()
                        for name in ('install','repair-browser','upgrade-browser','source-runtime-help','source-browser-instructions','tls-repair'):
                            expect(page.locator('#'+name)).to_be_hidden()
                        expect(page.locator('#check-browser')).to_be_enabled()
                        if not page.evaluate('document.documentElement.scrollWidth<=innerWidth'):raise AssertionError('portable UI overflows')
                        page.screenshot(path=str(report_path.parent/'portable-guided-mobile.png'),full_page=True)
                        if result['page_errors'] or result['external_browser_requests']:raise AssertionError('portable browser UI failed')
                    finally:browser.close()
                result['checks'].append('built application accepts artificial JD and produces original report/CSV; actual bundled browser renders report and portable guidance at 390px without external page requests')
            finally:app.close()
            # All requested writes finished before stopping this owned test
            # process. A fresh executable process must reopen the same report.
            result['stage']='restart_preserves_report'
            restarted=RunningApp(exe,workspace,cwd,env)
            try:
                if restarted.json('/api/report/'+ident)['id']!=ident:raise AssertionError('portable restart lost report')
                if restarted.json('/api/public/schedule/state')['status']!='disabled':raise AssertionError('portable restart implicitly scheduled work')
                if restarted.json('/api/guided/state')['browser_health']['ready']:raise AssertionError('portable restart trusted old browser readiness')
            finally:restarted.close()
            result['checks'].append('fresh exe process preserves original report, leaves daily plan off and requires a fresh browser check')
        if inventory(bundle)!=before:raise AssertionError('portable application modified its bundled components')
        result.update(success=True,stage='complete',files=before)
    except Exception as exc:
        result['error_type']=type(exc).__name__
        raise
    finally:
        report_path.parent.mkdir(parents=True,exist_ok=True)
        report_path.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    return result


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bundle',required=True,type=Path);parser.add_argument('--report',required=True,type=Path)
    args=parser.parse_args()
    try:result=verify(args.bundle,args.report)
    except Exception as exc:
        # Playwright exception text may include the private loopback token URL.
        # Keep detailed stages in the structured report, never raw tracebacks.
        print(json.dumps({'success':False,'error_type':type(exc).__name__}))
        raise SystemExit(1) from None
    print(json.dumps({k:v for k,v in result.items() if k!='files'},ensure_ascii=False,indent=2))
