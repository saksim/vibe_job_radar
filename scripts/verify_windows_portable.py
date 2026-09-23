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


def browser_health_summary(health):
    # Fixed facts only: no raw process logs, tokens, paths or credentials.
    fields=('code','stage','mode','ready','launch_tested','executable_exists',
        'process_started','process_exit_code','process_exit_hex','error_type',
        'playwright_version','browser_channel','browser_version','selection_applied')
    return {key:health[key] for key in fields if key in health}


def verify_startup_registration(app,exe,workspace,cwd,env):
    """Real HKCU writes are opt-in and confined to an ephemeral Windows runner."""
    if os.environ.get('GITHUB_ACTIONS')!='true':raise ValueError('startup registration acceptance requires ephemeral CI')
    from vibe_job_radar.windows_startup import WindowsRun,command_line,CONSENT
    state=app.json('/api/windows/startup/state')
    if state['status']!='disabled' or not state['can_enable']:raise AssertionError('startup was not initially off')
    registry=WindowsRun();name=state['value_name'];command=command_line(exe,workspace)
    if registry.read(name) is not None:raise AssertionError('refusing pre-existing startup entry')
    try:
        enabled=app.json('/api/windows/startup/enable',dict(revision=state['revision'],consent=True,consent_version=CONSENT))
        if enabled['status']!='registered' or registry.read(name)!=(1,command):raise AssertionError('exe did not register fixed startup command')
        second=RunningApp(exe,workspace,cwd,env)
        try:
            reopened=second.json('/api/windows/startup/state')
            if reopened['status']!='registered':raise AssertionError('new exe process did not observe owned startup entry')
            disabled=second.json('/api/windows/startup/disable',{'revision':reopened['revision']})
            if disabled['status']!='disabled' or registry.read(name) is not None:raise AssertionError('exe did not remove startup entry')
            if second.json('/api/public/schedule/state')['status']!='disabled':raise AssertionError('startup registration changed daily schedule')
        finally:second.close()
    finally:
        actual=registry.read(name)
        if actual==(1,command):registry.remove(name,command)
        elif actual is not None:raise AssertionError('unexpected startup value retained for inspection, not removed')
    if registry.read(name) is not None:raise AssertionError('startup acceptance did not clean up')


def verify(bundle,report_path,*,browser_choice='bundled',verify_login_startup=False):
    if verify_login_startup and os.environ.get('GITHUB_ACTIONS')!='true':
        raise ValueError('startup registration acceptance is restricted to ephemeral CI')
    if browser_choice not in ('bundled','msedge'):raise ValueError('unsupported verification browser')
    if sys.platform!='win32':raise ValueError('portable executable verification requires Windows')
    if bundle.is_symlink():raise ValueError('portable bundle is a symlink')
    bundle=bundle.resolve();exe=bundle/'VibeJobRadar.exe'
    if not exe.is_file():raise ValueError('portable executable absent')
    before=inventory(bundle)
    result={'success':False,'stage':'doctor','checks':[],'page_errors':[],'external_browser_requests':[],
        'verified_browser':browser_choice,'startup_registration_verified':False,
        'scope':'Built Windows executable with Python PATH/environment removed, artificial manual JD, original report, explicitly selected browser blank-page check. Only bundled-browser verification can qualify a build. No live recruiting certification.'}
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
                for path in ('/','/app.js','/guided','/guided.js','/advanced','/advanced.js','/network-settings.js','/public-schedule.js','/windows-startup.js'):
                    if app.call(path)[0]!=200:raise AssertionError('packaged static resource absent')
                status=app.json('/api/status')
                if status['counts']['records']!=0:raise AssertionError('packaged application did not use empty test workspace')
                state=app.json('/api/guided/state')
                if state['runtime']['kind']!='portable' or Path(state['python']).resolve()!=exe:raise AssertionError('not executing bundled application')
                public=app.json('/api/public/state')
                if {source['id'] for source in public['sources']}!={'greenhouse_anthropic','greenhouse_cloudflare'}:
                    raise AssertionError('packaged public source registry is incomplete')
                if public['task'].get('owned_elsewhere') is not False or state.get('owned_elsewhere') is not False:
                    raise AssertionError('packaged task ownership state is absent')
                if app.json('/api/public/schedule/state')['status']!='disabled':
                    raise AssertionError('packaged daily plan did not default to off')
                for mode in ('ensure','reinstall','upgrade','tls'):
                    if app.call('/api/guided/install',{'consent':True,'mode':mode})[0]!=400:
                        raise AssertionError('portable component mutation was not refused')
                result['stage']='bundled_browser_check' if browser_choice=='bundled' else 'explicit_edge_check'
                app.json('/api/guided/check_browser',{} if browser_choice=='bundled' else {'channel':'msedge','consent':True})
                deadline=time.monotonic()+60
                while time.monotonic()<deadline:
                    state=app.json('/api/guided/state')
                    if not state['busy'] and state['browser_health']['code']!='not_checked':break
                    time.sleep(.1)
                health=state['browser_health']
                result['browser_health']=browser_health_summary(health)
                if not health['ready'] or not health['launch_tested']:raise AssertionError('selected browser did not pass real blank-page check')
                if browser_choice=='bundled':
                    browser_exe=Path(health['executable_path']).resolve()
                    if bundle/'browsers' not in browser_exe.parents or not browser_exe.is_file():raise AssertionError('browser is outside portable browser directory')
                    browser_options={'executable_path':str(browser_exe)}
                else:
                    if health.get('browser_channel')!='msedge' or health.get('selection_applied') is not True:
                        raise AssertionError('explicit Edge selection was not saved')
                    browser_options={'channel':'msedge'}
                result['checks'].append('all static resources load; bundled runtime/metadata work; pip/repair mutations refused; explicitly selected browser starts and closes with original offline backend')
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
                    browser=pw.chromium.launch(headless=True,**browser_options)
                    try:
                        context=browser.new_context(viewport={'width':390,'height':844})
                        def local_only(route):
                            if route.request.url.startswith(app.origin+'/'):route.continue_()
                            else:result['external_browser_requests'].append('unexpected_nonlocal_request');route.abort()
                        context.route('**/*',local_only)
                        page=context.new_page();page.on('pageerror',lambda exc:result['page_errors'].append(type(exc).__name__))
                        page.goto(app.entry+'&report='+ident)
                        expect(page.locator('#report')).to_be_visible();expect(page.locator('#brief-capabilities')).to_contain_text('Cursor')
                        expect(page.locator('#public-source option')).to_have_count(2)
                        expect(page.locator('#public-source')).to_have_value('greenhouse_anthropic')
                        page.locator('#public-source').select_option('greenhouse_cloudflare')
                        if app.json('/api/public/state')['task']['status']!='idle':
                            raise AssertionError('source selection implicitly submitted a public query')
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
                result['checks'].append('built application accepts artificial JD and produces original report/CSV; selected real browser renders report and portable guidance at 390px without external page requests')
                result['checks'].append('packaged two-source registry and both ownership states present; source selection submits no job; daily plan stays off')
                if verify_login_startup:
                    result['stage']='startup_registration'
                    verify_startup_registration(app,exe,workspace,cwd,env)
                    result['startup_registration_verified']=True
                    result['checks'].append('ephemeral CI: actual exe explicitly registers fixed HKCU login command, second exe observes/removes it, cleanup leaves no entry or enabled daily plan; actual Windows logon not tested')
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
    parser.add_argument('--browser',choices=('bundled','msedge'),default='bundled',
        help='Explicit local verification choice; Edge results cannot qualify a portable build.')
    parser.add_argument('--verify-login-startup',action='store_true',
        help='Explicit ephemeral-CI-only startup register/read/remove acceptance; default is read-only.')
    args=parser.parse_args()
    try:result=verify(args.bundle,args.report,browser_choice=args.browser,verify_login_startup=args.verify_login_startup)
    except Exception as exc:
        # Playwright exception text may include the private loopback token URL.
        # Keep detailed stages in the structured report, never raw tracebacks.
        print(json.dumps({'success':False,'error_type':type(exc).__name__}))
        raise SystemExit(1) from None
    print(json.dumps({k:v for k,v in result.items() if k!='files'},ensure_ascii=False,indent=2))
