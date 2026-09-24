"""One explicit, keyless public job example; NOT a BOSS login workaround.

Only the documented Greenhouse read-only job-board endpoint is used. The case is
retrieved live, never replaced with synthetic text on failure. Upstream text is
stored only in the user's workspace, not bundled into this source distribution.
"""
from __future__ import annotations

import copy
import hashlib
import html
import json
import tempfile
import uuid
from pathlib import Path

from .collection import writer_lock
from .html_parser import plain_text
from .models import JobRecord
from .network import FetchError, SafeHTTP
from .store import Store
from .utils import atomic_json, parse_time, utc_now
from .workspace import InputError
from .guided.rate import Limits, RateLedger, RateLimit
from .public_lifecycle import check_cancelled

JOB_ID = 5421566008
API_URL = f'https://boards-api.greenhouse.io/v1/boards/anthropic/jobs/{JOB_ID}'
SOURCE_URL = f'https://job-boards.greenhouse.io/anthropic/jobs/{JOB_ID}'
API_CONTRACT = 'https://docs.greenhouse.io/job-board.html'


def parse_public_job(payload: dict) -> JobRecord:
    """Validate publisher identity, exact vacancy and full content before storage."""
    if not isinstance(payload, dict):
        raise FetchError('example_invalid_shape')
    if payload.get('id') != JOB_ID or payload.get('absolute_url') != SOURCE_URL:
        raise FetchError('example_source_changed')
    title, content = payload.get('title'), payload.get('content')
    if not isinstance(title, str) or 'architect' not in title.lower():
        raise FetchError('example_title_changed')
    if not isinstance(content, str) or not 100 <= len(content) <= 300000:
        raise FetchError('example_content_missing')
    # Job Board API may HTML-entity-encode its content; do not rewrite/translate it.
    markup = content
    for _ in range(2):
        if '&lt;' in markup and '<p' not in markup.lower():
            markup = html.unescape(markup)
    text = plain_text(markup)
    if len(text) < 100:
        raise FetchError('example_content_missing')
    location = payload.get('location') or {}
    if not isinstance(location, dict) or not isinstance(location.get('name', ''), str):
        raise FetchError('example_invalid_shape')
    return JobRecord(title=title, text=text, url=SOURCE_URL, company='Anthropic',
        location=location.get('name', ''), platform='greenhouse_anthropic',
        source_mode='authorized_feed', evidence_level='full_text', is_synthetic=False,
        source_ref=API_URL, rights_note='用户主动请求：Greenhouse公开只读职位接口；本机岗位研究，不提交申请、不推断转载授权。',
        parser='greenhouse_job_board_v1', raw_sha256=hashlib.sha256(content.encode()).hexdigest())


class PublicExample:
    def __init__(self, workspace, transport=None):
        self.workspace = workspace
        self.cancelled = None
        self.before_commit = None
        self.root = workspace.root / 'public_examples'
        self.root.mkdir(exist_ok=True, mode=0o700)
        if self.root.is_symlink():
            raise InputError('案例目录不能是符号链接。')
        self.client = transport or SafeHTTP({'boards-api.greenhouse.io'}, timeout=15,
                                            max_bytes=500000, interval=2, network_policy=workspace.network_policy())
        # Persistent per-workspace quota; the endpoint never accepts URL/Key/board input.
        self.ledger = RateLedger(self.root/'rates.sqlite', Limits(request_interval=30,
                                                                 requests_hour=12, requests_day=24))

    def run(self, data):
        if set(data) != {'consent'} or data.get('consent') is not True:
            raise InputError('请点击确认获取公开案例；不接受网址、密钥或额外参数。')
        with writer_lock(self.workspace.root):
            return self._run()

    def _run(self):
        check_cancelled(self.cancelled)
        if self.cancelled is not None and isinstance(self.client, SafeHTTP):
            self.client.network_policy.bind_cancellation(self.cancelled)
        last = self.root/'latest.json'
        if last.is_symlink():
            raise InputError('案例记录不能是符号链接。')
        if last.exists():
            previous = json.loads(last.read_text(encoding='utf-8'))
            age = (parse_time(utc_now()) - parse_time(previous['checked_at'])).total_seconds()
            if previous.get('success') and 0 <= age < 600:
                report = self.workspace.report(previous['report_id'])
                return {**previous, 'cache_reused': True, 'network_requests_this_click': 0,
                        'report': report, 'message': '复用10分钟内的真实获取结果，不重复请求；获取时间保持原值。'}
        try:
            check_cancelled(self.cancelled)
            self.ledger.reserve('greenhouse_public_example', 'request')
        except RateLimit as exc:
            raise InputError(f'真实案例请求过密，请至少等待 {int(exc.wait)+1} 秒；不会自动重试。') from exc
        audit = {'id': uuid.uuid4().hex, 'source_url': SOURCE_URL, 'api_url': API_URL,
                 'contract': API_CONTRACT, 'checked_at': utc_now(), 'success': False,
                 'cache_reused': False, 'network_requests_this_click': 1,
                 'scope': 'Anthropic公开架构师岗位；不是BOSS/中国大陆平台实站认证。', 'report_id': ''}
        try:
            payload = self.client.json(API_URL)  # no authentication header; no API redirect following
            check_cancelled(self.cancelled)
            record = parse_public_job(payload)
            if self.before_commit is not None:
                self.before_commit()
            else:
                check_cancelled(self.cancelled)
            with Store(self.workspace.db) as store:
                store.add(record)
            from .pipeline import analyze
            config = copy.deepcopy(self.workspace.config)
            config['platforms']['greenhouse_anthropic'] = {'label': 'Anthropic公开招聘（Greenhouse）',
                                                          'domains': ['job-boards.greenhouse.io']}
            report_id = uuid.uuid4().hex
            with tempfile.TemporaryDirectory(prefix='.public-example-', dir=self.root) as tmp:
                db = Path(tmp)/'batch.sqlite'
                with Store(db) as batch:
                    batch.add(record)
                manifest = analyze(db, self.workspace.root/'reports'/report_id, config=config,
                                   role_filter=['architect'], platform_filter=['greenhouse_anthropic'])
            audit.update(success=True, title=record.title, company=record.company,
                         record_id=record.record_id, report_id=report_id, collected_at=record.collected_at,
                         publisher_updated_at=payload.get('updated_at'), text_characters=len(record.text),
                         content_sha256=record.raw_sha256, stats=manifest['stats'],
                         code='real_source_received')
            atomic_json(self.root/(audit['id']+'.json'), audit)
            atomic_json(last, audit)
            # Attach source audit to the report and its hash manifest.
            folder = self.workspace.root/'reports'/report_id
            atomic_json(folder/'public_source.json', audit)
            manifest['output_files_sha256']['public_source.json'] = hashlib.sha256((folder/'public_source.json').read_bytes()).hexdigest()
            atomic_json(folder/'run_manifest.json', manifest)
            return {**audit, 'report': self.workspace.report(report_id),
                    'message': '已从真实公开接口取得正文并生成本批报告；英文原文保留，不是合成演示。'}
        except (FetchError, ValueError, TypeError, KeyError) as exc:
            code = exc.code if isinstance(exc, FetchError) else 'example_processing_failed'
            if code == 'http_429':
                self.ledger.cool('greenhouse_public_example', exc.retry_after or 300)
            audit.update(success=False, report_id='', code=code)
            atomic_json(self.root/(audit['id']+'.json'), audit)
            atomic_json(last, audit)
            from .network_settings import DNS_MESSAGES
            return {**audit, 'message': DNS_MESSAGES.get(code, '真实案例未获取成功：'+code+'。来源可能已下架或网络不可达；未使用合成数据替代。')}
