"""Artificial publisher-shaped pages; no live upstream or copied job content."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from vibe_job_radar.collection import Collector, TERMINAL
from vibe_job_radar.collection_guidance import preview
from vibe_job_radar.models import JobRecord
from vibe_job_radar.network import FetchError, Response, SiteFetcher
from vibe_job_radar.public_category import MODE, URL, parse_category
from vibe_job_radar.store import Store
from vibe_job_radar.workspace import InputError, Workspace

BODY = '岗位职责：负责软件架构与数据库设计。岗位要求：熟悉分布式系统，完成设计文档与测试。这是人工回归正文。'


def job_url(number):
    return f'https://www.liepin.com/job/{number}.shtml'


def card(number, *, href=None):
    title = f'软件架构师人工样本{number}'
    return (f'<div class="job-card-pc-container"><a data-nick="job-detail-job-info" '
            f'href="{href or job_url(number)}" data-promid="PRIVATE-TRACKING">'
            f'<div class="job-title-box"><div class="ellipsis-1" title="{title}">{title}</div></div>'
            '</a></div>')


def listing(cards=None, *, decoy=''):
    cards = ''.join(card(i) for i in range(1, 7)) if cards is None else cards
    return ('<html><head><title>【架构师招聘_招聘架构师人才】-猎聘</title>'
            '<html><head><title>Artificial embedded document</title></head></html></head><body>'
            '<svg><title>Artificial publisher logo</title></svg>'
            '<div id="main-container"><div class="left-job-box"><div class="job-list-box">'
            '<div class="left-list-box">' + cards + '</div></div></div>' + decoy + '</div></body></html>')


def detail(number, *, title=None, source_url=None, body=BODY):
    title = title or f'软件架构师人工样本{number}'
    posting = dict(title=title, description=body, url=source_url or job_url(number), **{'@type': 'JobPosting'})
    return f'<h1>{title}</h1><script type="application/ld+json">' + json.dumps(posting) + '</script>'


def data(**changes):
    return dict(mode=MODE, roles=['architect'], platforms=['liepin'], permit_platforms=['liepin'],
                consent=True, rights_note='Artificial collection regression only.', **changes)


class Wire:
    def __init__(self, html=None, *, statuses=None, robots=None, details=None):
        self.html = listing() if html is None else html
        self.statuses, self.details = statuses or {}, details or {}
        self.robots = robots or 'User-agent: *\nDisallow: /*?*\n'
        self.calls, self.interval = [], 0

    def public_get(self, url):
        self.calls.append(url)
        if url.endswith('/robots.txt'):
            return Response(200, {'content-type': 'text/plain'}, self.robots.encode(), url)
        body = self.html if url == URL else self.details.get(url, detail(url.rsplit('/', 1)[1].split('.')[0], source_url=url))
        return Response(self.statuses.get(url, 200), {'content-type': 'text/html'}, body.encode(), url)


class PublicCategoryTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.workspace = Workspace(tmp.name)
        self.collector = Collector(self.workspace)

    def run_batch(self, *, wire=None, changes=None):
        wire = wire or Wire()
        state = self.collector.start(data(**(changes or {})))
        self.collector.clients[(state['id'], 'liepin')] = SiteFetcher({'liepin.com'}, transport=wire)
        return self.finish(self.collector, state), wire

    def finish(self, collector, state):
        for _ in range(12):
            state = collector.step({'id': state['id']})
            if state['status'] in TERMINAL:
                return state
        self.fail('collection did not reach a terminal state')

    def test_discovery_to_five_strict_details_and_only_this_batch_report(self):
        with Store(self.workspace.db) as store:
            store.add(JobRecord(title='软件架构师历史记录', text=BODY, url=job_url(999),
                                platform='liepin', source_mode='public_fetch'))
        state, wire = self.run_batch(wire=Wire(listing(decoy=card(888))))
        self.assertEqual(state['status'], 'completed')
        self.assertEqual(wire.calls, ['https://www.liepin.com/robots.txt', URL] + [job_url(i) for i in range(1, 6)])
        self.assertEqual(state['category_attempts'], 1)
        self.assertEqual(state['detail_attempts'], 5)
        outcome = state['category_outcomes'][0]
        self.assertEqual(outcome['card_count'], 6)
        self.assertEqual(outcome['selected_positions'], [1, 2, 3, 4, 5])
        self.assertFalse(outcome['submitted_keyword'])
        self.assertNotIn('PRIVATE-TRACKING', json.dumps(state))
        self.assertTrue(all(d['detail_parser'] == 'liepin_public_detail_v1' for d in state['details']))
        directory = self.workspace.root / 'reports' / state['report_id']
        report = json.loads((directory / 'run_manifest.json').read_text(encoding='utf-8'))
        self.assertEqual(report['stats']['full_text_job_groups'], 5)
        audit = json.loads((directory / 'collection_manifest.json').read_text(encoding='utf-8'))
        self.assertEqual(audit['category_outcomes'][0]['selected_positions'], [1, 2, 3, 4, 5])
        self.assertEqual(report['stats']['stored_snapshots'], 5)
        self.assertEqual(report['stats']['current_source_records'], 5)

    def test_refusal_keeps_all_five_selections_and_never_fills_from_sixth(self):
        state, wire = self.run_batch(wire=Wire(statuses={job_url(2): 403}))
        self.assertEqual([d['status'] for d in state['details']], ['ok', 'http_403', 'host_stopped', 'host_stopped', 'host_stopped'])
        self.assertEqual(state['status'], 'needs_attention')
        self.assertEqual(state['detail_attempts'], 2)
        self.assertEqual(wire.calls[-2:], [job_url(1), job_url(2)])
        self.assertTrue(state['report_id'])

    def test_duplicate_retained_a_detail_supported_and_invalid_card_not_replaced(self):
        markup = listing(card(1) + card(1) + card(2, href='https://www.liepin.com/a/2.shtml')
                         + card(3, href=job_url(3)+'?token=PRIVATE-CREDENTIAL') + card(4) + card(5) + card(6))
        state, wire = self.run_batch(wire=Wire(markup))
        self.assertEqual(state['category_outcomes'][0]['selected_positions'], [1, 3, 4, 5, 6])
        self.assertEqual(state['category_outcomes'][0]['candidates'][1]['duplicate_of'], 1)
        self.assertEqual([d['status'] for d in state['details']], ['ok', 'ok', 'category_invalid_card', 'ok', 'ok'])
        self.assertEqual(state['details'][1]['detail_parser'], 'liepin_public_detail_v1')
        self.assertNotIn('PRIVATE-CREDENTIAL', json.dumps(state))
        self.assertNotIn(job_url(6), wire.calls)
        self.assertEqual(state['detail_attempts'], 4)

    def test_a_input_rejects_identity_conflict_and_truncated_jd(self):
        url = 'https://www.liepin.com/a/123.shtml'
        good = detail(123, source_url=url)
        for html, expected in ((good+'<link rel="canonical" href="https://www.liepin.com/a/999.shtml">', 'job_identity_mismatch'),
                               (detail(123, source_url=url, body=BODY+' 展开全部'), 'jd_incomplete')):
            with self.subTest(expected=expected):
                state = self.collector.start({**data(), 'mode': 'urls', 'urls': url, 'detail_budget': 1})
                wire = Wire(details={url: html})
                self.collector.clients[(state['id'], 'liepin')] = SiteFetcher({'liepin.com'}, transport=wire)
                state = self.finish(self.collector, state)
                self.assertEqual(state['details'][0]['status'], expected)
                self.assertEqual(state['report_id'], '')
                self.assertEqual(state['detail_attempts'], 1)

    def test_a_input_replaces_generic_cache_then_reuses_strict_cache_after_restart(self):
        url = 'https://www.liepin.com/a/123.shtml'
        with Store(self.workspace.db) as store:
            store.add(JobRecord(title='软件架构师人工样本123', text=BODY, url=url,
                                platform='liepin', source_mode='public_fetch', parser='jsonld'))
        for expected in ('ok', 'fresh_reused'):
            self.collector = Collector(self.workspace)
            state = self.collector.start({**data(), 'mode': 'urls', 'urls': url, 'detail_budget': 1})
            self.assertEqual(state['details'][0]['detail_parser'], 'liepin_public_detail_v1')
            wire = Wire()
            self.collector.clients[(state['id'], 'liepin')] = SiteFetcher({'liepin.com'}, transport=wire)
            state = self.finish(self.collector, state)
            self.assertEqual(state['details'][0]['status'], expected)
            self.assertEqual(wire.calls, ['https://www.liepin.com/robots.txt', url] if expected == 'ok' else [])
            self.assertTrue(state['report_id'])

    def test_a_input_does_not_inherit_job_share_parameter_removal(self):
        url = 'https://www.liepin.com/a/123.shtml?pgRef=artificial&skId=artificial'
        values = {**data(), 'mode': 'urls', 'urls': url, 'detail_budget': 1}
        checked = preview(self.workspace, values)
        self.assertEqual(checked['normalized_url_count'], 0)
        state = self.collector.start(values)
        self.assertEqual(state['details'][0]['url'], url)
        wire = Wire()
        self.collector.clients[(state['id'], 'liepin')] = SiteFetcher({'liepin.com'}, transport=wire)
        state = self.finish(self.collector, state)
        self.assertEqual(state['details'][0]['status'], 'robots_denied')
        self.assertEqual(wire.calls, ['https://www.liepin.com/robots.txt'])

    def test_page_changes_and_missing_list_are_errors_not_zero_jobs(self):
        cases = [listing(''), listing().replace('left-list-box', 'different-layout'),
                 listing().replace('架构师招聘_招聘架构师人才', '推荐岗位'),
                 listing().replace('<head>', f'<head><link rel="canonical" href="{URL}other/">', 1),
                 listing().replace('id="main-container"', 'id="unknown"')]
        for markup in cases:
            with self.subTest(markup=markup[:60]):
                state, wire = self.run_batch(wire=Wire(markup))
                self.assertEqual(state['status'], 'needs_attention')
                self.assertEqual(state['details'], [])
                self.assertEqual(state['detail_attempts'], 0)
                self.assertEqual(state['report_id'], '')
                self.assertEqual(wire.calls, ['https://www.liepin.com/robots.txt', URL])
        with self.assertRaisesRegex(FetchError, 'category_identity_mismatch'):
            parse_category(URL+'?keyword=x', listing())

    def test_robots_refusal_stops_before_category_and_details(self):
        state, wire = self.run_batch(wire=Wire(robots='User-agent: *\nDisallow: /career/\n'))
        self.assertEqual(state['category_outcomes'][0]['status'], 'robots_denied')
        self.assertEqual(state['category_attempts'], 1)
        self.assertEqual(wire.calls, ['https://www.liepin.com/robots.txt'])
        self.assertEqual(state['status'], 'needs_attention')

    def test_invalid_scope_budget_and_missing_permission_do_not_create_tasks(self):
        for changes in ({'roles': ['time_series']}, {'platforms': ['boss'], 'permit_platforms': ['boss']},
                        {'detail_budget': 6}, {'detail_budget': 0}, {'detail_budget': True},
                        {'permit_platforms': []}, {'consent': False}):
            with self.subTest(changes=changes):
                values = {**data(), **changes}
                self.assertFalse(preview(self.workspace, values)['ready'])
                with self.assertRaises(InputError):
                    self.collector.start(values)
        self.assertEqual(self.collector.list()['runs'], [])
        checked = preview(self.workspace, data())
        self.assertTrue(checked['ready'])
        self.assertEqual(checked['budgets']['detail_budget'], 5)
        self.assertEqual(checked['external_network_requests'], 0)
        self.assertFalse(checked['credential_configured'])

    def test_crash_during_category_is_retained_and_not_replayed(self):
        state = self.collector.start(data())
        class Interrupted:
            def fetch(self, url):
                raise KeyboardInterrupt('Artificial uncertain upstream')
        with patch.object(self.collector, '_client', return_value=Interrupted()):
            with self.assertRaises(KeyboardInterrupt):
                self.collector.step({'id': state['id']})
        restarted = Collector(self.workspace)
        state = restarted.status({'id': state['id']})
        self.assertEqual(state['category_outcomes'][0]['status'], 'interrupted_uncertain')
        self.assertEqual(state['category_attempts'], 1)
        with patch.object(restarted, '_client', side_effect=AssertionError('must not replay category')):
            final = self.finish(restarted, state)
        self.assertEqual(final['status'], 'needs_attention')
        self.assertEqual(final['report_id'], '')

    def test_restart_after_list_preserves_order_and_does_not_refetch_list(self):
        state = self.collector.start(data(detail_budget=2))
        wire = Wire()
        self.collector.clients[(state['id'], 'liepin')] = SiteFetcher({'liepin.com'}, transport=wire)
        state = self.collector.step({'id': state['id']})
        restarted = Collector(self.workspace)
        restarted.clients[(state['id'], 'liepin')] = self.collector.clients[(state['id'], 'liepin')]
        final = self.finish(restarted, state)
        self.assertEqual(final['status'], 'completed')
        self.assertEqual(wire.calls.count(URL), 1)
        self.assertEqual([d['category_position'] for d in final['details']], [1, 2])

    def test_verified_cache_reused_after_restart_but_changed_card_title_not_cached(self):
        first, _ = self.run_batch(changes={'detail_budget': 1})
        self.collector = Collector(self.workspace)
        second, wire = self.run_batch(changes={'detail_budget': 1})
        self.assertEqual(second['details'][0]['status'], 'fresh_reused')
        self.assertEqual(second['detail_attempts'], 0)
        self.assertEqual(wire.calls, ['https://www.liepin.com/robots.txt', URL])
        changed = listing().replace('软件架构师人工样本1', '新的架构师标题')
        third, wire = self.run_batch(wire=Wire(changed), changes={'detail_budget': 1})
        self.assertEqual(third['details'][0]['status'], 'category_job_title_changed')
        self.assertEqual(third['detail_attempts'], 1)
        self.assertEqual(third['report_id'], '')
        self.assertNotEqual(first['report_id'], second['report_id'])


if __name__ == '__main__':
    unittest.main()
