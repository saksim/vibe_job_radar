"""Data-only WSGI public gateway for an operator-reviewed immutable snapshot.

No network fetching, credentials, arbitrary URLs or record writes are exposed.
Production hosting, authentication, provider permission, ingress limits, refresh
workers and retention remain deployment gates; this module does not deploy them.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import time

from .public_contract import ContractError, PublicQuery, validate_batch, checked_registry
from .utils import utc_now


def encode_snapshot(jobs):
    """One byte representation for capacity checks and revision identity."""
    return json.dumps(jobs,sort_keys=True,ensure_ascii=False).encode('utf-8')


class PublicGateway:
    def __init__(self, registry, jobs, *, cursor_secret=None, clock=time.time):
        self.registry, self.clock=checked_registry(registry),clock
        self.secret=cursor_secret if cursor_secret is not None else secrets.token_bytes(32)
        if not isinstance(self.secret,bytes) or len(self.secret)<32:
            raise ContractError('public_cursor_configuration_invalid')
        if not isinstance(jobs,(list,tuple)) or len(jobs)>10000:
            raise ContractError('public_snapshot_too_large')
        seen=set(); self.jobs=[]
        # Authorize distribution at ingestion too, not only on search requests.
        for row in jobs:
            if not isinstance(row,dict) or not isinstance(row.get('source'),str):
                raise ContractError()
            q=PublicQuery('*',(row['source'],),limit=1)
            checked=validate_batch({'schema_version':1,'jobs':[row],'next_cursor':'',
                                    'generated_at':utc_now()},q,self.registry)['jobs'][0]
            key=checked['source']+'.'+checked['id']
            if key in seen:
                raise ContractError('public_duplicate_result')
            seen.add(key); self.jobs.append(checked)
        self.jobs.sort(key=lambda j:(j['source'],j['id']))
        encoded=encode_snapshot(self.jobs)
        if len(encoded)>50_000_000:
            raise ContractError('public_snapshot_too_large')
        self.revision=hashlib.sha256(encoded).hexdigest()

    def _sign(self, query, offset, expires):
        raw=f'{offset}.{expires}.{self.revision}.{query.digest(include_cursor=False)}'
        signature=hmac.new(self.secret,raw.encode(),hashlib.sha256).hexdigest()
        return raw+'.'+signature

    def _offset(self, query):
        if not query.cursor:
            return 0
        try:
            offset,expires,revision,digest,signature=query.cursor.split('.')
            if not offset.isdigit() or not expires.isdigit() or not 0<=int(offset)<=10000:
                raise ValueError
            expected=self._sign(query,int(offset),int(expires))
            if not hmac.compare_digest(expected,query.cursor) or int(expires)<self.clock():
                raise ValueError
            return int(offset)
        except (ValueError,TypeError) as exc:
            raise ContractError('public_cursor_invalid') from exc

    def search(self, value):
        query=PublicQuery.from_dict(value)
        for source in query.source_scope:
            if source not in self.registry or not self.registry[source].distribution_approved:
                raise ContractError('public_source_unapproved')
        terms=query.query.casefold().split()
        candidates=[row for row in self.jobs if row['source'] in query.source_scope
                    and all(term in (row['title']+' '+row['company']+' '+row['text']).casefold() for term in terms)
                    and (not query.region or query.region.casefold() in row['location'].casefold())
                    and (query.remote is None or row['remote'] is query.remote)]
        start=self._offset(query); end=start+query.limit
        cursor=self._sign(query,end,int(self.clock())+3600) if end<len(candidates) else ''
        result={'schema_version':1,'jobs':candidates[start:end],'next_cursor':cursor,'generated_at':utc_now()}
        return validate_batch(result,query,self.registry)

    def __call__(self, environ, start_response):
        status='200 OK'
        try:
            path=environ.get('PATH_INFO',''); method=environ.get('REQUEST_METHOD','GET')
            if environ.get('HTTP_COOKIE') or environ.get('HTTP_AUTHORIZATION') or environ.get('QUERY_STRING'):
                raise ContractError('public_request_invalid')
            if path=='/v1/catalog/sources' and method=='GET':
                data={'schema_version':1,'sources':[{'id':s.key,'label':s.label,'domains':list(s.domains),
                        'contract':s.contract} for s in self.registry.values() if s.distribution_approved]}
            elif path=='/v1/jobs/search' and method=='POST':
                if (environ.get('CONTENT_TYPE','').split(';')[0].strip()!='application/json'
                        or environ.get('HTTP_TRANSFER_ENCODING')):
                    raise ContractError('public_request_invalid')
                length=int(environ.get('CONTENT_LENGTH','0'))
                if not 0<length<=16000:
                    raise ContractError('public_request_too_large')
                raw=environ['wsgi.input'].read(length)
                if len(raw)!=length:
                    raise ContractError('public_request_invalid')
                data=self.search(json.loads(raw.decode('utf-8')))
            elif method=='GET' and re.fullmatch(r'/v1/jobs/[a-zA-Z0-9_-]+\.[a-zA-Z0-9_-]+',path):
                key=path.removeprefix('/v1/jobs/')
                row=next((j for j in self.jobs if j['source']+'.'+j['id']==key),None)
                if row is None:
                    status='404 Not Found'; data={'error':'public_job_not_found'}
                else:
                    data=validate_batch({'schema_version':1,'jobs':[row],'next_cursor':'','generated_at':utc_now()},
                                        PublicQuery('*',(row['source'],),limit=1),self.registry)
            else:
                status='404 Not Found'; data={'error':'public_route_not_found'}
        except (ValueError,TypeError,KeyError,UnicodeError) as exc:
            status='400 Bad Request'; data={'error':exc.code if isinstance(exc,ContractError) else 'public_request_invalid'}
        body=json.dumps(data,ensure_ascii=False,allow_nan=False).encode()
        start_response(status,[('Content-Type','application/json; charset=utf-8'),
                               ('Content-Length',str(len(body))),('Cache-Control','no-store'),
                               ('X-Content-Type-Options','nosniff')])
        return [body]
