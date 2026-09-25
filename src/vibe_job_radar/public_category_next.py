"""Explicit next batches from one saved main-list snapshot, never site pagination."""
from __future__ import annotations

import copy
import hashlib
import json
import re

from .public_category import MODE, MAX_DETAILS, category_for_state
from .public_job_links import public_detail_parser
from .utils import utc_now
from .workspace import InputError

HARD_STOP = frozenset({'http_401', 'http_403', 'http_429', 'robots_denied', 'robots_unavailable',
    'host_stopped', 'host_circuit_open', 'login_or_challenge', 'redirect_login_required',
    'redirect_verification_required', 'manual_required', 'tls_verification_failed',
    'tls_handshake_failed', 'interrupted_uncertain', 'cooldown', 'rate_wait', 'publisher_wait',
    'hourly_limit', 'daily_limit', 'clock_rollback', 'rate_storage_error', 'publisher_policy_invalid', 'unsafe_workspace'})


def _fields(data, allowed):
    if not isinstance(data, dict) or set(data) - allowed:
        raise InputError('名单续取只接受来源任务编号、预览指纹和执行确认，不接收新链接或密钥。')


def _fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode()).hexdigest()


def _snapshot(collector, ident, *, rate_recovery=False):
    state = collector._load(ident)
    if state.get('id') != ident:
        raise InputError('来源任务编号与保存记录不一致。')
    if (state.get('mode') != MODE or state.get('in_flight')
            or state.get('status') not in {'completed', 'needs_attention', 'empty'}):
        raise InputError('请先让公开分类的当前批次结束；其他来源或仍在运行的任务不能续取名单。')
    category = category_for_state(state)
    if 'category_page_retry' in state:
        from .public_category_retry import validate_parent
        validate_parent(collector, state)
    if 'category_rate_recovery' in state:
        from .public_category_recovery import validate_parent
        validate_parent(collector, state)
    if 'category_page_context' in state:
        from .public_category_page import validate_parent
        validate_parent(collector, state)
    if (state.get('permit_platforms') != ['liepin'] or not state.get('rights_note')):
        raise InputError('原分类任务的岗位、来源或许可范围不完整，不能自动扩大范围。')
    details = state.get('details')
    if not isinstance(details, list) or any(not isinstance(row, dict) for row in details):
        raise InputError('原批次的逐条结果不完整。')
    if rate_recovery:
        from .public_category_recovery import waiting_positions
        waiting_positions(state)
    elif (state.get('blocked_hosts') or any(row.get('status') in HARD_STOP for row in details)):
        raise InputError('原批次有访问拒绝、登录验证、证书或未知中断；先处理原因，不能用下一批继续访问该站。')
    outcomes = state.get('category_outcomes', [])
    if not isinstance(outcomes, list) or len(outcomes) != 1 or not isinstance(outcomes[0], dict):
        raise InputError('没有唯一已确认的分类名单。')
    source = outcomes[0]
    if (source.get('status') != 'ok'
            or not isinstance(source.get('raw_sha256'), str)
            or not re.fullmatch('[a-f0-9]{64}', source['raw_sha256'])):
        raise InputError('分类快照未成功读取或缺少来源摘要，不能续取。')
    candidates = source.get('candidates')
    if not isinstance(candidates, list) or not 1 <= len(candidates) <= 100:
        raise InputError('已保存分类名单不完整。')
    unique, seen = [], {}
    previous_urls = set(state.get('category_page_context', {}).get('seen_urls', []))
    for position, row in enumerate(candidates, 1):
        if not isinstance(row, dict) or type(row.get('position')) is not int or row['position'] != position:
            raise InputError('分类名单顺序不一致。')
        status, url, title = row.get('status'), row.get('url'), row.get('title')
        if status == 'category_invalid_card':
            if url != '' or title != '':
                raise InputError('无效卡片包含未核实的地址或标题。')
            unique.append(row)
            continue
        if (not isinstance(url, str) or not re.fullmatch(r'https://www\.liepin\.com/(?:job|a)/[0-9]{1,80}\.shtml', url)
                or not isinstance(title, str) or not title.strip() or len(title) > 200):
            raise InputError('分类名单含未核实的详情地址或标题。')
        if url in seen:
            if status != 'duplicate' or row.get('duplicate_of') != seen[url]:
                raise InputError('分类名单的重复项记录不一致。')
            continue
        if status == 'previous_page_duplicate':
            if url not in previous_urls:
                raise InputError('跨页重复项不在此前已验证的岗位集合中。')
            seen[url] = position
            continue
        if url in previous_urls:
            raise InputError('前页已出现的岗位没有标记为跨页重复。')
        if status not in {'available', 'category_unsupported_detail'}:
            raise InputError('分类名单含未知卡片状态。')
        seen[url] = position
        unique.append(row)
    selected = source.get('selected_positions')
    positions = [row['position'] for row in unique]
    if (not isinstance(selected, list) or not 1 <= len(selected) <= MAX_DETAILS
            or any(type(pos) is not int or pos not in positions for pos in selected)):
        raise InputError('原批次的已选范围无法确认。')
    start = positions.index(selected[0])
    if positions[start:start+len(selected)] != selected:
        raise InputError('原批次不是连续的名单范围。')
    details = state.get('details', [])
    if (len(details) != len(selected) or [d.get('category_position') for d in details] != selected
            or any(d.get('status') in {'pending', 'requesting'} for d in details)):
        raise InputError('原批次还有未确认的选择项，不能跳过继续。')
    for detail, candidate in zip(details, unique[start:start+len(selected)]):
        if detail.get('url') != candidate['url'] or detail.get('category_title') != candidate['title']:
            raise InputError('原批次与分类名单的职位身份不一致。')
    budget = state.get('detail_budget')
    if type(budget) is not int or not 1 <= budget <= MAX_DETAILS:
        raise InputError('原分类任务的批次预算无效。')
    upcoming = unique[start+len(selected):start+len(selected)+budget]
    fingerprint = _fingerprint(state)
    child_id = hashlib.sha256(('category-next-v1:'+state['id']).encode()).hexdigest()[:32]
    existing = None
    if collector._path(child_id).exists():
        existing = collector._load(child_id)
        relation = existing.get('category_continuation', {})
        if (relation.get('parent_id') != state['id'] or relation.get('parent_fingerprint') != fingerprint
                or existing.get('mode') != MODE):
            raise InputError('既有下一批与原预览不一致，不会覆盖已保存任务。')
    plan = dict(id=state['id'], fingerprint=fingerprint, source_url=source['url'],
                category_id=category.key, category_label=category.label,
                source_collection_id=source.get('source_collection_id', state['id']),
                snapshot_sha256=source['raw_sha256'], snapshot_observed_at=source.get('capture_finished_at'),
                source_task_created_at=source.get('source_task_created_at', state['created_at']),
                source_time_known=bool(source.get('capture_finished_at')),
                items=upcoming, exhausted=not upcoming, selection_limit=budget,
                selected_count=len(selected), total_unique_cards=len(unique),
                existing_task_id=child_id if existing else '', rights_note=state['rights_note'],
                external_network_requests=0, task_created=False,
                notice='从同一页已保存的主列表继续，不刷新名单、不翻页；原失败项和预算记录保留。')
    return state, plan, child_id, existing


def preview(collector, data):
    _fields(data, {'id'})
    return _snapshot(collector, data.get('id'))[1]


def start(collector, data):
    # Caller holds the existing process-wide collection writer lock. The child
    # ID is deterministic, so a crash after its atomic save needs no parent edit.
    _fields(data, {'id', 'fingerprint', 'consent'})
    if data.get('consent') is not True:
        raise InputError('请预览下一批具体职位并确认本批访问范围和预算。')
    parent, plan, child_id, existing = _snapshot(collector, data.get('id'))
    if data.get('fingerprint') != plan['fingerprint']:
        raise InputError('原任务已变化，请重新预览下一批；尚未执行。')
    if existing:
        return {'created': False, 'task': collector._view(existing)}
    if plan['exhausted']:
        raise InputError('这份分类名单已全部选择完毕；这不代表网站或市场上没有其他岗位。')
    child = copy.deepcopy(parent)
    child.pop('category_page_retry', None)
    child.update(id=child_id, status='paused', phase='detail', details=[], report_id='',
                 category_attempts=0, detail_attempts=0, search_requests=0, feed_requests=0,
                 warnings=[], created_at=utc_now(), updated_at=utc_now())
    child['category_continuation'] = dict(version=1, parent_id=parent['id'], parent_fingerprint=plan['fingerprint'])
    child.pop('category_rate_recovery', None)
    source = child['category_outcomes'][0]
    source.update(snapshot_reused=True, source_collection_id=plan['source_collection_id'],
                  source_task_created_at=plan['source_task_created_at'],
                  selected_positions=[row['position'] for row in plan['items']],
                  selection_rule='next_unique_cards_from_saved_main_list')
    if 'fetch_diagnostic' in source:
        source['source_fetch_diagnostic'] = source.pop('fetch_diagnostic')
    for candidate in plan['items']:
        parser = public_detail_parser(candidate['url'])
        detail = dict(url=candidate['url'], platform='liepin', record_id='',
                      category_position=candidate['position'], category_title=candidate['title'],
                      status='pending' if parser else candidate['status'])
        if parser:
            detail['detail_parser'] = parser
        child['details'].append(detail)
    collector._save(child)
    return {'created': True, 'task': collector._view(child)}
