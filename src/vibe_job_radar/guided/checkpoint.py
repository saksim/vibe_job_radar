"""Bounded task decoding and compatibility checks before any browser action.

Digests detect accidental changes; they are not signatures against a local user.
Legacy tasks retain their existing semantics and are explicitly unversioned.
"""
from __future__ import annotations

from dataclasses import asdict
import hashlib
import json

from ..html_parser import _unique_object
from ..workspace import InputError
from .batch_identity import ENTITY_V1, LEGACY
from .contracts import CrawlError
from .native_policy import contract_for

MAX_BYTES = 2_000_000
CONSENT_VERSION = 'guided-normal-access-v1'


def _text(value, limit=2048):
    return isinstance(value, str) and len(value) <= limit


def decode(path, ident):
    try:
        with path.open('rb') as stream:
            raw = stream.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise ValueError()
        state = json.loads(raw.decode('utf-8'), object_pairs_hook=_unique_object)
        if (not isinstance(state, dict) or type(state.get('schema_version')) is not int
                or state['schema_version'] != 1 or state.get('id') != ident):
            raise ValueError()
        for name in ('platform', 'keyword', 'search_url', 'status', 'phase', 'code', 'report_id'):
            if not _text(state.get(name)):
                raise ValueError()
        if (type(state.get('max_pages')) is not int or not 1 <= state['max_pages'] <= 5
                or type(state.get('max_jobs')) is not int or not 1 <= state['max_jobs'] <= 20
                or not isinstance(state.get('roles'), list) or not state['roles']
                or not all(_text(r, 100) and r for r in state['roles'])):
            raise ValueError()
        cards, selected, pages = state.get('cards'), state.get('selection'), state.get('pages_seen')
        if (not isinstance(cards, list) or len(cards) > 100 or not isinstance(selected, list)
                or len(selected) > state['max_jobs'] or not isinstance(pages, list)
                or len(pages) > state['max_pages'] or not all(_text(p, 128) for p in pages)):
            raise ValueError()
        identifiers = []
        for row in cards:
            if not isinstance(row, dict) or not row.get('id'):
                raise ValueError()
            if not all(_text(row.get(k)) for k in ('id', 'title', 'url', 'source_url', 'status', 'record_id', 'resolved_url')):
                raise ValueError()
            if row['status'] == 'ok' and not row['record_id']:
                raise ValueError()
            identifiers.append(row['id'])
        if (len(set(identifiers)) != len(identifiers) or not all(_text(i) for i in selected)
                or len(set(selected)) != len(selected) or not set(selected) <= set(identifiers)):
            raise ValueError()
        if state.get('backend', 'bridge') not in {'bridge', 'native'}:
            raise ValueError()
        if state.get('identity_strategy', LEGACY) not in {LEGACY, ENTITY_V1}:
            raise ValueError()
        binding = state.get('execution_binding')
        if 'query_scope_version' in state and (type(state['query_scope_version']) is not int or state['query_scope_version'] != 1):
            raise ValueError()
        cursors = state.get('cursors_seen', [])
        if not isinstance(cursors, list) or len(cursors) > 5 or not all(_text(c, 10) for c in cursors):
            raise ValueError()
        if binding is not None and (not isinstance(binding, dict) or type(binding.get('version')) is not int
                or binding['version'] != 1 or set(binding) != {'version', 'adapter', 'backend', 'query', 'consent'}
                or not all(_text(binding[k], 256) for k in ('adapter', 'backend', 'query', 'consent'))):
            raise ValueError()
        return state
    except (OSError, ValueError, TypeError, RecursionError):
        raise InputError('任务不存在、文件损坏或版本不兼容；原文件未改动，请从备份恢复或使用兼容版本。') from None


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()


def binding(state, adapter):
    query = {k: state.get(k) for k in ('platform', 'keyword', 'search_url', 'roles', 'max_pages',
             'max_jobs', 'auto_collect', 'identity_strategy', 'rights_note')}
    if 'query_scope_version' in state:
        query['query_scope_version'] = state['query_scope_version']
    mode = state.get('backend', 'bridge')
    backend = _digest(asdict(contract_for(adapter))) if mode == 'native' else 'bridge:v1'
    return {'version': 1, 'adapter': f'{adapter.key}:{getattr(adapter, "version", "custom")}',
            'backend': mode + ':' + backend, 'query': _digest(query), 'consent': CONSENT_VERSION}


def ensure_compatible(state, adapter):
    saved = state.get('execution_binding')
    if saved is not None and saved != binding(state, adapter):
        raise CrawlError('checkpoint_incompatible')
    # Validate URLs through the current adapter even for an unversioned task.
    adapter.accept_url(state['search_url'])
    for row in state['cards']:
        adapter.accept_url(row['url'], detail=True)
