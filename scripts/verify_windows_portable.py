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

from build_windows_portable import inventory, native_component_valid
from vibe_job_radar.guided.browser_health import HEALTH_MESSAGES


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


def verify_native_component(exe,cwd,env,channel,*,evidence=None):
    """Retain typed facts before rejecting a failed packaged-process check."""
    if channel not in {'bundled','chrome'}:raise ValueError('unsupported component check')
    if evidence is None:evidence={}
    if type(evidence) is not dict or evidence:raise ValueError('component evidence must be an empty dict')
    run=subprocess.run([str(exe),'--native-browser-check','--native-browser-channel',channel],
        cwd=cwd,env=env,capture_output=True,timeout=60,creationflags=subprocess.CREATE_NO_WINDOW)
    evidence['returncode']=run.returncode
    if len(run.stdout)>65536:raise AssertionError('native component output is oversized')
    row=json.loads(run.stdout.decode('utf-8'))
    if not isinstance(row,dict):raise AssertionError('native component output is invalid')
    for key in ('success','minimal_controller','blank_page_check','request_guard_check',
                'cleanup_verified','live_sites_certified'):
        evidence[key]=row.get(key) if type(row.get(key)) is bool else None
    choices={'runtime':{'source','portable'},'browser_channel':{'bundled','chrome','msedge'},
        'controller':{'minimal_cdp','playwright_public_cdp'},
        'stage':{'launch','blank_page','request_guard','cleanup','passed'},
        'code':set(HEALTH_MESSAGES)|{'native_check_failed','native_component_ready'}}
    for key,allowed in choices.items():
        value=row.get(key);evidence[key]=value if isinstance(value,str) and value in allowed else 'unrecognized'
    version=row.get('browser_version')
    evidence['browser_version']=version if isinstance(version,str) and re.fullmatch(r'[0-9]+(?:\.[0-9]+){1,4}',version) else ''
    count=row.get('external_connections')
    evidence['external_connections']=count if type(count) is int and 0<=count<=1000000 else None
    cleanup=row.get('cleanup');cleanup=cleanup if isinstance(cleanup,dict) else {}
    fields=['attempted','close_returned','browser_disconnected','tunnel_closed','tunnel_thread_stopped']
    if channel=='chrome':fields+=['profile_removed','profile_cleanup_ok','bridge_exited']
    evidence['cleanup']={key:cleanup.get(key) if type(cleanup.get(key)) is bool else None for key in fields}
    error=cleanup.get('close_error_type')
    evidence['cleanup']['close_error_type']=(error if isinstance(error,str) and error in
        {'','PermissionError','TimeoutExpired','OSError','RuntimeError','other'} else 'unrecognized')
    if not native_component_valid(evidence,channel):raise AssertionError('native component evidence incomplete')
    return evidence


def verify_native_components(exe,cwd,env,result):
    result['native_components']={}
    for channel in ['bundled','chrome']:
        result['stage']='native_component_'+channel
        evidence={};result['native_components'][channel]=evidence
        verify_native_component(exe,cwd,env,channel,evidence=evidence)
        result['checks'].append('actual exe checks native '+channel+' blank page, refused synthetic request, zero external connections and owned-browser cleanup')


def verify_public_share_input(app):
    """The actual packaged API prepares a synthetic share input, never fetches."""
    clean = 'https://www.liepin.com/job/123.shtml'
    shared = clean + '?pgRef=portable-artificial&skId=ARTIFICIAL-TRACKING'
    data = dict(mode='urls', roles=['architect'], platforms=['liepin'], permit_platforms=['liepin'],
                consent=True, rights_note='Artificial portable input check; no site request.',
                urls=clean+'\n'+shared, detail_budget=1)
    preview = app.json('/api/collection/preview', data)
    if (not preview['ready'] or preview['unique_url_count'] != 1
            or preview['normalized_url_count'] != 1 or preview['external_network_requests'] != 0):
        raise AssertionError('frozen share preflight did not prepare/deduplicate the copied URL')
    unknown = app.json('/api/collection/preview', {**data, 'urls': shared+'&jobId=999'})
    if unknown['normalized_url_count'] != 0:
        raise AssertionError('frozen preflight removed an unreviewed identity parameter')
    state = app.json('/api/collection/start', data)
    if (state['status'] != 'paused' or state['detail_attempts'] != 0 or state['report_id']
              or len(state['details']) != 1 or state['details'][0]['url'] != clean
              or state['details'][0]['detail_parser'] != 'liepin_public_detail_v1'
              or state['details'][0]['link_normalization']['policy'] != 'liepin_share_v1'
            or 'ARTIFICIAL-TRACKING' in json.dumps(state)):
        raise AssertionError('frozen share checkpoint changed scope, fetched or retained tracking values')
    clean_state = app.json('/api/collection/start', {**data, 'urls': clean})
    if (clean_state['status'] != 'paused' or clean_state['detail_attempts'] != 0 or clean_state['report_id']
            or len(clean_state['details']) != 1 or clean_state['details'][0]['url'] != clean
            or clean_state['details'][0]['detail_parser'] != 'liepin_public_detail_v1'
            or 'link_normalization' in clean_state['details'][0]):
        raise AssertionError('frozen clean input lost its strict parser or was incorrectly marked as a share')
    a_url = 'https://www.liepin.com/a/123.shtml'
    a_preview = app.json('/api/collection/preview', {**data, 'urls': a_url})
    if not a_preview['ready'] or a_preview['url_rows'][0]['detail_parser'] != 'liepin_public_detail_v1':
        raise AssertionError('frozen a detail input did not select the strict parser')
    a_state = app.json('/api/collection/start', {**data, 'urls': a_url})
    if (a_state['details'][0]['detail_parser'] != 'liepin_public_detail_v1'
            or a_state['detail_attempts'] != 0 or a_state['report_id']
            or a_state['status'] != 'paused' or a_state['details'][0]['url'] != a_url):
        raise AssertionError('frozen a detail checkpoint fetched or lost the selected identity')
    return [(row['id'], row['details']) for row in (state, clean_state, a_state)]


def verify_public_category_input(app):
    """Actual frozen API persists this explicit route, without a site request."""
    data = dict(mode='liepin_category', roles=['architect'], platforms=['liepin'],
                permit_platforms=['liepin'], consent=True, detail_budget=5,
                rights_note='Artificial portable category checkpoint; no upstream request.')
    preview = app.json('/api/collection/preview', data)
    if (not preview['ready'] or preview['external_network_requests'] != 0
            or preview['category_url'] != 'https://www.liepin.com/career/360321/'
            or preview['credential_configured']):
        raise AssertionError('frozen category preview changed its scope or requires a credential')
    if app.json('/api/collection/preview', {**data, 'detail_budget': 6})['ready']:
        raise AssertionError('frozen category preview exceeds five selections')
    state = app.json('/api/collection/start', data)
    if (state['status'] != 'paused' or state['phase'] != 'category'
            or state['category_attempts'] != 0 or state['detail_attempts'] != 0
            or state['details'] or state['report_id']
            or state['category_outcomes'][0]['status'] != 'pending'
            or state['category_outcomes'][0]['submitted_keyword']):
        raise AssertionError('frozen category task changed scope or attempted acquisition before execution')
    return state['id'], state['category_outcomes']


def verify_store_report_reader(app, workspace, previous):
    """The actual exe must read an initialized WAL store beside an open writer."""
    from contextlib import closing
    import sqlite3
    with closing(sqlite3.connect(Path(workspace)/'jobs.sqlite')) as writer:
        writer.execute('BEGIN IMMEDIATE')
        writer.execute("INSERT INTO events(created_at,action,status,details) VALUES('2000-01-01','uncommitted-portable-fixture','fixture','{}')")
        try:
            report=app.json('/api/analyze',{'dataset':'real','roles':['time_series']})
        finally:
            writer.rollback()
        if writer.execute("SELECT COUNT(*) FROM events WHERE action='uncommitted-portable-fixture'").fetchone()[0]:
            raise AssertionError('artificial writer was committed')
    if report['id']==previous['id'] or report['requirements']!=previous['requirements']:
        raise AssertionError('report read beside a writer lost committed requirements')
    if app.json('/api/report/'+previous['id'])['requirements']!=previous['requirements']:
        raise AssertionError('concurrent report read changed prior evidence')
    return report


def verify_payload_unchanged(bundle,before,result):
    """Preserve bounded path/hash diagnostics; never archive modified bytes."""
    result['stage']='payload_immutability'
    after=inventory(bundle)
    if after==before:return
    differences=[]
    for name in sorted(before.keys()|after.keys()):
        if before.get(name)==after.get(name):continue
        differences.append({'path':name,'before_sha256':before.get(name),'after_sha256':after.get(name)})
    result['payload_changes']={'count':len(differences),'files':differences[:100],
                               'truncated':len(differences)>100}
    raise AssertionError('portable application modified its bundled components')


def verify(bundle,report_path,*,browser_choice='bundled'):
    if browser_choice not in ('bundled','msedge'):raise ValueError('unsupported verification browser')
    if sys.platform!='win32':raise ValueError('portable executable verification requires Windows')
    if bundle.is_symlink():raise ValueError('portable bundle is a symlink')
    bundle=bundle.resolve();exe=bundle/'VibeJobRadar.exe'
    if not exe.is_file():raise ValueError('portable executable absent')
    before=inventory(bundle)
    result={'success':False,'stage':'doctor','checks':[],'page_errors':[],'external_browser_requests':[],
        'verified_browser':browser_choice,
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
            verify_native_components(exe,cwd,env,result)
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
                result['stage']='public_share_input'
                public_input_checkpoints=verify_public_share_input(app)
                result['public_share_input_verified']=True
                result['public_clean_detail_input_verified']=True
                result['public_a_detail_input_verified']=True
                category_id,category_outcomes=verify_public_category_input(app)
                result['public_category_input_verified']=True
                result['checks'].append('actual frozen API explains and deduplicates synthetic Liepin share inputs, preserves unknown parameters and saves a paused checkpoint without tracking values or any collection step')
                result['stage']='original_report'
                app.json('/api/job',{'title':'时间序列算法工程师','company':'便携包人工测试（非招聘事实）',
                    'platform':'manual','source_ref':'portable-acceptance:artificial',
                    'text':'人工夹具，不是市场岗位。要求熟练使用 Cursor 进行 AI 辅助编程，必须编写单元测试并进行代码审查。',
                    'rights_note':'独立编写的便携产物测试输入，不是外部招聘或本人业绩。',
                    'evidence_level':'full_text','full_text_confirmed':True})
                report=app.json('/api/analyze',{'dataset':'real','roles':['time_series']});ident=report['id']
                status,data=app.call(f'/api/download/{ident}/requirements_zh.csv')
                if status!=200 or b'Cursor' not in data:raise AssertionError('portable original report/download failed')
                result['stage']='existing_store_report_reader'
                reader_report=verify_store_report_reader(app,workspace,report)
                result['existing_store_report_reader_verified']=True
                result['checks'].append('actual frozen report reads the committed WAL snapshot while a separate artificial writer holds an uncommitted transaction; prior requirements remain and the artificial write is rolled back')
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
            finally:app.close()
            # All requested writes finished before stopping this owned test
            # process. A fresh executable process must reopen the same report.
            result['stage']='restart_preserves_report'
            restarted=RunningApp(exe,workspace,cwd,env)
            try:
                if restarted.json('/api/report/'+ident)['id']!=ident:raise AssertionError('portable restart lost report')
                if restarted.json('/api/report/'+reader_report['id'])['requirements']!=reader_report['requirements']:
                    raise AssertionError('portable restart changed the report read beside a writer')
                result['existing_store_report_reader_restart_verified']=True
                for public_ident,details in public_input_checkpoints:
                    saved=restarted.json('/api/collection/status', {'id':public_ident})
                    if saved['details']!=details or saved['detail_attempts']!=0 or saved['report_id']:
                        raise AssertionError('fresh frozen process changed or executed the paused public input task')
                result['public_share_restart_verified']=True
                result['public_clean_detail_restart_verified']=True
                result['public_a_detail_restart_verified']=True
                saved=restarted.json('/api/collection/status', {'id':category_id})
                if (saved['category_outcomes']!=category_outcomes or saved['category_attempts']!=0
                        or saved['details'] or saved['detail_attempts']!=0 or saved['report_id']):
                    raise AssertionError('fresh frozen process changed or executed the paused category task')
                result['public_category_restart_verified']=True
                if restarted.json('/api/public/schedule/state')['status']!='disabled':raise AssertionError('portable restart implicitly scheduled work')
                if restarted.json('/api/guided/state')['browser_health']['ready']:raise AssertionError('portable restart trusted old browser readiness')
            finally:restarted.close()
            result['checks'].append('fresh exe process preserves original report, leaves daily plan off and requires a fresh browser check')
        verify_payload_unchanged(bundle,before,result)
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
    args=parser.parse_args()
    try:result=verify(args.bundle,args.report,browser_choice=args.browser)
    except Exception as exc:
        # Playwright exception text may include the private loopback token URL.
        # Keep detailed stages in the structured report, never raw tracebacks.
        print(json.dumps({'success':False,'error_type':type(exc).__name__}))
        raise SystemExit(1) from None
    print(json.dumps({k:v for k,v in result.items() if k!='files'},ensure_ascii=False,indent=2))
