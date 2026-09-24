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
        'playwright_version','browser_channel','browser_version','selection_applied','blank_page_check')
    return {key:health[key] for key in fields if key in health}


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


def verify_public_category_input(app, category_key='architect'):
    """Actual frozen API persists this explicit route, without a site request."""
    from vibe_job_radar.public_category import get_category
    category = get_category(category_key)
    data = dict(mode='liepin_category', category_id=category_key, roles=list(category.roles), platforms=['liepin'],
                permit_platforms=['liepin'], consent=True, detail_budget=5,
                rights_note='Artificial portable category checkpoint; no upstream request.')
    preview = app.json('/api/collection/preview', data)
    if (not preview['ready'] or preview['external_network_requests'] != 0
            or preview['category_url'] != category.url
            or preview['credential_configured']):
        raise AssertionError('frozen category preview changed its scope or requires a credential')
    if app.json('/api/collection/preview', {**data, 'detail_budget': 6})['ready']:
        raise AssertionError('frozen category preview exceeds five selections')
    state = app.json('/api/collection/start', data)
    if (state['status'] != 'paused' or state['phase'] != 'category' or state['category_id'] != category_key
            or state['roles'] != list(category.roles) or state['category_outcomes'][0]['parser'] != category.parser
            or state['category_attempts'] != 0 or state['detail_attempts'] != 0
            or state['details'] or state['report_id']
            or state['category_outcomes'][0]['status'] != 'pending'
            or state['category_outcomes'][0]['submitted_keyword']):
        raise AssertionError('frozen category task changed scope or attempted acquisition before execution')
    return state['id'], state['category_outcomes']


def verify_category_next_checkpoint(app, workspace, category_key='architect'):
    """Actual frozen API consumes authored legacy-list metadata, never fetches."""
    from vibe_job_radar.utils import atomic_json
    from vibe_job_radar.public_category import get_category
    category = get_category(category_key)
    state = app.json('/api/collection/start', dict(mode='liepin_category', category_id=category_key, roles=list(category.roles),
        platforms=['liepin'], permit_platforms=['liepin'], consent=True, detail_budget=5,
        rights_note='Artificial saved category metadata only; no recruiting request or actual previous job collection.'))
    candidates = [dict(position=i, title=f'人工冻结验证{category.name}{i}',
                      url=f'https://www.liepin.com/job/{90000000000000000+i}.shtml', status='available') for i in range(1,7)]
    state.update(status='completed', phase='report', category_attempts=1, detail_attempts=5,
                 details=[dict(url=r['url'], platform='liepin', status='ok', record_id='artificial-metadata',
                    category_position=r['position'], category_title=r['title']) for r in candidates[:5]])
    state['category_outcomes'][0].update(status='ok', raw_sha256='f'*64, candidates=candidates,
                                         card_count=6, selected_positions=[1,2,3,4,5])
    if category_key == 'architect':
        state.pop('category_id')  # Original checkpoint remains readable.
    parent = workspace/'collections'/f'{state["id"]}.json'
    atomic_json(parent,state)
    before=parent.read_bytes()
    plan=app.json('/api/collection/category_next_preview',{'id':state['id']})
    if ([r['position'] for r in plan['items']] != [6] or plan['external_network_requests'] != 0
            or plan['source_url'] != category.url or plan['category_id'] != category_key
            or plan['source_time_known']):
        raise AssertionError('frozen next-batch preview changed the legacy snapshot or invented its capture time')
    args=dict(id=state['id'],fingerprint=plan['fingerprint'],consent=True)
    created=app.json('/api/collection/category_next_start',args)
    repeated=app.json('/api/collection/category_next_start',args)
    child=created['task']
    if (not created['created'] or repeated['created'] or repeated['task']['id']!=child['id']
            or child['status']!='paused' or child['category_attempts']!=0 or child['detail_attempts']!=0
            or child['report_id'] or child['category_outcomes'][0]['selected_positions']!=[6]
            or child.get('category_id','architect')!=category_key or child['roles']!=list(category.roles)
            or child['details'][0]['url']!=candidates[-1]['url'] or parent.read_bytes()!=before):
        raise AssertionError('frozen next-batch checkpoint changed the parent, repeated or requested a page')
    return child['id'],child['details'],child['category_outcomes']


def verify_category_page_checkpoint(app, workspace, category_key='architect'):
    """Actual frozen API freezes authored pagination metadata without fetching."""
    from vibe_job_radar.utils import atomic_json
    from vibe_job_radar.public_category import get_category, PAGINATION_PARSER
    category = get_category(category_key)
    state = app.json('/api/collection/start', dict(mode='liepin_category', category_id=category_key,
        roles=list(category.roles), platforms=['liepin'], permit_platforms=['liepin'], consent=True,
        detail_budget=1, rights_note='Artificial pagination metadata only; no live page or recruiting request.'))
    candidates = [dict(position=i, title=f'人工分页验证{category.name}{i}',
        url=f'https://www.liepin.com/job/{90000000000000100+i}.shtml', status='available') for i in (1,2)]
    state.update(status='completed', phase='report', category_attempts=1, detail_attempts=1,
        details=[dict(url=candidates[0]['url'], platform='liepin', status='ok', record_id='artificial-metadata',
                      category_position=1, category_title=candidates[0]['title'])])
    state['category_outcomes'][0].update(status='ok', raw_sha256='e'*64, candidates=candidates,
        card_count=2, selected_positions=[1], page_snapshot=dict(parser=PAGINATION_PARSER, page=0,
            url=category.url, status='available', next_url=category.url+'pn1/'))
    parent = workspace/'collections'/f'{state["id"]}.json'
    atomic_json(parent,state)
    before=parent.read_bytes()
    plan=app.json('/api/collection/category_page_preview',{'id':state['id']})
    if (not plan['can_start'] or plan['external_network_requests']!=0 or plan['task_created']
            or plan['current_page']!=1 or plan['next_page']!=2 or plan['remaining_on_current_page']!=1
            or plan['next_url']!=category.url+'pn1/' or plan['selection_limit']!=1):
        raise AssertionError('frozen pagination preview changed its page, budget or remaining-list evidence')
    args=dict(id=state['id'],fingerprint=plan['fingerprint'],consent=True)
    created=app.json('/api/collection/category_page_start',args)
    repeated=app.json('/api/collection/category_page_start',args)
    child=created['task'];context=child['category_page_context']
    if (not created['created'] or repeated['created'] or repeated['task']['id']!=child['id']
            or child['status']!='paused' or child['phase']!='category'
            or child['category_attempts']!=0 or child['detail_attempts']!=0 or child['details'] or child['report_id']
            or child['category_id']!=category_key or child['roles']!=list(category.roles)
            or context['page']!=1 or context['parent_id']!=state['id']
            or context['parent_fingerprint']!=plan['fingerprint'] or context['visited_urls']!=[category.url]
            or context['seen_urls']!=[row['url'] for row in candidates]
            or child['category_outcomes'][0]['url']!=category.url+'pn1/' or parent.read_bytes()!=before):
        raise AssertionError('frozen pagination checkpoint fetched, changed scope or rewrote its parent')
    same_page=app.json('/api/collection/category_next_preview',{'id':state['id']})
    if [row['position'] for row in same_page['items']]!=[2]:
        raise AssertionError('creating an adjacent-page task lost the original unselected list')
    return child['id'],context,child['category_outcomes'],parent,before


def verify_collection_shared_cooldown(app, workspace, *, register=True):
    """Actual exe default factory sees a real ledger; reserved .invalid input only."""
    from vibe_job_radar.guided.rate import RateLedger
    if register:
        app.json('/api/collection/register',dict(key='rate_acceptance',label='人工限额验收',domains='quota-probe.invalid'))
        RateLedger(workspace/'guided/rates.sqlite').cool('rate_acceptance',3600)
    state=app.json('/api/collection/start',dict(mode='urls',urls='https://quota-probe.invalid/job/1',
        roles=['architect'],platforms=['rate_acceptance'],permit_platforms=['rate_acceptance'],consent=True,
        detail_budget=1,rights_note='Artificial shared-cooldown fixture on a reserved invalid domain; no recruiting request.'))
    for _ in range(4):
        state=app.json('/api/collection/step',{'id':state['id']})
        if state['status']=='needs_attention':break
    row=state['details'][0]
    ledger=RateLedger(workspace/'guided/rates.sqlite')
    counts=ledger.summary('rate_acceptance')
    if (state['status']!='needs_attention' or row['status']!='cooldown' or state['report_id']
            or row['fetch_diagnostic']['http_attempts']!=0 or not row.get('retry_after_seconds')
            or any(counts[k]['day'] for k in counts)):
        raise AssertionError('frozen default advanced factory ignored the shared cooldown or reserved an HTTP visit')
    return state['id'],(workspace/'collections'/f'{state["id"]}.json').read_bytes()


def verify_detail_history(app, workspace):
    """Author metadata only; the actual exe must preserve it through stop/restart.

    This is not an acquisition run. No browser is opened or site requested.
    Source Python prepares a fixture, but only the frozen worker updates it.
    """
    import uuid
    from vibe_job_radar.utils import atomic_json, utc_now
    ident = uuid.uuid4().hex
    now = utc_now()
    ids = ['1'*24, '2'*24]
    attempts = [dict(sequence=i+1, selection=1, item_id=key, started_at=now, mode='navigation',
                     finished_at=now if i == 0 else None, elapsed_ms=2.5 if i == 0 else None,
                     outcome='jd_incomplete' if i == 0 else 'unfinished', record_id='')
                for i,key in enumerate(ids)]
    history = dict(version=1, origin='task_creation', checkpoint_at=now,
                   selections=[dict(sequence=1, selected_at=now, items=ids)], attempts=attempts)
    state = dict(id=ident, schema_version=1, platform='liepin', keyword='ARTIFICIAL FIXTURE',
                 search_url='https://www.liepin.com/zhaopin/?key=ARTIFICIAL+FIXTURE',
                 roles=['time_series'], max_pages=1, max_jobs=2, backend='bridge',
                 created_at=now, updated_at=now, status='paused', phase='collect', code='paused',
                 report_id='', pages_seen=[], selection=ids, detail_attempt_history=history,
                 cards=[dict(id=key, title='ARTIFICIAL METADATA ONLY', source_url='',
                     url=f'https://www.liepin.com/job/{i+1}.shtml', resolved_url='', record_id='',
                     status='jd_incomplete' if i == 0 else 'opening') for i,key in enumerate(ids)])
    path=workspace/'guided'/f'{ident}.json'
    atomic_json(path,state)
    for expected_origin in ('task_creation','legacy_partial'):
        app.json('/api/guided/action',{'id':ident,'action':'stop'})
        deadline=time.monotonic()+15
        while time.monotonic()<deadline:
            snapshot=app.json('/api/guided/state')
            row=next((r for r in snapshot['jobs'] if r['id']==ident),None)
            if not snapshot['busy'] and row and row['status']=='stopped':break
            time.sleep(.05)
        else:raise AssertionError('frozen worker did not stop artificial history task')
        saved=json.loads(path.read_text(encoding='utf-8'))
        actual=saved['detail_attempt_history']
        if actual['attempts']!=attempts or actual['origin']!=expected_origin:
            raise AssertionError('frozen worker altered earlier attempts or lost the legacy gap')
        if actual['checkpoint_at']!=saved['updated_at'] or row['browser_open']:
            raise AssertionError('frozen history checkpoint incomplete or browser unexpectedly open')
        if expected_origin=='task_creation':
            # Simulate an old checkpoint writer, which retains unknown fields
            # but cannot synchronize the newer history marker.
            saved.update(updated_at=utc_now(),status='paused',code='paused')
            atomic_json(path,saved)
    return ident, actual


def verify_idle_worker(app,exe,workspace,cwd,env,*,registered_command=None):
    """Start the actual independent executable; never enable a daily plan."""
    if app.json('/api/public/schedule/state')['status']!='disabled':
        raise AssertionError('idle worker acceptance requires a disabled plan')
    if app.json('/api/public/queue/state')['items']:
        raise AssertionError('idle worker acceptance requires an empty queue')
    # During ephemeral startup acceptance, execute the exact fixed registered
    # string with CreateProcess (shell=False), not a reconstructed argv list.
    command=registered_command if registered_command is not None else [str(exe),'--workspace',str(workspace),'--public-worker']
    child=subprocess.Popen(command,cwd=cwd,env=env,
        stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,creationflags=subprocess.CREATE_NO_WINDOW)
    observed=queue.Queue(maxsize=8)
    def read():
        for raw in child.stdout:
            if len(raw)>4096:continue
            try:value=json.loads(raw)
            except (ValueError,UnicodeError):continue
            if isinstance(value,dict) and value.get('event') in {'worker_started','worker_state','worker_failed'}:
                try:observed.put_nowait(value)
                except queue.Full:pass
    reader=threading.Thread(target=read,daemon=True);reader.start()
    try:
        started=observed.get(timeout=30);state=observed.get(timeout=10)
        if started!={'event':'worker_started','http_server':False,'browser':False}:
            raise AssertionError('frozen independent worker did not start')
        if state.get('event')!='worker_state' or state.get('plan_status')!='disabled' or state.get('retained_runs')!=0:
            raise AssertionError('frozen worker implicitly enabled a plan')
        if state.get('queue_status')!='idle' or state.get('queued_queries')!=0:
            raise AssertionError('frozen worker implicitly enqueued a query')
        if app.json('/api/public/state')['task']['status']!='idle':
            raise AssertionError('frozen idle worker created a public task')
    finally:
        if child.poll() is None:
            child.terminate()
            try:child.wait(5)
            except subprocess.TimeoutExpired:child.kill();child.wait(5)
        reader.join(5);child.stdout.close()
    if app.json('/api/public/schedule/state')['status']!='disabled':
        raise AssertionError('stopped worker changed the disabled plan')


def verify_queued_worker(exe,root,cwd,env,*,command_prefix=None,system_pac_config_id=None):
    """Original cached pipeline in a fresh workspace; no HTTP server required.

    The source harness authors one catalog and explicit queue consent. The
    separate executable must consume the unchanged cache, run its own packaged
    queue/task/report code, and leave the original rate ledger untouched.
    command_prefix is used only for the source preflight test.
    """
    from unittest.mock import patch
    from vibe_job_radar.local_public import API_URL,SOURCE,LocalPublicDataClient
    from vibe_job_radar.public_contract import PublicQuery
    from vibe_job_radar.public_queue import PublicQueue
    from vibe_job_radar.public_tasks import PublicTasks
    from vibe_job_radar.workspace import Workspace
    workspace=Workspace(root/'queued-workspace')
    if system_pac_config_id is not None:
        workspace.network_system_pac_preferences(dict(config_id=system_pac_config_id,revision=0,consent=True))
    class AuthoredWire:
        def json(self,url):
            assert url==API_URL
            return {'jobs':[{'id':81000,'absolute_url':'https://job-boards.greenhouse.io/anthropic/jobs/81000',
                'title':'Technical Architect ARTIFICIAL PORTABLE FIXTURE','location':{'name':'London'},
                'content':'<p>Independently authored test catalog, not real market data.</p>'
                          '<p>Qualifications: You must use Cursor for software architecture and code review.</p>'}],
                'meta':{'total':1}}
    with patch('urllib.request.getproxies',return_value={}):
        client=LocalPublicDataClient(workspace,transport=AuthoredWire())
        request=PublicQuery(query='Architect',source_scope=(SOURCE.key,),limit=20)
        client.search(request,consent=True)
        tasks=PublicTasks(workspace,hybrid_client=client);queue_state=PublicQueue(workspace,tasks)
        try:queue_state.enqueue({'revision':0,'consent':True,'query':request.payload()})
        finally:tasks.close()
    rate=client.ledger.path.read_bytes();cache=client.path.read_bytes()
    command=(command_prefix if command_prefix is not None else [str(exe)])+['--workspace',str(workspace.root),'--public-worker']
    child=subprocess.Popen(command,cwd=cwd,env=env,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    try:
        deadline=time.monotonic()+30
        while time.monotonic()<deadline:
            if child.poll() is not None:raise AssertionError('queued worker exited before report')
            state=queue_state.state()
            if state['history']:break
            time.sleep(.05)
        else:raise AssertionError('queued worker did not produce its original report')
        row=state['history'][0]
        if state['status']!='idle' or len(state['history'])!=1 or row['status']!='completed' or row['stale']:
            raise AssertionError('queued worker did not complete exactly once')
        observer=PublicTasks(workspace,hybrid_client=client)
        try:task=observer.snapshot()
        finally:observer.close()
        if task.get('id')!=row['task_id'] or task.get('network_requests_this_click')!=0 or task.get('cache_reused') is not True:
            raise AssertionError('queued worker did not reuse the original cache')
        report=workspace.report(row['report_id'])
        if report['manifest']['stats']['full_text_job_groups']!=1 or report['manifest']['stats']['selected_source_records']!=1:
            raise AssertionError('queued worker did not use the original complete-JD report pipeline')
        if client.ledger.path.read_bytes()!=rate or client.path.read_bytes()!=cache:
            raise AssertionError('cached queued query changed the original budget or catalog')
        if (workspace.root/'public_schedule').exists():raise AssertionError('queue enabled a daily plan')
        return {'completed_queries':1,'network_requests':0,'full_text_job_groups':1,'daily_plan_enabled':False}
    finally:
        # The one-shot query is already terminal on success. This verifies the
        # actual entry point, not normal signal cleanup or 24-hour operation.
        if child.poll() is None:
            child.terminate()
            try:child.wait(5)
            except subprocess.TimeoutExpired:child.kill();child.wait(5)


def verify_startup_registration(app,exe,workspace,cwd,env):
    """Real HKCU writes are opt-in and confined to an ephemeral Windows runner."""
    if os.environ.get('GITHUB_ACTIONS')!='true':raise ValueError('startup registration acceptance requires ephemeral CI')
    from vibe_job_radar.windows_startup import WindowsRun,command_line,MODE_CONSENT
    state=app.json('/api/windows/startup/state')
    if state['status']!='disabled' or not state['can_enable']:raise AssertionError('startup was not initially off')
    registry=WindowsRun();name=state['value_name']
    if registry.read(name) is not None:raise AssertionError('refusing pre-existing startup entry')
    for mode in ('workbench','public_worker'):
        command=command_line(exe,workspace,mode)
        state=app.json('/api/windows/startup/state')
        try:
            enabled=app.json('/api/windows/startup/enable',dict(revision=state['revision'],consent=True,consent_version=MODE_CONSENT,mode=mode))
            if (enabled['status']!='registered' or enabled['mode']!=mode
                    or registry.read(name)!=(1,command)):
                raise AssertionError('exe did not register selected fixed startup command')
            if mode=='public_worker':
                verify_idle_worker(app,exe,workspace,cwd,env,registered_command=command)
            second=RunningApp(exe,workspace,cwd,env)
            try:
                reopened=second.json('/api/windows/startup/state')
                if reopened['status']!='registered' or reopened['mode']!=mode:
                    raise AssertionError('new exe process did not observe owned startup mode')
                disabled=second.json('/api/windows/startup/disable',{'revision':reopened['revision']})
                if disabled['status']!='disabled' or registry.read(name) is not None:raise AssertionError('exe did not remove startup entry')
                if second.json('/api/public/schedule/state')['status']!='disabled':raise AssertionError('startup registration changed daily schedule')
            finally:second.close()
        finally:
            actual=registry.read(name)
            if actual==(1,command):registry.remove(name,command)
            elif actual is not None:raise AssertionError('unexpected startup value retained for inspection, not removed')
        if registry.read(name) is not None:raise AssertionError('startup acceptance did not clean up')


def verify_english_report(app,previous_id,previous_csv):
    """Use the same API in source preflight and the actual frozen executable."""
    app.json('/api/job',{'title':'Software Architect','company':'ARTIFICIAL PORTABLE FIXTURE',
        'platform':'manual','source_ref':'portable-acceptance:authored-english',
        'text':'About us:\nWe build Claude Code for software teams.\nBenefits:\nWe provide a Cursor subscription.\n'
               'Qualifications:\nYou must use Cursor. You must not upload customer secrets.',
        'rights_note':'Independently authored acceptance input, not a real job or personal achievement.',
        'evidence_level':'full_text','full_text_confirmed':True})
    english=app.json('/api/analyze',{'dataset':'real','roles':['architect']})
    if english['id']==previous_id:raise AssertionError('new report overwrote the previous run')
    rows=english['requirements']
    if (english['manifest']['rule_engine']!='rules-0.2.1'
            or english['manifest']['stats']['accepted_positive_requirement_rows']!=2
            or any('subscription' in row['quote'] or 'software teams' in row['quote'] for row in rows)
            or not any(row['quote']=='You must use Cursor.' and row['strength']=='required' for row in rows)
            or not any(row['quote']=='You must not upload customer secrets.' and row['strength']=='prohibited' for row in rows)):
        raise AssertionError('frozen English obligation extraction is incomplete')
    status,english_csv=app.call(f"/api/download/{english['id']}/requirements_zh.csv")
    if status!=200 or b'You must use Cursor.' not in english_csv or b'subscription' in english_csv:
        raise AssertionError('frozen English report CSV is incorrect')
    if app.call(f'/api/download/{previous_id}/requirements_zh.csv')!=(200,previous_csv):
        raise AssertionError('new English analysis changed the previous report CSV')
    return english


def verify_numbered_wrap_report(app, previous):
    """Check new extraction in the actual program, without changing old reports."""
    quote = '推动AI编程在研发小组中\n的应用，提升交付效率。'
    text = '【岗位职责】\n1、' + quote + '\n2、禁止使用 Codex。\n【福利待遇】\n公司提供 Cursor 会员。'
    app.json('/api/job', {'title':'软件架构师', 'company':'ARTIFICIAL WRAP FIXTURE',
        'platform':'manual', 'source_ref':'portable-acceptance:authored-wrap', 'text':text,
        'rights_note':'Independently authored wrap fixture, not a real job.',
        'evidence_level':'full_text', 'full_text_confirmed':True})
    report = app.json('/api/analyze', {'dataset':'real', 'roles':['architect']})
    rows = [r for r in report['requirements'] if r['company']=='ARTIFICIAL WRAP FIXTURE']
    if (report['id']==previous['id'] or report['manifest']['rule_engine']!='rules-0.2.1'
            or len(rows)!=4 or any(text[r['start']:r['end']]!=r['quote'] for r in rows)
            or not any(r['quote']==quote and r['strength']=='expected' and r['relation']=='direct' for r in rows)
            or any(r['strength']!='prohibited' for r in rows if 'Codex' in r['quote'])
            or any('会员' in r['quote'] for r in rows)):
        raise AssertionError('frozen numbered wrap extraction or original offsets are incorrect')
    status, data = app.call(f"/api/download/{report['id']}/requirements_zh.csv")
    if status!=200 or quote.encode('utf-8') not in data or '会员'.encode('utf-8') in data:
        raise AssertionError('frozen numbered wrap CSV is incomplete')
    if app.json('/api/report/'+previous['id'])['requirements']!=previous['requirements']:
        raise AssertionError('wrapped analysis changed previous English evidence')
    return report


def verify(bundle,report_path,*,browser_choice='bundled',verify_login_startup=False,verify_system_pac=False):
    if verify_login_startup and os.environ.get('GITHUB_ACTIONS')!='true':
        raise ValueError('startup registration acceptance is restricted to ephemeral CI')
    if verify_system_pac and os.environ.get('GITHUB_ACTIONS')!='true':
        raise ValueError('system PAC acceptance is restricted to ephemeral CI')
    if browser_choice not in ('bundled','msedge'):raise ValueError('unsupported verification browser')
    if sys.platform!='win32':raise ValueError('portable executable verification requires Windows')
    if bundle.is_symlink():raise ValueError('portable bundle is a symlink')
    bundle=bundle.resolve();exe=bundle/'VibeJobRadar.exe'
    if not exe.is_file():raise ValueError('portable executable absent')
    before=inventory(bundle)
    result={'success':False,'stage':'doctor','checks':[],'page_errors':[],'external_browser_requests':[],
        'verified_browser':browser_choice,'startup_registration_verified':False,'startup_worker_verified':False,'system_pac_verified':False,
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
                for path in ('/','/app.js','/guided','/guided.js','/advanced','/advanced.js','/network-settings.js','/public-schedule.js','/public-queue.js','/windows-startup.js'):
                    if app.call(path)[0]!=200:raise AssertionError('packaged static resource absent')
                status=app.json('/api/status')
                if status['counts']['records']!=0:raise AssertionError('packaged application did not use empty test workspace')
                state=app.json('/api/guided/state')
                if state['runtime']['kind']!='portable' or Path(state['python']).resolve()!=exe:raise AssertionError('not executing bundled application')
                public=app.json('/api/public/state')
                if {source['id'] for source in public['sources']}!={'greenhouse_anthropic','greenhouse_cloudflare','ashby_cursor'}:
                    raise AssertionError('packaged public source registry is incomplete')
                if public['task'].get('owned_elsewhere') is not False or state.get('owned_elsewhere') is not False:
                    raise AssertionError('packaged task ownership state is absent')
                result['stage']='detail_history_checkpoint'
                history_id,history_saved=verify_detail_history(app,workspace)
                result['detail_history_checkpoint_verified']=True
                result['checks'].append('actual frozen worker preserves authored failed/unfinished detail history, never opens a browser and marks an unsynchronized old-writer checkpoint as partial; no acquisition or measured latency is claimed by this fixture')
                result['stage']='public_share_input'
                public_input_checkpoints=verify_public_share_input(app)
                result['public_share_input_verified']=True
                result['public_clean_detail_input_verified']=True
                result['public_a_detail_input_verified']=True
                category_id,category_outcomes=verify_public_category_input(app)
                algorithm_id,algorithm_outcomes=verify_public_category_input(app,'algorithm')
                result['public_algorithm_category_input_verified']=True
                result['public_category_input_verified']=True
                category_next_id,category_next_details,category_next_outcomes=verify_category_next_checkpoint(app,workspace)
                algorithm_next_id,algorithm_next_details,algorithm_next_outcomes=verify_category_next_checkpoint(app,workspace,'algorithm')
                result['public_algorithm_category_next_checkpoint_verified']=True
                result['public_category_next_checkpoint_verified']=True
                result['stage']='public_category_page_checkpoint'
                category_pages=[verify_category_page_checkpoint(app,workspace,key) for key in ('architect','algorithm')]
                result['public_category_page_checkpoint_verified']=True
                result['public_algorithm_category_page_checkpoint_verified']=True
                result['stage']='advanced_shared_cooldown'
                rate_task_id,rate_task_bytes=verify_collection_shared_cooldown(app,workspace)
                result['advanced_shared_cooldown_verified']=True
                result['checks'].append('actual frozen default advanced HTTP factory reads the guided ledger and blocks a reserved .invalid fixture before HTTP; no request/page reservation or historical report')
                result['checks'].append('frozen adjacent-page API freezes authored same-category page links and ancestry, preserves the unselected old list, and returns one paused child on repeated confirmation without acquisition')
                result['checks'].append('actual frozen API explains and deduplicates synthetic Liepin share inputs, preserves unknown parameters and saves a paused checkpoint without tracking values or any collection step')
                if app.json('/api/public/schedule/state')['status']!='disabled':
                    raise AssertionError('packaged daily plan did not default to off')
                queue_default=app.json('/api/public/queue/state')
                if queue_default['status']!='idle' or queue_default['items'] or (workspace/'public_queue').exists():
                    raise AssertionError('packaged queue did not default to empty without a record')
                result['stage']='independent_idle_worker'
                verify_idle_worker(app,exe,workspace,cwd,env)
                result['public_worker_idle_verified']=True
                result['checks'].append('separate frozen exe starts the independent worker without Python PATH, keeps the plan disabled and creates no task; only the owned idle test process is terminated')
                result['stage']='independent_queued_worker'
                result['public_queue_cached_worker']=verify_queued_worker(exe,root,cwd,env)
                result['public_queue_cached_worker_verified']=True
                result['checks'].append('separate frozen worker consumes one explicitly queued query from an authored fresh cache, creates the original full-JD report with zero network requests, leaves the cache and rate ledger unchanged and enables no daily plan')
                result['stage']='native_pac_worker'
                network=app.json('/api/network/state')
                if network['proxy_mode']!='auto' or not network['pac_available']:
                    raise AssertionError('portable PAC unavailable or enabled by default')
                scripted='function FindProxyForURL(url,host){return "SOCKS5 127.0.0.1:1080; DIRECT";}'
                imported=app.json('/api/network/pac',dict(name='artificial.pac',script=scripted,
                    revision=network['revision'],consent=True))
                checked=app.json('/api/network/pac/check',{'revision':imported['revision']})
                if not checked['passed'] or checked['transport']!='loopback_socks5_proxy' or checked['target_requested']:
                    raise AssertionError('frozen WinHTTP PAC worker failed or normalized SOCKS5')
                rollback=app.json('/api/network/proxy',dict(mode='auto',endpoint='',consent=False,revision=imported['revision']))
                if rollback['schema_version']!=2 or rollback['proxy_mode']!='auto':
                    raise AssertionError('portable PAC rollback failed')
                result['pac_worker_verified']=True
                result['checks'].append('frozen exe spawns native WinHTTP PAC worker with Python PATH removed, preserves SOCKS5 return, checks only fixed domain and rolls back without any proxy or target connection')
                if verify_system_pac:
                    from system_pac_acceptance import configured_source,verify_app
                    result['stage']='configured_system_pac_setup'
                    with configured_source() as fixture:
                        counts=result['system_pac_source_request_counts']={'before_ui_check':len(fixture.requests)}
                        result['stage']='configured_system_pac_ui_check'
                        verify_app(app,fixture)
                        counts['after_ui_check']=len(fixture.requests)
                        result['stage']='configured_system_pac_cached_worker'
                        result['system_pac_cached_worker']=verify_queued_worker(exe,root/'system-pac-worker',cwd,env,
                            system_pac_config_id=fixture.source.config_id)
                        counts['after_cached_worker']=len(fixture.requests)
                        result['stage']='configured_system_pac_request_count'
                        if len(fixture.requests)!=1:raise AssertionError('cached worker implicitly downloaded PAC')
                        result['stage']='configured_system_pac_restore'
                    result['system_pac_verified']=True
                    result['checks'].append('actual frozen exe reads ephemeral CI current-user PAC URL via WinHTTP, saves v4 offline, downloads once in its own frozen child, evaluates SOCKS5 without target connection and rolls back; independent frozen worker consumes a cached query under the same policy with no extra PAC download; original CI registry value restored')
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
                result['stage']='english_report_isolation'
                previous_files=inventory(workspace/'reports'/ident)
                english=verify_english_report(app,ident,data)
                if inventory(workspace/'reports'/ident)!=previous_files:
                    raise AssertionError('English analysis changed an existing report file')
                result['english_obligation_verified']=True
                result['checks'].append('frozen English extractor excludes company/benefits, separates positive use from prohibition, exports original CSV and preserves prior report bytes')
                result['stage']='numbered_wrap_report'
                wrapped=verify_numbered_wrap_report(app,english)
                result['numbered_wrap_report_verified']=True
                result['checks'].append('frozen numbered Chinese wrap retains full original quote and offsets in report/CSV; next prohibition and company/benefits stay separate')
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
                        expect(page.locator('#public-source option')).to_have_count(3)
                        expect(page.locator('#public-source')).to_have_value('greenhouse_anthropic')
                        page.locator('#public-source').select_option('greenhouse_cloudflare')
                        page.locator('#public-source').select_option('ashby_cursor')
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
                result['checks'].append('packaged three-source registry and both ownership states present; source selection submits no job; daily plan stays off')
                if verify_login_startup:
                    result['stage']='startup_registration'
                    verify_startup_registration(app,exe,workspace,cwd,env)
                    result['startup_registration_verified']=True
                    result['startup_worker_verified']=True
                    result['checks'].append('ephemeral CI: actual exe explicitly registers both fixed HKCU launch modes, exact registered worker command runs with disabled plan, second exe observes/removes each; cleanup leaves no entry or enabled plan; actual Windows logon not tested')
            finally:app.close()
            # All requested writes finished before stopping this owned test
            # process. A fresh executable process must reopen the same report.
            result['stage']='restart_preserves_report'
            restarted=RunningApp(exe,workspace,cwd,env)
            try:
                if restarted.json('/api/report/'+ident)['id']!=ident:raise AssertionError('portable restart lost report')
                if restarted.json('/api/report/'+english['id'])['requirements']!=english['requirements']:
                    raise AssertionError('portable restart changed English evidence')
                if restarted.json('/api/report/'+wrapped['id'])['requirements']!=wrapped['requirements']:
                    raise AssertionError('portable restart changed numbered wrap evidence')
                result['numbered_wrap_restart_verified']=True
                if inventory(workspace/'reports'/ident)!=previous_files:
                    raise AssertionError('portable restart changed an existing report file')
                if restarted.json('/api/public/schedule/state')['status']!='disabled':raise AssertionError('portable restart implicitly scheduled work')
                if restarted.json('/api/guided/state')['browser_health']['ready']:raise AssertionError('portable restart trusted old browser readiness')
                histories=restarted.json('/api/guided/state')['jobs']
                history_row=next(row for row in histories if row['id']==history_id)
                if history_row['detail_attempt_history']!=history_saved or history_row['browser_open']:
                    raise AssertionError('fresh frozen process changed saved detail history')
                result['detail_history_restart_verified']=True
                for ident,details in public_input_checkpoints:
                    saved=restarted.json('/api/collection/status', {'id':ident})
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
                saved=restarted.json('/api/collection/status', {'id':category_next_id})
                if (saved['details']!=category_next_details or saved['category_outcomes']!=category_next_outcomes
                        or saved['category_attempts']!=0 or saved['detail_attempts']!=0 or saved['report_id']):
                    raise AssertionError('fresh frozen process changed or executed the paused next-batch task')
                result['public_category_next_restart_verified']=True
                saved=restarted.json('/api/collection/status', {'id':algorithm_id})
                if (saved['category_id']!='algorithm' or saved['category_outcomes']!=algorithm_outcomes
                        or saved['category_attempts']!=0 or saved['detail_attempts']!=0 or saved['report_id']):
                    raise AssertionError('frozen algorithm checkpoint lost its category or executed on restart')
                result['public_algorithm_category_restart_verified']=True
                saved=restarted.json('/api/collection/status', {'id':algorithm_next_id})
                if (saved['category_id']!='algorithm' or saved['details']!=algorithm_next_details
                        or saved['category_outcomes']!=algorithm_next_outcomes or saved['category_attempts']!=0
                        or saved['detail_attempts']!=0 or saved['report_id']):
                    raise AssertionError('frozen algorithm continuation lost its scope or executed on restart')
                result['public_algorithm_category_next_restart_verified']=True
                for page_id,context,outcomes,parent,parent_bytes in category_pages:
                    saved=restarted.json('/api/collection/status',{'id':page_id})
                    if (saved['category_page_context']!=context or saved['category_outcomes']!=outcomes
                            or saved['status']!='paused' or saved['phase']!='category'
                            or saved['category_attempts']!=0 or saved['detail_attempts']!=0
                            or saved['details'] or saved['report_id'] or parent.read_bytes()!=parent_bytes):
                        raise AssertionError('fresh frozen process changed or executed a paused adjacent-page checkpoint')
                    reopened=restarted.json('/api/collection/category_page_start',dict(
                        id=context['parent_id'],fingerprint=context['parent_fingerprint'],consent=True))
                    if reopened['created'] or reopened['task']['id']!=page_id:
                        raise AssertionError('fresh frozen process duplicated the saved adjacent-page task')
                result['public_category_page_restart_verified']=True
                result['public_algorithm_category_page_restart_verified']=True
                verify_collection_shared_cooldown(restarted,workspace,register=False)
                if (workspace/'collections'/f'{rate_task_id}.json').read_bytes()!=rate_task_bytes:
                    raise AssertionError('frozen restart changed the original cooldown outcome')
                result['advanced_shared_cooldown_restart_verified']=True
            finally:restarted.close()
            result['checks'].append('fresh exe process preserves original report, leaves daily plan off and requires a fresh browser check')
        if inventory(bundle)!=before:raise AssertionError('portable application modified its bundled components')
        result.update(success=True,stage='complete',files=before)
    except Exception as exc:
        from portable_failure_diagnostics import failure_frames
        result['error_type']=type(exc).__name__
        result['failure_frames']=failure_frames(exc)
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
    parser.add_argument('--verify-system-pac',action='store_true',help='Explicit ephemeral-CI-only configured PAC registry fixture; restores its value.')
    args=parser.parse_args()
    try:result=verify(args.bundle,args.report,browser_choice=args.browser,verify_login_startup=args.verify_login_startup,verify_system_pac=args.verify_system_pac)
    except Exception as exc:
        # Playwright exception text may include the private loopback token URL.
        # Keep detailed stages in the structured report, never raw tracebacks.
        print(json.dumps({'success':False,'error_type':type(exc).__name__}))
        raise SystemExit(1) from None
    print(json.dumps({k:v for k,v in result.items() if k!='files'},ensure_ascii=False,indent=2))
