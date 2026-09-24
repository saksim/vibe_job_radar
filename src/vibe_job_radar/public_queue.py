"""Explicit, finite local queries; durable order is never permission to replay."""
from contextlib import closing, contextmanager
import copy
import json
import sqlite3
import threading
import time
import uuid

from .public_contract import PublicQuery, ContractError
from .public_schedule import DAY, _finite, _held, _unique, ScheduleBusy
from .public_tasks import PublicTaskBusy
from .workspace import InputError

CONSENT='local-public-queue-v1'
CAPACITY=5
GAP=30
MESSAGES={
    'idle':'没有待运行的公开查询。',
    'queued':'已保存待办，按顺序运行；工作台或独立进程需保持运行。',
    'dispatching':'正在记录并启动这一条查询。',
    'running':'正在执行这一条查询，结果进入原报告。',
    'paused':'队列已暂停；尚未开始的查询需明确确认后继续。',
    'interrupted':'上一条执行结果不确定，已保留记录并暂停，不自动重放；请先核对原任务。',
    'result_attention':'上一条失败、被停止或使用过期缓存；已保留结果并暂停剩余待办。',
    'expired':'排队许可超过24小时，未发起该查询；剩余待办已暂停，请重新核对。',
    'conditions_changed':'来源或网络路线已变化，待办已暂停；请核对当前条件后重新确认。',
    'clock_rollback':'系统时间回拨，队列已暂停；核对时间后重新确认。',
    'storage_error':'待办记录不可用，队列已停止；请保留原记录并检查工作区。',
    'owner_busy':'另一个本机进程执行此队列，本实例只显示状态。',
}


def _default():
    return dict(schema_version=1,revision=0,status='idle',code='idle',next_due=0,last_seen=0,items=[],history=[])


def _id(value,length=32,empty=False):
    return type(value) is str and ((empty and value=='') or len(value)==length and all(c in '0123456789abcdef' for c in value))


class PublicQueue:
    def __init__(self,workspace,tasks,*,clock=time.time):
        self.workspace,self.tasks,self.clock=workspace,tasks,clock
        self.root=workspace.root/'public_queue';self.path=self.root/'queue.sqlite'
        self._lock=threading.RLock();self._stop=threading.Event();self._wake=threading.Event()
        self._thread=None;self._owner=False;self._error='';self._last_seen=0

    def _safe(self):
        if self.root.is_symlink() or self.path.is_symlink():raise InputError('待办目录和记录不能使用符号链接。')
        if self.path.exists() and (not self.path.is_file() or self.path.stat().st_size>1024*1024):
            raise InputError('待办记录大小或类型异常，请保留记录并检查工作区。')

    @contextmanager
    def _edit(self):
        with self._lock:
            self._safe();self.root.mkdir(exist_ok=True,mode=0o700)
            control=self.root/'control';control.mkdir(exist_ok=True,mode=0o700)
            if control.is_symlink():raise InputError('待办控制目录无效。')
            with _held(control):yield

    @staticmethod
    def _schema(conn,create=False):
        version=conn.execute('PRAGMA user_version').fetchone()[0]
        tables={row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if create and version==0 and not tables:
            conn.execute('CREATE TABLE queue (id INTEGER PRIMARY KEY CHECK(id=1), payload TEXT NOT NULL)')
            conn.execute('PRAGMA user_version=1')
        elif version!=1 or tables!={'queue'}:raise ValueError('unsupported queue')

    @staticmethod
    def _query(query):
        request=PublicQuery.from_dict(query)
        if request.cursor or request.limit>20 or len(request.source_scope)!=1:
            raise InputError('每条待办只能选择一个来源、最多20条首屏结果；不接受翻页游标。')
        return request.payload()

    def _validate(self,value):
        if (not isinstance(value,dict) or set(value)!=set(_default()) or type(value['schema_version']) is not int
                or value['schema_version']!=1 or type(value['revision']) is not int or not 1<=value['revision']<2**31
                or value['status'] not in {'idle','queued','dispatching','running','paused'} or value['code'] not in MESSAGES
                or not _finite(value['next_due']) or not _finite(value['last_seen'])
                or not isinstance(value['items'],list) or len(value['items'])>CAPACITY
                or not isinstance(value['history'],list) or len(value['history'])>20):raise ValueError('invalid queue')
        ids=set()
        for index,item in enumerate(value['items']):
            if (not isinstance(item,dict) or set(item)!={'id','query','binding','policy_id','confirmed_at','expires_at','consent','phase','task_id'}
                    or not _id(item['id']) or item['id'] in ids or not _id(item['binding'],64) or not _id(item['policy_id'],16)
                    or item['consent']!=CONSENT or item['phase'] not in {'pending','dispatching','running'}
                    or not _finite(item['confirmed_at']) or not _finite(item['expires_at'])
                    or item['expires_at']!=item['confirmed_at']+DAY or not _id(item['task_id'],empty=True)
                    or item['phase']=='running' and not item['task_id']
                    or item['phase']!='running' and item['task_id']
                    or index>0 and item['phase']!='pending' or self._query(item['query'])!=item['query']):raise ValueError('invalid item')
            ids.add(item['id'])
        if value['status']=='idle' and value['items']:raise ValueError('idle with work')
        if value['status']!='paused' and value['code']!=value['status']:raise ValueError('code mismatch')
        if value['status']=='paused' and value['code'] not in {
                'paused','interrupted','result_attention','expired','conditions_changed','clock_rollback'}:
            raise ValueError('invalid paused reason')
        if value['status'] in {'queued','dispatching','running'}:
            if not value['items']:raise ValueError('missing work')
            phase={'queued':'pending','dispatching':'dispatching','running':'running'}[value['status']]
            if value['items'][0]['phase']!=phase:raise ValueError('phase mismatch')
        for item in value['history']:
            if (not isinstance(item,dict) or set(item)!={'id','query','task_id','report_id','status','stale','finished_at'}
                    or not _id(item['id']) or item['id'] in ids or not _id(item['task_id'],empty=True)
                    or not _id(item['report_id'],empty=True) or type(item['stale']) is not bool
                    or item['status'] not in {'completed','failed','cancelled','interrupted','expired'}
                    or item['status']=='completed' and (not item['task_id'] or not item['report_id'])
                    or item['status']=='expired' and (item['task_id'] or item['report_id'] or item['stale'])
                    or not _finite(item['finished_at']) or self._query(item['query'])!=item['query']):raise ValueError('invalid history')
            ids.add(item['id'])
        return value

    def _read(self):
        self._safe()
        if not self.path.exists():return _default()
        try:
            with closing(sqlite3.connect(self.path.resolve().as_uri()+'?mode=ro',uri=True,timeout=5)) as conn:
                conn.execute('BEGIN');self._schema(conn)
                row=conn.execute('SELECT payload FROM queue WHERE id=1').fetchone()
            if row is None or type(row[0]) is not str or len(row[0])>131072:raise ValueError('invalid record')
            return self._validate(json.loads(row[0],object_pairs_hook=_unique))
        except (ValueError,TypeError,KeyError,RecursionError,sqlite3.Error,OSError):
            raise InputError('待办记录无效或版本不兼容，未开始查询；请保留原记录。') from None

    def _write(self,value):
        self._safe();value['revision']+=1
        try:
            self._validate(value)
            with closing(sqlite3.connect(self.path.resolve().as_uri()+'?mode=rwc',uri=True,timeout=5)) as conn:
                conn.execute('BEGIN IMMEDIATE');self._schema(conn,True)
                conn.execute('INSERT OR REPLACE INTO queue VALUES(1,?)',(json.dumps(value,separators=(',',':')),));conn.commit()
        except (ValueError,TypeError,KeyError,sqlite3.Error,OSError):
            raise InputError('无法保存待办，未确认成功；请保留原记录。') from None

    def _now(self):
        now=self.clock()
        if not _finite(now) or not _finite(now+DAY):raise InputError('当前时间无效，未启动查询。')
        return now

    def _conditions(self,query):
        query=self._query(query);request=PublicQuery.from_dict(query)
        if self.tasks.mode()!='local_direct':raise InputError('待办仅用于本机已批准的固定公开目录。')
        try:self.tasks.hybrid._scope(request)
        except ContractError:raise InputError('所选来源尚未批准本机查询。') from None
        if any(not self.tasks.hybrid.registry[key].local_access_approved for key in request.source_scope):
            raise InputError('所选来源尚未批准本机查询。')
        policy=self.workspace.network_policy()
        if policy.error:raise InputError('网络设置未就绪，请处理后再确认待办。')
        return query,self.tasks._binding('search',query),policy.fingerprint

    def state(self):
        with self._lock:
            value=self._read();code=self._error or value['code']
            message=MESSAGES[value['code']]+' '+MESSAGES[code] if code=='owner_busy' else MESSAGES[code]
            return {**copy.deepcopy(value),'code':code,'message':message,
                    'worker_active':self._owner,'available':self.tasks.mode()=='local_direct','capacity':CAPACITY,
                    'interval_seconds':GAP,'consent_seconds':DAY,'network_tested':False}

    def _request(self,data,fields):
        if (not isinstance(data,dict) or set(data)!=fields or type(data.get('revision')) is not int
                or 'consent' in fields and data['consent'] is not True):raise InputError('请核对当前待办并明确确认操作。')
        if self._error=='storage_error':raise InputError(MESSAGES['storage_error'])

    @staticmethod
    def _revision(value,data):
        if value['revision']!=data['revision']:raise InputError('待办已更新，请刷新后重新核对。')

    def enqueue(self,data):
        self._request(data,{'revision','consent','query'})
        query,binding,policy_id=self._conditions(data['query']);now=self._now()
        with self._edit():
            value=self._read();self._revision(value,data)
            if len(value['items'])>=CAPACITY:raise InputError('最多保存5条待办，请先完成或移除已有条目。')
            value['items'].append(dict(id=uuid.uuid4().hex,query=query,binding=binding,policy_id=policy_id,
                confirmed_at=now,expires_at=now+DAY,consent=CONSENT,phase='pending',task_id=''))
            if value['status']=='idle':value.update(status='queued',code='queued')
            value['last_seen']=max(value['last_seen'],now);self._last_seen=max(self._last_seen,now)
            self._write(value)
        self._wake.set();return self.state()

    def remove(self,data):
        self._request(data,{'revision','id'})
        with self._edit():
            value=self._read();self._revision(value,data)
            item=next((item for item in value['items'] if item['id']==data['id']),None)
            if item is None or item['phase']!='pending':raise InputError('只能移除尚未开始的当前待办。')
            value['items'].remove(item)
            if not value['items'] and value['status']!='paused':value.update(status='idle',code='idle')
            self._write(value)
        self._wake.set();return self.state()

    def _task(self,item):
        task=self.tasks.snapshot()
        if not item['task_id']:return {}
        if task.get('id')!=item['task_id'] or task.get('attempt',1)!=1:
            task=self.tasks.previous_outcome(item['task_id'],1) or {}
        return task if task.get('kind')=='search' and task.get('resume_binding')==item['binding'] else {}

    def _cancel_active(self,value):
        if not value['items'] or value['items'][0]['phase']=='pending':return
        with self.tasks._lock:
            task=self._task(value['items'][0])
            if not task.get('owned_elsewhere') and task.get('status') in {'queued','running','cancelling'}:
                self.tasks.cancel({'id':task['id']})

    def pause(self,data):
        self._request(data,{'revision'})
        with self._edit():
            value=self._read();self._revision(value,data)
            value.update(status='paused',code='paused');self._write(value);self._cancel_active(value)
        self._wake.set();return self.state()

    def resume(self,data):
        self._request(data,{'revision','consent'});now=self._now()
        with self._edit():
            value=self._read();self._revision(value,data)
            if value['status']!='paused' or not value['items'] or any(i['phase']!='pending' for i in value['items']):
                raise InputError('请等待当前查询结束；只有尚未运行的待办可以重新确认继续。')
            for item in value['items']:
                query,binding,policy_id=self._conditions(item['query'])
                item.update(query=query,binding=binding,policy_id=policy_id,confirmed_at=now,expires_at=now+DAY)
            value.update(status='queued',code='queued',last_seen=now,next_due=max(value['next_due'],now))
            self._write(value);self._last_seen=now
        self._wake.set();return self.state()

    def _finish(self,value,task,now,*,status=None):
        item=value['items'].pop(0)
        status=status or (task.get('status') if task.get('status') in {'completed','failed','cancelled'} else 'interrupted')
        stale=task.get('stale') is True
        value['history'].append(dict(id=item['id'],query=item['query'],task_id=item['task_id'],
            report_id=task.get('report_id',''),status=status,stale=stale,finished_at=now))
        value['history']=value['history'][-20:]
        if value['status']!='paused':
            code=('queued' if value['items'] else 'idle') if status=='completed' and not stale else (
                status if status in {'interrupted','expired'} else 'result_attention')
            value.update(status=code if code in {'queued','idle'} else 'paused',code=code)
        value.update(last_seen=max(value['last_seen'],now),next_due=max(value['last_seen'],now)+GAP);self._write(value)

    def recover(self):
        with self._edit():
            value=self._read()
            if not value['items'] or value['items'][0]['phase']=='pending':return
            now=self._now()
            if now<value['last_seen']:value.update(status='paused',code='clock_rollback')
            with self.tasks._lock:
                task=self._task(value['items'][0])
                if task.get('status') in {'queued','running','cancelling'}:
                    if value['code']=='clock_rollback':self._write(value)
                    return
                self._finish(value,task,now)

    def tick(self):
        if not self.path.exists():return
        with self._edit():
            value=self._read();now=self._now()
            if now<max(value['last_seen'],self._last_seen) and value['code']!='clock_rollback':
                value.update(status='paused',code='clock_rollback');self._write(value)
            self._last_seen=max(self._last_seen,now)
            if not value['items']:return
            item=value['items'][0]
            with self.tasks._lock:
                if item['phase']!='pending':
                    task=self._task(item)
                    if task.get('status') in {'queued','running','cancelling'}:
                        if value['status']=='paused':self._cancel_active(value)
                    else:self._finish(value,task,now)
                    return
                if value['status']!='queued' or now<value['next_due'] or self._stop.is_set():return
                if now>=item['expires_at']:
                    self._finish(value,{},now,status='expired');return
                try:
                    _,binding,policy_id=self._conditions(item['query'])
                    if (binding,policy_id)!=(item['binding'],item['policy_id']):raise InputError('changed')
                except (ValueError,TypeError,KeyError):
                    value.update(status='paused',code='conditions_changed');self._write(value);return
                if self.tasks.busy():return
                item['phase']='dispatching';value.update(status='dispatching',code='dispatching',last_seen=now)
                self._write(value)
                try:started=self.tasks.scheduled_search(item['query'],binding=binding,policy_id=policy_id)
                except PublicTaskBusy:
                    item['phase']='pending';value.update(status='queued',code='queued');self._write(value);return
                except Exception:self._finish(value,{},now);return
                item.update(phase='running',task_id=started['id']);value.update(status='running',code='running')
                try:self._write(value)
                except (InputError,OSError):
                    try:self.tasks.cancel({'id':started['id']})
                    except InputError:pass
                    raise

    def start(self):
        if self.is_running():return
        self._stop.clear();self._thread=threading.Thread(target=self._run,name='radar-public-queue',daemon=True);self._thread.start()

    def _run(self):
        while not self._stop.is_set():
            if not self.path.exists():self._wake.wait(2);self._wake.clear();continue
            try:
                self._safe();owner=self.root/'owner';owner.mkdir(exist_ok=True,mode=0o700)
                if owner.is_symlink():raise InputError('invalid owner')
                with _held(owner):
                    self._owner=True;self._error='';self.recover()
                    while not self._stop.is_set():
                        try:self.tick()
                        except ScheduleBusy:pass
                        self._wake.wait(2);self._wake.clear()
            except ScheduleBusy:
                self._error='owner_busy';self._wake.wait(2);self._wake.clear()
            except (InputError,OSError):self._error='storage_error';return
            finally:self._owner=False

    def is_running(self):return bool(self._thread and self._thread.is_alive())
    def request_stop(self):self._stop.set();self._wake.set()
    def close(self):
        self.request_stop()
        if self.is_running() and self._thread is not threading.current_thread():self._thread.join(5)
