"""Opt-in daily local queries, using the original public task/report pipeline."""
from __future__ import annotations

from contextlib import closing, contextmanager, ExitStack
import copy
import json
import math
import sqlite3
import threading
import time
import uuid

from .collection import writer_lock
from .public_contract import ContractError, PublicQuery
from .public_tasks import PublicTaskBusy
from .workspace import InputError

DAY = 86400
CONSENT = 'local-public-daily-v1'
MESSAGES = {
    'disabled': '计划未启用，不会定时联网。',
    'scheduled': '已保存，每24小时执行一次；工作台服务需保持运行，关闭网页不影响计划。',
    'dispatching': '正在记录并启动本次公开查询。',
    'running': '本次公开查询正在运行，结果沿用原报告流程。',
    'interrupted': '上次执行中断或结果不确定，计划已暂停；请先核对原任务，再重新确认计划。',
    'conditions_changed': '来源契约或网络路线已变化，计划已暂停；请核对新条件后重新确认。',
    'result_attention': '本次查询失败、被停止或使用过期缓存，计划已暂停；原结果保留，请先处理原因。',
    'clock_rollback': '系统时间回拨，计划已暂停；核对时间后重新确认，不补跑历史次数。',
    'storage_error': '计划记录暂不可用，调度已停止；请保留原记录并检查工作区。',
    'owner_busy': '另一工作台服务持有调度锁；此实例等待，不重复执行。',
}


def _default():
    return dict(schema_version=1,revision=0,status='disabled',code='disabled',query=None,
                binding='',policy_id='',consent_version='',next_due=None,last_seen=0,
                attempt_id='',active_task_id='',history=[])


def _finite(value):
    return type(value) in (int,float) and math.isfinite(value) and 0 <= value < 253402300799


class ScheduleBusy(InputError):
    pass


@contextmanager
def _held(root):
    with ExitStack() as stack:
        try:
            stack.enter_context(writer_lock(root))
        except InputError as exc:
            if isinstance(exc.__cause__,OSError):
                raise ScheduleBusy('计划正在由另一操作处理，请稍后刷新。') from None
            raise
        yield


def _unique(pairs):
    result={}
    for key,value in pairs:
        if key in result:raise ValueError('duplicate field')
        result[key]=value
    return result


class PublicSchedule:
    def __init__(self, workspace, tasks, *, clock=time.time):
        self.workspace,self.tasks,self.clock=workspace,tasks,clock
        self.root=workspace.root/'public_schedule'
        self.path=self.root/'schedule.sqlite'
        self._lock=threading.RLock();self._stop=threading.Event();self._wake=threading.Event()
        self._thread=None;self._owner=False;self._error='';self._last_seen=0

    def _safe(self):
        if self.root.is_symlink() or self.path.is_symlink():
            raise InputError('计划目录和记录不能使用符号链接。')

    @contextmanager
    def _edit(self):
        with self._lock:
            self._safe();self.root.mkdir(exist_ok=True,mode=0o700)
            control=self.root/'control';control.mkdir(exist_ok=True,mode=0o700)
            if control.is_symlink():raise InputError('计划控制目录无效。')
            # Covers the durable dispatch marker AND task creation. A stop
            # cannot slip between granting an attempt and starting that task.
            with _held(control):yield

    def _read(self):
        self._safe()
        if not self.path.exists():return _default()
        try:
            with closing(sqlite3.connect(self.path.resolve().as_uri()+'?mode=ro',uri=True,timeout=5)) as conn:
                row=conn.execute('SELECT payload FROM schedule WHERE id=1').fetchone()
            if row is None or len(row[0])>131072:raise ValueError
            value=json.loads(row[0],object_pairs_hook=_unique)
            if (not isinstance(value,dict) or set(value)!=set(_default())
                    or type(value['schema_version']) is not int or value['schema_version']!=1
                    or type(value['revision']) is not int or not 1<=value['revision']<2**31
                    or value['status'] not in {'disabled','scheduled','dispatching','running','paused'}
                    or value['code'] not in MESSAGES or not _finite(value['last_seen'])
                    or value['next_due'] is not None and not _finite(value['next_due'])
                    or not isinstance(value['history'],list) or len(value['history'])>20):raise ValueError
            for field,length in (('binding',64),('policy_id',16),('attempt_id',32),('active_task_id',32)):
                item=value[field]
                if not isinstance(item,str) or item and (len(item)!=length or any(c not in '0123456789abcdef' for c in item)):raise ValueError
            if value['query'] is not None:
                query=PublicQuery.from_dict(value['query'])
                if (query.cursor or value['consent_version']!=CONSENT
                        or not value['binding'] or not value['policy_id']):raise ValueError
            elif value['status']!='disabled' or value['consent_version']:raise ValueError
            if value['active_task_id'] and not value['attempt_id']:raise ValueError
            if value['status'] in {'disabled','paused'} and value['next_due'] is not None:raise ValueError
            if value['status']=='scheduled' and (value['next_due'] is None or value['attempt_id'] or value['active_task_id']):raise ValueError
            if value['status'] in {'running','dispatching'} and not value['attempt_id']:raise ValueError
            if value['status']=='running' and not value['active_task_id']:raise ValueError
            for entry in value['history']:
                if (not isinstance(entry,dict) or set(entry)!={'attempt_id','task_id','status','report_id','stale','finished_at'}
                        or entry['status'] not in {'completed','failed','cancelled','interrupted','unknown'}
                        or type(entry['stale']) is not bool or not _finite(entry['finished_at'])):raise ValueError
                for field in ('attempt_id','task_id','report_id'):
                    item=entry[field]
                    if not isinstance(item,str) or item and (len(item)!=32 or any(c not in '0123456789abcdef' for c in item)):raise ValueError
            return value
        except (ValueError,TypeError,KeyError,sqlite3.Error,OSError):
            raise InputError('计划记录无效或版本不兼容，未启动网络；请保留记录并检查工作区。') from None

    def _write(self,value,*,advance=True):
        self._safe()
        if advance:value['revision']+=1
        if value['revision']>=2**31:raise InputError('计划修订号已达上限，未覆盖记录。')
        try:
            with closing(sqlite3.connect(self.path.resolve().as_uri()+'?mode=rwc',uri=True,timeout=5)) as conn:
                conn.execute('BEGIN IMMEDIATE')
                conn.execute('CREATE TABLE IF NOT EXISTS schedule (id INTEGER PRIMARY KEY CHECK(id=1), payload TEXT NOT NULL)')
                conn.execute('INSERT OR REPLACE INTO schedule VALUES(1,?)',(json.dumps(value,separators=(',',':')),))
                conn.commit()
        except (sqlite3.Error,OSError):raise InputError('无法保存计划，未确认成功；请保留记录并检查工作区。') from None

    def state(self):
        with self._lock:
            value=self._read()
            return {**{k:copy.deepcopy(value[k]) for k in ('revision','status','query','next_due','active_task_id','history')},
                    'code':self._error or value['code'],'message':MESSAGES[self._error or value['code']],
                    'interval_seconds':DAY,'worker_active':self._owner,
                    'available':self.tasks.mode()=='local_direct','network_tested':False}

    def _conditions(self,query):
        if self.tasks.mode()!='local_direct':raise InputError('定时计划仅用于本机已批准的公开目录查询。')
        request=PublicQuery.from_dict(query)
        if request.cursor:raise InputError('定时计划不能保存游标或自动读取下一页。')
        try:self.tasks.hybrid._scope(request)
        except ContractError:
            raise InputError('所选来源尚未批准本机公开查询。') from None
        if any(not self.tasks.hybrid.registry[key].local_access_approved for key in request.source_scope):
            raise InputError('所选来源尚未批准本机公开查询。')
        policy=self.workspace.network_policy()
        if policy.error:raise InputError('当前网络配置未就绪，请处理后再确认计划。')
        return request.payload(),self.tasks._binding('search',request.payload()),policy.fingerprint

    def _now(self):
        now=self.clock()
        if not _finite(now) or not _finite(now+DAY):raise InputError('当前时间无效，未启动任务。')
        return now

    def configure(self,data):
        if self._error=='storage_error':raise InputError('调度记录发生异常，请检查工作区并重启服务后再确认。')
        if (not isinstance(data,dict) or set(data)!={'query','consent','revision'}
                or data['consent'] is not True or type(data['revision']) is not int):
            raise InputError('请明确同意按当前来源和查询每24小时执行；不接受额外地址或凭据。')
        query,binding,policy_id=self._conditions(data['query']);now=self._now()
        with self._edit():
            value=self._read()
            if value['revision']!=data['revision']:raise InputError('计划已更新，请刷新后确认。')
            if value['attempt_id']:raise InputError('本次计划查询尚未结束，请先停止并等待当前只读步骤完成。')
            value.update(query=query,binding=binding,policy_id=policy_id,consent_version=CONSENT,
                         status='scheduled',code='scheduled',next_due=now+DAY,last_seen=now)
            self._write(value);self._error='';self._last_seen=now
        self._wake.set()
        return self.state()

    def disable(self,data):
        if not isinstance(data,dict) or set(data)!={'revision'} or type(data['revision']) is not int:
            raise InputError('请选择当前版本的计划。')
        with self._edit():
            value=self._read()
            if value['revision']!=data['revision']:raise InputError('计划已更新，请刷新后重试。')
            value.update(status='disabled',code='disabled',next_due=None)
            self._write(value)
            self._cancel_active(value)
        self._wake.set()
        return self.state()

    def _finish(self,value,task,now,*,interrupted=False):
        success=not interrupted and task.get('status')=='completed' and task.get('stale') is not True
        value['history'].append(dict(attempt_id=value['attempt_id'],task_id=value['active_task_id'],
            status=task.get('status','unknown') if task.get('status') in {'completed','failed','cancelled','interrupted'} else 'unknown',
            report_id=task.get('report_id',''),stale=task.get('stale') is True,finished_at=now))
        value['history']=value['history'][-20:]
        held=value['status'] in {'disabled','paused'}
        previous_status,previous_code=value['status'],value['code']
        value.update(attempt_id='',active_task_id='',last_seen=now,
                     status=previous_status if held else 'scheduled' if success else 'paused',
                     code=previous_code if held else 'scheduled' if success else 'interrupted' if interrupted else 'result_attention',
                     next_due=now+DAY if success and not held else None)
        self._write(value)

    def _task(self):
        # Task-only snapshot: status polling must not repeatedly discover OS
        # proxy settings. Lock is shared with manual submit/cancel operations.
        return self.tasks.snapshot()

    def _cancel_active(self,value):
        with self.tasks._lock:
            task=self._task()
            if (not task.get('owned_elsewhere') and value['active_task_id'] and task.get('id')==value['active_task_id']
                    and task.get('status') in {'queued','running','cancelling'}):
                self.tasks.cancel({'id':task['id']})

    def recover(self):
        """Only call after this worker holds the OS owner lock."""
        with self._edit():
            value=self._read()
            if not value['attempt_id']:return
            task=self._task()
            matched=bool(value['active_task_id']) and task.get('id')==value['active_task_id']
            if matched and task.get('owned_elsewhere') and task.get('status') in {'queued','running','cancelling'}:return
            complete=matched and task.get('status')=='completed'
            self._finish(value,task if matched else {},self._now(),interrupted=not complete)

    def tick(self):
        """One bounded scheduling decision; network happens in PublicTasks."""
        if not self.path.exists():return
        with self._edit():
            value=self._read();now=self._now()
            if now<max(value['last_seen'],self._last_seen):
                value.update(status='paused',code='clock_rollback',next_due=None,last_seen=now);self._write(value)
            self._last_seen=now
            if value['active_task_id']:
                with self.tasks._lock:
                    task=self._task()
                    if task.get('id')!=value['active_task_id']:
                        self._finish(value,{},now,interrupted=True);return
                    if task.get('status') in {'queued','running','cancelling'}:
                        if value['status'] in {'paused','disabled'} and not task.get('owned_elsewhere'):
                            self.tasks.cancel({'id':task['id']})
                        return
                    self._finish(value,task,now);return
            if value['status']!='scheduled' or now<value['next_due']:return
            try:
                _,binding,policy_id=self._conditions(value['query'])
                if binding!=value['binding'] or policy_id!=value['policy_id']:raise InputError('changed')
            except (ValueError,TypeError,KeyError):
                value.update(status='paused',code='conditions_changed',next_due=None);self._write(value);return
            with self.tasks._lock:
                if self._stop.is_set() or self.tasks.busy():return
                value.update(status='dispatching',code='dispatching',attempt_id=uuid.uuid4().hex,last_seen=now)
                self._write(value)  # Crash afterwards is uncertain, never replayed.
                try:
                    started=self.tasks.scheduled_search(value['query'],binding=value['binding'],policy_id=value['policy_id'])
                except PublicTaskBusy:
                    # The lease was refused before creating a task/request.
                    value.update(status='scheduled',code='scheduled',attempt_id='',active_task_id='')
                    self._write(value);return
                except Exception:
                    self._finish(value,{},now,interrupted=True);return
                value.update(status='running',code='running',active_task_id=started['id'])
                self._write(value)

    def start(self):
        if self._thread and self._thread.is_alive():return
        self._stop.clear();self._thread=threading.Thread(target=self._run,name='radar-public-schedule',daemon=True);self._thread.start()

    def _run(self):
        while not self._stop.is_set():
            if not self.path.exists():
                self._wake.wait(2);self._wake.clear();continue
            try:
                self._safe();owner=self.root/'owner';owner.mkdir(exist_ok=True,mode=0o700)
                if owner.is_symlink():raise InputError('invalid owner')
                with _held(owner):
                    self._owner=True
                    self._error=''
                    self.recover()
                    while not self._stop.is_set():
                        try:self.tick()
                        except ScheduleBusy:pass  # Another page is configuring.
                        self._wake.wait(2);self._wake.clear()
            except ScheduleBusy:
                self._error='owner_busy'
                self._wake.wait(2);self._wake.clear()
            except (InputError,OSError):
                # A failed write after dispatch is uncertain. Never loop back
                # into recovery in this process or silently resume dispatch.
                self._error='storage_error'
                return
            finally:self._owner=False

    def close(self):
        self._stop.set();self._wake.set()
        if self._thread:self._thread.join(timeout=10)
