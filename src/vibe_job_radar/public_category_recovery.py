"""Explicit bounded recovery of rate-stopped details in a saved category batch."""
from __future__ import annotations

import copy
from contextlib import closing
import hashlib
import json
import sqlite3

from .public_category import MODE
from .public_category_next import HARD_STOP, _fields, _fingerprint, _snapshot
from .public_job_links import public_detail_parser
from .utils import utc_now
from .workspace import InputError

WAITING = frozenset({'rate_wait', 'publisher_wait', 'hourly_limit', 'daily_limit', 'cooldown'})
MAX_RECOVERIES = 3
RUNTIME = {'status', 'phase', 'created_at', 'updated_at', 'report_id', 'in_flight',
           'detail_attempts', 'blocked_hosts', 'warnings'}
DETAIL_RUNTIME = {'status', 'record_id', 'final_url', 'fetch_diagnostic', 'retry_after_seconds'}


def waiting_positions(state):
    """Only a witnessed rate stop can account for subsequent host-stopped rows."""
    if (state.get('schema_version') != 1 or state.get('status') != 'needs_attention'
            or state.get('mode') != MODE or state.get('blocked_hosts') != ['www.liepin.com']):
        raise InputError('仅支持已有分类名单中因共享等待而停止的正文；其他原因须先核对。')
    positions, seen_wait = [], False
    for index, row in enumerate(state.get('details', [])):
        status = row.get('status')
        if status in WAITING:
            seen_wait = True
        elif status == 'host_stopped':
            if not seen_wait:
                raise InputError('无法确认同站未执行项由前面的等待造成。')
        elif status in HARD_STOP:
            raise InputError('原批次含访问拒绝、验证、证书、账本或未知中断，不能从此入口恢复。')
        else:
            continue
        if (row.get('platform') != 'liepin' or not isinstance(row.get('url'), str) or not public_detail_parser(row['url'])
                or row.get('detail_parser') != public_detail_parser(row['url'])):
            raise InputError('保存的正文身份或解析器版本无法确认，请保留旧记录核对。')
        positions.append(index)
    if not seen_wait or not 1 <= len(positions) <= 5:
        raise InputError('没有可恢复的共享等待项，或超出原小批次范围。')
    return positions


def _generation(state):
    context = state.get('category_rate_recovery')
    if context is None:
        return 0
    if (not isinstance(context, dict) or set(context) != {'version','generation','parent_id','parent_fingerprint','indices','inherited_success_count'}
            or type(context['version']) is not int or context['version'] != 1 or type(context['generation']) is not int
            or not 1 <= context['generation'] <= MAX_RECOVERIES):
        raise InputError('恢复检查点版本或次数无效，未访问网站。')
    return context['generation']


def _template(parent, indices, ident):
    child = copy.deepcopy(parent)
    child.update(id=ident, status='paused', phase='detail', report_id='', category_attempts=0,
        detail_attempts=0, detail_budget=len(indices), blocked_hosts=[], warnings=[],
        created_at=utc_now(), updated_at=utc_now(), category_rate_recovery=dict(
            version=1, generation=_generation(parent)+1, parent_id=parent['id'],
            parent_fingerprint=_fingerprint(parent), indices=indices,
            inherited_success_count=sum(d['status'] in {'ok','fresh_reused'} for d in parent['details'])))
    for index in indices:
        row = child['details'][index]
        for key in DETAIL_RUNTIME:
            row.pop(key, None)
        row.update(status='pending', record_id='')
    source = child['category_outcomes'][0]
    source.update(snapshot_reused=True, source_collection_id=source.get('source_collection_id', parent['id']),
                  source_task_created_at=source.get('source_task_created_at', parent['created_at']))
    if 'fetch_diagnostic' in source:
        source['source_fetch_diagnostic'] = source.pop('fetch_diagnostic')
    return child


def _fixed(state, indices):
    value = copy.deepcopy(state)
    for key in RUNTIME:
        value.pop(key, None)
    for index in indices:
        for key in DETAIL_RUNTIME:
            value['details'][index].pop(key, None)
    return value


def _preserved_records(collector, parent):
    rows = [row for row in parent['details'] if row['status'] in {'ok','fresh_reused'}]
    if not rows:
        return
    from .models import JobRecord
    try:
        # Read-only: preview cannot create a missing store or silently omit an
        # inherited success if its original immutable body is no longer present.
        path = collector.workspace.db
        if path.is_symlink():
            raise ValueError('unsafe store')
        with closing(sqlite3.connect(path.resolve().as_uri()+'?mode=ro', uri=True)) as db:
            for row in rows:
                found = db.execute('SELECT body FROM records WHERE record_id=?', (row['record_id'],)).fetchone()
                record = JobRecord.from_dict(json.loads(found[0])) if found else None
                if (record is None or record.record_id != row['record_id'] or record.is_synthetic
                        or record.platform != 'liepin' or record.evidence_level != 'full_text'
                        or record.url != row.get('final_url', row['url'])
                        or ' '.join(record.title.split()) != row['category_title']):
                    raise ValueError('inherited record changed')
    except (OSError, sqlite3.Error, ValueError, TypeError, KeyError) as exc:
        raise InputError('原成功正文缺失、内容变化或无法读取；保留任务，先恢复原数据再继续。') from exc


def validate_parent(collector, state):
    generation = _generation(state)
    if not generation:
        return
    context = state['category_rate_recovery']
    parent = collector._load(context['parent_id'])
    # Check the decreasing generation before following ancestry or snapshots.
    if _generation(parent) != generation-1 or _fingerprint(parent) != context['parent_fingerprint']:
        raise InputError('恢复来源已变化或形成循环，未继续访问；请保留记录核对。')
    _, plan, ident, _ = _plan(collector, parent['id'], inspect_existing=False)
    if (state.get('phase') not in {'detail','report'} or not isinstance(state.get('details'), list)
            or len(state['details']) != len(parent['details']) or any(not isinstance(d,dict) for d in state['details'])):
        raise InputError('恢复任务的阶段或逐项记录不完整，未访问网站。')
    if _fixed(state, plan['indices']) != _fixed(
            _template(parent, plan['indices'], ident), plan['indices']):
        raise InputError('恢复任务的职位、许可、预算或名单已变化，未继续访问。')


def _plan(collector, ident, *, inspect_existing=True):
    parent, saved, _, _ = _snapshot(collector, ident, rate_recovery=True)
    if _generation(parent) >= MAX_RECOVERIES:
        raise InputError('该原批次已达到3次显式恢复上限；保留结果并核对等待原因。')
    indices = waiting_positions(parent)
    _preserved_records(collector, parent)
    child_id = hashlib.sha256(('category-rate-v1:'+parent['id']).encode()).hexdigest()[:32]
    plan = dict(id=parent['id'], fingerprint=saved['fingerprint'], indices=indices,
        items=[dict(position=parent['details'][i]['category_position'], title=parent['details'][i]['category_title'],
                    url=parent['details'][i]['url'], prior_status=parent['details'][i]['status']) for i in indices],
        rights_note=parent['rights_note'], selection_limit=len(indices), existing_task_id='',
        inherited_success_count=sum(d['status'] in {'ok','fresh_reused'} for d in parent['details']),
        external_network_requests=0, task_created=False,
        notice='只恢复本批因共享等待而未完成的正文；保留已成功和其他失败。保存后暂不联网，执行时仍检查原限额与发布方规则。')
    existing = collector._load(child_id) if inspect_existing and collector._path(child_id).exists() else None
    if existing is not None:
        validate_parent(collector, existing)
        plan['existing_task_id'] = child_id
    return parent, plan, child_id, existing


def preview(collector, data):
    _fields(data, {'id'})
    return _plan(collector, data.get('id'))[1]


def start(collector, data):
    _fields(data, {'id','fingerprint','consent'})
    if data.get('consent') is not True:
        raise InputError('请核对原失败、剩余具体岗位与本次预算，再确认保存恢复任务。')
    parent, plan, ident, existing = _plan(collector, data.get('id'))
    if data.get('fingerprint') != plan['fingerprint']:
        raise InputError('原任务已变化，请重新预览；未创建或访问。')
    if existing is not None:
        return dict(created=False, task=collector._view(existing))
    child = _template(parent, plan['indices'], ident)
    collector._save(child)
    return dict(created=True, task=collector._view(child))
