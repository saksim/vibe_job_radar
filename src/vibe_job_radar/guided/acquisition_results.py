"""Per-source outcomes from the original report, never a second classifier."""
from __future__ import annotations

import csv
import json

from ..models import Requirement


def analysis_by_record(folder):
    with (folder / 'input_audit.csv').open(encoding='utf-8-sig', newline='') as stream:
        results = {row['record_id']: {'analysis_status': row['status'], 'requirement_ids': [],
                   'explicit_ai_requirement_ids': []} for row in csv.DictReader(stream)}
    with (folder / 'requirements.jsonl').open(encoding='utf-8') as stream:
        for line in stream:
            requirement = Requirement(**json.loads(line))
            for ident in set(requirement.source_record_ids or [requirement.record_id]):
                if ident in results:
                    results[ident]['requirement_ids'].append(requirement.requirement_id)
                    if requirement.accepted and requirement.positive:
                        results[ident]['explicit_ai_requirement_ids'].append(requirement.requirement_id)
    return results


def item_outcome(row, analysis=None):
    status = row['status']
    if status == 'ok':
        if analysis is None:
            return 'analysis_unavailable'
        if analysis['analysis_status'] != 'selected':
            return 'complete_excluded'
        return ('complete_with_explicit_ai_requirements' if analysis['explicit_ai_requirement_ids']
                else 'complete_without_explicit_ai_requirements')
    if status == 'discovered':
        return 'not_attempted'
    if status in {'structure_changed', 'not_job_url', 'invalid_job_data', 'job_identity_mismatch', 'jd_incomplete'}:
        return 'unconfirmed_full_jd'
    if status in {'http_401', 'http_403', 'robots_denied', 'manual_required'}:
        return 'access_not_completed'
    if status == 'job_unavailable':
        return 'job_unavailable'
    if status in {'rate_wait', 'publisher_wait', 'cooldown', 'http_429', 'hourly_limit', 'daily_limit', 'paused'}:
        return 'deferred'
    return 'acquisition_incomplete'


def audit_items(state, analysis):
    selected = set(state['selection'])
    items = []
    for row in state['cards']:
        if row['id'] not in selected:
            continue
        details = analysis.get(row.get('record_id')) if row['status'] == 'ok' else None
        item = {k: row.get(k, '') for k in ('id', 'url', 'source_url', 'resolved_url', 'status', 'record_id',
            'platform_job_id', 'parser', 'body_sha256', 'raw_sha256', 'collected_at', 'adapter_version', 'acquisition_path')}
        item['full_jd_confirmed'] = row['status'] == 'ok'
        item['result'] = item_outcome(row, details)
        if details is not None:
            item.update(details)
        items.append(item)
    return items
