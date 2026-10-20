"""Opt-in team connection; device credentials never enter queries or reports."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import time
import threading
import tempfile
import weakref
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit

from .guided.contracts import CrawlError
from .guided.rate import RateLedger, RateLimit
from .network import SafeHTTP, FetchError
from .network_policy import current_policy
from .public_contract import PublicSource, checked_registry, ContractError
from .public_data import PublicDataClient
from .public_cache_guard import TRANSIENT_FETCH_ERRORS
from .team_store import TeamError, strict_json, ident, TOKEN, safe_path, service_host
from .utils import atomic_json
from .workspace import InputError
from . import system_credentials as store


SYNC_CONFLICT_MESSAGES={
    'team_sync_reset_required':'同步历史已过期，请选择完整重置后重新同步。',
    'team_snapshot_changed':'同步期间目录已更新，请重新同步。',
    'team_cursor_invalid':'同步游标已失效，请重新同步。',
    'team_watermark_not_read':'服务尚未确认本次读取，请重新同步。',
    'team_watermark_invalid':'同步水位无效，请选择完整重置后重新同步。',
}


def _windows_credentials():
    return os.name == 'nt'


def origin(value):
    try:
        p=urlsplit(value)
        if (p.scheme!='https' or not p.hostname or p.username is not None or p.password is not None
                or p.port not in (None,443) or p.path not in ('','/') or p.query or p.fragment
                or any(ord(c)<33 for c in value) or len(value)>2048):raise ValueError()
        service_host(p.hostname)
    except (ValueError,TypeError) as error:raise TeamError('team_connection_invalid') from error
    return 'https://'+p.hostname


def configuration(value):
    if not isinstance(value,dict) or set(value)!={'schema_version','origin','quota_resources','public_sources','use_public_service'} or type(value['schema_version']) is not int or value['schema_version']!=1:
        raise TeamError('team_connection_invalid')
    value={**value,'origin':origin(value['origin'])}
    mapping=value['quota_resources']
    if not isinstance(mapping,dict) or len(mapping)>100 or type(value['use_public_service']) is not bool:
        raise TeamError('team_connection_invalid')
    for site,resource in mapping.items():ident(site);ident(resource)
    if not isinstance(value['public_sources'],list) or len(value['public_sources'])>100:
        raise TeamError('team_connection_invalid')
    registry={}
    for raw in value['public_sources']:
        if not isinstance(raw,dict) or set(raw)!=set(PublicSource.__dataclass_fields__) or not isinstance(raw['domains'],list):
            raise TeamError('team_connection_invalid')
        source=PublicSource(**{**raw,'domains':tuple(raw['domains'])})
        if source.key in registry or not source.distribution_approved:raise TeamError('team_distribution_unapproved',403)
        registry[source.key]=source
    if value['use_public_service'] and not registry:raise TeamError('team_sources_missing')
    return value,checked_registry(registry)


def install_connection(workspace_root,config_path,credential_path,*,encrypted=False):
    root=Path(workspace_root)/'.team'
    if root.exists():raise TeamError('team_connection_exists',409)
    config,registry=configuration(strict_json(Path(config_path).read_bytes()))
    credential=strict_json(Path(credential_path).read_bytes())
    if not isinstance(credential,dict) or set(credential)!={'schema_version','device_token'} or credential['schema_version']!=1 or not isinstance(credential['device_token'],str) or not TOKEN.fullmatch(credential['device_token']):
        raise TeamError('team_credential_invalid')
    encoded=credential['device_token'].encode()
    scheme='owner-file'
    if _windows_credentials():
        from .guided.saved_session import _dpapi
        encoded=_dpapi(encoded,config['origin'].encode());scheme='windows-dpapi'
    from .team_store import private_json
    from .collection import writer_lock
    root.parent.mkdir(parents=True,exist_ok=True,mode=0o700);safe_path(root)
    with writer_lock(root.parent):
        if root.exists():raise TeamError('team_connection_exists',409)
        if encrypted and not _windows_credentials():
            try:
                encoded = base64.b64decode(store.seal(root.parent, 'team-device', config['origin'], encoded), validate=True)
                scheme = store.SCHEME
            except store.CredentialStoreError as error:
                raise TeamError(error.code,503) from None
        # An interrupted installation can only leave a private staging sibling;
        # it cannot activate an incomplete connection or prevent a fresh retry.
        with tempfile.TemporaryDirectory(prefix='.team-install-',dir=root.parent) as staging:
            staged=Path(staging)
            private_json(staged/'credential.json',{'scheme':scheme,'value':base64.b64encode(encoded).decode()})
            private_json(staged/'connection.json',config)
            private_json(staged/'enrolled.json',{'schema_version':1})
            staged.rename(root)
            if os.name!='nt':
                parent=os.open(root.parent,os.O_RDONLY|os.O_DIRECTORY)
                try:os.fsync(parent)
                finally:os.close(parent)

    return {'configured':True,'restart_required':True,'credential_storage':scheme}


def protect_connection(workspace_root, *, expected_revision=None):
    """Explicit local migration; no team server requests or rewrite on failure."""
    from .collection import writer_lock
    with writer_lock(Path(workspace_root)):
        connection = TeamConnection(workspace_root)
        if expected_revision is not None and connection._binding != expected_revision:
            raise TeamError('team_connection_changed',409)
        value = strict_json(connection._credential)
        token = connection._token()
        if value.get('scheme') in (store.SCHEME, 'windows-dpapi'):
            return {'configured':True,'restart_required':False,'credential_storage':value['scheme'],
                    'message':'当前设备凭据已使用系统加密，原配置保留；未联网。'}
        if value.get('scheme') != 'owner-file' or _windows_credentials():
            raise TeamError('team_credential_invalid')
        try:
            payload = store.seal(connection.root.parent, 'team-device', connection.origin, token.encode('ascii'))
        except store.CredentialStoreError as error:
            raise TeamError(error.code,503) from None
        connection._read()  # Recheck the original binding after OS access.
        safe_path(connection.root/'credential.json')
        atomic_json(connection.root/'credential.json', {'scheme':store.SCHEME,'value':payload})
        return {'configured':True,'restart_required':True,'credential_storage':store.SCHEME,
                'message':'已加密迁移本机设备凭据，未连接团队服务。请停止任务并重启工作台；管理员原凭据文件与旧备份仍需按原流程保管。'}


class _RequestGate:
    """Serialize local peers through the response, then space their next send."""
    def __init__(self, key):
        self.key = key
        self.lock = threading.Lock()
        self.completed = 0.0

    @contextmanager
    def slot(self):
        with self.lock:
            delay = 0.1 - (time.monotonic() - self.completed)
            if delay > 0:
                time.sleep(delay)
            try:
                yield
            finally:
                # Include slow/failed responses: queued calls must not burst
                # together when the network unblocks. No request is retried.
                self.completed = time.monotonic()
                with _request_gates_lock:
                    _expire_request_gates(self.completed)
                    _recent_request_gates[self.key] = self


_request_gates = weakref.WeakValueDictionary()
_request_gates_lock = threading.Lock()
_recent_request_gates = {}


def _expire_request_gates(now):
    # Keep short-lived UI connections alive through their cooldown, without
    # retaining idle workspaces forever or starting one timer per request.
    for key, gate in list(_recent_request_gates.items()):
        if now - gate.completed >= 0.1:
            del _recent_request_gates[key]


def _request_gate(root):
    key = os.path.normcase(str(root.resolve()))
    with _request_gates_lock:
        _expire_request_gates(time.monotonic())
        gate = _request_gates.get(key)
        if gate is None:
            gate = _RequestGate(key)
            _request_gates[key] = gate
        return gate


class TeamConnection:
    def __init__(self,workspace_root,*,transport=None,policy_factory=None):
        self.root=Path(workspace_root)/'.team'
        self.transport=transport
        self.policy_factory=policy_factory or current_policy;self._policy=None
        self.path=self.root/'connection.json'
        self._binding=None
        self._gate = _request_gate(self.root)
        self.config,self.registry=self._read()
        self.origin=self.config['origin']

    def _read(self):
        try:
            from .guided.saved_session import _safe_file
            _safe_file(self.root,directory=True)
            for path in (self.path,self.root/'credential.json',self.root/'enrolled.json'):
                info=_safe_file(path)
                if info.st_size>200_000:raise TeamError('team_connection_invalid')
            raw=self.path.read_bytes()
            credential=(self.root/'credential.json').read_bytes()
            digest=hashlib.sha256(raw+credential).hexdigest()
            if self._binding is not None and digest!=self._binding:raise TeamError('team_connection_changed',409)
            value,registry=configuration(strict_json(raw))
            self._binding=digest
            self._credential=credential
            return value,registry
        except (OSError,ValueError,CrawlError) as error:
            if isinstance(error,TeamError):raise
            raise TeamError('team_connection_invalid',503) from error

    def _token(self):
        self._read()
        value=strict_json(self._credential)
        if not isinstance(value,dict) or set(value)!={'scheme','value'}:raise TeamError('team_credential_invalid')
        try:
            raw=base64.b64decode(value['value'],validate=True)
            if value['scheme']=='windows-dpapi' and _windows_credentials():
                from .guided.saved_session import _dpapi
                raw=_dpapi(raw,self.origin.encode(),decrypt=True)
            elif value['scheme'] == store.SCHEME:
                raw = store.restore(self.root.parent, 'team-device', self.origin, value['value'])
            elif value['scheme']!='owner-file' or _windows_credentials():raise TeamError('team_credential_invalid')
            token=raw.decode('ascii')
            if not TOKEN.fullmatch(token):raise TeamError('team_credential_invalid')
            return token
        except store.CredentialStoreError as error:raise TeamError(error.code,503) from None
        except (ValueError,UnicodeError,CrawlError) as error:raise TeamError('team_credential_invalid') from error

    def request(self,path,*,payload=None):
        if path not in {'/v1/team/state','/v1/team/metrics','/v1/team/quota','/v1/team/task','/v1/sync/read','/v1/sync/ack','/v1/jobs/search'}:
            raise TeamError('team_route_invalid')
        with self._gate.slot():
            return self._request(path, payload=payload)

    def _request(self,path,*,payload):
        token=self._token()
        if self.transport is None and self._policy is None:self._policy=self.policy_factory()
        client=self.transport or SafeHTTP({urlsplit(self.origin).hostname},timeout=10,max_bytes=32_000_000,interval=0,network_policy=self._policy,retry_after_floor=1)
        from .team_ownership import TASK_MESSAGES
        conflict_messages = {**SYNC_CONFLICT_MESSAGES, **TASK_MESSAGES}
        try:
            value=client.json(self.origin+path,method='POST' if payload is not None else 'GET',
                              headers={'Authorization':'Bearer '+token},payload=payload,error_codes={409:conflict_messages})
        except FetchError as error:
            if error.code=='http_429':raise RateLimit(error.retry_after or 60,'team_rate_wait') from None
            if error.code in conflict_messages:raise CrawlError(error.code) from None
            # No offline quota allowance, automatic retry, redirect, token echo,
            # credential forwarding to publishers or fallback to another origin.
            code='team_unauthorized' if error.code=='http_401' else 'team_forbidden' if error.code=='http_403' else 'team_unavailable'
            raise _TeamTransportError(code, error.code) from None
        if not isinstance(value,dict) or value.get('schema_version')!=1 or token in json.dumps(value,ensure_ascii=False):
            raise CrawlError('team_response_invalid')
        return value

    def reserve(self,site,kind,origin=None):
        return self.quota(site,'reserve',kind=kind,origin=origin)

    def quota(self,site,operation,**fields):
        resource=self.config['quota_resources'].get(site)
        if not resource:raise CrawlError('team_resource_unconfigured')
        if operation == 'reserve':
            from .team_ownership import reservation_proof
            lease = reservation_proof(self,site)
            if lease is not None:fields['lease'] = lease
        value=self.request('/v1/team/quota',payload={'resource':resource,'site':site,'operation':operation,**fields})
        if set(value)!={'schema_version','result'} or not isinstance(value['result'],dict):raise CrawlError('team_response_invalid')
        return value['result']


class TeamLedger(RateLedger):
    def __init__(self,path,connection,limits=None,clock=time.time):
        self.team=connection
        super().__init__(path,limits,clock=clock)

    def _before_reservation(self,site,kind,origin):
        try:self.team.reserve(site,kind,origin)
        except TeamError as error:raise CrawlError(error.code) from None

    def set_publisher(self,site,origin,*,delay=0,requests=None,seconds=None):
        super().set_publisher(site,origin,delay=delay,requests=requests,seconds=seconds)
        try:self.team.quota(site,'publisher',origin=origin,delay=delay,requests=requests,seconds=seconds)
        except TeamError as error:raise CrawlError(error.code) from None

    def defer(self,site,seconds):
        until=super().defer(site,seconds)
        try:self.team.quota(site,'defer',seconds=seconds)
        except TeamError as error:raise CrawlError(error.code) from None
        return until


def configured(workspace_root):
    return (Path(workspace_root)/'.team').exists()


def workspace_ledger(workspace_root,path,limits=None,*,clock=time.time,local_factory=RateLedger,policy_factory=None):
    if not configured(workspace_root):
        if limits is None and clock is time.time:return local_factory(path)
        if clock is time.time:return local_factory(path,limits)
        return local_factory(path,limits,clock=clock)
    try:connection=TeamConnection(workspace_root,policy_factory=policy_factory)
    except (TeamError,ContractError) as error:
        # Broken optional coordination does not stop offline reports/UI startup.
        # Every attempted reservation still fails before an upstream request.
        class Unavailable:
            def reserve(self,*args,**kwargs):raise CrawlError('team_connection_invalid')
            def quota(self,*args,**kwargs):raise CrawlError('team_connection_invalid')
        connection=Unavailable()
    return TeamLedger(path,connection,limits,clock=clock)


class _TeamTransportError(CrawlError):
    """Keep the existing team error contract plus a safe cache-only classification."""
    def __init__(self, code, fetch_code):
        super().__init__(code)
        self.transient_fetch_code = fetch_code if fetch_code in TRANSIENT_FETCH_ERRORS else None


class AuthenticatedPublicTransport:
    def __init__(self,connection):self.connection=connection
    def json(self,url,*,method='GET',payload=None):
        if url!=self.connection.origin+'/v1/jobs/search' or method!='POST':raise FetchError('team_route_invalid')
        try:return self.connection.request('/v1/jobs/search',payload=payload)
        except RateLimit as error:raise FetchError('http_429',retry_after=error.wait) from None
        except (CrawlError,TeamError) as error:
            code={'team_unauthorized':'http_401','team_forbidden':'http_403'}.get(error.code,'team_unavailable')
            if isinstance(error, _TeamTransportError) and error.transient_fetch_code:
                code = error.transient_fetch_code
            raise FetchError(code) from None


def public_client(workspace):
    from .local_public import LocalPublicDataClient
    if not configured(workspace.root):return LocalPublicDataClient(workspace)
    try:
        connection=TeamConnection(workspace.root,policy_factory=workspace.network_policy)
        if not connection.config['use_public_service']:return LocalPublicDataClient(workspace)
        return PublicDataClient(workspace.root/'team_public_cache',connection.origin,connection.registry,
                                transport=AuthenticatedPublicTransport(connection))
    except (TeamError,ContractError):
        # Do not silently switch a broken remote configuration to direct access.
        class Unavailable:
            registry={};execution_mode='remote_service';origin=''
            def _scope(self,*args):raise InputError('团队服务配置不可用，请修复原连接并重启。已保存报告仍可读取。')
            def search(self,*args,**kwargs):return self._scope()
        return Unavailable()


def local_state(workspace_root):
    if not configured(workspace_root):return {'configured':False,'message':'未启用团队连接，当前按原本机模式工作。'}
    try:
        connection=TeamConnection(workspace_root)
        credential = strict_json(connection._credential)
        if not isinstance(credential,dict) or set(credential) != {'scheme','value'}:raise TeamError('team_credential_invalid')
        scheme = credential.get('scheme')
        if scheme not in {'owner-file','windows-dpapi',store.SCHEME}:raise TeamError('team_credential_invalid')
        return {'configured':True,'origin':connection.origin,'public_service':connection.config['use_public_service'],
                'coordinated_sites':sorted(connection.config['quota_resources']),
                'credential_storage':scheme,'credential_revision':connection._binding,
                'credential_migration_available':scheme == 'owner-file' and store.available(),
                'message':('已安装团队连接；状态页未联网、未解锁密钥库。更换配置后需重启工作台。'
                    + ('当前设备凭据仅受文件权限保护，可明确迁移到系统加密。' if scheme == 'owner-file'
                       else '当前设备凭据使用系统加密；锁定或授权失效时新请求停止。'))}
    except (TeamError,ContractError):return {'configured':True,'error':'team_connection_invalid','message':'团队连接损坏，联网任务会停止；已保存的报告仍可读取。'}


class TeamUI:
    def __init__(self,workspace):
        self.workspace=workspace;self._lock=threading.Lock();self._thread=None;self._stop=threading.Event()
        self._state={'status':'idle'}
    def state(self):
        with self._lock:return {**local_state(self.workspace.root),'sync':dict(self._state)}
    def protect(self,data):
        if (not isinstance(data,dict) or set(data) != {'consent','revision'} or data['consent'] is not True
                or not isinstance(data['revision'],str) or not re.fullmatch('[0-9a-f]{64}',data['revision'])):
            raise InputError('请明确同意加密迁移当前设备凭据；不接受令牌或额外参数。')
        with self._lock:
            if self._thread is not None and self._thread.is_alive():raise InputError('请等待团队同步完成后再迁移。')
            try:return protect_connection(self.workspace.root,expected_revision=data['revision'])
            except TeamError as error:
                raise InputError(store.MESSAGES.get(error.code,'团队配置已变化或不可读取，请刷新并核对原连接。')) from None
    def check(self,data):
        if data!={'consent':True}:raise InputError('请明确同意连接已配置的团队服务。')
        try:return TeamConnection(self.workspace.root,policy_factory=self.workspace.network_policy).request('/v1/team/state')
        except TeamError as error:raise InputError(store.MESSAGES.get(error.code,'团队连接不可用：'+error.code)) from None
    def sync(self,data):
        if set(data)-{'consent','reset'} or data.get('consent') is not True or type(data.get('reset',False)) is not bool:
            raise InputError('请明确确认同步已配置的公开来源。')
        with self._lock:
            if self._stop.is_set():raise InputError('工作台正在退出。')
            if self._thread is not None and self._thread.is_alive():raise InputError('团队同步正在执行，请等待当前结果。')
            self._state={'status':'running','message':'正在同步已批准的公开目录。'}
            self._thread=threading.Thread(target=self._run,args=(data.get('reset',False),),daemon=True)
            self._thread.start();return dict(self._state)
    def _run(self,reset):
        from .team_sync import TeamSync
        try:
            connection=TeamConnection(self.workspace.root,policy_factory=self.workspace.network_policy)
            result={'status':'completed',**TeamSync(self.workspace.root,connection=connection).run(reset=reset,cancelled=self._stop)}
        except (TeamError,RuntimeError,ContractError,OSError,ValueError) as error:
            code=getattr(error,'code','team_sync_failed')
            result={'status':'failed','code':code,'message':{**SYNC_CONFLICT_MESSAGES,**store.MESSAGES}.get(code,'同步未完成，已保存岗位和报告保留。')}
        with self._lock:self._state=result
    def close(self):
        self._stop.set()
        if self._thread is not None:self._thread.join(timeout=12)


def main(argv=None):
    import argparse
    parser=argparse.ArgumentParser(description='安装或检查可选团队连接；设备凭据从本机文件读取，不写入命令行。')
    parser.add_argument('--workspace',type=Path,required=True)
    sub=parser.add_subparsers(dest='command',required=True)
    install=sub.add_parser('install');install.add_argument('--config',type=Path,required=True);install.add_argument('--credential',type=Path,required=True)
    install.add_argument('--encrypted',action='store_true',help='明确使用系统密钥库加密；不可用时停止安装')
    sub.add_parser('protect-credential',help='将原 owner-file 设备凭据明确迁移到系统加密；未联网')
    sub.add_parser('check')
    sync=sub.add_parser('sync');sync.add_argument('--reset',action='store_true')
    args=parser.parse_args(argv)
    try:
        if args.command=='install':result=install_connection(args.workspace,args.config,args.credential,encrypted=args.encrypted)
        elif args.command=='protect-credential':result=protect_connection(args.workspace)
        else:
            from .workspace import Workspace
            workspace=Workspace(args.workspace)
            try:
                connection=TeamConnection(args.workspace,policy_factory=workspace.network_policy)
                if args.command=='check':result=connection.request('/v1/team/state')
                else:
                    from .team_sync import TeamSync
                    result=TeamSync(args.workspace,connection=connection).run(reset=args.reset)
            finally:workspace.close_network()
        print(json.dumps(result,ensure_ascii=False));return 0
    except (TeamError,CrawlError,ContractError,OSError,ValueError) as error:
        print(json.dumps({'ok':False,'error':getattr(error,'code','team_connection_failed'),
                          'message':store.MESSAGES.get(getattr(error,'code',''),'团队操作未完成，原配置保留。')},ensure_ascii=False));return 2


if __name__=='__main__':raise SystemExit(main())
