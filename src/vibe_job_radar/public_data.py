"""Optional public-service client with local, source-bound response caching.

The service origin and registry are supplied by the application operator, never
by a search request. No recruitment cookies, evidence, reports or API secrets are
read. The source service must be deployed/approved separately; no default cloud
endpoint is invented here.
"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import math
import time
from pathlib import Path
from urllib.parse import urlsplit

from .collection import writer_lock
from .guided.rate import Limits, RateLedger, RateLimit
from .network import FetchError, SafeHTTP
from .public_contract import ContractError, PublicQuery, validate_batch, checked_registry
from .utils import atomic_json, parse_time
from .workspace import InputError
from .public_cache_guard import CacheFailureGuard, TRANSIENT_FETCH_ERRORS


class PublicDataClient:
    def __init__(self, cache_root: Path, service_origin: str, registry: dict, *, transport=None, clock=time.time):
        try:
            p = urlsplit(service_origin)
            if (p.scheme != 'https' or not p.hostname or p.username is not None or p.password is not None
                    or p.port not in (None,443) or p.path not in ('','/') or p.query or p.fragment
                    or len(service_origin)>2048 or any(ord(c)<33 for c in service_origin)):
                raise ValueError
            try:
                address=ipaddress.ip_address(p.hostname)
            except ValueError:
                address=None
            if address is not None and not address.is_global:
                raise ValueError
        except (ValueError,TypeError) as exc:
            raise ContractError('public_service_configuration_invalid') from exc
        self.origin=service_origin.rstrip('/')
        self.registry, self.clock = checked_registry(registry), clock
        self.root=Path(cache_root)
        self.root.mkdir(parents=True,exist_ok=True,mode=0o700)
        if self.root.is_symlink():
            raise InputError('公开数据缓存目录不能是符号链接。')
        self.client=transport or SafeHTTP({p.hostname},timeout=15,max_bytes=32_000_000,interval=2)
        self.scope=hashlib.sha256(self.origin.encode()).hexdigest()
        self.failure_guard=CacheFailureGuard(self.root, self.origin)
        self._rate_blocked=False
        self._host=p.hostname
        self.ledger=RateLedger(self.root/'rates.sqlite',Limits(request_interval=2,requests_hour=60,requests_day=500),clock=clock)

    def _path(self, query):
        name=hashlib.sha256((self.scope+query.digest()).encode()).hexdigest()+'.json'
        path=self.root/name
        if path.is_symlink():
            raise InputError('缓存文件不能是符号链接。')
        return path

    def _scope(self, query):
        if not isinstance(query,PublicQuery):
            raise ContractError()
        for source in query.source_scope:
            if source not in self.registry or not self.registry[source].distribution_approved:
                raise ContractError('public_source_unapproved')

    def _cached(self, query, now):
        if type(now) not in (int,float) or not math.isfinite(now):
            raise ContractError('public_timestamp_invalid')
        path=self._path(query)
        if path.is_symlink():
            raise InputError('缓存文件不能是符号链接。')
        if not path.exists():
            return None
        try:
            if path.stat().st_size>32_100_000:
                raise ContractError('public_cache_invalid')
            value=json.loads(path.read_text(encoding='utf-8'))
            if set(value)!={'observed_at','response'}:
                raise ValueError
            stamp=value['observed_at']
            if type(stamp) not in (int,float) or not math.isfinite(stamp) or stamp>now:
                raise ValueError
            response=validate_batch(value['response'],query,self.registry)
            if now-stamp>7*86400:
                return None
            return {'response':response,'cache_reused':True,'stale':now-stamp>=600,
                    'observed_at':stamp,'network_requests':0,'refresh_error':None}
        except (ValueError,TypeError,KeyError) as exc:
            raise ContractError('public_cache_invalid') from exc

    def cached(self, query):
        """Local-only read remains possible even when the public service is off."""
        self._scope(query)
        now=self.clock()
        cached=self._cached(query,now)
        return {**cached,'refresh_error':self.failure_guard.read(now)} if cached else None

    def search(self, query: PublicQuery, *, consent=False):
        self._scope(query)
        if consent is not True:
            raise InputError('使用公开服务前，请确认发送查询词、地区和来源范围；不要包含个人资料。')
        with writer_lock(self.root):
            now=self.clock()
            cached=self._cached(query,now)
            hard_failure=self.failure_guard.read(now)
            if cached and not cached['stale'] and not hard_failure:
                return cached
            try:
                self.ledger.reserve(self.scope,'request')
            except RateLimit as exc:
                if hard_failure:
                    raise FetchError(hard_failure) from None
                if cached and exc.code!='clock_rollback':
                    return {**cached,'refresh_error':exc.code}
                raise
            if self._rate_blocked:
                # Only clear a previous 429 after the durable quota/cooldown has
                # allowed this user-confirmed read. Never reset a 401/403 circuit.
                if isinstance(self.client,SafeHTTP):
                    self.client.blocked_hosts.discard(self._host)
                self._rate_blocked=False
            try:
                payload=self.client.json(self.origin+'/v1/jobs/search',method='POST',payload=query.payload())
            except FetchError as exc:
                if exc.code=='http_429':
                    self._rate_blocked=True
                    self.ledger.cool(self.scope,exc.retry_after or 300)
                if exc.code not in TRANSIENT_FETCH_ERRORS:
                    self.failure_guard.record(exc.code,now)
                elif hard_failure:
                    raise FetchError(hard_failure) from None
                if cached and exc.code in TRANSIENT_FETCH_ERRORS:
                    return {**cached,'refresh_error':exc.code,'network_requests':1}
                raise  # No fallback on 401/403, invalid source, TLS, or bad data.
            try:
                result=validate_batch(payload,query,self.registry)
                if parse_time(result['generated_at']).timestamp()>now+300:
                    raise ContractError('public_timestamp_invalid')
            except ContractError as exc:
                self.failure_guard.record(exc.code,now)
                raise
            atomic_json(self._path(query),{'observed_at':now,'response':result})
            self.failure_guard.clear()
            return {'response':result,'cache_reused':False,'stale':False,
                    'observed_at':now,'network_requests':1,'refresh_error':None}
