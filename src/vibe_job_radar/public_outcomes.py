"""Bounded receipts for completed tasks before the current task is replaced.

The task's existing OS owner lease serializes writes. Readers use SQLite's
transaction snapshot. No query, description, URL, path or credential is copied.
"""
from contextlib import closing
import json
import re
import sqlite3

from .workspace import InputError

RETAINED = 64
FIELDS = {'schema_version','id','attempt','kind','resume_binding','status','report_id','stale'}


def _hex(value, length, *, empty=False):
    return type(value) is str and (empty and value=='' or re.fullmatch('[a-f0-9]{'+str(length)+'}',value) is not None)


def _validate(value):
    if (not isinstance(value,dict) or set(value)!=FIELDS
            or type(value['schema_version']) is not int or value['schema_version']!=1
            or not _hex(value['id'],32) or type(value['attempt']) is not int or not 1<=value['attempt']<2**31
            or value['kind'] not in ('example','search')
            or not _hex(value['resume_binding'],64,empty=True)
            or value['status'] not in ('completed','failed','cancelled')
            or not _hex(value['report_id'],32,empty=value['status']!='completed') or type(value['stale']) is not bool):
        raise ValueError('invalid outcome')
    return value


def _unique(pairs):
    value={}
    for key,item in pairs:
        if key in value:raise ValueError('duplicate field')
        value[key]=item
    return value


class PublicOutcomes:
    def __init__(self,root):
        self.root=root;self.path=root/'outcomes-v1.sqlite'

    def _safe(self):
        if self.root.is_symlink() or self.path.is_symlink():
            raise InputError('公开任务完成回执不能使用符号链接。')
        if self.path.exists() and (not self.path.is_file() or self.path.stat().st_size>1024*1024):
            raise InputError('公开任务完成回执大小或类型异常，请保留记录并检查工作区。')

    def _schema(self,conn,*,create=False):
        version=conn.execute('PRAGMA user_version').fetchone()[0]
        tables={row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if create and version==0 and not tables:
            conn.execute('CREATE TABLE outcomes (seq INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL, attempt INTEGER NOT NULL, payload TEXT NOT NULL, UNIQUE(task_id,attempt))')
            conn.execute('PRAGMA user_version=1')
        elif version!=1 or tables!={'outcomes','sqlite_sequence'}:
            raise ValueError('unsupported outcomes')
        if conn.execute('SELECT COUNT(*) FROM outcomes').fetchone()[0]>RETAINED:
            raise ValueError('unbounded outcomes')

    def remember(self,task):
        if task.get('status') not in ('completed','failed','cancelled'):return
        self._safe()
        try:
            value=_validate(dict(schema_version=1,id=task.get('id'),attempt=task.get('attempt',1),
                kind=task.get('kind'),resume_binding=task.get('resume_binding',''),status=task['status'],
                report_id=task.get('report_id',''),stale=task.get('stale') is True))
            payload=json.dumps(value,sort_keys=True,separators=(',',':'))
            with closing(sqlite3.connect(self.path.resolve().as_uri()+'?mode=rwc',uri=True,timeout=5)) as conn:
                conn.execute('BEGIN IMMEDIATE');self._schema(conn,create=True)
                old=conn.execute('SELECT payload FROM outcomes WHERE task_id=? AND attempt=?',(value['id'],value['attempt'])).fetchone()
                if old is not None:
                    if old[0]!=payload:raise ValueError('outcome changed')
                else:
                    conn.execute('INSERT INTO outcomes(task_id,attempt,payload) VALUES(?,?,?)',(value['id'],value['attempt'],payload))
                    conn.execute('DELETE FROM outcomes WHERE seq NOT IN (SELECT seq FROM outcomes ORDER BY seq DESC LIMIT ?)',(RETAINED,))
                conn.commit()
        except (ValueError,TypeError,RecursionError,sqlite3.Error,OSError):
            raise InputError('无法保存上一公开任务的完成回执；未开始新查询，请保留原任务和报告并检查工作区。') from None

    def get(self,task_id,attempt=1):
        self._safe()
        if not _hex(task_id,32) or type(attempt) is not int or not 1<=attempt<2**31:
            raise InputError('公开任务完成回执身份无效。')
        if not self.path.exists():return None
        try:
            with closing(sqlite3.connect(self.path.resolve().as_uri()+'?mode=ro',uri=True,timeout=5)) as conn:
                conn.execute('BEGIN');self._schema(conn)
                row=conn.execute('SELECT payload FROM outcomes WHERE task_id=? AND attempt=?',(task_id,attempt)).fetchone()
                if row is None:return None
                if type(row[0]) is not str or len(row[0])>2048:raise ValueError('invalid outcome')
                value=_validate(json.loads(row[0],object_pairs_hook=_unique))
                if (value['id'],value['attempt'])!=(task_id,attempt):raise ValueError('mismatched outcome')
                return value
        except (ValueError,TypeError,RecursionError,sqlite3.Error,OSError):
            raise InputError('公开任务完成回执损坏或版本不兼容，未推断原任务成功；请保留记录并检查工作区。') from None
