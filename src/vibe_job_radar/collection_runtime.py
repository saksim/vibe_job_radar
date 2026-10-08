"""One explicitly started category batch, owned by the local application."""
from contextlib import contextmanager
import hashlib
import json
import re
import threading

from .utils import atomic_json
from .workspace import InputError


class CollectionBusy(InputError):
    pass


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def validate_scope(collector, state):
    """A stored checkpoint cannot turn a bounded category batch into another route."""
    from .public_category import category_for_state, MAX_DETAILS
    from .public_job_links import public_detail_parser
    category_for_state(state)
    if 'category_page_retry' in state:
        from .public_category_retry import validate_parent
        validate_parent(collector, state)
    if (type(state.get('schema_version')) is not int or state['schema_version'] != 1 or state.get('status') not in {'paused','running'}
            or state.get('phase') not in {'category','detail','report'}
            or type(state.get('detail_budget')) is not int or not 1 <= state['detail_budget'] <= MAX_DETAILS
            or type(state.get('detail_attempts')) is not int or not 0 <= state['detail_attempts'] <= state['detail_budget']
            or type(state.get('category_attempts')) is not int or not 0 <= state['category_attempts'] <= 1
            or state.get('permit_platforms') != ['liepin'] or not isinstance(state.get('rights_note'), str)
            or not state['rights_note'].strip() or len(state['rights_note']) > 2000):
        raise InputError('保存的分类批次版本、阶段、许可或预算无效，未继续访问。')
    details=state.get('details');source=state['category_outcomes'][0]
    if not isinstance(details,list) or len(details)>MAX_DETAILS or any(not isinstance(d,dict) for d in details):
        raise InputError('保存的分类正文范围无效，未继续访问。')
    if source.get('status') == 'ok':
        candidates=source.get('candidates');positions=source.get('selected_positions')
        if (not isinstance(source.get('raw_sha256'),str) or not re.fullmatch('[a-f0-9]{64}',source['raw_sha256'])
                or not isinstance(candidates,list) or not 1<=len(candidates)<=100
                or any(not isinstance(c,dict) for c in candidates)
                or not isinstance(positions,list) or not 1<=len(positions)<=MAX_DETAILS
                or len(details)!=len(positions) or any(type(p) is not int for p in positions)
                or len(set(positions))!=len(positions)):
            raise InputError('原分类名单或已选范围不完整，未继续访问。')
        for row,position in zip(details,positions):
            matches=[c for c in candidates if c.get('position')==position]
            if (len(matches)!=1 or row.get('category_position')!=position or row.get('platform')!='liepin'
                    or row.get('url')!=matches[0].get('url') or row.get('category_title')!=matches[0].get('title')
                    or (row.get('status')=='pending' and (not isinstance(row.get('url'),str)
                        or not public_detail_parser(row['url']) or row.get('detail_parser')!=public_detail_parser(row['url'])))):
                raise InputError('正文身份与原分类选择不一致，未继续访问。')
    elif details:
        raise InputError('没有成功分类名单，不能执行其中的正文。')
    context=state.get('category_continuation')
    if context is not None:
        if not isinstance(context,dict) or context.get('version')!=1:
            raise InputError('原名单下一批来源无效。')
        # A recovery child inherits the original batch's continuation relation.
        # Validate that recovery chain, then compare the actual original batch
        # with the deterministic continuation, not the newer recovery ID.
        original=state
        from .public_category_recovery import MAX_RECOVERIES, validate_parent
        for _ in range(MAX_RECOVERIES):
            recovery=original.get('category_rate_recovery')
            if recovery is None:break
            validate_parent(collector,original)
            original=collector._load(recovery['parent_id'])
        if 'category_rate_recovery' in original:
            raise InputError('保存的恢复来源链超出原范围。')
        plan=collector.category_next_preview({'id':context.get('parent_id')})
        if (plan['fingerprint']!=context.get('parent_fingerprint') or plan['existing_task_id']!=original['id']):
            raise InputError('原名单来源已变化，未继续访问。')


def claim(root):
    from .collection import writer_lock
    owner = root/'owner'
    if root.is_symlink() or owner.is_symlink():
        raise InputError('采集所有权目录不能使用符号链接。')
    owner.mkdir(exist_ok=True, mode=0o700)
    lease = writer_lock(owner)
    try:
        lease.__enter__()
    except InputError as exc:
        if isinstance(exc.__cause__, OSError):
            raise CollectionBusy('另一个本机采集操作正在执行；可以查看进度，请回到原工作台暂停。') from None
        raise
    return lease


@contextmanager
def operation(collector):
    if getattr(collector._execution, 'owned', False):
        yield
        return
    lease = claim(collector.root)
    try:
        yield
    finally:
        lease.__exit__(None, None, None)


def held(root):
    try:
        lease = claim(root)
    except CollectionBusy:
        return True
    lease.__exit__(None, None, None)
    return False


class CollectionRunner:
    """No scheduler, login, new batch or automatic replay after application exit."""
    def __init__(self, collector):
        self.collector = collector
        self.root = collector.root/'background'
        if self.root.is_symlink():
            raise InputError('后台采集记录不能使用符号链接。')
        self.root.mkdir(exist_ok=True, mode=0o700)
        self.path = self.root/'state.json'
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread = None
        self._closed = False
        self._read()

    def _read(self):
        from .collection import ID
        from .record_lock import record_lock
        with record_lock(self.root):
            if self.root.is_symlink() or self.path.is_symlink():
                raise InputError('后台采集记录不能使用符号链接。')
            if not self.path.exists():
                return dict(version=1, id='', status='idle', code='idle')
            try:
                if self.path.stat().st_size > 8192:
                    raise ValueError
                value = json.loads(self.path.read_text(encoding='utf-8'))
                if (not isinstance(value, dict) or set(value) != {'version','id','status','code'}
                        or type(value['version']) is not int or value['version'] != 1
                        or not isinstance(value['id'], str) or not ID.fullmatch(value['id'])
                        or value['status'] not in {'running','pausing','paused','completed','needs_attention','empty'}
                        or value['code'] not in {'running','user_pause','application_closed','finished',
                                                'conditions_changed','interrupted_uncertain','execution_failed'}):
                    raise ValueError
                return value
            except (ValueError, OSError, TypeError) as exc:
                raise InputError('后台采集记录损坏，未覆盖或启动请求。') from exc

    def _save(self, ident, status, code):
        from .record_lock import record_lock
        with record_lock(self.root):
            if self.root.is_symlink() or self.path.is_symlink():
                raise InputError('后台采集记录不能使用符号链接。')
            atomic_json(self.path, dict(version=1, id=ident, status=status, code=code))

    def is_running(self):
        return bool(self._thread and self._thread.is_alive())

    def state(self, data=None):
        if data not in (None, {}):
            raise InputError('查看后台状态不接受新任务或配置。')
        with self._lock:
            value = self._read()
            local = self.is_running()
            foreign = not local and value['status'] in {'running','pausing'} and held(self.collector.root)
            active = local or foreign
            if not active and value['status'] in {'running','pausing'}:
                value.update(status='paused', code='interrupted_uncertain')
            task = self.collector.status({'id': value['id']}) if value['id'] else None
            return dict(**value, active=active, owned_elsewhere=foreign, task=task)

    def start(self, data):
        from .collection import TERMINAL, writer_lock
        from .public_category import MODE
        if not isinstance(data, dict) or set(data) != {'id','consent'} or data['consent'] is not True:
            raise InputError('请确认在本机完成已选择的当前分类批次。')
        with self._lock:
            if self._closed:
                raise InputError('本工作台已关闭，未启动新请求。')
            previous = self._read()
            if self.is_running():
                if previous['id'] != data['id']:
                    raise CollectionBusy('本机正在完成另一个批次，请先暂停。')
                return self.state()
            lease = claim(self.collector.root)
            try:
                with writer_lock(self.collector.root):
                    state = self.collector._load(data['id'])
                    if state['mode'] != MODE or state['status'] in TERMINAL:
                        raise InputError('后台执行只接受尚未结束的公开分类批次。')
                    validate_scope(self.collector, state)
                    if state.get('in_flight'):
                        raise InputError('上次请求结果未知；请重新打开工作台核对中断记录，不能直接重放。')
                    if state.get('permit_platforms') != ['liepin'] or not state.get('rights_note'):
                        raise InputError('原任务许可不完整，未执行。')
                    policy = self.collector.workspace.network_policy()
                    if policy.error:
                        raise InputError('当前网络设置不可用，未执行。')
                    expected = fingerprint(state)
                    self._save(state['id'], 'running', 'running')
                self._stop.clear()
                self._thread = threading.Thread(target=self._run,
                    args=(state['id'], expected, policy.fingerprint, lease), daemon=True)
                self._thread.start()
            except BaseException:
                lease.__exit__(None, None, None)
                raise
            return self.state()

    def pause(self, data):
        if not isinstance(data, dict) or set(data) != {'id'}:
            raise InputError('暂停只接受当前批次编号。')
        with self._lock:
            state = self._read()
            if state['id'] != data['id']:
                raise InputError('所选批次不是当前后台任务。')
            if self.is_running() and state['status'] in {'running','pausing'}:
                self._stop.set()
                self._save(state['id'], 'pausing', 'user_pause')
            elif not self.is_running() and state['status'] in {'running','pausing'} and held(self.collector.root):
                raise CollectionBusy('该批次由另一个本机服务执行，请回到原服务暂停。')
            return self.state()

    def _run(self, ident, expected, policy_id, lease):
        from .collection import TERMINAL, writer_lock
        execution = self.collector._execution
        execution.owned = True
        execution.expected = (ident, expected)
        execution.policy_id = policy_id
        status, code = 'paused', 'user_pause'
        try:
            # One original small batch has at most one list, five details,
            # the detail-to-report transition and report commit.
            for _ in range(9):
                if self._stop.is_set():
                    break
                task = self.collector.step({'id': ident})
                if task['status'] in TERMINAL:
                    status, code = task['status'], 'finished'
                    break
            else:
                raise InputError('当前批次超过原步骤上限。')
        except InputError:
            code = 'conditions_changed'
        except Exception:
            code = 'execution_failed'
        finally:
            try:
                with writer_lock(self.collector.root):
                    saved = self.collector._load(ident)
                    if (ident, fingerprint(saved)) != execution.expected:
                        code = 'conditions_changed'  # Do not overwrite changed data.
                    elif saved.get('in_flight'):
                        code = 'interrupted_uncertain'  # Original startup recovery owns classification.
                    elif saved['status'] not in TERMINAL:
                        saved['status'] = 'paused'
                        self.collector._save(saved)
                with self._lock:
                    if self._closed and code == 'user_pause':
                        code = 'application_closed'
                    self._save(ident, status, code)
            finally:
                execution.__dict__.clear()
                lease.__exit__(None, None, None)

    def close(self):
        with self._lock:
            self._closed = True
            self._stop.set()
            thread = self._thread
        # If a request still runs, its owner stays held until its real exit.
        # Process exit releases OS ownership; in-flight recovery never replays.
        if thread and thread is not threading.current_thread():
            thread.join(timeout=5)
