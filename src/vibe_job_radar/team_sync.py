"""Explicit, bounded full/delta synchronization; acknowledge only after commit."""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

from .collection import writer_lock
from .public_contract import PublicQuery, validate_batch, ID
from .team_catalog import encoded
from .team_client import TeamConnection
from .team_store import TeamError, strict_json, safe_path
from .utils import atomic_json, parse_time, utc_now


class TeamSync:
    def __init__(self,workspace_root,*,connection=None,clock=time.time):
        self.clock=clock
        self.connection=connection or TeamConnection(workspace_root)
        self.root=Path(workspace_root)/'team_sync'
        self.sources=sorted(self.connection.registry)
        self.binding=hashlib.sha256(encoded([self.connection.origin,self.sources]).encode()).hexdigest()

    def run(self,*,reset=False,cancelled=None):
        def check_cancelled():
            if cancelled is not None and cancelled.is_set():raise TeamError('team_sync_cancelled',409)
        check_cancelled()
        if not self.sources:raise TeamError('team_sources_missing')
        if type(reset) is not bool:raise TeamError()
        self.root.mkdir(mode=0o700,exist_ok=True)
        path=safe_path(self.root/'snapshot.json')
        with writer_lock(self.root):
            after=0;jobs={};sizes={};byte_count=0;generated_at=None
            # Explicit reset reads no old bytes; failed downloads still leave the
            # old file intact until the fully validated replacement is ready.
            if path.exists() and not reset:
                if path.stat().st_size>51_000_000:raise TeamError('team_sync_cache_invalid')
                saved=strict_json(path.read_bytes())
                fields={'schema_version','binding','revision','jobs'}
                if (not isinstance(saved,dict) or type(saved.get('schema_version')) is not int
                        or not (saved['schema_version']==1 and set(saved)==fields
                                or saved['schema_version']==2 and set(saved)==fields|{'generated_at'})
                        or saved['binding']!=self.binding
                        or type(saved['revision']) is not int or saved['revision']<0
                        or not isinstance(saved['jobs'],list) or len(saved['jobs'])>10000):raise TeamError('team_sync_cache_invalid')
                after=saved['revision']
                # Read the original v1 cache under its original time rule, then
                # persist the server envelope on the next successful sync.
                generated_at=self._stamp(saved.get('generated_at',utc_now()))
                for job in saved['jobs']:
                    checked=self._job(job,generated_at)
                    key=(checked['source'],checked['id'])
                    if key in jobs:raise TeamError('team_sync_cache_invalid')
                    jobs[key]=checked;sizes[key]=len(encoded(checked).encode());byte_count+=sizes[key]
            cursor='';revision=None;mode=None;deadline=time.monotonic()+300;events=0
            for page in range(2001):
                check_cancelled()
                if time.monotonic()>deadline:raise TeamError('team_sync_time_limit',409)
                value=self.connection.request('/v1/sync/read',payload={'source_scope':self.sources,'after_revision':after,'cursor':cursor,'limit':50})
                if (not isinstance(value,dict) or set(value)!={'schema_version','mode','events','next_cursor','revision','complete','sources','generated_at'}
                        or type(value['schema_version']) is not int or value['schema_version']!=1
                        or value['mode'] not in {'snapshot','changes'} or not isinstance(value['events'],list)
                        or len(value['events'])>50 or type(value['revision']) is not int or value['revision']<after
                        or type(value['complete']) is not bool or not isinstance(value['next_cursor'],str)
                        or len(value['next_cursor'])>4096 or value['complete']!=(not bool(value['next_cursor']))):
                    raise TeamError('team_sync_response_invalid')
                stamp=self._stamp(value['generated_at'])
                if generated_at is None or parse_time(stamp)>parse_time(generated_at):generated_at=stamp
                if revision is None:revision=value['revision'];mode=value['mode']
                if revision!=value['revision'] or mode!=value['mode'] or mode!=('snapshot' if after==0 else 'changes'):
                    raise TeamError('team_sync_response_invalid')
                for event in value['events']:
                    expected={'source','id','kind','job'}|({'revision'} if mode=='changes' else set())
                    if (not isinstance(event,dict) or set(event)!=expected or event['source'] not in self.sources
                            or not isinstance(event['id'],str) or not (ID.fullmatch(event['id']) or event['kind']=='observed' and event['id']=='')):
                        raise TeamError('team_sync_response_invalid')
                    if mode=='changes' and (type(event['revision']) is not int or not after<event['revision']<=revision):
                        raise TeamError('team_sync_response_invalid')
                    key=(event['source'],event['id']);kind=event['kind']
                    if kind in ({'snapshot'} if mode=='snapshot' else {'added','changed'}):
                        job=self._job(event['job'],stamp)
                        if (job['source'],job['id'])!=key or mode=='snapshot' and key in jobs:raise TeamError('team_sync_response_invalid')
                        byte_count-=sizes.get(key,0);jobs[key]=job;sizes[key]=len(encoded(job).encode());byte_count+=sizes[key]
                    elif mode=='changes' and kind=='not_observed' and event['job'] is None:
                        jobs.pop(key,None);byte_count-=sizes.pop(key,0)
                    elif not (mode=='changes' and kind=='observed' and event['job'] is None):raise TeamError('team_sync_response_invalid')
                    events+=1
                    if len(jobs)>10000 or byte_count>50_000_000:raise TeamError('team_sync_size_limit')
                if value['complete']:break
                if value['next_cursor']==cursor:raise TeamError('team_sync_response_invalid')
                cursor=value['next_cursor']
            else:raise TeamError('team_sync_page_limit')
            check_cancelled()
            # Full validation precedes the only local snapshot replacement.
            atomic_json(path,{'schema_version':2,'binding':self.binding,'revision':revision,
                              'generated_at':generated_at,'jobs':list(jobs.values())})
            path.chmod(0o600)
            acknowledged=False
            try:
                reply=self.connection.request('/v1/sync/ack',payload={'source_scope':self.sources,'revision':revision})
                acknowledged=reply=={'schema_version':1,'revision':revision,'acknowledged':True}
            except (TeamError,RuntimeError):pass
            return {'schema_version':1,'revision':revision,'mode':mode,'events':events,'jobs':len(jobs),
                    'acknowledged':acknowledged,'message':'同步完成，私人岗位库和旧报告保持原样。' if acknowledged else '本机已保存；服务端水位未确认，下次同步会再次核对。'}

    def _stamp(self,value):
        try:
            if not isinstance(value,str) or len(value)>60 or parse_time(value).timestamp()>self.clock()+300:
                raise ValueError()
        except (ValueError,TypeError,OverflowError) as error:
            raise TeamError('team_sync_timestamp_invalid') from error
        return value

    def _job(self,job,generated_at):
        if not isinstance(job,dict) or job.get('source') not in self.sources:raise TeamError('team_sync_response_invalid')
        query=PublicQuery('*',(job['source'],),limit=1)
        return validate_batch({'schema_version':1,'jobs':[job],'next_cursor':'','generated_at':generated_at},query,self.connection.registry)['jobs'][0]
