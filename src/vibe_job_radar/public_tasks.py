"""Authenticated local jobs for the public example and optional hybrid service.

Only validated query fields leave the device. Browser credentials and private
workspace records are never inputs to a public query. Startup/status are offline;
network requests require a user's explicit start/search consent.
"""
from __future__ import annotations

import copy
import hashlib
import json
import tempfile
import threading
import uuid
from pathlib import Path

from .catalog_changes import compact_change
from .collection import writer_lock
from .network_policy import current_policy
from .public_contract import PublicQuery, as_record
from .public_example import PublicExample
from .store import Store
from .utils import atomic_json, utc_now, parse_time
from .workspace import InputError


class PublicTasks:
    def __init__(self, workspace, *, hybrid_client=None, example_factory=PublicExample):
        self.workspace, self.hybrid = workspace, hybrid_client
        self.factory=example_factory
        self.root=workspace.root/'public_tasks'
        self.root.mkdir(exist_ok=True,mode=0o700)
        self.path=self.root/'state.json'
        if self.root.is_symlink() or self.path.is_symlink():
            raise InputError('公开任务记录不能使用符号链接。')
        self._lock=threading.RLock(); self._thread=None; self._closed=False
        self._state={'status':'idle','message':'点击获取后才会联网；个人材料和登录态保留本地。'}
        if self.path.exists():
            if self.path.stat().st_size>2_000_000:
                raise InputError('公开任务记录过大，请检查工作区。')
            self._state=json.loads(self.path.read_text(encoding='utf-8'))
            if self._state.get('status') in {'queued','running'}:
                self._state.update(status='interrupted',message='上次服务已退出；已保存条件和数据，确认后可继续。')

    def _save(self, **changes):
        with self._lock:
            if self.path.is_symlink():
                raise InputError('公开任务记录不能使用符号链接。')
            self._state.update(changes,updated_at=utc_now())
            with writer_lock(self.root):
                atomic_json(self.path,self._state)

    def mode(self):
        return getattr(self.hybrid, 'execution_mode', 'remote_service') if self.hybrid else 'disabled'

    def state(self):
        with self._lock:
            return {'task':copy.deepcopy(self._state),'hybrid_service_configured':self.mode() == 'remote_service',
                    'query_available':self.hybrid is not None,'execution_mode':self.mode(),
                    'sources':[{'id':s.key,'label':s.label} for s in self.hybrid.registry.values()
                               if (s.local_access_approved if self.mode() == 'local_direct' else s.distribution_approved)] if self.hybrid else [],
                    'network_policy':self.workspace.network_policy().describe(),
                    'privacy':('本机请求固定公开接口，查询词和地区在本机筛选，不发给外部服务；个人材料和登录态不发送。'
                               if self.mode() == 'local_direct' else
                               '只发送已确认的查询字段；登录态、简历、个人证据和私人报告不发送。')}

    def start(self, data):
        if set(data)!={'consent'} or data['consent'] is not True:
            raise InputError('请确认获取公开案例；不接受网址、凭据或额外参数。')
        return self._submit('example',{})

    def search(self, data):
        if set(data)!={'consent','query'} or data['consent'] is not True:
            raise InputError('请确认按页面说明获取所选公开来源；不要包含个人资料。')
        query=PublicQuery.from_dict(data['query'])
        if self.hybrid is None:
            raise InputError('此运行实例已停用公开查询；本地案例、登录采集和已保存数据仍可使用。无需部署生产服务器。')
        self.hybrid._scope(query)
        return self._submit('search',query.payload())

    def _submit(self, kind, query):
        with self._lock:
            if self._closed:
                raise InputError('本地服务正在关闭。')
            if self._thread and self._thread.is_alive():
                raise InputError('公开任务正在执行；进度已保留，请勿重复提交。')
            self._state={'id':uuid.uuid4().hex,'kind':kind,'query':query,'created_at':utc_now()}
            self._save(status='queued',message='任务已保存，正在准备获取公开数据。',report_id='')
            self._thread=threading.Thread(target=self._run,args=(kind,query),name='radar-public-data',daemon=True)
            self._thread.start()
            return {'id':self._state['id'],'queued':True}

    def _run(self, kind, value):
        try:
            self._save(status='running',message='正在获取公开数据并生成本地报告；不读取个人证据或登录态。')
            if kind=='example':
                result=self.factory(self.workspace).run({'consent':True})
            else:
                query=PublicQuery.from_dict(value)
                result=self._import(self.hybrid.search(query,consent=True),query)
            # Do not persist the report's full private rendering in task status.
            allowed={'success','message','code','report_id','source_url','collected_at','checked_at',
                     'cache_reused','stale','refresh_error','network_requests_this_click','scope','source_scope',
                     'next_cursor','matching_jobs','returned_jobs','available_jobs','execution_mode','catalog_change'}
            view={k:v for k,v in result.items() if k in allowed}
            self._save(**view,status='completed' if result.get('success') else 'failed')
        except Exception as exc:
            # Error messages from external providers may echo credentials.
            from .network import FetchError
            from .network_settings import DNS_MESSAGES
            from .proxy_credentials import ERROR_MESSAGES as PROXY_AUTH_MESSAGES
            messages = {**DNS_MESSAGES, **PROXY_AUTH_MESSAGES}
            code = exc.code if isinstance(exc, FetchError) and exc.code in messages else 'public_task_failed'
            self._save(status='failed',code=code,error_type=type(exc).__name__,
                       message=messages.get(code,'任务未完成，已有数据仍在本机；请检查来源可用性或稍后重新确认，不会生成模拟数据。'))

    def _import(self, result, query):
        jobs=result['response']['jobs']
        records=[as_record(j,self.hybrid.registry[j['source']]) for j in jobs]
        summary={'success':True,'collected_at':min((j['collected_at'] for j in jobs),key=parse_time) if jobs else '',
                 'source_scope':list(query.source_scope),'cache_reused':result['cache_reused'],
                 'stale':result['stale'],'refresh_error':result['refresh_error'],
                 'network_requests_this_click':result['network_requests'],'report_id':'',
                 'next_cursor':result['response']['next_cursor'],'execution_mode':self.mode()}
        for key in ('matching_jobs','returned_jobs','available_jobs'):
            if key in result:
                summary[key]=result[key]
        change = result.get('catalog_change') if self.mode() == 'local_direct' else None
        if change is not None:
            summary['catalog_change'] = compact_change(change)
        if not records:
            return {**summary,'code':'public_empty','message':'所选来源中未取得匹配条目，不代表整个市场没有岗位。'}
        from .pipeline import analyze
        with writer_lock(self.workspace.root):
            with Store(self.workspace.db) as store:
                for record in records: store.add(record)
            config=copy.deepcopy(self.workspace.config)
            for key in query.source_scope:
                source=self.hybrid.registry[key]
                config['platforms'][key]={'label':source.label,'domains':list(source.domains)}
            report_id=uuid.uuid4().hex
            folder=self.workspace.root/'reports'/report_id
            with tempfile.TemporaryDirectory(prefix='.public-batch-',dir=self.root) as tmp:
                db=Path(tmp)/'batch.sqlite'
                with Store(db) as batch:
                    for record in records: batch.add(record)
                manifest=analyze(db,folder,config=config,platform_filter=list(query.source_scope))
            audit={**summary,'report_id':report_id,'generated_at':result['response']['generated_at'],
                   'observed_at':result['observed_at'],'next_cursor':result['response']['next_cursor'],
                   'jobs':[dict(j, text_sha256=hashlib.sha256(j['text'].encode()).hexdigest())
                           for j in jobs]}
            for job in audit['jobs']:job.pop('text')
            if change is not None:
                atomic_json(folder/'catalog_changes.json', change)
                manifest['output_files_sha256']['catalog_changes.json'] = hashlib.sha256(
                    (folder/'catalog_changes.json').read_bytes()).hexdigest()
            atomic_json(folder/'public_source.json',audit)
            manifest['output_files_sha256']['public_source.json']=hashlib.sha256((folder/'public_source.json').read_bytes()).hexdigest()
            atomic_json(folder/'run_manifest.json',manifest)
        message=('使用缓存结果生成本地报告，采集时间保持原值。' if result['cache_reused']
                 else '已取得所选公开来源结果并生成本地报告；完整正文与摘要保持区分。')
        if self.mode() == 'local_direct':
            message+=' 本机直取 Anthropic 公开招聘；本页 '+str(len(jobs))+' 条，筛选匹配 '+str(result.get('matching_jobs',len(jobs)))+' 条。'
        message+=' 最早的来源采集时间：'+summary['collected_at']+'。'
        if result['stale']:
            message+=' 刷新暂不可用，当前是过期缓存，不是实时结果。'
        return {**summary,'report_id':report_id,'code':'public_results_received','message':message}

    def close(self):
        with self._lock:
            self._closed=True
        if self._thread:
            self._thread.join(timeout=20)
