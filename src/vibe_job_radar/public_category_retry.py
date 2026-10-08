"""Explicit same-page retry after a transport failure, retaining every attempt."""
from __future__ import annotations

import copy
import hashlib
import re

from .public_category import MODE, MAX_DETAILS, category_for_state, new_outcome, page_for_state
from .public_category_next import _fields, _fingerprint
from .utils import utc_now
from .workspace import InputError

MAX_RETRIES = 3
CONTEXT = 'category_page_retry'
RUNTIME = {'status','phase','created_at','updated_at','report_id','in_flight',
           'category_attempts','detail_attempts','blocked_hosts','warnings','details','category_outcomes'}


def _generation(state):
    context = state.get(CONTEXT)
    if CONTEXT not in state:
        return 0
    if (not isinstance(context, dict) or set(context) != {'version','generation','parent_id','parent_fingerprint'}
            or type(context['version']) is not int or context['version'] != 1
            or type(context['generation']) is not int or not 1 <= context['generation'] <= MAX_RETRIES
            or not isinstance(context['parent_id'], str) or not re.fullmatch('[a-f0-9]{32}', context['parent_id'])
            or not isinstance(context['parent_fingerprint'], str) or not re.fullmatch('[a-f0-9]{64}', context['parent_fingerprint'])):
        raise InputError('保存的同页重试来源或次数无效，未访问网站。')
    return context['generation']


def _parent(collector, ident):
    parent = collector._load(ident)
    if (parent.get('id') != ident or type(parent.get('schema_version')) is not int or parent['schema_version'] != 1
            or parent.get('mode') != MODE or parent.get('status') != 'needs_attention' or parent.get('phase') != 'report'
            or parent.get('in_flight') or parent.get('details') != [] or type(parent.get('detail_attempts')) is not int or parent['detail_attempts'] != 0
            or type(parent.get('category_attempts')) is not int or parent['category_attempts'] != 1
            or parent.get('report_id') != '' or parent.get('blocked_hosts') != []
            or parent.get('permit_platforms') != ['liepin'] or not isinstance(parent.get('rights_note'), str)
            or not parent['rights_note'].strip() or len(parent['rights_note']) > 2000
            or type(parent.get('detail_budget')) is not int or not 1 <= parent['detail_budget'] <= MAX_DETAILS
            or 'category_continuation' in parent or 'category_rate_recovery' in parent):
        raise InputError('仅支持尚未取得名单或正文、已结束的分类页网络错误；原结果保持。')
    category_for_state(parent)
    source = parent['category_outcomes'][0]
    if (source.get('status') != 'network_error' or source.get('candidates') != []
            or any(key in source for key in ('raw_sha256','final_url','page_snapshot','selected_positions','snapshot_reused'))):
        raise InputError('仅可重试未取得页面的网络错误；拒绝、登录、证书和未知中断不能从此入口继续。')
    validate_parent(collector, parent)
    if 'category_page_context' in parent:
        from .public_category_page import validate_parent as validate_page
        validate_page(collector, parent)
    return parent


def _template(parent):
    category = category_for_state(parent)
    child = copy.deepcopy(parent)
    child.update(id=hashlib.sha256(('category-page-retry-v1:'+parent['id']).encode()).hexdigest()[:32],
        status='paused', phase='category', created_at=utc_now(), updated_at=utc_now(), report_id='',
        category_attempts=0, detail_attempts=0, blocked_hosts=[], warnings=[], details=[],
        category_outcomes=[new_outcome(category.key, page=page_for_state(parent))],
        category_page_retry=dict(version=1, generation=_generation(parent)+1, parent_id=parent['id'],
                                 parent_fingerprint=_fingerprint(parent)))
    child.pop('in_flight', None)
    return child


def _fixed(state):
    return {key:value for key,value in state.items() if key not in RUNTIME}


def validate_parent(collector, state):
    generation = _generation(state)
    if not generation:
        return
    context = state[CONTEXT]
    previous = collector._load(context['parent_id'])
    # Decrease before recursion: even a cyclic or substituted checkpoint cannot
    # turn the bounded three-attempt history into an unbounded traversal.
    if _generation(previous) != generation-1 or _fingerprint(previous) != context['parent_fingerprint']:
        raise InputError('原失败任务已变化或重试来源形成循环，未继续访问。')
    parent = _parent(collector, previous['id'])
    if _fixed(state) != _fixed(_template(parent)):
        raise InputError('同页重试的类别、页号、许可或原预算已变化，未继续访问。')
    category_for_state(state)


def _plan(collector, ident):
    parent = _parent(collector, ident)
    generation = _generation(parent)+1
    if generation > MAX_RETRIES:
        raise InputError('该页已达到3次显式网络重试上限；请保留原失败并核对网络原因。')
    child = _template(parent)
    existing = collector._load(child['id']) if collector._path(child['id']).exists() else None
    if existing is not None:
        validate_parent(collector, existing)
        if _fixed(existing) != _fixed(child):
            raise InputError('既有同页重试与原失败不一致，不覆盖任务或重新访问。')
    plan = dict(id=ident, fingerprint=_fingerprint(parent), category_id=parent.get('category_id','architect'),
        page=page_for_state(parent)+1, url=parent['category_outcomes'][0]['url'], prior_status='network_error',
        generation=generation, maximum_retries=MAX_RETRIES, selection_limit=parent['detail_budget'],
        rights_note=parent['rights_note'], existing_task_id=existing['id'] if existing else '',
        external_network_requests=0, task_created=False,
        notice='只重试这一个已确认的分类页；原失败和已消耗额度保留。保存后暂停，执行时仍检查原网络、robots与共享限额。')
    return plan, child, existing


def preview(collector, data):
    _fields(data, {'id'})
    return _plan(collector, data.get('id'))[0]


def start(collector, data):
    _fields(data, {'id','fingerprint','consent'})
    if data.get('consent') is not True:
        raise InputError('请核对原失败、同页地址及最多5条正文范围，再确认保存重试。')
    plan, child, existing = _plan(collector, data.get('id'))
    if data.get('fingerprint') != plan['fingerprint']:
        raise InputError('原失败已变化，请重新预览；未保存或访问。')
    if existing is not None:
        return dict(created=False, task=collector._view(existing))
    collector._save(child)
    return dict(created=True, task=collector._view(child))
