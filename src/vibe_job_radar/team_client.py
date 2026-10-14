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
from pathlib import Path
from urllib.parse import urlsplit

from .guided.contracts import CrawlError
from .guided.rate import RateLedger, RateLimit
from .network import SafeHTTP, FetchError
from .network_policy import current_policy
from .public_contract import PublicSource, checked_registry, ContractError
from .public_data import PublicDataClient
from .team_store import TeamError, strict_json, ident, TOKEN, safe_path, service_host
from .utils import atomic_json
from .workspace import InputError


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


def install_connection(workspace_root,config_path,credential_path):
    root=Path(workspace_root)/'.team'
    if root.exists():raise TeamError('team_connection_exists',409)
    config,registry=configuration(strict_json(Path(config_path).read_bytes()))
    credential=strict_json(Path(credential_path).read_bytes())
    if not isinstance(credential,dict) or set(credential)!={'schema_version','device_token'} or credential['schema_version']!=1 or not isinstance(credential['device_token'],str) or not TOKEN.fullmatch(credential['device_token']):
        raise TeamError('team_credential_invalid')
    encoded=credential['device_token'].encode()
    scheme='owner-file'
    if os.name=='nt':
        from .guided.saved_session import _dpapi
        encoded=_dpapi(encoded,config['origin'].encode());scheme='windows-dpapi'
    from .team_store import private_json
    from .collection import writer_lock
    root.parent.mkdir(parents=True,exist_ok=True,mode=0o700);safe_path(root)
    with writer_lock(root.parent):
        if root.exists():raise TeamError('team_connection_exists',409)
        # An interrupted installation can only leave a private staging sibling;
        # it cannot activate an incomplete connection or prevent a fresh retry.
        with tempfile.TemporaryDirectory(prefix='.team-install-',dir=root.parent) as staging:
            staged=Path(staging)
            private_json(staged/'credential.json',{'scheme':scheme,'value':base64.b64encode(encoded).decode()})
            private_json(staged/'connection.json',config)
            private_json(staged/'enrolled.json',{'schema_version':1})
            staged.rename(root)

    return {'configured':True,'restart_required':True,'credential_storage':scheme}


class TeamConnection:
    def __init__(self,workspace_root,*,transport=None,policy_factory=None):
        self.root=Path(workspace_root)/'.team'
        self.transport=transport
        self.policy_factory=policy_factory or current_policy;self._policy=None
        self.path=self.root/'connection.json'
        self._binding=None
        self._last_request=0.0
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
            if value['scheme']=='windows-dpapi' and os.name=='nt':
                from .guided.saved_session import _dpapi
                raw=_dpapi(raw,self.origin.encode(),decrypt=True)
            elif value['scheme']!='owner-file' or os.name=='nt':raise TeamError('team_credential_invalid')
            token=raw.decode('ascii')
            if not TOKEN.fullmatch(token):raise TeamError('team_credential_invalid')
            return token
        except (ValueError,UnicodeError,CrawlError) as error:raise TeamError('team_credential_invalid') from error

    def request(self,path,*,payload=None):
        if path not in {'/v1/team/state','/v1/team/metrics','/v1/team/quota','/v1/sync/read','/v1/sync/ack','/v1/jobs/search'}:
            raise TeamError('team_route_invalid')
        token=self._token()
        delay=0.1-(time.monotonic()-self._last_request)
        if delay>0:time.sleep(delay)
        self._last_request=time.monotonic()
        if self.transport is None and self._policy is None:self._policy=self.policy_factory()
        client=self.transport or SafeHTTP({urlsplit(self.origin).hostname},timeout=10,max_bytes=32_000_000,interval=0,network_policy=self._policy)
        try:
            value=client.json(self.origin+path,method='POST' if payload is not None else 'GET',
                              headers={'Authorization':'Bearer '+token},payload=payload)
        except FetchError as error:
            if error.code=='http_429':raise RateLimit(error.retry_after or 60,'team_rate_wait') from None
            # No offline quota allowance, automatic retry, redirect, token echo,
            # credential forwarding to publishers or fallback to another origin.
            code='team_unauthorized' if error.code=='http_401' else 'team_forbidden' if error.code=='http_403' else 'team_unavailable'
            raise CrawlError(code) from None
        if not isinstance(value,dict) or value.get('schema_version')!=1 or token in json.dumps(value,ensure_ascii=False):
            raise CrawlError('team_response_invalid')
        return value

    def reserve(self,site,kind,origin=None):
        return self.quota(site,'reserve',kind=kind,origin=origin)

    def quota(self,site,operation,**fields):
        resource=self.config['quota_resources'].get(site)
        if not resource:raise CrawlError('team_resource_unconfigured')
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


class AuthenticatedPublicTransport:
    def __init__(self,connection):self.connection=connection
    def json(self,url,*,method='GET',payload=None):
        if url!=self.connection.origin+'/v1/jobs/search' or method!='POST':raise FetchError('team_route_invalid')
        try:return self.connection.request('/v1/jobs/search',payload=payload)
        except RateLimit as error:raise FetchError('http_429',retry_after=error.wait) from None
        except (CrawlError,TeamError) as error:
            code={'team_unauthorized':'http_401','team_forbidden':'http_403'}.get(error.code,'team_unavailable')
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
        return {'configured':True,'origin':connection.origin,'public_service':connection.config['use_public_service'],
                'coordinated_sites':sorted(connection.config['quota_resources']),
                'message':'已安装团队连接；状态页未联网。更换配置后需重启工作台。'}
    except (TeamError,ContractError):return {'configured':True,'error':'team_connection_invalid','message':'团队连接损坏，联网任务会停止；已保存的报告仍可读取。'}


class TeamUI:
    def __init__(self,workspace):
        self.workspace=workspace;self._lock=threading.Lock();self._thread=None;self._stop=threading.Event()
        self._state={'status':'idle'}
    def state(self):
        with self._lock:return {**local_state(self.workspace.root),'sync':dict(self._state)}
    def check(self,data):
        if data!={'consent':True}:raise InputError('请明确同意连接已配置的团队服务。')
        try:return TeamConnection(self.workspace.root,policy_factory=self.workspace.network_policy).request('/v1/team/state')
        except TeamError as error:raise InputError('团队连接不可用：'+error.code) from None
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
            result={'status':'failed','code':getattr(error,'code','team_sync_failed'),'message':'同步未完成，已保存岗位和报告保留。'}
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
    sub.add_parser('check')
    sync=sub.add_parser('sync');sync.add_argument('--reset',action='store_true')
    args=parser.parse_args(argv)
    try:
        if args.command=='install':result=install_connection(args.workspace,args.config,args.credential)
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
        print(json.dumps({'ok':False,'error':getattr(error,'code','team_connection_failed')}));return 2


if __name__=='__main__':raise SystemExit(main())
