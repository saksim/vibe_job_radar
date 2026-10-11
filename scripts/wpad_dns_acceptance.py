"""Actual Windows DNS WPAD acceptance, restricted to disposable hosted CI.

The OS API, application workers, source matching, grant and fetch are real.
Only exact WPAD DNS answers and an owned HTTP PAC source are fixtures.
Private hostnames/rules exist only in bounded parent/child IPC.
"""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import queue
import subprocess
import threading
import time
import uuid

from wpad_dns_protocol import LoopbackDns, NrptLease, require_hosted, validate_names

ROOT=Path(__file__).resolve().parents[1]


def powershell():
    return str(Path(os.environ['SystemRoot'])/'System32/WindowsPowerShell/v1.0/powershell.exe')


class HostOperationError(RuntimeError):
    def __init__(self,detail):
        super().__init__('WPAD fixture host operation failed')
        self.detail=detail


def host_operation(action,payload):
    require_hosted()
    if action not in {'inspect','rules','add','remove'}:raise ValueError('unknown host operation')
    try:
        result=subprocess.run([powershell(),'-NoProfile','-NonInteractive','-File',
            str(ROOT/'scripts/wpad_dns_host.ps1'),'-Action',action],
            input=json.dumps(payload,ensure_ascii=True)+'\n',capture_output=True,text=True,
            encoding='utf8',timeout=30,creationflags=subprocess.CREATE_NO_WINDOW)
        if len(result.stdout)>65536 or len(result.stderr)>16384:raise ValueError()
        value=json.loads(result.stdout)
        if isinstance(value,dict) and value.get('error')=='host_operation_failed':
            detail={'action':action,'code':'host_operation_failed'}
            line=value.get('line')
            if type(line) is int and 0<line<2000:detail['line']=line
            raise HostOperationError(detail)
        if result.returncode or isinstance(value,dict) and 'error' in value:raise ValueError()
        return value
    except HostOperationError:
        raise
    except Exception:
        # Do not include PowerShell output, args, raw host data or exceptions.
        raise HostOperationError({'action':action,'code':'host_operation_transport_failed'}) from None


class OwnedHttp:
    def __init__(self,names,observer):
        self.names=validate_names(names);self.observer=observer
        self.proc=None;self.reader=None;self.events=queue.Queue(maxsize=4)
        self.lock=threading.Lock();self.writer_lock=threading.Lock()
        self.requests=0;self.observer_failed=False;self.protocol_failed=False
        self.final=None;self.closed=False

    def _send(self,value):
        with self.writer_lock:
            self.proc.stdin.write(json.dumps(value,ensure_ascii=True)+'\n')
            self.proc.stdin.flush()

    def start(self):
        require_hosted()
        self.proc=subprocess.Popen([powershell(),'-NoProfile','-NonInteractive','-File',
            str(ROOT/'scripts/wpad_dns_http.ps1')],stdin=subprocess.PIPE,stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,text=True,encoding='utf8',
            creationflags=subprocess.CREATE_NO_WINDOW)
        self.reader=threading.Thread(target=self._read,daemon=True);self.reader.start()
        self._send({'names':self.names})
        event=self.events.get(timeout=30)
        if event!={'event':'ready','explicit_prefixes':len(self.names)}:
            raise RuntimeError('owned WPAD HTTP listener did not start')
        return self

    def _read(self):
        try:
            while True:
                line=self.proc.stdout.readline(8193)
                if not line:break
                if len(line)>8192 or not line.endswith('\n'):raise ValueError()
                event=json.loads(line)
                if not isinstance(event,dict):raise ValueError()
                if event.get('event')=='request':
                    if (set(event)!={'event','sequence','agent','peer_port','local_port'}
                            or type(event['sequence']) is not int or not 1<=event['sequence']<=16
                            or event['agent'] not in {'project_pac','missing','other'}
                            or type(event['peer_port']) is not int or not 1<=event['peer_port']<=65535
                            or type(event['local_port']) is not int or event['local_port']!=80):
                        raise ValueError()
                    with self.lock:
                        if event['sequence']!=self.requests+1:raise ValueError()
                        self.requests+=1
                    try:
                        self.observer(('127.0.0.1',event['peer_port']),('127.0.0.1',80),
                            {'project_pac':'VibeJobRadar-PAC','missing':'','other':'OTHER'}[event['agent']])
                    except Exception:
                        with self.lock:self.observer_failed=True
                    self._send({'command':'ack','sequence':event['sequence']})
                elif event.get('event')=='closed':
                    allowed={'event','success','stage','requests','cleanup_errors','listener_closed',
                             'existing_listener_preserved','error'}
                    if set(event)!=allowed or event['stage'] not in {'input','register','serve','complete'}:
                        raise ValueError()
                    if (any(type(event[k]) is not bool for k in ('success','listener_closed','existing_listener_preserved'))
                            or any(type(event[k]) is not int or not 0<=event[k]<=17 for k in ('requests','cleanup_errors'))
                            or event['error'] not in (None,'owned_http_fixture_failed')):
                        raise ValueError()
                    with self.lock:self.final=event
                elif event.get('event')=='ready':
                    self.events.put_nowait(event)
                else:raise ValueError()
        except Exception:
            with self.lock:self.protocol_failed=True
        finally:
            try:self.events.put_nowait({'event':'eof'})
            except queue.Full:pass

    def close(self):
        if self.closed:return
        if self.proc is None:self.closed=True;return
        if self.proc.poll() is None:
            try:self._send({'command':'stop'})
            except (OSError,ValueError):pass
            try:self.proc.wait(10)
            except subprocess.TimeoutExpired:
                self.proc.kill();self.proc.wait(5)
                with self.lock:self.protocol_failed=True
        if self.reader:self.reader.join(5)
        for pipe in (self.proc.stdin,self.proc.stdout):
            if pipe:pipe.close()
        self.closed=True

    def snapshot(self):
        with self.lock:
            return {'requests':self.requests,'observer_failed':self.observer_failed,
                    'protocol_failed':self.protocol_failed,'child_closed':self.closed and
                        (self.proc is None or self.proc.poll() is not None),
                    'reader_closed':self.reader is None or not self.reader.is_alive(),
                    'cleanup':dict(self.final) if self.final is not None else None}


class DnsWpadFixture:
    def __init__(self,observer):
        self.observer=observer;self.lease=None;self.dns=None;self.http=None
        self.names=[];self.stage='preflight';self.primary_error=None
        self.cleanup_errors=0;self.closed=False;self.host_error=None

    def __enter__(self):
        require_hosted()  # before PowerShell, socket creation or settings reads
        try:
            state=host_operation('inspect',{})
            if (not isinstance(state,dict) or
                    any(state.get(k) is not True for k in ('admin','policy_allowed','service_enabled'))
                    or state.get('rules')!=[]):
                raise ValueError('host prerequisites outside fixture scope')
            self.names=validate_names(state['names'])
            self.lease=NrptLease(host_operation,'vjr-wpad-'+uuid.uuid4().hex,self.names,state['rules'])
            self.stage='dns_start'
            self.dns=LoopbackDns(self.names).start()
            self.stage='http_start'
            self.http=OwnedHttp(self.names,self.observer);self.http.start()
            self.stage='nrpt_add'
            self.lease.acquire()
            self.stage='ready'
            return self
        except BaseException as error:
            self.primary_error='fixture_setup_failed'
            if isinstance(error,HostOperationError):self.host_error=error.detail
            self.close()
            raise

    def __exit__(self,kind,error,tb):
        if kind is not None:self.primary_error='acceptance_failed'
        self.close()
        if kind is None and not self.restored():
            raise AssertionError('owned DNS WPAD fixture restoration failed')

    def close(self):
        if self.closed:return
        # Restore DNS policy while its endpoints still exist; attempt all
        # cleanup even after failed Add output or an independent cleanup error.
        for item in (self.lease,self.http,self.dns):
            if item is None:continue
            try:item.close()
            except Exception:self.cleanup_errors+=1
        self.closed=True

    def restored(self):
        h=self.http.snapshot() if self.http else {}
        d=self.dns.snapshot() if self.dns else {}
        nr=self.lease.cleanup if self.lease else {}
        return (self.closed and not self.cleanup_errors and nr.get('restored') is True
                and nr.get('errors')==0 and h.get('child_closed') is True
                and h.get('reader_closed') is True and not h.get('protocol_failed')
                and not h.get('observer_failed') and isinstance(h.get('cleanup'),dict)
                and h['cleanup'].get('success') is True and d.get('listeners_closed') is True
                and d.get('live_threads')==0 and d.get('errors')==0)

    def expected_ids(self):
        return {hashlib.sha256(('http://'+n+p+'/wpad.dat').encode('ascii')).hexdigest()
                for n in self.names for p in ('',':80')}

    def snapshot(self):
        return {'stage':self.stage,'primary_error':self.primary_error,'cleanup_errors':self.cleanup_errors,
                'restored':self.restored(),'host_error':self.host_error,'namespace_count':len(self.names),
                'namespace_hashes':sorted(hashlib.sha256(n.encode('ascii')).hexdigest() for n in self.names),
                'dns':self.dns.snapshot() if self.dns else None,
                'http':self.http.snapshot() if self.http else None,
                'nrpt_cleanup':dict(self.lease.cleanup) if self.lease else None,
                'nrpt_add_facts':self.lease.add_facts if self.lease else None,
                'nrpt_cleanup_facts':self.lease.cleanup_facts if self.lease else None,
                'nrpt_cleanup_failures':list(self.lease.cleanup_failures) if self.lease else [],
                'changes':'owned exact-name local NRPT rules and HTTP prefixes only',
                'adapter_dns_or_suffix_changes':False,'global_proxy_or_security_changes':False}


def verify_app(app,fixture,trace,evidence):
    """The same API checks run against source control and the actual frozen exe."""
    evidence.update(success=False,stage='initial_state',real_dns_wpad=False,
                    source_matched=False,offline_grant=False,script_checked=False,rolled_back=False)
    trace.stage('ui_check')
    original=app.json('/api/network/state')
    if original['proxy_mode']!='auto':raise AssertionError('WPAD requires restored original auto policy')
    for _ in range(2):app.json('/api/network/state')
    if fixture.http.snapshot()['requests'] or fixture.dns.snapshot()['queries']:
        raise AssertionError('WPAD source was requested before explicit discovery')
    evidence['stage']='discover'
    started=app.json('/api/network/wpad/discover',{'consent':True,'revision':original['revision']})
    deadline=time.monotonic()+12  # observe the unchanged 8s discovery plus owned cleanup
    current=started
    while current['wpad']['discovery_running'] and time.monotonic()<deadline:
        time.sleep(.05);current=app.json('/api/network/state')
    preview=current['wpad'];code=preview.get('code')
    from vibe_job_radar.wpad import MESSAGES
    evidence['discovery_code']=code if code in MESSAGES else 'unrecognized'
    if preview.get('code')!='wpad_source_ready' or preview.get('discovery_running'):
        raise AssertionError('actual DNS WPAD source was not discovered')
    counts=fixture.dns.snapshot()['queries']
    if (preview.get('config_id') not in fixture.expected_ids()
            or not sum(v for k,v in counts.items() if k.endswith('_owned_a'))):
        raise AssertionError('WPAD source lacks observed owned DNS/source binding')
    evidence.update(real_dns_wpad=True,source_matched=True,stage='offline_grant')
    if fixture.http.snapshot()['requests']:
        raise AssertionError('WPAD discovery fetched the PAC source')
    saved=app.json('/api/network/wpad',{'consent':True,'revision':current['revision'],'candidate':preview['candidate']})
    if saved['schema_version']!=5 or saved['proxy_mode']!='wpad' or fixture.http.snapshot()['requests']:
        raise AssertionError('WPAD grant was not offline schema 5')
    evidence.update(offline_grant=True,stage='script_check')
    checked=app.json('/api/network/pac/check',{'revision':saved['revision']})
    evidence['check_passed']=checked.get('passed') is True
    from vibe_job_radar.network_settings import PAC_MESSAGES, system_pac, wpad
    code=checked.get('code')
    evidence['check_code']=code if code in {*PAC_MESSAGES,*system_pac.MESSAGES,*wpad.MESSAGES,'pac_evaluated'} else 'unrecognized'
    if (checked.get('passed') is not True or checked.get('transport')!='loopback_socks5_proxy'
            or checked.get('target_requested') is not False or fixture.http.snapshot()['requests']!=1):
        raise AssertionError('actual WPAD fetch/evaluation failed')
    observed=trace.snapshot()
    if (observed['requests_observed']!=1 or observed['events_dropped'] or observed['root_registration_failures']
            or len(observed['events'])!=1
            or observed['events'][0]['connection_owner_scope']!='workbench'
            or observed['events'][0]['declared_client']!='project_pac'):
        raise AssertionError('PAC request was not attributed to the tested app')
    evidence.update(script_checked=True,stage='rollback')
    for _ in range(2):app.json('/api/network/state')
    if fixture.http.snapshot()['requests']!=1:raise AssertionError('WPAD status fetched source again')
    rollback=app.json('/api/network/proxy',{'mode':'auto','endpoint':'','consent':False,'revision':saved['revision']})
    if rollback['schema_version']!=2 or rollback['proxy_mode']!='auto':
        raise AssertionError('WPAD rollback failed')
    evidence.update(rolled_back=True,stage='complete',success=True)
