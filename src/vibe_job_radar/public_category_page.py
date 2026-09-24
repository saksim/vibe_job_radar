"""Explicit adjacent-page children, bound to immutable verified parent evidence."""
from __future__ import annotations

import copy
import hashlib

from .public_category import (MODE, MAX_CATEGORY_PAGES, PAGINATION_PARSER,
                              category_for_state, category_page_url, new_outcome, page_for_state)
from .public_category_next import _fields, _snapshot
from .utils import utc_now
from .workspace import InputError


def validate_parent(collector, state):
    page = page_for_state(state)
    if not page:
        return
    context = state['category_page_context']
    parent = collector._load(context['parent_id'])
    # The strictly decreasing page index bounds ancestry checks and rejects cycles.
    if page_for_state(parent) != page - 1:
        raise InputError('相邻页的来源页号已变化或形成循环。')
    _, plan, _, _ = _plan(collector, parent['id'])
    if not plan['can_start'] or context != plan['context']:
        raise InputError('来源任务、列表或分页关系已变化，请保留记录核对；未执行。')


def _plan(collector, ident):
    state, saved, _, _ = _snapshot(collector, ident)
    category = category_for_state(state)
    page = page_for_state(state)
    source = state['category_outcomes'][0]
    remaining = sum(c['position'] > source['selected_positions'][-1]
                    and c['status'] not in {'duplicate','previous_page_duplicate'} for c in source['candidates'])
    plan = dict(id=state['id'], fingerprint=saved['fingerprint'], category_id=category.key,
        category_label=category.label, current_page=page+1, next_page=page+2,
        source_url=source['url'], snapshot_sha256=source['raw_sha256'],
        rights_note=state['rights_note'],
        remaining_on_current_page=remaining,
        selection_limit=state['detail_budget'], can_start=False, next_url='', existing_task_id='',
        external_network_requests=0, task_created=False, context=None)
    paging = source.get('page_snapshot')
    if paging is None:
        plan.update(code='legacy_no_pagination', notice='这份旧名单没有保存分页证据，可继续本页名单；请新建公开分类任务以观察实际分页链接。')
        return state, plan, '', None
    if (not isinstance(paging, dict) or set(paging) != {'parser','page','url','status','next_url'}
            or paging['parser'] != PAGINATION_PARSER or type(paging['page']) is not int or paging['page'] != page
            or paging['url'] != source['url'] or paging['status'] not in {'available','terminal','unavailable','limit'}
            or paging['status'] != 'available' and paging['next_url'] != ''):
        raise InputError('保存的分页证据无效，不能使用其中的地址。')
    if page + 1 == MAX_CATEGORY_PAGES:
        plan.update(code='page_limit', notice='本次前5页范围已用完；不是网站末页或全市场完成。')
        return state, plan, '', None
    if paging['status'] != 'available':
        code = 'publisher_terminal' if paging['status'] == 'terminal' else 'pagination_unavailable'
        plan.update(code=code, notice=('平台分页控件明确标记没有下一页；不代表全市场覆盖。' if code == 'publisher_terminal'
                                      else '未观察到可验证的相邻页链接，不能推断或构造地址。'))
        return state, plan, '', None
    if paging['next_url'] != category_page_url(category, page+1):
        raise InputError('保存的链接不是同一公开分类的紧邻页。')
    prior = state.get('category_page_context', {})
    seen = list(dict.fromkeys(prior.get('seen_urls', []) + [c['url'] for c in source['candidates'] if c['url']]))
    context = dict(version=1, page=page+1, url=paging['next_url'], parent_id=state['id'],
        parent_fingerprint=saved['fingerprint'], seen_urls=seen,
        visited_urls=prior.get('visited_urls', []) + [source['url']])
    # Apply the same bounds before creating any task, including all-invalid lists.
    page_for_state(dict(category_id=category.key, category_page_context=context))
    child_id = hashlib.sha256(('category-page-v1:'+state['id']).encode()).hexdigest()[:32]
    existing = collector._load(child_id) if collector._path(child_id).exists() else None
    if existing is not None and (existing.get('mode') != MODE or existing.get('category_page_context') != context):
        raise InputError('既有下一页与当前来源不一致，不覆盖任务或重放访问。')
    plan.update(code='available', can_start=True, next_url=paging['next_url'], context=context,
        existing_task_id=child_id if existing else '',
        notice=(f'本页还有{remaining}项未选择，原名单保留，可从历史任务继续。' if remaining else '')
        +f'将读取平台明确给出的第{page+2}页，再选取最多{state["detail_budget"]}个未在前页出现的主列表职位；共享限额和原失败记录保持。')
    return state, plan, child_id, existing


def preview(collector, data):
    _fields(data, {'id'})
    plan = _plan(collector, data.get('id'))[1]
    return {key:value for key,value in plan.items() if key != 'context'}


def start(collector, data):
    _fields(data, {'id','fingerprint','consent'})
    if data.get('consent') is not True:
        raise InputError('请确认相邻页地址、最多5条正文及本次访问范围。')
    parent, plan, child_id, existing = _plan(collector, data.get('id'))
    if data.get('fingerprint') != plan['fingerprint']:
        raise InputError('来源任务已变化，请重新预览；尚未访问下一页。')
    if not plan['can_start']:
        raise InputError(plan['notice'])
    if existing is not None:
        return dict(created=False, task=collector._view(existing))
    child = copy.deepcopy(parent)
    child.update(id=child_id, status='paused', phase='category', details=[], report_id='',
        category_attempts=0, detail_attempts=0, search_requests=0, feed_requests=0, warnings=[],
        created_at=utc_now(), updated_at=utc_now(), category_page_context=plan['context'],
        category_outcomes=[new_outcome(plan['category_id'], page=plan['next_page']-1)])
    child.pop('category_continuation', None)
    collector._save(child)
    return dict(created=True, task=collector._view(child))
