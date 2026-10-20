"""Operator-approved public snapshots and bounded, source-scoped delta cursors."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import math
import re
import secrets
import sqlite3
import time
from contextlib import closing, contextmanager
from dataclasses import asdict
from pathlib import Path

from .public_contract import PublicSource, checked_registry
from .public_gateway import PublicGateway, encode_snapshot
from .team_store import TeamError, strict_json, safe_path
from .utils import parse_time, utc_now

MAX_SNAPSHOT_BYTES=50_000_000


def encoded(value):
    return json.dumps(value,sort_keys=True,ensure_ascii=False,allow_nan=False,separators=(',',':'))


def read_snapshot(path):
    path=safe_path(Path(path))
    if not path.is_file() or path.stat().st_size>MAX_SNAPSHOT_BYTES:
        raise TeamError('team_snapshot_invalid')
    value=strict_json(path.read_bytes())
    if (not isinstance(value,dict) or set(value)-{'schema_version','sources','jobs','observed_at'}
            or not {'schema_version','sources','jobs'}<=set(value) or type(value['schema_version']) is not int or value['schema_version']!=1):
        raise TeamError('team_snapshot_invalid')
    if not isinstance(value['sources'],list) or not 1<=len(value['sources'])<=100:
        raise TeamError('team_snapshot_invalid')
    registry={}
    for raw in value['sources']:
        if not isinstance(raw,dict) or set(raw)!=set(PublicSource.__dataclass_fields__) or not isinstance(raw['domains'],list):
            raise TeamError('team_snapshot_invalid')
        source=PublicSource(**{**raw,'domains':tuple(raw['domains'])})
        if source.key in registry or not source.distribution_approved:
            raise TeamError('team_distribution_unapproved',403)
        registry[source.key]=source
    gateway=PublicGateway(registry,value['jobs'])
    observations=value.get('observed_at',{})
    if not isinstance(observations,dict) or set(observations)-set(registry):raise TeamError('team_snapshot_invalid')
    try:observations={key:parse_time(stamp).timestamp() for key,stamp in observations.items()}
    except (ValueError,TypeError):raise TeamError('team_snapshot_invalid') from None
    return registry,gateway.jobs,observations


SCHEMA_SQL = '''
BEGIN IMMEDIATE;
CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE jobs(source TEXT,id TEXT,body TEXT NOT NULL,digest TEXT NOT NULL,PRIMARY KEY(source,id));
CREATE TABLE changes(seq INTEGER PRIMARY KEY,revision INTEGER NOT NULL,source TEXT NOT NULL,
                     id TEXT NOT NULL,kind TEXT NOT NULL,body TEXT,ts REAL NOT NULL);
CREATE INDEX change_revision ON changes(revision,seq);
CREATE TABLE sources(id TEXT PRIMARY KEY,definition TEXT NOT NULL,observed REAL NOT NULL,revision INTEGER NOT NULL);
PRAGMA user_version=1;
COMMIT;
'''


class TeamCatalog:
    def __init__(self,root,*,clock=time.time):
        self.path=Path(root)/'catalog.sqlite'
        self.clock=clock

    def initialize(self):
        safe_path(self.path)
        with self.path.open('xb'):pass
        self.path.chmod(0o600)
        with closing(sqlite3.connect(self.path)) as conn:
            conn.executescript(SCHEMA_SQL)
            conn.executemany('INSERT INTO meta VALUES(?,?)',[('revision','0'),('floor','0'),('clock','0'),
                              ('secret',secrets.token_hex(32))]);conn.commit()

    @contextmanager
    def connection(self,*,write=False):
        from .team_store import ensure_recovery_complete
        ensure_recovery_complete(self.path.parent)
        safe_path(self.path)
        try:
            with closing(sqlite3.connect(self.path.resolve().as_uri()+'?mode=rw',uri=True,timeout=10)) as conn:
                conn.row_factory=sqlite3.Row
                conn.execute('BEGIN IMMEDIATE' if write else 'BEGIN')
                names={r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if names!={'meta','jobs','changes','sources'} or conn.execute('PRAGMA user_version').fetchone()[0]!=1:
                    raise TeamError('team_catalog_invalid',503)
                meta=dict(conn.execute('SELECT key,value FROM meta'))
                if (set(meta)!={'revision','floor','clock','secret'}
                        or not isinstance(meta['secret'],str)
                        or re.fullmatch(r'[0-9a-fA-F]{64}',meta['secret']) is None):
                    raise TeamError('team_catalog_invalid',503)
                now=self.clock()
                if type(now) not in (int,float) or not math.isfinite(now) or now<float(meta['clock'])-1:
                    raise TeamError('team_clock_rollback',503)
                yield conn,meta
                if write:conn.commit()
        except (sqlite3.Error,ValueError,TypeError) as error:
            if isinstance(error,TeamError):raise
            raise TeamError('team_catalog_unavailable',503) from error

    @staticmethod
    def registry(conn):
        result={}
        for row in conn.execute('SELECT id,definition FROM sources'):
            raw=json.loads(row['definition']);raw['domains']=tuple(raw['domains'])
            result[row['id']]=PublicSource(**raw)
        return checked_registry(result)

    def publish(self,registry,jobs,*,observed_at=None):
        registry=checked_registry(registry)
        if not registry or any(not source.distribution_approved for source in registry.values()):
            raise TeamError('team_distribution_unapproved',403)
        observed_at={} if observed_at is None else observed_at
        if not isinstance(observed_at,dict) or set(observed_at)-set(registry):raise TeamError('team_observation_invalid')
        if any(type(stamp) not in (int,float) or not math.isfinite(stamp) or stamp<=0 or stamp>self.clock()+1 for stamp in observed_at.values()):
            raise TeamError('team_observation_invalid')
        gateway=PublicGateway(registry,jobs)
        with self.connection(write=True) as (conn,meta):
            revision=int(meta['revision'])+1;now=self.clock();counts={'added':0,'changed':0,'not_observed':0}
            for source in registry.values():
                old={r['id']:dict(r) for r in conn.execute('SELECT * FROM jobs WHERE source=?',(source.key,))}
                current={j['id']:j for j in gateway.jobs if j['source']==source.key}
                for ident,job in current.items():
                    body=encoded(job)
                    digest=hashlib.sha256(encoded({k:v for k,v in job.items() if k!='collected_at'}).encode()).hexdigest()
                    kind='added' if ident not in old else 'changed' if old[ident]['digest']!=digest else None
                    if kind:
                        # An unchanged body keeps its original collection time;
                        # source observation time advances separately. Full and
                        # delta clients must converge at the same revision.
                        conn.execute('INSERT INTO jobs VALUES(?,?,?,?) ON CONFLICT(source,id) DO UPDATE SET body=excluded.body,digest=excluded.digest',
                                     (source.key,ident,body,digest))
                        conn.execute('INSERT INTO changes(revision,source,id,kind,body,ts) VALUES(?,?,?,?,?,?)',
                                     (revision,source.key,ident,kind,body,now));counts[kind]+=1
                for ident in old.keys()-current.keys():
                    conn.execute('DELETE FROM jobs WHERE source=? AND id=?',(source.key,ident))
                    conn.execute('INSERT INTO changes(revision,source,id,kind,body,ts) VALUES(?,?,?,?,NULL,?)',
                                 (revision,source.key,ident,'not_observed',now));counts['not_observed']+=1
                conn.execute('INSERT INTO sources VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE '
                             'SET definition=excluded.definition,observed=excluded.observed,revision=excluded.revision',
                             (source.key,encoded(asdict(source)),observed_at.get(source.key,0),revision))
                # Every completed observation advances the source watermark,
                # including unchanged/empty catalogs. It never fabricates jobs.
                conn.execute('INSERT INTO changes(revision,source,id,kind,body,ts) VALUES(?,?,?,?,NULL,?)',
                             (revision,source.key,'','observed',now))
            if conn.execute('SELECT COUNT(*) FROM jobs').fetchone()[0]>10000:
                raise TeamError('team_snapshot_too_large')
            # Include all sources and the gateway's exact UTF-8 JSON framing,
            # separators and whitespace before committing a usable catalog.
            aggregate=[json.loads(row[0]) for row in conn.execute('SELECT body FROM jobs')]
            if len(encode_snapshot(aggregate))>MAX_SNAPSHOT_BYTES:
                raise TeamError('team_snapshot_too_large')
            conn.execute("UPDATE meta SET value=? WHERE key='revision'",(str(revision),))
            conn.execute("UPDATE meta SET value=? WHERE key='clock'",(str(now),))
            self._expire_changes(conn,meta,now)
            return {'schema_version':1,'revision':revision,**counts}

    @staticmethod
    def _expire_changes(conn,meta,now):
        conn.execute('PRAGMA secure_delete=ON')
        cut=conn.execute('SELECT COALESCE(MAX(revision),0) FROM changes WHERE ts<?',(now-30*86400,)).fetchone()[0]
        count_cut=conn.execute('SELECT revision FROM changes ORDER BY seq DESC LIMIT 1 OFFSET 100000').fetchone()
        if count_cut:cut=max(cut,count_cut[0])
        if cut:
            conn.execute('DELETE FROM changes WHERE revision<=?',(cut,))
            conn.execute("UPDATE meta SET value=? WHERE key='floor'",(str(max(cut,int(meta['floor']))),))

    def expire_changes(self):
        with self.connection(write=True) as (conn,meta):
            now=self.clock()
            self._expire_changes(conn,meta,now)
            conn.execute("UPDATE meta SET value=? WHERE key='clock'",(str(max(now,float(meta['clock']))),))

    def withdraw(self,source):
        from .team_store import source_ident
        source_ident(source)
        with self.connection(write=True) as (conn,meta):
            if not conn.execute('SELECT 1 FROM sources WHERE id=?',(source,)).fetchone():raise TeamError('team_source_missing',404)
            conn.execute('PRAGMA secure_delete=ON')
            conn.execute('DELETE FROM jobs WHERE source=?',(source,))
            conn.execute('DELETE FROM changes WHERE source=?',(source,))
            conn.execute('DELETE FROM sources WHERE id=?',(source,))
            revision=int(meta['revision'])+1
            conn.execute("UPDATE meta SET value=? WHERE key='revision'",(str(revision),))
            # Any pre-withdrawal delta must restart; no retained body from the
            # withdrawn source can be delivered through an old cursor.
            conn.execute("UPDATE meta SET value=? WHERE key='floor'",(str(revision),))
            conn.execute("UPDATE meta SET value=? WHERE key='clock'",(str(self.clock()),))
        return {'schema_version':1,'withdrawn':source,'revision':revision}

    def _source_state(self,conn,sources):
        result=[]
        for source in sources:
            row=conn.execute('SELECT observed,revision FROM sources WHERE id=?',(source,)).fetchone()
            result.append({'source':source,'observed_at':row['observed'] if row else None,
                           'revision':row['revision'] if row else None,
                           'stale':not row or row['observed']<=0 or self.clock()-row['observed']>7*86400})
        return result

    def source_state(self,sources):
        with self.connection() as (conn,meta):return self._source_state(conn,sources)

    def _require_fresh(self,conn,sources):
        state=self._source_state(conn,sources)
        if any(s['observed_at'] is None for s in state):raise TeamError('team_distribution_unapproved',403)
        if any(s['stale'] for s in state):raise TeamError('team_catalog_stale',503)

    def require_fresh(self,sources):
        with self.connection() as (conn,meta):self._require_fresh(conn,sources)

    def gateway(self,*,fresh_sources=None):
        with self.connection() as (conn,meta):
            if fresh_sources is not None:self._require_fresh(conn,fresh_sources)
            registry=self.registry(conn)
            return PublicGateway(registry,[json.loads(r[0]) for r in conn.execute('SELECT body FROM jobs')],
                                 cursor_secret=bytes.fromhex(meta['secret']),clock=self.clock)

    @staticmethod
    def _sign(value,secret):
        payload=base64.urlsafe_b64encode(encoded(value).encode()).decode().rstrip('=')
        return payload+'.'+hmac.new(bytes.fromhex(secret),payload.encode(),hashlib.sha256).hexdigest()

    def sync(self,principal,value):
        if (not isinstance(value,dict) or set(value)-{'source_scope','after_revision','cursor','limit'}
                or not {'source_scope'}<=set(value)):
            raise TeamError()
        sources=value['source_scope'];after=value.get('after_revision',0);limit=value.get('limit',20);cursor=value.get('cursor','')
        if (type(after) is not int or after<0 or type(limit) is not int or not 1<=limit<=50
                or not isinstance(cursor,str) or len(cursor)>4096):raise TeamError()
        scope=sorted(sources)
        scope_id=hashlib.sha256(encoded(scope).encode()).hexdigest()
        with self.connection() as (conn,meta):
            self._require_fresh(conn,scope)
            registry=self.registry(conn)
            if any(s not in registry or not registry[s].distribution_approved for s in scope):
                raise TeamError('team_distribution_unapproved',403)
            target=int(meta['revision']);offset=0;mode='snapshot' if after==0 else 'changes';expires=int(self.clock())+600
            if after>target:raise TeamError('team_watermark_invalid',409)
            if cursor:
                try:
                    raw,signature=cursor.split('.')
                    saved=strict_json(base64.urlsafe_b64decode(raw+'='*((-len(raw))%4)))
                    if (set(saved)!={'device','scope','after','limit','target','offset','mode','expires'}
                            or not hmac.compare_digest(cursor,self._sign(saved,meta['secret']))
                            or saved['device']!=principal['device'] or saved['scope']!=scope_id or saved['after']!=after
                            or saved['limit']!=limit or saved['mode']!=mode or saved['expires']<self.clock()
                            or type(saved['offset']) is not int or saved['offset']<0):raise ValueError()
                    target,offset,expires=saved['target'],saved['offset'],saved['expires']
                except (ValueError,TypeError,KeyError) as error:raise TeamError('team_cursor_invalid',409) from error
            if target!=int(meta['revision']):raise TeamError('team_snapshot_changed',409)
            if mode=='changes' and after<int(meta['floor']):raise TeamError('team_sync_reset_required',409)
            placeholders=','.join('?' for _ in scope)
            if mode=='snapshot':
                rows=conn.execute('SELECT source,id,body FROM jobs WHERE source IN ('+placeholders+') '
                                  'ORDER BY source,id LIMIT ? OFFSET ?',(*scope,limit+1,offset)).fetchall()
                events=[{'source':r['source'],'id':r['id'],'kind':'snapshot','job':json.loads(r['body'])} for r in rows[:limit]]
            else:
                rows=conn.execute('SELECT source,id,kind,body,revision FROM changes WHERE source IN ('+placeholders+') '
                                  'AND revision>? AND revision<=? ORDER BY seq LIMIT ? OFFSET ?',
                                  (*scope,after,target,limit+1,offset)).fetchall()
                events=[{'source':r['source'],'id':r['id'],'kind':r['kind'],'revision':r['revision'],
                         'job':json.loads(r['body']) if r['body'] else None} for r in rows[:limit]]
            more=len(rows)>limit
            next_cursor=self._sign({'device':principal['device'],'scope':scope_id,'after':after,'limit':limit,
                                   'target':target,'offset':offset+limit,'mode':mode,'expires':expires},meta['secret']) if more else ''
            observations=[dict(r) for r in conn.execute('SELECT id,observed,revision FROM sources WHERE id IN ('+placeholders+')',scope)]
            return {'schema_version':1,'mode':mode,'events':events,'next_cursor':next_cursor,
                    'revision':target,'complete':not more,'sources':observations,'generated_at':utc_now()}
