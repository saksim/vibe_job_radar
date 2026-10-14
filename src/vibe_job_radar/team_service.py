"""Authenticated service for reviewed public data and metadata-only coordination."""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import sqlite3
import sys
import threading
import tempfile
import time
from collections import deque
from contextlib import closing
from pathlib import Path

from .guided.rate import Limits, RateLedger, RateLimit
from .guided.contracts import CrawlError
from .public_contract import ContractError, PublicQuery
from .team_store import TeamError, TeamStore, strict_json, safe_path, private_json, service_host
from .team_catalog import TeamCatalog, read_snapshot


def initialize_service(root):
    from .collection import writer_lock
    from .team_store import remove_private_file
    root=Path(root);root.mkdir(parents=True,exist_ok=True,mode=0o700)
    names=('control.sqlite','rates.sqlite','catalog.sqlite')
    with writer_lock(root):
        for name in names:
            path=safe_path(root/name)
            if path.exists():raise TeamError('team_already_initialized',409)
        # Build complete databases privately. Ordinary failures clean the
        # staging directory; publication never replaces an existing database.
        with tempfile.TemporaryDirectory(prefix='.team-init-',dir=root) as directory:
            staged=Path(directory)
            TeamStore(staged).initialize();TeamCatalog(staged).initialize()
            created=[]
            try:
                for name in names:
                    source=staged/name;info=source.stat()
                    os.link(source,root/name)
                    created.append((root/name,(info.st_dev,info.st_ino)))
                if os.name!='nt':
                    descriptor=os.open(root,os.O_RDONLY|os.O_DIRECTORY)
                    try:os.fsync(descriptor)
                    finally:os.close(descriptor)
            except BaseException:
                for path,identity in reversed(created):remove_private_file(path,identity)
                raise


def sync_directory(path):
    if os.name=='nt':return
    descriptor=os.open(path,os.O_RDONLY|os.O_DIRECTORY)
    try:os.fsync(descriptor)
    finally:os.close(descriptor)


class TeamApplication:
    def __init__(self, root, *, host, clock=time.time):
        service_host(host)
        self.store=TeamStore(root,clock=clock);self.catalog=TeamCatalog(root,clock=clock)
        self.host=host;self.clock=clock
        self._admission_lock=threading.Lock();self._arrivals=deque(maxlen=2000)
        self._health_arrivals=deque(maxlen=2000)
        self._started=time.monotonic()
        # Startup fails on missing/corrupt state; never recreates an empty ledger.
        with self.store.connection():pass
        with self.catalog.connection():pass

    def _admit(self, *, health=False):
        now=time.monotonic()
        with self._admission_lock:
            arrivals=self._health_arrivals if health else self._arrivals
            while arrivals and arrivals[0]<=now-60:arrivals.popleft()
            if len(arrivals)>=2000:raise TeamError('team_capacity_wait',429,wait=60)
            arrivals.append(now)

    def _body(self,environ):
        if environ.get('CONTENT_TYPE','').split(';')[0].strip()!='application/json':raise TeamError()
        length=environ.get('CONTENT_LENGTH','')
        if not isinstance(length,str) or not length.isdigit() or not 0<int(length)<=16000:raise TeamError()
        raw=environ['wsgi.input'].read(int(length))
        if len(raw)!=int(length):raise TeamError()
        value=strict_json(raw)
        if not isinstance(value,dict):raise TeamError()
        return value

    def __call__(self,environ,start_response):
        status=200;headers=[]
        try:
            if (environ.get('wsgi.url_scheme')!='https' or environ.get('HTTP_HOST') not in {self.host,self.host+':443'}
                    or environ.get('HTTP_COOKIE') or environ.get('QUERY_STRING') or environ.get('HTTP_ORIGIN')
                    or environ.get('HTTP_TRANSFER_ENCODING')):
                raise TeamError('team_request_boundary',400)
            path=environ.get('PATH_INFO','');method=environ.get('REQUEST_METHOD','GET')
            if path=='/healthz' and method=='GET':
                self._admit(health=True)
                self.store.expire_rate_history()
                self.catalog.expire_changes()
                data={'status':'ready','schema_version':1}
            else:
                auth=environ.get('HTTP_AUTHORIZATION','')
                if not auth.startswith('Bearer '):raise TeamError('team_unauthorized',401)
                token=auth[7:]
                with self.store.connection() as conn:who=self.store.principal(conn,token)
                self._admit()
                # A second durable limit protects service capacity across devices
                # and restarts; source/account limits remain independently stricter.
                ingress=RateLedger(self.store.rate_path,Limits(request_interval=0.05,requests_hour=3600,requests_day=20000),clock=self.clock)
                ingress.reserve('api:'+who['tenant'],'request')
                if path=='/v1/team/state' and method=='GET':
                    data=self.store.state(token)
                    data['source_updates']=self.catalog.source_state(data['sources'])
                elif path=='/v1/team/metrics' and method=='GET':
                    data={**self.store.metrics(token),'uptime_seconds':round(time.monotonic()-self._started)}
                elif path=='/v1/team/quota' and method=='POST':data=self.store.quota(token,self._body(environ))
                elif path=='/v1/sync/ack' and method=='POST':
                    value=self._body(environ)
                    if set(value)!={'source_scope','revision'}:raise TeamError()
                    data=self.store.sync_seen(token,value['source_scope'],value['revision'],acknowledge=True)
                elif path=='/v1/sync/read' and method=='POST':
                    value=self._body(environ)
                    with self.store.connection() as conn:self.store.authorize(conn,who,value.get('source_scope'))
                    data=self.catalog.sync(who,value)
                    if data['complete']:self.store.sync_seen(token,value['source_scope'],data['revision'])
                elif path in {'/v1/catalog/sources','/v1/jobs/search'} or re.fullmatch(r'/v1/jobs/[a-zA-Z0-9_-]+\.[a-zA-Z0-9_-]+',path):
                    if path=='/v1/catalog/sources' and method=='GET':
                        state=self.store.state(token)
                        gateway=self.catalog.gateway()
                        data={'schema_version':1,'sources':[{'id':s.key,'label':s.label,'domains':list(s.domains),'contract':s.contract}
                              for s in gateway.registry.values() if s.distribution_approved and s.key in state['sources']]}
                    elif path=='/v1/jobs/search' and method=='POST':
                        value=self._body(environ);query=PublicQuery.from_dict(value)
                        with self.store.connection() as conn:self.store.authorize(conn,who,list(query.source_scope))
                        gateway=self.catalog.gateway(fresh_sources=list(query.source_scope))
                        data=gateway.search(value)
                    elif path.startswith('/v1/jobs/') and method=='GET':
                        source,ident=path.removeprefix('/v1/jobs/').split('.')
                        with self.store.connection() as conn:self.store.authorize(conn,who,[source])
                        gateway=self.catalog.gateway(fresh_sources=[source])
                        row=next((j for j in gateway.jobs if j['source']==source and j['id']==ident),None)
                        if row is None:raise TeamError('public_job_not_found',404)
                        from .utils import utc_now
                        data={'schema_version':1,'jobs':[row],'next_cursor':'','generated_at':utc_now()}
                    else:raise TeamError('team_route_not_found',404)
                else:raise TeamError('team_route_not_found',404)
        except RateLimit as error:
            status=429;data={'error':'team_rate_wait','reason':error.code}
            headers.append(('Retry-After',str(max(1,math.ceil(error.wait)))))
        except TeamError as error:
            status=error.status;data={'error':error.code}
            if error.wait is not None:headers.append(('Retry-After',str(max(1,math.ceil(error.wait)))))
        except (ContractError,ValueError,TypeError,KeyError,UnicodeError):
            status=400;data={'error':'team_request_invalid'}
        except (OSError,sqlite3.Error,CrawlError):
            status=503;data={'error':'team_storage_unavailable'}
        reason={200:'OK',400:'Bad Request',401:'Unauthorized',403:'Forbidden',404:'Not Found',409:'Conflict',429:'Too Many Requests',503:'Service Unavailable'}[status]
        body=json.dumps(data,ensure_ascii=False,allow_nan=False).encode()
        start_response(f'{status} {reason}',[('Content-Type','application/json; charset=utf-8'),('Content-Length',str(len(body))),
                       ('Cache-Control','no-store'),('X-Content-Type-Options','nosniff'),*headers])
        return [body]


def serve(root,config):
    raw=strict_json(Path(config).read_bytes())
    if (not isinstance(raw,dict) or set(raw)!={'schema_version','host','port','threads','connection_limit'}
            or raw['schema_version']!=1 or type(raw['port']) is not int or not 1024<=raw['port']<=65535
            or type(raw['threads']) is not int or not 1<=raw['threads']<=8
            or type(raw['connection_limit']) is not int or not 4<=raw['connection_limit']<=100):
        raise TeamError('team_server_config_invalid')
    app=TeamApplication(root,host=raw['host'])
    from waitress import serve as waitress_serve
    from .collection import writer_lock
    # A single process owns this local SQLite deployment. Multiple enrolled
    # devices use its HTTP API; never mount this directory on a network share.
    with writer_lock(Path(root)):
        waitress_serve(app,listen='127.0.0.1:'+str(raw['port']),threads=raw['threads'],
                       connection_limit=raw['connection_limit'],backlog=64,channel_timeout=20,
                       max_request_header_size=8192,max_request_body_size=16000,
                       trusted_proxy='127.0.0.1',trusted_proxy_count=1,
                       trusted_proxy_headers={'x-forwarded-proto'},clear_untrusted_proxy_headers=True,
                       expose_tracebacks=False,ident='vibe-radar')


def main(argv=None):
    parser=argparse.ArgumentParser(description='公开团队服务与跨设备配额管理；不读取私人工作区。')
    parser.add_argument('--data',type=Path,required=True)
    subs=parser.add_subparsers(dest='command',required=True)
    subs.add_parser('init')
    run=subs.add_parser('serve');run.add_argument('--config',type=Path,required=True)
    member=subs.add_parser('member');member.add_argument('--tenant',required=True);member.add_argument('--member',required=True)
    member.add_argument('--role',choices=['reader','collector','admin'],required=True);member.add_argument('--disable',action='store_true')
    issue=subs.add_parser('device');issue.add_argument('--tenant',required=True);issue.add_argument('--member',required=True)
    issue.add_argument('--device',required=True);issue.add_argument('--days',type=int,default=30);issue.add_argument('--output',type=Path,required=True)
    revoke=subs.add_parser('revoke');revoke.add_argument('--tenant',required=True);revoke.add_argument('--device',required=True)
    resource=subs.add_parser('resource');resource.add_argument('--file',type=Path,required=True)
    grant=subs.add_parser('grant');grant.add_argument('--tenant',required=True);grant.add_argument('--member',required=True)
    grant.add_argument('--value',required=True);grant.add_argument('--source',action='store_true');grant.add_argument('--remove',action='store_true')
    publish=subs.add_parser('publish');publish.add_argument('--snapshot',type=Path,required=True)
    withdraw=subs.add_parser('withdraw');withdraw.add_argument('--source',required=True)
    refresh=subs.add_parser('refresh');refresh.add_argument('--approval',type=Path,required=True);refresh.add_argument('--source',required=True)
    backup=subs.add_parser('backup');backup.add_argument('--output',type=Path,required=True)
    erase=subs.add_parser('erase-member');erase.add_argument('--tenant',required=True);erase.add_argument('--member',required=True)
    args=parser.parse_args(argv);store=TeamStore(args.data);catalog=TeamCatalog(args.data)
    try:
        result={'ok':True,'operation':args.command}
        if args.command=='init':initialize_service(args.data)
        elif args.command=='serve':serve(args.data,args.config)
        elif args.command=='member':store.member(args.tenant,args.member,args.role,active=not args.disable)
        elif args.command=='device':
            # Refuse an existing output before minting an otherwise orphaned token.
            if args.output.exists():raise TeamError('team_output_exists',409)
            store.issue(args.tenant,args.member,args.device,days=args.days,output=args.output)
        elif args.command=='revoke':store.revoke(args.tenant,args.device)
        elif args.command=='resource':
            value=strict_json(args.file.read_bytes())
            if not isinstance(value,dict) or set(value)!={'tenant','resource','site','origins','limits'}:raise TeamError()
            store.resource(value['tenant'],value['resource'],value['site'],value['origins'],limits=Limits(**value['limits']))
        elif args.command=='grant':store.grant(args.tenant,args.member,args.value,source=args.source,remove=args.remove)
        elif args.command=='publish':
            registry,jobs,observations=read_snapshot(args.snapshot);result=catalog.publish(registry,jobs,observed_at=observations)
        elif args.command=='withdraw':result=catalog.withdraw(args.source)
        elif args.command=='refresh':
            from .team_refresh import refresh_approved
            result=refresh_approved(args.data,args.approval,args.source)
        elif args.command=='backup':
            from .collection import writer_lock
            with writer_lock(args.data):
                store.expire_rate_history()
                catalog.expire_changes()
                safe_path(args.output)
                if args.output.exists():raise TeamError('team_output_exists',409)
                args.output.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
                # No published output exists until all three databases are
                # copied and protected. Failure leaves the same path retryable.
                with tempfile.TemporaryDirectory(prefix='.team-backup-',dir=args.output.parent) as directory:
                    staged=Path(directory)
                    for name in ('control.sqlite','rates.sqlite','catalog.sqlite'):
                        with closing(sqlite3.connect((args.data/name).resolve().as_uri()+'?mode=ro',uri=True)) as source:
                            with closing(sqlite3.connect(staged/name)) as target:source.backup(target)
                        (staged/name).chmod(0o600)
                    if os.name!='nt':
                        for path in staged.iterdir():
                            with path.open('rb') as stream:os.fsync(stream.fileno())
                    sync_directory(staged)
                    if args.output.exists():raise TeamError('team_output_exists',409)
                    identity=staged.stat()
                    staged.rename(args.output)
                    try:sync_directory(args.output.parent)
                    except OSError:
                        # Retract only our just-published directory, never an
                        # operator's replacement. TemporaryDirectory then
                        # removes the uncommitted backup and permits retry.
                        current=args.output.lstat()
                        if ((current.st_dev,current.st_ino)==(identity.st_dev,identity.st_ino)
                                and not staged.exists()):
                            args.output.rename(staged)
                        raise
        else:
            from .team_store import ident
            ident(args.tenant);ident(args.member)
            with store.connection(write=True) as conn:
                conn.execute('PRAGMA secure_delete=ON')
                device_ids=[r[0] for r in conn.execute('SELECT id FROM devices WHERE tenant=? AND member=?',(args.tenant,args.member))]
                for device in device_ids:
                    for table in ('watermarks','sync_views'):conn.execute(f'DELETE FROM {table} WHERE device=?',(device,))
                    conn.execute("DELETE FROM audit WHERE tenant=? AND target=? AND action IN ('device_issued','device_revoked')",(args.tenant,device))
                for table in ('devices','grants','source_grants','members'):
                    conn.execute(f'DELETE FROM {table} WHERE tenant=? AND member=?',(args.tenant,args.member))
                # Audit actors are operator actions, not member identities. A
                # resource/source may legitimately use the same opaque ID.
                conn.execute("DELETE FROM audit WHERE tenant=? AND target=? AND action='member_changed'",(args.tenant,args.member))
                # Resource quotas remain: deleting membership cannot refund visits.
        print(json.dumps(result,ensure_ascii=False));return 0
    except (TeamError,ContractError,CrawlError,OSError,ValueError,TypeError,ImportError,sqlite3.Error) as error:
        print(json.dumps({'ok':False,'error':getattr(error,'code','team_operation_failed')}),file=sys.stderr);return 2


if __name__=='__main__':raise SystemExit(main())
