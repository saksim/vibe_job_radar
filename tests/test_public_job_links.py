"""Synthetic share-link inputs through real URL preflight, robots and reports."""
import json
from pathlib import Path
import tempfile
import unittest

from vibe_job_radar.collection import Collector, TERMINAL
from vibe_job_radar.collection_guidance import preview
from vibe_job_radar.models import JobRecord
from vibe_job_radar.network import Response, SiteFetcher
from vibe_job_radar.store import Store
from vibe_job_radar.workspace import Workspace, InputError

URL = 'https://www.liepin.com/job/123.shtml'
SHARE = URL + '?pgRef=artificial-card&skId=PRIVATE-TRACKING&d_curPage=0&sfrom=search_job_pc'
BODY = '岗位职责：负责数据架构与模型设计。岗位要求：熟悉数据库和系统设计，编写设计文档。本段为人工回归材料。'
TITLE = '数据架构师（人工测试）'


def data(urls=SHARE):
    return dict(mode='urls', roles=['architect'], platforms=['liepin'], permit_platforms=['liepin'],
                consent=True, rights_note='Artificial regression only', urls=urls, detail_budget=1)


def markup(*, canonical=URL, body=BODY):
    posting = {'@type': 'JobPosting', 'title': TITLE, 'description': body, 'url': URL}
    return (f'<h1>{TITLE}</h1><link rel="canonical" href="{canonical}">'
            '<script type="application/ld+json">' + json.dumps(posting) + '</script>')


class Responses:
    def __init__(self, html=None, *, status=200, redirect=''):
        self.html = markup() if html is None else html
        self.status, self.redirect = status, redirect
        self.calls, self.interval = [], 0

    def public_get(self, url):
        self.calls.append(url)
        if url.endswith('/robots.txt'):
            return Response(200, {'content-type': 'text/plain'}, b'User-agent: *\nDisallow: /*?*\n', url)
        if self.redirect and url == URL:
            return Response(302, {'location': self.redirect}, b'', url)
        return Response(self.status, {'content-type': 'text/html'}, self.html.encode(), url)


class PublicJobLinkTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.workspace = Workspace(tmp.name)
        self.collector = Collector(self.workspace)

    def collect(self, *, urls=SHARE, wire=None):
        state = self.collector.start(data(urls))
        wire = wire or Responses()
        self.collector.clients[(state['id'], 'liepin')] = SiteFetcher({'liepin.com'}, transport=wire)
        for _ in range(4):
            state = self.collector.step({'id': state['id']})
            if state['status'] in TERMINAL:
                break
        return state, wire

    def test_copied_share_link_reaches_same_job_report_under_query_disallow(self):
        state, wire = self.collect()
        self.assertEqual(state['status'], 'completed')
        self.assertEqual(wire.calls, ['https://www.liepin.com/robots.txt', URL])
        self.assertEqual(state['detail_attempts'], 1)
        with Store(self.workspace.db) as store:
            jobs = store.records()
        self.assertEqual([(j.url, j.title, j.text, j.source_mode) for j in jobs], [(URL, TITLE, BODY, 'public_fetch')])
        directory = self.workspace.root / 'reports' / state['report_id']
        manifest = json.loads((directory / 'run_manifest.json').read_text(encoding='utf-8'))
        self.assertEqual(manifest['stats']['full_text_job_groups'], 1)
        self.assertEqual(manifest['stats']['accepted_positive_requirement_rows'], 0)
        audit = json.loads((directory / 'collection_manifest.json').read_text(encoding='utf-8'))
        normalized = audit['details'][0]['link_normalization']
        self.assertEqual(normalized['policy'], 'liepin_share_v1')
        self.assertEqual(normalized['removed_parameters'], ['d_curPage', 'pgRef', 'sfrom', 'skId'])
        self.assertNotIn('PRIVATE-TRACKING', json.dumps(state))
        self.assertNotIn('PRIVATE-TRACKING', json.dumps(audit))

    def test_preview_and_execution_deduplicate_the_same_shared_job(self):
        values = data(SHARE + '\n' + URL + '?pgRef=another-artificial-card\n' + URL)
        checked = preview(self.workspace, values)
        self.assertTrue(checked['ready'])
        self.assertEqual(checked['unique_url_count'], 1)
        self.assertEqual(checked['normalized_url_count'], 2)
        self.assertEqual([r['duplicate'] for r in checked['url_rows']], [False, True, True])
        self.assertTrue(any('分享链接' in text for text in checked['warnings']))
        state = self.collector.start(values)
        self.assertEqual([r['url'] for r in state['details']], [URL])
        self.assertEqual(checked['url_rows'][0]['link_normalization'], state['details'][0]['link_normalization'])

    def test_unknown_or_identity_parameter_keeps_query_and_robots_refusal(self):
        for suffix in ('&jobId=999', '&unknown=1', '&d_unreviewed=1'):
            with self.subTest(suffix=suffix):
                state, wire = self.collect(urls=SHARE + suffix)
                self.assertEqual(state['details'][0]['status'], 'robots_denied')
                self.assertEqual(wire.calls, ['https://www.liepin.com/robots.txt'])
                self.assertNotIn('link_normalization', state['details'][0])
                self.assertEqual(state['report_id'], '')

    def test_credentials_are_rejected_before_query_preparation_and_persistence(self):
        for suffix in ('&token=PRIVATE-CREDENTIAL', '&%61uth=PRIVATE-CREDENTIAL', '&key=PRIVATE-CREDENTIAL'):
            with self.subTest(suffix=suffix):
                values = data(SHARE + suffix)
                checked = preview(self.workspace, values)
                self.assertFalse(checked['ready'])
                self.assertNotIn('PRIVATE-CREDENTIAL', json.dumps(checked))
                with self.assertRaises(InputError):
                    self.collector.start(values)
        self.assertEqual(self.collector.list()['runs'], [])

    def test_clean_first_duplicate_keeps_share_audit_and_identity_check(self):
        urls = URL + '\n' + SHARE + '\n' + URL + '?d_sfrom=artificial'
        state, wire = self.collect(urls=urls, wire=Responses(markup(canonical='/job/999.shtml')))
        self.assertEqual(state['details'][0]['status'], 'job_identity_mismatch')
        self.assertEqual(state['details'][0]['link_normalization']['removed_parameters'],
                         ['d_curPage', 'd_sfrom', 'pgRef', 'sfrom', 'skId'])
        self.assertEqual(wire.calls, ['https://www.liepin.com/robots.txt', URL])
        self.assertEqual(state['report_id'], '')

    def test_clean_link_alone_uses_same_liepin_parser_and_report(self):
        state, wire = self.collect(urls=URL)
        self.assertEqual(state['details'][0]['status'], 'ok')
        self.assertEqual(state['details'][0]['detail_parser'], 'liepin_public_detail_v1')
        self.assertNotIn('link_normalization', state['details'][0])
        checked = preview(self.workspace, data(URL))
        self.assertEqual(checked['normalized_url_count'], 0)
        self.assertEqual(checked['url_rows'][0]['detail_parser'], 'liepin_public_detail_v1')
        with Store(self.workspace.db) as store:
            jobs = store.records()
        self.assertEqual(len(jobs), 1)
        self.assertTrue(jobs[0].parser.startswith('liepin_public_detail_v1:'))
        self.assertEqual(jobs[0].text, BODY)
        self.assertTrue(state['report_id'])
        self.assertEqual(wire.calls, ['https://www.liepin.com/robots.txt', URL])

    def assert_clean_link_refused(self, html, expected):
        state, wire = self.collect(urls=URL, wire=Responses(html))
        self.assertEqual(state['details'][0]['status'], expected)
        self.assertEqual(state['report_id'], '')
        with Store(self.workspace.db) as store:
            self.assertEqual(store.records(), [])
        self.assertEqual(wire.calls, ['https://www.liepin.com/robots.txt', URL])

    def test_clean_link_identity_conflict_never_becomes_full_jd(self):
        self.assert_clean_link_refused(markup(canonical='/job/999.shtml'), 'job_identity_mismatch')

    def test_clean_link_truncation_never_becomes_full_jd(self):
        self.assert_clean_link_refused(markup(body=BODY + '展开全部'), 'jd_incomplete')

    def test_clean_job_parser_survives_restart_before_first_fetch(self):
        state = self.collector.start(data(URL))
        self.collector = Collector(self.workspace)
        wire = Responses(markup(canonical='/job/999.shtml'))
        self.collector.clients[(state['id'], 'liepin')] = SiteFetcher({'liepin.com'}, transport=wire)
        state = self.collector.step({'id': state['id']})
        self.assertEqual(state['details'][0]['status'], 'job_identity_mismatch')
        self.assertEqual(state['details'][0]['detail_parser'], 'liepin_public_detail_v1')
        self.assertEqual(state['report_id'], '')

    def test_unverified_generic_cache_cannot_skip_identity_or_completeness(self):
        prior = JobRecord(url=URL, platform='liepin', title=TITLE, text=BODY,
                          parser='json_ld_jobposting', source_mode='public_fetch', rights_note='Artificial legacy cache')
        with Store(self.workspace.db) as store:
            store.add(prior)
        for value in (URL, SHARE):
            with self.subTest(value=value):
                state, wire = self.collect(urls=value, wire=Responses(markup(body=BODY + '展开全部')))
                self.assertEqual(state['details'][0]['status'], 'jd_incomplete')
                self.assertEqual(state['report_id'], '')
                self.assertEqual(state['detail_attempts'], 1)
                self.assertEqual(wire.calls, ['https://www.liepin.com/robots.txt', URL])
        with Store(self.workspace.db) as store:
            self.assertEqual([r.record_id for r in store.records()], [prior.record_id])

    def test_verified_cache_is_reused_by_clean_link_after_restart_without_request(self):
        prior = JobRecord(url=URL, platform='liepin', title=TITLE, text=BODY,
                          parser='json_ld_jobposting', source_mode='public_fetch', rights_note='Artificial legacy cache')
        with Store(self.workspace.db) as store:
            store.add(prior)
        initial, first_wire = self.collect()
        self.assertEqual(initial['details'][0]['status'], 'ok')
        self.assertEqual(first_wire.calls, ['https://www.liepin.com/robots.txt', URL])
        self.collector = Collector(self.workspace)
        state, wire = self.collect(urls=URL)
        self.assertEqual(state['details'][0]['status'], 'fresh_reused')
        self.assertEqual(state['details'][0]['record_id'], initial['details'][0]['record_id'])
        self.assertEqual(state['detail_attempts'], 0)
        self.assertTrue(state['report_id'])
        self.assertEqual(wire.calls, [])

    def test_other_hosts_and_job_families_are_not_rewritten(self):
        for url in ('https://m.liepin.com/job/123.shtml?pgRef=sample',
                    'https://www.liepin.com/a/123.shtml?pgRef=sample',
                    'https://www.liepin.com/lptjob/123?pgRef=sample',
                    'https://www.liepin.com/job/123.html?pgRef=sample',
                    'https://www.liepin.com/zhaopin/?pgRef=sample'):
            with self.subTest(url=url):
                state = self.collector.start(data(url))
                self.assertEqual(state['details'][0]['url'], url)
                self.assertNotIn('link_normalization', state['details'][0])
                self.assertNotIn('detail_parser', state['details'][0])

    def test_redirected_other_job_cannot_be_imported_as_selected_share(self):
        state, wire = self.collect(wire=Responses(redirect='/job/999.shtml'))
        self.assertEqual(state['details'][0]['status'], 'job_identity_mismatch')
        self.assertEqual(state['report_id'], '')
        with Store(self.workspace.db) as store:
            self.assertEqual(store.records(), [])

    def test_conflicting_canonical_in_successful_html_is_not_a_full_jd(self):
        state, _ = self.collect(wire=Responses(markup(canonical='/job/999.shtml')))
        self.assertEqual(state['details'][0]['status'], 'job_identity_mismatch')
        self.assertEqual(state['report_id'], '')

    def test_truncated_body_is_not_imported_as_full_text(self):
        state, _ = self.collect(wire=Responses(markup(body=BODY + '展开全部')))
        self.assertEqual(state['details'][0]['status'], 'jd_incomplete')
        self.assertEqual(state['report_id'], '')

    def test_login_redirect_stops_without_requesting_login_or_alternative(self):
        state, wire = self.collect(wire=Responses(redirect='/login'))
        self.assertEqual(state['details'][0]['status'], 'redirect_login_required')
        self.assertEqual(wire.calls, ['https://www.liepin.com/robots.txt', URL])
        self.assertEqual(state['report_id'], '')

    def test_existing_task_keeps_its_original_observed_url(self):
        state = self.collector.start(data(URL))
        # A checkpoint from a preceding version has an observed query URL and
        # no preparation marker. step() must not migrate or retry it elsewhere.
        old = self.collector._load(state['id'])
        old['details'][0]['url'] = SHARE
        old['details'][0].pop('detail_parser', None)
        self.collector._save(old)
        wire = Responses()
        self.collector.clients[(state['id'], 'liepin')] = SiteFetcher({'liepin.com'}, transport=wire)
        state = self.collector.step({'id': state['id']})
        self.assertEqual(state['details'][0]['url'], SHARE)
        self.assertEqual(state['details'][0]['status'], 'robots_denied')
        self.assertEqual(wire.calls, ['https://www.liepin.com/robots.txt'])


if __name__ == '__main__':
    unittest.main()
