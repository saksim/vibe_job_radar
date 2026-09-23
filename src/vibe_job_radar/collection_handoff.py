"""Consent-based, local-only handoff of saved HTTP failures to the browser.

The browser receives server-selected stable detail URLs, never client URLs,
credentials, a refunded HTTP budget, or an instruction to evade a refusal.
Preview performs no networking. Confirmation rechecks the saved task snapshot.
"""
from __future__ import annotations

import copy
import hashlib
import json
from urllib.parse import urlsplit

from .collection import writer_lock
from .guided.contracts import CrawlError
from .utils import utc_now
from .workspace import InputError

# A login route may be inspected by the user in their normal browser session.
# Hard publisher refusals, verification challenges, rate limits, uncertain
# requests, and never-attempted budget items are deliberately not eligible.
ELIGIBLE = frozenset({'redirect_login_required', 'parse_error', 'redirect_not_followed',
                     'redirect_limit', 'dns_error', 'non_public_address',
                     'network_error', 'connection_failed', 'local_proxy_connection_failed'})
HARD_STOP = frozenset({'http_401', 'http_403', 'http_429', 'robots_denied',
                       'robots_unavailable', 'host_circuit_open', 'login_or_challenge',
                       'redirect_verification_required', 'tls_verification_failed',
                       'tls_handshake_failed', 'interrupted_uncertain', 'cooldown'})
SAVED = frozenset({'ok', 'fresh_reused'})


class CollectionHandoff:
    def __init__(self, collector, guided):
        if collector.workspace.root != guided.workspace.root:
            raise ValueError('handoff must remain in the same local workspace')
        self.collector, self.guided = collector, guided

    @staticmethod
    def _fields(data, allowed):
        if not isinstance(data, dict) or set(data) - allowed:
            raise InputError('转交仅接受本机任务编号和确认选项；不接收链接、密钥或账号密码。')

    def _snapshot(self, ident):
        state = self.collector._load(ident)
        if (state.get('in_flight') or state.get('status') not in
                {'paused', 'completed', 'needs_attention', 'empty'}):
            raise InputError('请先暂停原采集任务并等待当前请求结束，再转交浏览器。')
        if state.get('mode') not in {'urls', 'search'}:
            raise InputError('授权数据源任务不自动转为浏览器访问。')
        roles = state.get('roles')
        rights = state.get('rights_note')
        if (not isinstance(roles, list) or not roles or
                any(not isinstance(r, str) or r not in self.guided.workspace.config['roles'] for r in roles)
                or not isinstance(rights, str) or not rights.strip() or len(rights) > 2000):
            raise InputError('原任务的岗位或授权范围需要人工核对，不能在转交时自动替换。')
        fingerprint = hashlib.sha256(json.dumps(state, sort_keys=True, ensure_ascii=True,
                                                separators=(',', ':')).encode()).hexdigest()
        details = state['details']
        by_host = {}
        for row in details:
            by_host.setdefault(urlsplit(row['url']).hostname, set()).add(row['status'])
        groups, excluded, successful_by_platform = {}, {}, {}
        for prior in details:
            if prior['status'] not in SAVED:
                continue
            try:
                adapter = self.guided.registry.get(prior['platform'])
                for raw in (prior['url'], prior.get('final_url') or prior['url']):
                    successful_by_platform.setdefault(prior['platform'], set()).add(
                        adapter.accept_url(raw, detail=True))
            except (CrawlError, ValueError, TypeError):
                pass
        for index, row in enumerate(details):
            status, platform, host = row['status'], row['platform'], urlsplit(row['url']).hostname
            reason = None
            if status not in ELIGIBLE:
                reason = 'already_saved' if status in SAVED else 'not_eligible'
            elif platform not in state['permit_platforms']:
                reason = 'permission_required'
            elif by_host[host] & HARD_STOP:
                reason = 'publisher_or_security_stop'
            elif host in state.get('blocked_hosts', []) and 'redirect_login_required' not in by_host[host]:
                reason = 'host_stopped'
            try:
                adapter = self.guided.registry.get(platform)
                url = adapter.accept_url(row['url'], detail=True)
            except (CrawlError, ValueError, TypeError):
                reason = reason or 'unsupported_detail_url'
                url = ''
            if reason:
                excluded[reason] = excluded.get(reason, 0) + 1
                continue
            # Do not re-acquire a successful normalized URL under another row.
            successful = successful_by_platform.get(platform, set())
            group = groups.setdefault(platform, {'platform': platform, 'label': adapter.label, 'items': []})
            if url in successful or any(item['url'] == url for item in group['items']):
                excluded['duplicate_or_saved'] = excluded.get('duplicate_or_saved', 0) + 1
                continue
            group['items'].append({'index': index, 'url': url, 'reason': status})
        for group in groups.values():
            child_id = self._child_id(ident, group['platform'])
            if self.guided._path(child_id).exists():
                child = self.guided._load(child_id)
                if child.get('handoff', {}).get('parent_id') != ident or child.get('platform') != group['platform']:
                    raise InputError('既有转交记录不匹配；不会覆盖任务。')
                group['existing_url'] = '/guided?task=' + child_id
        preview = {'id': ident, 'fingerprint': fingerprint,
                   'groups': [g for g in groups.values() if g['items']], 'excluded': excluded,
                   'rights_note': rights, 'roles': roles, 'source_report_id': state.get('report_id', ''),
                   'budget': {'used': state['detail_attempts'], 'limit': state['detail_budget'],
                              'remaining': max(0, state['detail_budget'] - state['detail_attempts'])},
                   'max_transfer': 20, 'network_started': False,
                   'notice': '只转交已尝试但未成功的稳定详情链接；原HTTP预算不退款。确认后另授权所选数量的浏览器正文尝试，仍受共享限频、robots和访问范围约束。'}
        return state, preview

    @staticmethod
    def _child_id(parent_id, platform):
        return hashlib.sha256(('http-browser-handoff-v1:' + parent_id + ':' + platform).encode()).hexdigest()[:32]

    def handoff_preview(self, data):
        self._fields(data, {'id'})
        with writer_lock(self.collector.root):
            return self._snapshot(data.get('id'))[1]

    def handoff_start(self, data):
        self._fields(data, {'id', 'fingerprint', 'platform', 'indices', 'consent'})
        if data.get('consent') is not True:
            raise InputError('请确认原授权范围及本次额外浏览器尝试；取消不会联网。')
        indices = data.get('indices')
        if (not isinstance(indices, list) or not 1 <= len(indices) <= 20 or
                any(type(i) is not int for i in indices) or len(set(indices)) != len(indices)):
            raise InputError('每次请选择1至20条预览中的岗位，不能重复。')
        with writer_lock(self.collector.root), self.guided._lock:
            state, preview = self._snapshot(data.get('id'))
            if data.get('fingerprint') != preview['fingerprint']:
                raise InputError('原任务已变化，请重新预览；尚未启动浏览器。')
            group = next((g for g in preview['groups'] if g['platform'] == data.get('platform')), None)
            known = {i['index']: i for i in group['items']} if group else {}
            if any(i not in known for i in indices):
                raise InputError('所选条目不在当前可转交范围内；不会扩大来源或访问权限。')
            rows = [known[i] for i in sorted(indices)]
            # Stable identity makes double-clicks and process restarts idempotent.
            identity = json.dumps([state['id'], data['platform'], [r['url'] for r in rows]],
                                  ensure_ascii=True, separators=(',', ':'))
            ident = self._child_id(state['id'], data['platform'])
            path = self.guided._path(ident)
            if path.exists():
                existing = self.guided._load(ident)
                if existing.get('handoff', {}).get('identity') != identity:
                    raise InputError('该平台已有不同选项的浏览器任务，请通过预览入口继续既有任务；不会重复抓取。')
                return {'id': ident, 'reused': True, 'queued': False, 'url': '/guided?task=' + ident}
            if self.guided._busy:
                raise InputError('已有浏览器动作运行，请等待或暂停；原任务没有改变。')
            cards = [{'id': hashlib.sha256(r['url'].encode()).hexdigest()[:24],
                      'title': '原任务待获取岗位 ' + str(r['index'] + 1),
                      'url': r['url'], 'source_url': r['url'], 'status': 'discovered',
                      'record_id': '', 'resolved_url': ''} for r in rows]
            now = utc_now()
            child = {'id': ident, 'schema_version': 1, 'platform': data['platform'],
                     'keyword': '原HTTP任务未完成岗位', 'roles': copy.deepcopy(state['roles']),
                     'max_pages': 1, 'max_jobs': len(cards), 'rights_note': state['rights_note'],
                     'search_url': cards[0]['url'], 'status': 'queued', 'code': 'new',
                     'cards': cards, 'pages_seen': [], 'report_id': '', 'phase': 'collect',
                     'selection': [r['id'] for r in cards], 'created_at': now, 'updated_at': now,
                     'authentication': 'not_checked', 'certification': 'not_live_verified',
                     'handoff': {'identity': identity, 'parent_id': state['id'],
                                 'parent_fingerprint': preview['fingerprint'], 'parent_mode': state['mode'],
                                 'source_report_id': preview['source_report_id'], 'rows': rows,
                                 'http_budget': preview['budget'], 'additional_browser_budget': len(cards),
                                 'confirmed_at': now, 'credentials_transferred': False}}
            from .guided.checkpoint import binding
            child['execution_binding'] = binding(child, self.guided.registry.get(child['platform']))
            self.guided._save(child)
            self.guided._submit('collect', ident)
            return {'id': ident, 'reused': False, 'queued': True, 'url': '/guided?task=' + ident}
