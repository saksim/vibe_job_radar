"""Persistent, server-owned membership and account resource authorization.

No recruitment credentials or private documents are accepted by this store.
Device secrets are random capabilities stored only as SHA-256 digests.
"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import math
import os
import re
import secrets
import sqlite3
import time
from contextlib import closing, contextmanager
from dataclasses import asdict
from pathlib import Path

from .guided.rate import Limits, RateLedger, RateLimit, MAX_PUBLISHER_WINDOW
from .guided.contracts import CrawlError
from .public_contract import ID as PUBLIC_SOURCE_ID

ID = re.compile(r'[a-zA-Z0-9_-]{1,80}')
TOKEN = re.compile(r'vjr_[a-zA-Z0-9_-]{43}')
ROLES = {'reader', 'collector', 'admin'}
TABLES = {'members', 'devices', 'resources', 'grants', 'source_grants', 'audit', 'clock', 'watermarks', 'sync_views'}


class TeamError(ValueError):
    def __init__(self, code='team_request_invalid', status=400, *, wait=None):
        self.code, self.status, self.wait = code, status, wait
        super().__init__(code)


def service_host(value):
    if (not isinstance(value,str) or len(value)>253 or '.' not in value
            or any(not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?',label)
                   for label in value.split('.'))):
        raise TeamError('team_host_invalid')
    try:address=ipaddress.ip_address(value)
    except ValueError:address=None
    if address is not None:raise TeamError('team_host_invalid')
    return value


def ident(value):
    if not isinstance(value, str) or not ID.fullmatch(value):
        raise TeamError()
    return value


def source_ident(value):
    if not isinstance(value,str) or not PUBLIC_SOURCE_ID.fullmatch(value):
        raise TeamError()
    return value


def strict_json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise TeamError()
            result[key] = value
        return result
    try:
        return json.loads(raw, object_pairs_hook=pairs,
                          parse_constant=lambda _: (_ for _ in ()).throw(TeamError()))
    except (ValueError, TypeError, UnicodeError) as error:
        raise TeamError() from error


def safe_path(path):
    path = Path(path)
    if path.is_symlink() or path.parent.is_symlink():
        raise TeamError('team_storage_unsafe', 503)
    for suffix in ('-wal', '-shm', '-journal'):
        if Path(str(path) + suffix).is_symlink():
            raise TeamError('team_storage_unsafe', 503)
    return path


def remove_private_file(path, identity):
    path=Path(path)
    try:
        current=path.lstat()
        if not path.is_symlink() and (current.st_dev,current.st_ino)==identity:
            path.unlink()
    except FileNotFoundError:pass


def private_json(path,value):
    path=Path(path);safe_path(path)
    descriptor=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    info=os.fstat(descriptor);identity=(info.st_dev,info.st_ino)
    try:
        with os.fdopen(descriptor,'w',encoding='utf-8') as stream:
            json.dump(value,stream,ensure_ascii=False,indent=2);stream.write('\n')
            stream.flush();os.fsync(stream.fileno())
        if os.name!='nt':
            parent=os.open(path.parent,os.O_RDONLY|os.O_DIRECTORY)
            try:os.fsync(parent)
            finally:os.close(parent)
        return identity
    except BaseException:
        remove_private_file(path,identity)
        raise


class TeamStore:
    def __init__(self, root, *, clock=time.time):
        self.root, self.clock = Path(root), clock
        self.path = self.root / 'control.sqlite'
        self.rate_path = self.root / 'rates.sqlite'

    def initialize(self):
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        safe_path(self.path); safe_path(self.rate_path)
        if self.path.exists() or self.rate_path.exists():
            raise TeamError('team_already_initialized', 409)
        # Initialization is an explicit operator action, never a request fallback.
        with self.path.open('xb'):
            pass
        self.path.chmod(0o600)
        with closing(sqlite3.connect(self.path)) as conn:
            conn.executescript('''
                BEGIN IMMEDIATE;
                CREATE TABLE members(tenant TEXT, member TEXT, role TEXT NOT NULL, active INTEGER NOT NULL,
                                     PRIMARY KEY(tenant,member));
                CREATE TABLE devices(id TEXT PRIMARY KEY, tenant TEXT NOT NULL, member TEXT NOT NULL,
                                     digest TEXT UNIQUE NOT NULL, expires REAL NOT NULL, revoked INTEGER NOT NULL);
                CREATE TABLE resources(tenant TEXT, id TEXT, site TEXT NOT NULL, origins TEXT NOT NULL,
                                       limits TEXT NOT NULL, PRIMARY KEY(tenant,id));
                CREATE TABLE grants(tenant TEXT, member TEXT, resource TEXT, PRIMARY KEY(tenant,member,resource));
                CREATE TABLE source_grants(tenant TEXT, member TEXT, source TEXT, PRIMARY KEY(tenant,member,source));
                CREATE TABLE audit(seq INTEGER PRIMARY KEY, ts REAL NOT NULL, tenant TEXT NOT NULL,
                                   actor TEXT NOT NULL, action TEXT NOT NULL, target TEXT NOT NULL);
                CREATE TABLE clock(id INTEGER PRIMARY KEY CHECK(id=1), ts REAL NOT NULL);
                CREATE TABLE watermarks(device TEXT, scope TEXT, revision INTEGER NOT NULL, updated REAL NOT NULL,
                                        PRIMARY KEY(device,scope));
                CREATE TABLE sync_views(device TEXT, scope TEXT, revision INTEGER NOT NULL,
                                        PRIMARY KEY(device,scope));
                PRAGMA user_version=1;
                COMMIT;
            ''')
        RateLedger(self.rate_path, clock=self.clock)
        self.rate_path.chmod(0o600)

    @contextmanager
    def connection(self, *, write=False):
        safe_path(self.path); safe_path(self.rate_path)
        try:
            with closing(sqlite3.connect(self.path.resolve().as_uri()+'?mode=rw', uri=True, timeout=10)) as conn:
                conn.row_factory = sqlite3.Row
                conn.execute('BEGIN IMMEDIATE' if write else 'BEGIN')
                tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if conn.execute('PRAGMA user_version').fetchone()[0] != 1 or tables != TABLES:
                    raise TeamError('team_storage_invalid', 503)
                if not self.rate_path.is_file():
                    raise TeamError('team_storage_invalid', 503)
                with closing(sqlite3.connect(self.rate_path.resolve().as_uri()+'?mode=ro',uri=True,timeout=10)) as rates:
                    if not RateLedger._schema_ready(rates):
                        raise TeamError('team_rate_storage_invalid',503)
                # Expiry also runs on reads/health checks/backups, even when no
                # later administrative event is recorded. Only overdue rows
                # require a write transaction.
                cutoff=self.now(conn)-90*86400
                if conn.execute('SELECT 1 FROM audit WHERE ts<? LIMIT 1',(cutoff,)).fetchone():
                    if not write:
                        conn.rollback();conn.execute('BEGIN IMMEDIATE')
                    conn.execute('PRAGMA secure_delete=ON')
                    conn.execute('DELETE FROM audit WHERE ts<?',(cutoff,))
                    if not write:
                        conn.commit();conn.execute('BEGIN')
                yield conn
                if write:
                    conn.commit()
        except sqlite3.Error as error:
            raise TeamError('team_storage_unavailable', 503) from error

    def now(self, conn, *, record=False):
        now = self.clock()
        row = conn.execute('SELECT ts FROM clock WHERE id=1').fetchone()
        if type(now) not in (int, float) or not math.isfinite(now) or (row and now < row[0]-1):
            raise TeamError('team_clock_rollback', 503)
        if record:
            conn.execute('INSERT INTO clock VALUES(1,?) ON CONFLICT(id) DO UPDATE SET ts=MAX(ts,excluded.ts)', (now,))
        return now

    def expire_rate_history(self):
        # Persist the control clock before deleting rate rows: a rollback or a
        # failure between these databases must never make expired quota reusable.
        with self.connection(write=True) as conn:
            now=self.now(conn,record=True)
        try:
            with closing(sqlite3.connect(self.rate_path.resolve().as_uri()+'?mode=rw',uri=True,timeout=10)) as rates:
                rates.execute('BEGIN IMMEDIATE')
                if not RateLedger._schema_ready(rates):raise TeamError('team_rate_storage_invalid',503)
                rates.execute('PRAGMA secure_delete=ON')
                rates.execute('DELETE FROM visits WHERE ts<?',(now-172800,))
                rates.execute('DELETE FROM publisher_visits WHERE ts<?',(now-MAX_PUBLISHER_WINDOW,))
                rates.commit()
        except sqlite3.Error as error:
            raise TeamError('team_storage_unavailable',503) from error

    def audit(self, conn, tenant, actor, action, target):
        now = self.now(conn, record=True)
        conn.execute('INSERT INTO audit(ts,tenant,actor,action,target) VALUES(?,?,?,?,?)',
                     (now, tenant, actor, action, target))
        # Audit contains fixed actions and opaque identifiers, never query text.
        conn.execute('DELETE FROM audit WHERE ts<?', (now-90*86400,))
        conn.execute('DELETE FROM audit WHERE seq <= (SELECT COALESCE(MAX(seq),0)-100000 FROM audit)')

    def member(self, tenant, member, role, *, active=True):
        ident(tenant); ident(member)
        if role not in ROLES or type(active) is not bool:
            raise TeamError()
        with self.connection(write=True) as conn:
            conn.execute('INSERT INTO members VALUES(?,?,?,?) ON CONFLICT(tenant,member) '
                         'DO UPDATE SET role=excluded.role,active=excluded.active', (tenant, member, role, int(active)))
            self.audit(conn, tenant, 'operator', 'member_changed', member)

    def issue(self, tenant, member, device, *, days=30, output=None):
        ident(tenant); ident(member); ident(device)
        if type(days) is not int or not 1 <= days <= 90:
            raise TeamError()
        token = 'vjr_' + secrets.token_urlsafe(32)
        identity=None
        try:
            with self.connection(write=True) as conn:
                if not conn.execute('SELECT 1 FROM members WHERE tenant=? AND member=? AND active=1',
                                    (tenant, member)).fetchone():
                    raise TeamError('team_member_missing', 404)
                if conn.execute('SELECT 1 FROM devices WHERE id=?', (device,)).fetchone():
                    raise TeamError('team_device_exists', 409)
                expires = self.now(conn, record=True) + days*86400
                conn.execute('INSERT INTO devices VALUES(?,?,?,?,?,0)',
                             (device, tenant, member, hashlib.sha256(token.encode()).hexdigest(), expires))
                self.audit(conn, tenant, 'operator', 'device_issued', device)
                if output is not None:
                    identity=private_json(output,{'schema_version':1,'device_token':token})
            return token
        except BaseException:
            if identity is not None:remove_private_file(output,identity)
            raise

    def revoke(self, tenant, device):
        ident(tenant); ident(device)
        with self.connection(write=True) as conn:
            if conn.execute('UPDATE devices SET revoked=1 WHERE tenant=? AND id=?', (tenant,device)).rowcount != 1:
                raise TeamError('team_device_missing', 404)
            self.audit(conn, tenant, 'operator', 'device_revoked', device)

    def resource(self, tenant, resource, site, origins, *, limits=None):
        ident(tenant); ident(resource); ident(site)
        if not isinstance(origins, list) or not origins or len(origins)>30:
            raise TeamError()
        try:
            origins = sorted({RateLedger._origin(origin) for origin in origins})
        except CrawlError as error:
            raise TeamError() from error
        baseline = (Limits(request_interval=30, requests_hour=12, requests_day=24)
                    if site == 'greenhouse_public_example' else Limits())
        bounds = asdict(baseline)
        values = asdict(limits or baseline)
        # Operators can make the original product limits stricter, never larger.
        for key, value in values.items():
            if type(value) not in (int,float) or not math.isfinite(value) or value <= 0:
                raise TeamError('team_limits_invalid')
            if key.endswith('interval') and value < bounds[key] or not key.endswith('interval') and value > bounds[key]:
                raise TeamError('team_limits_invalid')
            if not key.endswith('interval') and type(value) is not int:
                raise TeamError('team_limits_invalid')
        with self.connection(write=True) as conn:
            existing = conn.execute('SELECT site,origins,limits FROM resources WHERE tenant=? AND id=?', (tenant,resource)).fetchone()
            if existing:
                old = json.loads(existing['limits'])
                if site != existing['site'] or origins != json.loads(existing['origins']):
                    raise TeamError('team_resource_binding_immutable',409)
                for key, value in values.items():
                    if key.endswith('interval') and value < old[key] or not key.endswith('interval') and value > old[key]:
                        raise TeamError('team_limits_cannot_relax',409)
            conn.execute('INSERT INTO resources VALUES(?,?,?,?,?) ON CONFLICT(tenant,id) '
                         'DO UPDATE SET limits=excluded.limits', (tenant,resource,site,json.dumps(origins),json.dumps(values)))
            self.audit(conn,tenant,'operator','resource_changed',resource)

    def grant(self, tenant, member, value, *, source=False, remove=False):
        ident(tenant);ident(member)
        (source_ident if source else ident)(value)
        table, column = ('source_grants','source') if source else ('grants','resource')
        with self.connection(write=True) as conn:
            if not conn.execute('SELECT 1 FROM members WHERE tenant=? AND member=?', (tenant,member)).fetchone():
                raise TeamError('team_member_missing',404)
            if not source and not conn.execute('SELECT 1 FROM resources WHERE tenant=? AND id=?', (tenant,value)).fetchone():
                raise TeamError('team_resource_missing',404)
            if remove:
                conn.execute(f'DELETE FROM {table} WHERE tenant=? AND member=? AND {column}=?', (tenant,member,value))
            else:
                conn.execute(f'INSERT OR IGNORE INTO {table} VALUES(?,?,?)', (tenant,member,value))
            self.audit(conn,tenant,'operator','grant_removed' if remove else 'grant_added',value)

    def principal(self, conn, token):
        if not isinstance(token,str) or not TOKEN.fullmatch(token):
            raise TeamError('team_unauthorized',401)
        row = conn.execute('SELECT d.id AS device,d.tenant,d.member,d.expires,d.revoked,m.role,m.active '
                           'FROM devices d JOIN members m ON d.tenant=m.tenant AND d.member=m.member WHERE d.digest=?',
                           (hashlib.sha256(token.encode()).hexdigest(),)).fetchone()
        now = self.now(conn)
        if not row or row['revoked'] or not row['active'] or row['expires'] <= now:
            raise TeamError('team_unauthorized',401)
        return dict(row)

    def authorize(self, conn, principal, sources):
        if not isinstance(sources,list) or not 1 <= len(sources) <= 100 or len(set(sources)) != len(sources):
            raise TeamError()
        for source in sources:
            source_ident(source)
            if not conn.execute('SELECT 1 FROM source_grants WHERE tenant=? AND member=? AND source=?',
                                (principal['tenant'],principal['member'],source)).fetchone():
                raise TeamError('team_forbidden',403)

    def _resource(self, conn, principal, resource, site):
        ident(resource);ident(site)
        if principal['role'] not in {'collector','admin'}:
            raise TeamError('team_forbidden',403)
        row = conn.execute('SELECT r.* FROM resources r JOIN grants g ON r.tenant=g.tenant AND r.id=g.resource '
                           'WHERE r.tenant=? AND g.member=? AND r.id=?',
                           (principal['tenant'],principal['member'],resource)).fetchone()
        if not row or row['site'] != site:
            raise TeamError('team_forbidden',403)
        return row

    def quota(self, token, value):
        required = {'resource','site','operation'}
        if not isinstance(value,dict) or not required <= set(value):
            raise TeamError()
        operation = value['operation']
        extras = {'reserve':{'kind','origin'},'publisher':{'origin','delay','requests','seconds'},
                  'cool':{'seconds'},'defer':{'seconds'},'summary':set(),'login_availability':set()}
        if operation not in extras or set(value)-required-extras[operation]:
            raise TeamError()
        with self.connection(write=True) as conn:
            who = self.principal(conn,token)
            resource = self._resource(conn,who,value['resource'],value['site'])
            origin = value.get('origin')
            if origin is not None and origin not in json.loads(resource['origins']):
                raise TeamError('team_origin_forbidden',403)
            # Keep membership authorization locked until the durable reservation
            # completes, so revocation and accepted reservations have an order.
            ledger = RateLedger(self.rate_path,Limits(**json.loads(resource['limits'])),clock=self.clock)
            key = hashlib.sha256((who['tenant']+':'+resource['id']).encode()).hexdigest()
            result = {}
            if operation == 'reserve':
                if value.get('kind') not in {'request','page','login'} or value.get('kind')=='request' and origin is None:
                    raise TeamError()
                ledger.reserve(key,value['kind'],origin=origin)
            elif operation == 'publisher':
                if origin is None:
                    raise TeamError()
                ledger.set_publisher(key,origin,delay=value.get('delay',0),
                                     requests=value.get('requests'),seconds=value.get('seconds'))
            elif operation in {'cool','defer'}:
                seconds = value.get('seconds')
                if type(seconds) not in (int,float) or not math.isfinite(seconds) or not 0 <= seconds <= MAX_PUBLISHER_WINDOW:
                    raise TeamError()
                if operation == 'cool': ledger.cool(key,seconds)
                else: result = {'until':ledger.defer(key,seconds)}
            elif operation == 'summary': result = ledger.summary(key)
            else: result = ledger.login_availability(key)
            self.now(conn,record=True)
            return {'schema_version':1,'result':result}

    def state(self, token):
        with self.connection() as conn:
            who = self.principal(conn,token)
            resources = [dict(row) for row in conn.execute('SELECT r.id,r.site,r.limits FROM resources r JOIN grants g '
                        'ON r.tenant=g.tenant AND r.id=g.resource WHERE r.tenant=? AND g.member=?',
                        (who['tenant'],who['member']))]
            for resource in resources:
                limits=json.loads(resource['limits'])
                ledger=RateLedger(self.rate_path,Limits(**limits),clock=self.clock)
                key=hashlib.sha256((who['tenant']+':'+resource['id']).encode()).hexdigest()
                resource['limits']=limits
                resource['usage']=ledger.summary(key)
            sources = [r[0] for r in conn.execute('SELECT source FROM source_grants WHERE tenant=? AND member=? ORDER BY source',
                       (who['tenant'],who['member']))]
            marks = [dict(row) for row in conn.execute('SELECT scope,revision,updated FROM watermarks WHERE device=?',(who['device'],))]
            return {'schema_version':1, 'tenant':who['tenant'],'member':who['member'],'device':who['device'],
                    'role':who['role'],'expires_at':who['expires'],'resources':resources,'sources':sources,'watermarks':marks}

    def sync_seen(self, token, sources, revision, *, acknowledge=False):
        scope = hashlib.sha256(json.dumps(sorted(sources)).encode()).hexdigest()
        if type(revision) is not int or revision < 0:
            raise TeamError()
        with self.connection(write=True) as conn:
            who=self.principal(conn,token);self.authorize(conn,who,sources)
            if acknowledge:
                viewed=conn.execute('SELECT revision FROM sync_views WHERE device=? AND scope=?',(who['device'],scope)).fetchone()
                if not viewed or viewed[0] != revision:
                    raise TeamError('team_watermark_not_read',409)
                conn.execute('INSERT INTO watermarks VALUES(?,?,?,?) ON CONFLICT(device,scope) '
                             'DO UPDATE SET revision=MAX(revision,excluded.revision),updated=excluded.updated',
                             (who['device'],scope,revision,self.now(conn,record=True)))
            else:
                previous=conn.execute('SELECT revision FROM sync_views WHERE device=? AND scope=?',(who['device'],scope)).fetchone()
                # Replacing refreshes rowid order. Keep the 64 most recently
                # completed scopes per device, including their acknowledgments.
                # An evicted scope must be read again before a new acknowledgment.
                conn.execute('INSERT OR REPLACE INTO sync_views VALUES(?,?,?)',
                             (who['device'],scope,max(revision,previous[0] if previous else 0)))
                conn.execute('DELETE FROM sync_views WHERE device=? AND rowid NOT IN '
                             '(SELECT rowid FROM sync_views WHERE device=? ORDER BY rowid DESC LIMIT 64)',
                             (who['device'],who['device']))
                conn.execute('DELETE FROM watermarks WHERE device=? AND scope NOT IN '
                             '(SELECT scope FROM sync_views WHERE device=?)',(who['device'],who['device']))
        return {'schema_version':1,'revision':revision,'acknowledged':acknowledge}

    def metrics(self, token):
        with self.connection() as conn:
            who=self.principal(conn,token)
            if who['role'] != 'admin':raise TeamError('team_forbidden',403)
            return {'schema_version':1,'members':conn.execute('SELECT COUNT(*) FROM members WHERE tenant=? AND active=1',(who['tenant'],)).fetchone()[0],
                    'devices':conn.execute('SELECT COUNT(*) FROM devices WHERE tenant=? AND revoked=0 AND expires>?',(who['tenant'],self.now(conn))).fetchone()[0],
                    'recent_audit_events':conn.execute('SELECT COUNT(*) FROM audit WHERE tenant=?',(who['tenant'],)).fetchone()[0]}
