"""Fenced, expiring ownership of an enrolled account resource.

Only opaque task IDs leave the workspace. A lease serializes cooperating
collectors; it cannot recall a network request already accepted by a publisher.
"""
from __future__ import annotations

import hashlib
import hmac
import math
import re
import secrets
import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

from .guided.contracts import CrawlError
from .team_store import TeamError

LEASE_SECONDS = 90
RENEW_SECONDS = 25
PROOF_FIELDS = {'task', 'generation', 'token'}
TASK_MESSAGES = {
    'team_task_busy': '另一设备正在使用这个账号资源执行任务；请等待任务结束或租约到期，再确认重试。',
    'team_task_lost': '任务所有权已失效，已停止后续请求和新的报告保存；已有本机数据保留。',
    'team_task_unavailable': '无法确认跨设备任务所有权，已停止后续请求；请检查团队服务后重新确认。',
}
_active = ContextVar('radar_team_task_lease', default=None)


def proof(value):
    if (not isinstance(value, dict) or set(value) != PROOF_FIELDS
            or not isinstance(value['task'], str) or not re.fullmatch('[0-9a-f]{32}', value['task'])
            or type(value['generation']) is not int or not 1 <= value['generation'] < 2**63
            or not isinstance(value['token'], str) or not re.fullmatch('[0-9a-f]{64}', value['token'])):
        raise TeamError()
    return value


def _row(conn, tenant, resource):
    # Read-only requests against a legacy database do not migrate it.
    if conn.execute('PRAGMA user_version').fetchone()[0] == 1:
        return None
    return conn.execute('SELECT * FROM task_leases WHERE tenant=? AND resource=?', (tenant, resource)).fetchone()


def _valid(row, who, value, now):
    return bool(row and row['state'] == 'active' and row['expires'] > now
                and row['device'] == who['device'] and row['member'] == who['member']
                and row['task'] == value['task'] and row['generation'] == value['generation']
                and hmac.compare_digest(row['digest'], hashlib.sha256(value['token'].encode()).hexdigest()))


def verify_reservation(conn, who, resource, value, now):
    row = _row(conn, who['tenant'], resource)
    if value is not None:
        if not _valid(row, who, proof(value), now):
            raise TeamError('team_task_lost', 409)
    elif row and row['state'] == 'active' and row['expires'] > now:
        # Older clients cannot accidentally bypass a currently owned resource.
        raise TeamError('team_task_busy', 409, wait=row['expires']-now)


def task_summary(conn, who, resource, now):
    row = _row(conn, who['tenant'], resource)
    active = bool(row and row['state'] == 'active' and row['expires'] > now)
    return {'active': active, 'this_device': bool(active and row['device'] == who['device']),
            'expires_at': row['expires'] if active else None}


def service_task(store, token, value):
    required = {'resource', 'site', 'operation'}
    if not isinstance(value, dict) or not required <= set(value):
        raise TeamError()
    operation = value['operation']
    extra = {'acquire': {'task'}, 'renew': {'lease'}, 'release': {'lease'}}
    if operation not in extra or set(value) != required | extra[operation]:
        raise TeamError()
    if operation == 'acquire':
        if not isinstance(value['task'], str) or not re.fullmatch('[0-9a-f]{32}', value['task']):
            raise TeamError()
    else:
        proof(value['lease'])
    with store.connection(write=True) as conn:
        who = store.principal(conn, token)
        resource = store._resource(conn, who, value['resource'], value['site'])
        now = store.now(conn, record=True)
        row = _row(conn, who['tenant'], resource['id'])
        if operation == 'acquire':
            if row and row['state'] == 'active' and row['expires'] > now:
                raise TeamError('team_task_busy', 409, wait=row['expires']-now)
            generation = row['generation']+1 if row else 1
            if generation >= 2**63:
                raise TeamError('team_task_unavailable', 503)
            secret = secrets.token_hex(32)
            current = {'task': value['task'], 'generation': generation, 'token': secret}
            expires = min(now+LEASE_SECONDS, who['expires'])
            conn.execute('INSERT INTO task_leases VALUES(?,?,?,?,?,?,?,?,?) '
                         'ON CONFLICT(tenant,resource) DO UPDATE SET task=excluded.task,device=excluded.device,'
                         'member=excluded.member,generation=excluded.generation,digest=excluded.digest,'
                         'expires=excluded.expires,state=excluded.state',
                         (who['tenant'],resource['id'],current['task'],who['device'],who['member'],generation,
                          hashlib.sha256(secret.encode()).hexdigest(),expires,'active'))
            store.audit(conn,who['tenant'],who['member'],'task_acquired',resource['id'])
        else:
            current = value['lease']
            if not _valid(row, who, current, now):
                raise TeamError('team_task_lost', 409)
            expires = min(now+LEASE_SECONDS, who['expires']) if operation == 'renew' else now
            conn.execute('UPDATE task_leases SET expires=?,state=? WHERE tenant=? AND resource=?',
                         (expires,'active' if operation == 'renew' else 'released',who['tenant'],resource['id']))
            if operation == 'release':
                store.audit(conn,who['tenant'],who['member'],'task_released',resource['id'])
                return {'schema_version':1,'released':True}
        return {'schema_version':1,'resource':resource['id'],'lease':current,
                'server_time':now,'expires_at':expires}


def reservation_proof(connection, site):
    current = _active.get()
    if current is None or Path(connection.root).parent.resolve() != current.workspace_root or site != current.site:
        return None
    current.ensure_active()
    if connection.config['quota_resources'].get(site) != current.resource:
        raise CrawlError('team_connection_changed')
    current.check()
    return dict(current.value)


class TaskLease:
    def __init__(self, connection=None, site='greenhouse_public_example', *, monotonic=time.monotonic,
                 connection_factory=None, workspace_root=None, lazy=False):
        self.connection, self.site, self.monotonic = connection, site, monotonic
        self._factory, self._lazy = connection_factory, lazy
        self.workspace_root = Path(workspace_root).resolve() if workspace_root is not None else connection.root.parent.resolve()
        self.resource = connection.config['quota_resources'].get(site) if connection is not None else None
        self.value = None
        self._deadline = 0.0
        self._failed = False
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self._context = None

    def _call(self, operation):
        if not self.resource:
            raise CrawlError('team_resource_unconfigured')
        payload = {'site':self.site,'resource':self.resource,'operation':operation}
        payload.update({'task':secrets.token_hex(16)} if operation == 'acquire' else {'lease':self.value})
        # Subtract request duration: an old response cannot extend the local lease.
        started = self.monotonic()
        result = self.connection.request('/v1/team/task', payload=payload)
        if operation == 'release':
            if result != {'schema_version':1,'released':True}:
                raise CrawlError('team_response_invalid')
            return
        try:
            if set(result) != {'schema_version','resource','lease','server_time','expires_at'}:
                raise ValueError()
            current = proof(result['lease'])
            if (result['resource'] != self.resource
                    or operation == 'acquire' and current['task'] != payload['task']
                    or operation == 'renew' and current != self.value
                    or any(type(result[k]) not in (int,float) or not math.isfinite(result[k])
                           for k in ('server_time','expires_at'))):
                raise ValueError()
            remaining = result['expires_at']-result['server_time']
            if not 0 < remaining <= LEASE_SECONDS:
                raise ValueError()
            deadline = started+remaining
            if self.monotonic() >= deadline:
                raise ValueError()
        except (ValueError,TypeError,KeyError):
            raise CrawlError('team_response_invalid') from None
        self.value = dict(current)
        self._deadline = deadline

    def ensure_active(self):
        with self._lock:
            if self.value is None:
                if self.connection is None:
                    self.connection = self._factory()
                    self.resource = self.connection.config['quota_resources'].get(self.site)
                self._call('acquire')
                self._thread = threading.Thread(target=self._heartbeat,name='radar-team-lease',daemon=True)
                self._thread.start()
            self.check()

    def check(self):
        if self._failed or self.value is not None and self.monotonic() >= self._deadline:
            raise CrawlError('team_task_lost')

    def confirm(self):
        with self._lock:
            if self.value is None:return  # Cache/report-only work remains offline.
            self.check()
            try:self._call('renew')
            except Exception:
                self._failed = True
                raise CrawlError('team_task_unavailable') from None

    def _heartbeat(self):
        while not self._stop.wait(RENEW_SECONDS):
            try:self.confirm()
            except Exception:
                self._failed = True
                return

    def __enter__(self):
        self._context = _active.set(self)
        try:
            if not self._lazy:self.ensure_active()
        except BaseException:
            self.__exit__(None,None,None)
            raise
        return self

    def __exit__(self, *unused):
        self._stop.set()
        if self._thread is not None and self._thread.ident is not None:
            self._thread.join(timeout=12)
        try:
            if self.value is not None and not self._failed and (self._thread is None or not self._thread.is_alive()):
                with self._lock:self._call('release')
        except Exception:
            # An uncertain release never allows more publisher requests. The
            # authoritative expiry remains; another owner cannot be released.
            pass
        finally:
            if self._context is not None:_active.reset(self._context)


@contextmanager
def task_ownership(workspace, *, enabled=True, network_policy=None):
    from .team_client import configured, TeamConnection
    if not enabled or not configured(workspace.root):
        yield None
        return
    policy = (lambda:network_policy) if network_policy is not None else workspace.network_policy
    # Delay service access until the first actual publisher quota reservation.
    # Fresh cache pages and existing report generation must remain available offline.
    factory = lambda:TeamConnection(workspace.root,policy_factory=policy)
    with TaskLease(connection_factory=factory,workspace_root=workspace.root,lazy=True) as lease:
        yield lease
