"""One explicitly accepted report operation, independent of an HTTP request."""
from __future__ import annotations

import copy
import json
import re
import threading
import uuid

from .collection import writer_lock
from .record_lock import record_lock
from .utils import atomic_json
from .workspace import InputError


ID = re.compile(r'[a-f0-9]{32}')
ERRORS = {
    'input_changed': '报告输入或证据版本已变化；请核对原报告与输入后重新确认。',
    'execution_failed': '报告生成失败；请检查输入、工作区权限和原报告，已有内容未清空。',
    'interrupted': '上次报告结果未确认；请先查看历史报告，不会自动重新生成。',
    'storage_error': '报告任务结果未能保存；请先核对历史报告和工作区，未自动重新生成。',
    'start_failed': '报告线程未能启动；未自动重新生成，请核对任务状态。',
}


class ReportTaskBusy(InputError):
    """No second report or overlapping application mutation was accepted."""


class ReportTasks:
    """A small last-task receipt, not a scheduler or a durable replay queue."""

    def __init__(self, application):
        self.application = application
        self.root = application.workspace.root / 'report_tasks'
        self.path = self.root / 'state.json'
        self.owner = self.root / 'owner'
        self._lock = threading.RLock()
        self._thread = None
        self._closed = False
        self._uncertain = None
        self._safe()
        self.root.mkdir(exist_ok=True, mode=0o700)
        self._read()

    def _safe(self):
        if self.root.is_symlink() or self.path.is_symlink() or self.owner.is_symlink():
            raise InputError('报告任务目录、记录或所有权不能使用符号链接。')

    @staticmethod
    def _validate(value):
        if not isinstance(value, dict) or set(value) != {'version', 'task'} or type(value['version']) is not int or value['version'] != 1:
            raise ValueError('invalid report receipt')
        task = value['task']
        fields = {'id', 'kind', 'status', 'report_id', 'source_run_id', 'revision', 'error'}
        if (not isinstance(task, dict) or set(task) != fields
                or not isinstance(task['id'], str) or not ID.fullmatch(task['id'])
                or task['kind'] not in {'analysis', 'evidence'}
                or task['status'] not in {'running', 'succeeded', 'failed'}
                or not isinstance(task['report_id'], str)
                or task['report_id'] and not ID.fullmatch(task['report_id'])
                or not isinstance(task['source_run_id'], str)):
            raise ValueError('invalid report task')
        if task['kind'] == 'evidence':
            if not ID.fullmatch(task['source_run_id']) or type(task['revision']) is not int or task['revision'] < 0:
                raise ValueError('invalid evidence receipt')
        elif task['source_run_id'] or task['revision'] is not None:
            raise ValueError('unexpected evidence metadata')
        error = task['error']
        if task['status'] == 'succeeded':
            if not task['report_id'] or error is not None:
                raise ValueError('missing successful report')
        elif task['status'] == 'running':
            if task['report_id'] or error is not None:
                raise ValueError('premature report result')
        elif (not isinstance(error, dict) or set(error) != {'code', 'message'}
                or error.get('code') not in ERRORS or error.get('message') != ERRORS[error['code']]
                or task['report_id']):
            raise ValueError('invalid safe error')
        return value

    def _read(self):
        with record_lock(self.root):
            self._safe()
            if not self.path.exists():
                return {'version': 1, 'task': None}
            try:
                if not self.path.is_file() or self.path.stat().st_size > 8192:
                    raise ValueError('oversized receipt')
                return self._validate(json.loads(self.path.read_text(encoding='utf-8')))
            except (ValueError, TypeError, KeyError, OSError, RecursionError):
                raise InputError('报告任务记录损坏或版本不兼容；未覆盖记录或重放任务。') from None

    def _save(self, task):
        value = self._validate({'version': 1, 'task': task})
        with record_lock(self.root):
            self._safe()
            atomic_json(self.path, value)

    def _claim(self):
        self._safe()
        self.owner.mkdir(exist_ok=True, mode=0o700)
        lease = writer_lock(self.owner)
        try:
            lease.__enter__()
        except InputError as exc:
            if isinstance(exc.__cause__, OSError):
                raise ReportTaskBusy('另一个本机工作台正在生成报告；此处可查看进度，请等待其完成。') from None
            raise
        return lease

    def _local_running(self):
        return bool(self._thread and self._thread.is_alive())

    def _foreign_running(self):
        try:
            lease = self._claim()
        except ReportTaskBusy:
            return True
        lease.__exit__(None, None, None)
        return False

    def is_running(self):
        with self._lock:
            return self._local_running() or self._foreign_running()

    @staticmethod
    def _error(code):
        return {'code': code, 'message': ERRORS[code]}

    def state(self, data=None):
        if data not in (None, {}):
            raise InputError('查看报告任务不接受新的输入。')
        with self._lock:
            # A failed terminal write is kept in memory only as uncertain. It
            # never authorizes replay or replaces the durable running receipt.
            value = self._read()
            if self._uncertain:
                if value['task'] and value['task']['id'] == self._uncertain['task']['id']:
                    value = copy.deepcopy(self._uncertain)
                else:
                    # Another owner may have accepted a later task. Its receipt
                    # supersedes uncertainty about this instance's old task.
                    self._uncertain = None
            local = self._local_running()
            foreign = not local and self._foreign_running()
            busy = local or foreign
            task = value['task']
            if task and task['status'] == 'running' and not busy:
                task.update(status='interrupted', error=self._error('interrupted'))
            return {'task': task, 'busy': busy, 'owned_elsewhere': foreign}

    def _input(self, data):
        if (not isinstance(data, dict) or not isinstance(data.get('kind'), str)
                or data['kind'] not in {'analysis', 'evidence'}):
            raise InputError('请选择普通分析或个人证据报告。')
        kind = data['kind']
        if kind == 'analysis':
            if set(data) - {'kind', 'roles', 'platforms', 'dataset'}:
                raise InputError('报告任务含有不支持的字段。')
            roles, platforms = self.application.workspace.filters(data)
            dataset = data.get('dataset', 'real')
            if not isinstance(dataset, str) or dataset not in {'real', 'demo'}:
                raise InputError('dataset 必须是 real 或 demo。')
            if dataset == 'real' and not self.application.workspace.db.is_file():
                raise InputError('还没有真实数据，请先粘贴 JD、导入文件或执行搜索发现。')
            return kind, {'roles': roles, 'platforms': platforms, 'dataset': dataset}
        if (set(data) != {'kind', 'run_id', 'expected_revision'}
                or not isinstance(data['run_id'], str) or not ID.fullmatch(data['run_id'])
                or type(data['expected_revision']) is not int or data['expected_revision'] < 0):
            raise InputError('请核对来源报告和个人证据版本。')
        if data['expected_revision'] != self.application.evidence.state()['revision']:
            raise InputError('证据版本已变化，请刷新后生成报告。')
        self.application.workspace.report_file(data['run_id'], 'run_manifest.json')
        return kind, {'run_id': data['run_id'], 'expected_revision': data['expected_revision']}

    def start(self, data):
        mutation = self.application.mutation_lock
        if not mutation.acquire(blocking=False):
            raise ReportTaskBusy('已有操作执行中，请勿重复提交报告。')
        lease = None
        transferred = False
        task = None
        try:
            with self._lock:
                if self._closed:
                    raise InputError('本工作台已关闭，未开始报告。')
                if self._local_running():
                    raise ReportTaskBusy('本工作台正在生成报告，请等待完成。')
                lease = self._claim()
                self._read()  # Never replace an invalid old receipt.
                kind, payload = self._input(data)
                payload = copy.deepcopy(payload)
                task = {'id': uuid.uuid4().hex, 'kind': kind, 'status': 'running', 'report_id': '',
                        'source_run_id': payload.get('run_id', ''),
                        'revision': payload.get('expected_revision'), 'error': None}
                self._save(task)
                self._uncertain = None
                thread = threading.Thread(target=self._run, args=(task, payload, lease, mutation),
                                          daemon=True, name='radar-report')
                self._thread = thread
                try:
                    thread.start()
                except BaseException:
                    self._thread = None
                    failed = dict(task, status='failed', error=self._error('start_failed'))
                    try:
                        self._save(failed)
                    except Exception:
                        self._uncertain = {'version': 1, 'task': dict(task, status='interrupted', error=self._error('storage_error'))}
                    raise InputError(ERRORS['start_failed']) from None
                transferred = True
                # Return an acceptance receipt directly. Another state read
                # cannot turn an accepted task into an HTTP start failure.
                return {'task': copy.deepcopy(task), 'busy': True, 'owned_elsewhere': False}
        finally:
            if not transferred:
                try:
                    if lease is not None:
                        lease.__exit__(None, None, None)
                finally:
                    mutation.release()

    def _run(self, task, payload, lease, mutation):
        result = dict(task)
        try:
            if task['kind'] == 'analysis':
                report = self.application.workspace.analyze(payload)
            else:
                # The original service rechecks revision and frozen semantics.
                report = self.application.evidence.generate(payload)
            ident = report.get('id') if isinstance(report, dict) else None
            if not isinstance(ident, str) or not ID.fullmatch(ident):
                raise ValueError('invalid report result')
            result.update(status='succeeded', report_id=ident)
        except InputError:
            result.update(status='failed', error=self._error('input_changed'))
        except BaseException:
            result.update(status='failed', error=self._error('execution_failed'))
        finally:
            payload.clear()
            try:
                with self._lock:
                    try:
                        self._save(result)
                    except Exception:
                        # The original report may already have been published.
                        # Keep its ID if known, do not delete it or claim success.
                        result.update(status='interrupted', error=self._error('storage_error'))
                        self._uncertain = {'version': 1, 'task': result}
            finally:
                try:
                    lease.__exit__(None, None, None)
                finally:
                    mutation.release()

    def close(self):
        with self._lock:
            self._closed = True
            thread = self._thread
        if thread and thread is not threading.current_thread():
            thread.join(timeout=5)
        # A live worker still owns both locks. Only its actual exit releases
        # them; process termination leaves a receipt requiring human review.
