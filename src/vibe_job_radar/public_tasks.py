"""Authenticated local jobs for the public example and optional hybrid service.

Only validated query fields leave the device. Browser credentials and private
workspace records are never inputs to a public query. Startup/status are offline;
network requests require a user's explicit start/search consent.
"""
from __future__ import annotations

import copy
from contextlib import contextmanager
import hashlib
import json
import tempfile
import threading
import time
import uuid
from dataclasses import asdict
from pathlib import Path

from .catalog_changes import compact_change
from .collection import writer_lock
from .record_lock import record_lock
from .network_policy import current_policy
from .public_contract import PublicQuery, as_record
from .public_example import PublicExample
from .public_lifecycle import PublicTaskCancelled, check_cancelled
from .store import Store
from .utils import atomic_json, utc_now, parse_time
from .workspace import InputError


class PublicTaskBusy(InputError):
    """No task was submitted and no new source request was started."""


class PublicTasks:
    def __init__(self, workspace, *, hybrid_client=None, example_factory=PublicExample):
        self.workspace, self.hybrid = workspace, hybrid_client
        self.factory=example_factory
        self.root=workspace.root/'public_tasks'
        self.root.mkdir(exist_ok=True,mode=0o700)
        self.path=self.root/'state.json'
        if self.root.is_symlink():
            raise InputError('公开任务记录不能使用符号链接。')
        self._lock=threading.RLock(); self._thread=None; self._closed=False
        self._cancel = threading.Event();self._lease=None;self._foreign=False
        self.owner_root=self.root/'owner'
        self._state={'status':'idle','message':'点击获取后才会联网；个人材料和登录态保留本地。'}
        self._refresh()

    def _claim(self):
        if self.root.is_symlink() or self.owner_root.is_symlink():
            raise InputError('公开任务所有权目录不能使用符号链接。')
        self.owner_root.mkdir(exist_ok=True,mode=0o700)
        lease=writer_lock(self.owner_root)
        try:lease.__enter__()
        except InputError as exc:
            if isinstance(exc.__cause__,OSError):
                raise PublicTaskBusy('另一个本机服务正在执行公开任务，请回到原工作台查看或停止。') from None
            raise
        return lease

    def busy(self):
        with self._lock:
            if self._lease is not None or self._thread and self._thread.is_alive():return True
            if not self.owner_root.exists():return False
            try:lease=self._claim()
            except PublicTaskBusy:return True
            else:lease.__exit__(None,None,None);return False

    @contextmanager
    def _record_lock(self):
        # Atomic replacement on Windows can fail while another instance reads
        # the old file. Readers/writers share a short lock; owner leases remain
        # nonblocking and are never inferred from PID/stale-file timestamps.
        with record_lock(self.root):yield

    def _read_record(self):
        if self.root.is_symlink():
            raise InputError('公开任务记录不能使用符号链接。')
        with self._record_lock():
            # Even Windows metadata probes can briefly hold a file handle.
            # Keep all accesses to state.json under the replacement lock.
            if self.path.is_symlink():raise InputError('公开任务记录不能使用符号链接。')
            if self.path.exists():
                if self.path.stat().st_size>2_000_000:
                    raise InputError('公开任务记录过大，请检查工作区。')
                try:value=json.loads(self.path.read_text(encoding='utf-8'))
                except (ValueError,OSError):raise InputError('公开任务记录损坏，未恢复或启动网络请求。') from None
                if not isinstance(value,dict):raise InputError('公开任务记录损坏，未恢复或启动网络请求。')
                self._state=value
            else:self._state={'status':'idle','message':'点击获取后才会联网；个人材料和登录态保留本地。'}

    def _refresh(self):
        if self._lease is not None:return
        self._foreign=self.busy()
        self._read_record()
        if not self._foreign and self._state.get('status') in {'queued','running','cancelling'}:
            self._state.update(status='interrupted',message='上次服务已退出；已保存条件和数据，确认后可继续。')

    def snapshot(self):
        """Current task and ownership only; no network-policy discovery."""
        with self._lock:
            self._refresh()
            task=copy.deepcopy(self._state)
            task['owned_elsewhere']=self._foreign
            task['can_cancel']=not self._foreign and self._lease is not None and task.get('status') in {'queued','running'}
            task['can_resume']=not self._foreign and self._can_resume()
            if self._foreign:
                task['message']='另一个本机服务正在执行公开任务；此处只查看进度，请回到原工作台停止。'
            return task

    def _save(self, **changes):
        with self._lock:
            with self._record_lock():
                if self.path.is_symlink():
                    raise InputError('公开任务记录不能使用符号链接。')
                self._state.update(changes,updated_at=utc_now())
                atomic_json(self.path,self._state)

    def mode(self):
        return getattr(self.hybrid, 'execution_mode', 'remote_service') if self.hybrid else 'disabled'

    def state(self):
        with self._lock:
            task = self.snapshot()
            return {'task':task,'hybrid_service_configured':self.mode() == 'remote_service',
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

    def _binding(self, kind, query):
        if kind == 'example':
            if query != {}:
                raise InputError('公开案例记录不接受额外查询条件。')
            from .public_example import API_URL, API_CONTRACT
            value = ['example', API_URL, API_CONTRACT]
        elif kind == 'search' and self.hybrid is not None:
            request = PublicQuery.from_dict(query)
            self.hybrid._scope(request)
            value = ['search', self.mode(), getattr(self.hybrid, 'origin', ''), request.payload(),
                     [asdict(self.hybrid.registry[key]) for key in request.source_scope]]
        else:
            raise InputError('原任务的来源或执行方式不可用，请重新核对来源后建立任务。')
        return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()

    def scheduled_search(self, query, *, binding, policy_id):
        """Internal dispatch from a previously confirmed durable local plan.

        Freeze the approved route before starting the worker: a preferences
        change while the thread is queued must not silently change its route.
        No policy object or credential is persisted in the task record.
        """
        with self._lock:
            policy=self.workspace.network_policy()
            if (self.mode()!='local_direct' or policy.error or policy.fingerprint!=policy_id
                    or self._binding('search',query)!=binding):
                raise InputError('计划来源或网络条件已变化，请重新确认。')
            return self._submit('search',query,network_policy=policy)

    def _can_resume(self):
        state = self._state
        if (state.get('status') not in {'cancelled','interrupted'}
                or not isinstance(state.get('id'), str) or len(state['id']) != 32
                or any(c not in '0123456789abcdef' for c in state['id'])
                or type(state.get('attempt')) is not int or state['attempt'] < 1):
            return False
        try:
            return state.get('resume_binding') == self._binding(state.get('kind'), state.get('query'))
        except (InputError, ValueError, KeyError, TypeError):
            return False

    def cancel(self, data):
        with self._lock:
            self._refresh()
            if self._foreign:raise PublicTaskBusy('该任务由另一个本机服务执行，请回到原工作台停止。')
            if set(data) != {'id'} or not isinstance(data['id'], str) or data['id'] != self._state.get('id'):
                raise InputError('请选择当前公开任务，旧页面不能停止另一个任务。')
            if self._state.get('status') not in {'queued','running','cancelling'}:
                return {'id':data['id'], 'message':'任务已经结束，已保存结果保留。'}
            self._cancel.set()
            self._save(status='cancelling', message='正在停止后续步骤；当前只读请求或已经开始的本地保存结束后停止，已有数据不删除。')
            return {'id':data['id'], 'cancelling':True}

    def resume(self, data):
        with self._lock:
            self._refresh()
            if (set(data) != {'id','consent'} or data['consent'] is not True
                    or not isinstance(data['id'], str) or data['id'] != self._state.get('id')):
                raise InputError('请明确确认继续当前任务的已保存查询；不接受新地址、凭据或替换条件。')
            if self._foreign or not self._can_resume():
                raise InputError('此任务不处于可恢复的中断状态，或来源/契约已变化；已保存结果保留，请核对后新建任务。')
            return self._submit(self._state['kind'], self._state['query'], resume_id=data['id'])

    def _submit(self, kind, query, *, resume_id='', network_policy=None):
        with self._lock:
            if self._closed:
                raise InputError('本地服务正在关闭。')
            if self._thread and self._thread.is_alive():
                raise PublicTaskBusy('公开任务正在执行；进度已保留，请勿重复提交。')
            self._lease=self._claim()
            try:
                self._foreign=False;self._read_record()
                if self._state.get('status') in {'queued','running','cancelling'}:
                    self._state['status']='interrupted'
                if resume_id:
                    if self._state.get('id')!=resume_id or not self._can_resume():
                        raise InputError('原任务已变化，请刷新后核对；没有继续旧页面的任务。')
                    kind,query=self._state['kind'],self._state['query']
                binding = self._binding(kind, query)
                previous = self._state if resume_id else {}
                self._cancel.clear()
                self._state={'id':previous.get('id', uuid.uuid4().hex),'kind':kind,'query':copy.deepcopy(query),
                             'created_at':previous.get('created_at', utc_now()), 'resume_binding':binding,
                             'attempt':previous.get('attempt', 0)+1}
                self._save(status='queued',message='任务已保存，正在准备获取公开数据。',report_id='')
                args=(kind,query) if network_policy is None else (kind,query,network_policy)
                self._thread=threading.Thread(target=self._run_owned,args=args,name='radar-public-data',daemon=True)
                self._thread.start()
                return {'id':self._state['id'],'queued':True}
            except BaseException:
                if not self._thread or not self._thread.is_alive():
                    self._release_owner()
                    if self._thread and self._thread.ident is None:self._thread=None
                raise

    def _release_owner(self):
        lease,self._lease=self._lease,None
        if lease is not None:lease.__exit__(None,None,None)

    def _run_owned(self,*args):
        try:self._run(*args)
        finally:
            with self._lock:self._release_owner()

    def _begin_commit(self):
        # Cancellation before this point prevents persistence. Once the local
        # commit starts, finish it and retain/report the result atomically.
        with self._lock:
            check_cancelled(self._cancel)
            self._save(phase='saving', message='正在保存本批公开结果；完成后保留报告，不再启动后续查询。')

    def _run(self, kind, value, network_policy=None):
        try:
            with self._lock:
                check_cancelled(self._cancel)
                self._save(status='running',message='正在获取公开数据并生成本地报告；不读取个人证据或登录态。')
            if kind=='example':
                example = self.factory(self.workspace)
                example.cancelled, example.before_commit = self._cancel, self._begin_commit
                result=example.run({'consent':True})
            else:
                query=PublicQuery.from_dict(value)
                check_cancelled(self._cancel)
                options={} if network_policy is None else {'network_policy':network_policy}
                response=self.hybrid.search(query,consent=True,**options)
                self._begin_commit()
                result=self._import(response,query)
            # Do not persist the report's full private rendering in task status.
            allowed={'success','message','code','report_id','source_url','collected_at','checked_at',
                     'cache_reused','stale','refresh_error','network_requests_this_click','scope','source_scope',
                     'next_cursor','matching_jobs','returned_jobs','available_jobs','execution_mode','catalog_change'}
            view={k:v for k,v in result.items() if k in allowed}
            if self._cancel.is_set() and result.get('success'):
                view['message'] = '停止请求到达时本批保存已开始或结果已完成；本批结果保留，没有启动下一页。'
                view['cancel_requested'] = True
            self._save(**view,status='completed' if result.get('success') else 'failed')
        except PublicTaskCancelled:
            self._save(status='cancelled',code='public_task_cancelled',report_id='',
                       message='公开任务已停止；未开始新的本地报告，已有缓存和历史数据保留。可确认后继续已保存条件。')
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
            self._cancel.set()
        if self._thread:
            self._thread.join(timeout=20)
