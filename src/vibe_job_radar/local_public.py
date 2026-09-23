"""Local-only public-board acquisition; no application-owned server required.

One fixed, documented read-only API, explicit user consent, no arbitrary URL,
credentials or job application endpoint. Query filtering and paging stay local.
Local research access is deliberately NOT a grant of redistribution rights.
"""
from __future__ import annotations

import copy
import hashlib
import hmac
import html
import json
import math
import secrets
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit

from .catalog_changes import compare_catalogs, validate_change
from .collection import writer_lock
from .guided.rate import Limits, RateLedger, RateLimit
from .html_parser import plain_text
from .public_cache_guard import CacheFailureGuard
from .network import FetchError, SafeHTTP
from .public_contract import ContractError, PublicQuery, PublicSource, validate_batch
from .utils import atomic_json
from .workspace import InputError

SOURCE = PublicSource('greenhouse_anthropic', 'Anthropic 公开招聘（本机获取）',
    ('job-boards.greenhouse.io', 'boards.greenhouse.io'),
    '用户主动请求：Greenhouse公开只读职位接口；仅本机岗位研究，不提交申请、不推断转载授权。',
    local_access_approved=True)
API_URL = 'https://boards-api.greenhouse.io/v1/boards/anthropic/jobs?content=true'
HOST = 'boards-api.greenhouse.io'
SCOPE = 'greenhouse_public_example'  # Same workspace budget as the one-job example.
MAX_BYTES = 32_000_000


def timestamp(now):
    if type(now) not in (int, float) or not math.isfinite(now):
        raise ContractError('public_timestamp_invalid')
    return datetime.fromtimestamp(now, timezone.utc).isoformat()


def revision(jobs):
    return hashlib.sha256(json.dumps(jobs, sort_keys=True, ensure_ascii=False).encode('utf-8')).hexdigest()


def parse_board(payload, now):
    """Read only listed full job descriptions; never infer a missing field."""
    if (not isinstance(payload, dict) or not isinstance(payload.get('jobs'), list)
            or len(payload['jobs']) > 10000 or not isinstance(payload.get('meta'), dict)
            or type(payload['meta'].get('total')) is not int
            or payload['meta']['total'] != len(payload['jobs'])):
        raise ContractError('local_public_board_incomplete')
    jobs, identities = [], set()
    for raw in payload['jobs']:
        if not isinstance(raw, dict) or type(raw.get('id')) is not int or raw['id'] <= 0:
            raise ContractError('local_public_job_invalid')
        ident = str(raw['id'])
        if ident in identities:
            raise ContractError('public_duplicate_result')
        identities.add(ident)
        url = raw.get('absolute_url')
        SOURCE.accepts(url)
        parsed = urlsplit(url)
        if parsed.path.rstrip('/') != '/anthropic/jobs/' + ident or parsed.query:
            raise ContractError('public_source_mismatch')
        title, content, location = raw.get('title'), raw.get('content'), raw.get('location')
        if (not isinstance(title, str) or not title.strip() or not isinstance(content, str)
                or len(content) > 500000 or not isinstance(location, dict)
                or not isinstance(location.get('name'), str)):
            raise ContractError('local_public_job_invalid')
        markup = content
        for _ in range(2):
            if '&lt;' in markup and '<p' not in markup.lower():
                markup = html.unescape(markup)
        body = plain_text(markup)
        # A malformed/truncated board is not accepted as a complete acquisition.
        if not 100 <= len(body.strip()) <= 150000:
            raise ContractError('public_content_incomplete')
        jobs.append({'id': ident, 'source': SOURCE.key, 'title': title, 'company': 'Anthropic',
            'location': location['name'], 'remote': None, 'text': body, 'url': url, 'final_url': url,
            'collected_at': timestamp(now), 'completeness': 'full_text',
            'adapter_version': 'greenhouse_local_board_v1'})
    jobs.sort(key=lambda row: row['id'])
    return jobs


class LocalPublicDataClient:
    execution_mode = 'local_direct'

    def __init__(self, workspace, *, transport=None, clock=time.time):
        self.workspace, self.clock = workspace, clock
        self.registry = {SOURCE.key: SOURCE}
        self.root = workspace.root / 'local_public'
        self.path = self.root / 'anthropic-board-v2.json'
        self.legacy_path = self.root / 'anthropic-board-v1.json'
        self.key_path = self.root / 'cursor.key'
        self.failure_guard = CacheFailureGuard(self.root, API_URL)
        self._default_transport = transport is None
        self.client = transport or SafeHTTP({HOST}, timeout=15, max_bytes=MAX_BYTES, interval=2)
        self.ledger = None
        self.secret = None
        self._rate_blocked = False

    def _prepare(self):
        # Initialization is local and lazy: a damaged optional cache must not
        # prevent the unrelated offline workspace from opening.
        self.root.mkdir(exist_ok=True, mode=0o700)
        if self.root.is_symlink():
            raise InputError('本机公开数据目录不能使用符号链接。')
        rate_root = self.workspace.root / 'public_examples'
        rate_root.mkdir(exist_ok=True, mode=0o700)
        if rate_root.is_symlink():
            raise InputError('案例目录不能使用符号链接。')
        if self.key_path.is_symlink():
            raise InputError('分页密钥不能使用符号链接。')
        if not self.key_path.exists():
            with self.key_path.open('xb') as out:
                out.write(secrets.token_bytes(32))
            self.key_path.chmod(0o600)
        if self.key_path.stat().st_size != 32:
            raise ContractError('public_cursor_invalid')
        self.secret = self.key_path.read_bytes()
        if self.ledger is None:
            # Match the existing example's database, scope AND limits. Switching
            # entrypoints or query terms cannot reset its request budget.
            self.ledger = RateLedger(rate_root / 'rates.sqlite',
                Limits(request_interval=30, requests_hour=12, requests_day=24), clock=self.clock)

    def _scope(self, query):
        if not isinstance(query, PublicQuery) or query.source_scope != (SOURCE.key,):
            raise ContractError('public_source_unapproved')

    def _validate_snapshot(self, value, now):
        if (not isinstance(value, dict) or set(value) not in ({'observed_at', 'api', 'revision', 'jobs'},
                                  {'observed_at', 'api', 'revision', 'jobs', 'catalog_change'})
                or value['api'] != API_URL or not isinstance(value['jobs'], list)
                or len(value['jobs']) > 10000):
            raise ContractError('public_cache_invalid')
        stamp = value['observed_at']
        if type(stamp) not in (int, float) or not math.isfinite(stamp) or stamp > now:
            raise ContractError('public_cache_invalid')
        stamp_text = timestamp(stamp)
        seen = set()
        for offset in range(0, len(value['jobs']), 50):
            checked = validate_batch({'schema_version': 1, 'jobs': value['jobs'][offset:offset+50],
                'next_cursor': '', 'generated_at': stamp_text},
                PublicQuery('snapshot', (SOURCE.key,), limit=50), self.registry, access_mode='local')
            for job in checked['jobs']:
                parsed = urlsplit(job['url'])
                if (job['id'] in seen or job['source'] != SOURCE.key or job['company'] != 'Anthropic'
                        or not job['id'].isdigit() or parsed.path.rstrip('/') != '/anthropic/jobs/' + job['id']
                        or parsed.query or job['final_url'] != job['url'] or job['collected_at'] != stamp_text):
                    raise ContractError('public_cache_invalid')
                seen.add(job['id'])
        if value['revision'] != revision(value['jobs']):
            raise ContractError('public_cache_invalid')
        if 'catalog_change' in value:
            validate_change(value['catalog_change'], value, SOURCE.key)
        return value if now - stamp <= 7*86400 else None

    def _cached(self, now):
        timestamp(now)
        # Keep v1 untouched for rollback. A broken v2 must not fall back to v1.
        path = self.path if self.path.exists() or self.path.is_symlink() else self.legacy_path
        if path.is_symlink():
            raise InputError('缓存文件不能使用符号链接。')
        if not path.exists():
            return None
        if path.stat().st_size > MAX_BYTES:
            raise ContractError('public_cache_invalid')
        try:
            return self._validate_snapshot(json.loads(path.read_text(encoding='utf-8')), now)
        except (ValueError, TypeError, KeyError) as exc:
            raise ContractError('public_cache_invalid') from exc

    def _cursor(self, offset, query, snapshot):
        body = f"{offset}.{snapshot['revision']}.{query.digest(include_cursor=False)}"
        return body + '.' + hmac.new(self.secret, body.encode(), hashlib.sha256).hexdigest()

    def _select(self, query, snapshot, now, *, cached, requests=0, error=None):
        terms = query.query.casefold().split()
        jobs = [j for j in snapshot['jobs']
                if all(term in (j['title']+' '+j['company']+' '+j['text']).casefold() for term in terms)
                and (not query.region or query.region.casefold() in j['location'].casefold())
                and (query.remote is None or j['remote'] is query.remote)]
        offset = 0
        if query.cursor:
            try:
                raw_offset = query.cursor.split('.')[0]
                offset = int(raw_offset)
                if (str(offset) != raw_offset or not 0 < offset < len(jobs)
                        or not hmac.compare_digest(query.cursor, self._cursor(offset, query, snapshot))):
                    raise ValueError
            except (ValueError, TypeError) as exc:
                raise ContractError('public_cursor_invalid') from exc
        following = offset + query.limit
        next_cursor = self._cursor(following, query, snapshot) if following < len(jobs) else ''
        batch = validate_batch({'schema_version': 1, 'jobs': jobs[offset:following],
            'next_cursor': next_cursor, 'generated_at': timestamp(now)}, query, self.registry, access_mode='local')
        return {'response': batch, 'cache_reused': cached, 'stale': now-snapshot['observed_at'] >= 600,
                'observed_at': snapshot['observed_at'], 'network_requests': requests, 'refresh_error': error,
                'execution_mode': self.execution_mode, 'available_jobs': len(snapshot['jobs']),
                'matching_jobs': len(jobs), 'returned_jobs': len(batch['jobs']),
                'catalog_change': copy.deepcopy(snapshot.get('catalog_change'))}

    def cached(self, query):
        self._scope(query)
        with writer_lock(self.workspace.root):
            self._prepare()
            now = self.clock(); value = self._cached(now)
            return self._select(query, value, now, cached=True,
                                error=self.failure_guard.read(now)) if value else None

    def search(self, query, *, consent=False, network_policy=None):
        self._scope(query)
        if consent is not True:
            raise InputError('请确认本机获取所选公开来源；不提交申请或上传个人资料。')
        with writer_lock(self.workspace.root):
            self._prepare()
            now = self.clock(); cached = self._cached(now)
            hard_failure = self.failure_guard.read(now)
            if query.cursor:
                # Pagination never triggers another fetch or changes the snapshot.
                if cached is None:
                    raise ContractError('public_cursor_invalid')
                if hard_failure:
                    raise FetchError(hard_failure)
                return self._select(query, cached, now, cached=True)
            if cached and now-cached['observed_at'] < 600 and not hard_failure:
                return self._select(query, cached, now, cached=True)
            try:
                self.ledger.reserve(SCOPE, 'request')
            except RateLimit as exc:
                if hard_failure:
                    raise FetchError(hard_failure) from None
                if cached and exc.code != 'clock_rollback':
                    return self._select(query, cached, now, cached=True, error=exc.code)
                raise
            if self._rate_blocked:
                if isinstance(self.client, SafeHTTP):
                    self.client.blocked_hosts.discard(HOST)
                self._rate_blocked = False
            try:
                if self._default_transport:
                    # A new confirmed query may adopt changed preferences. The
                    # active query and existing circuit state are not reset.
                    self.client.network_policy = network_policy or self.workspace.network_policy()
                    self.client.resolver = self.workspace.dns_resolver
                # This constant GET carries no user query, region, files, cookies,
                # passwords, application API key or Authorization header.
                payload = self.client.json(API_URL)
            except FetchError as exc:
                if exc.code == 'http_429':
                    self._rate_blocked = True
                    self.ledger.cool(SCOPE, max(300, exc.retry_after or 0))
                recoverable = {'dns_error', 'network_error', 'http_429', 'http_500', 'http_502',
                               'http_503', 'http_504', 'local_proxy_connection_failed',
                               'encrypted_dns_unavailable', 'encrypted_dns_timeout',
                               'encrypted_dns_cooldown', 'encrypted_dns_budget'}
                if exc.code not in recoverable:
                    self.failure_guard.record(exc.code, now)
                elif hard_failure:
                    raise FetchError(hard_failure) from None
                if cached and exc.code in recoverable:
                    return self._select(query, cached, now, cached=True, requests=1, error=exc.code)
                raise
            try:
                jobs = parse_board(payload, now)
                value = {'observed_at': now, 'api': API_URL, 'revision': revision(jobs), 'jobs': jobs}
                value['catalog_change'] = compare_catalogs(cached, value, SOURCE.key)
                self._validate_snapshot(value, now)
                if len((json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False)+'\n').encode('utf-8')) > MAX_BYTES:
                    raise ContractError('public_cache_invalid')
            except ContractError as exc:
                self.failure_guard.record(exc.code, now)
                raise
            if self.path.is_symlink():
                raise InputError('缓存文件不能使用符号链接。')
            atomic_json(self.path, value)
            self.failure_guard.clear()
            return self._select(query, value, now, cached=False, requests=1)
