"""Different units and incomplete attempts stay visible in the original report."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from vibe_job_radar.guided.acquisition_results import item_outcome
from vibe_job_radar.guided.adapters import builtins, Registry
from vibe_job_radar.guided.service import GuidedService
from vibe_job_radar.guided.contracts import PageSnapshot, CrawlError
from vibe_job_radar.workspace import Workspace
from vibe_job_radar.store import Store
from test_core import job
from test_automatic_collection import MemoryBackend

ADAPTER = builtins().get('liepin')
AI_BODY = ('岗位职责：负责时间序列预测算法和系统。任职要求：必须熟练使用 GitHub Copilot '
           '生成代码并编写单元测试。以上是受控测试文字，不是真实岗位。')


class ReportBackend(MemoryBackend):
    def open(self, url, *, authentication=False):
        page = super().open(url, authentication=authentication)
        if '/job/' in url:
            # Same employer/title/text deliberately forms one report group;
            # two observed platform entities must still count as two jobs.
            raw = {'@type':'JobPosting', 'title':'时间序列工程师',
                   'description':AI_BODY, 'hiringOrganization':{'name':'合成测试公司'}}
            page = PageSnapshot(url, '<script type="application/ld+json">'+json.dumps(raw)+'</script>')
            self.page = page
        return page


class AcquisitionResultsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.workspace = Workspace(Path(self.tmp.name))
        self.service = GuidedService(self.workspace, registry=Registry([ADAPTER]), backend_factory=ReportBackend)
        self.addCleanup(self.service.close)
        self.service._submit = Mock()

    def run_batch(self):
        ident = self.service.create(dict(platform='liepin', keyword='时间序列', roles=['time_series'],
            consent=True, rights_note='Synthetic test', max_pages=1, max_jobs=2, auto_collect=True))['id']
        state = self.service._load(ident)
        self.service._run('search', state, None)
        return self.service._load(ident)

    def test_funnel_counts_source_jobs_separately_from_groups_and_requirements(self):
        state = self.run_batch(); outcome = state['outcome']
        self.assertEqual([outcome[k] for k in ('discovered','selected','full_jd','target_relevant',
                                              'jobs_with_explicit_ai_requirements')], [3,2,2,2,2])
        self.assertEqual((outcome['target_jobs'], outcome['ai_jobs']), (1,1))
        self.assertGreater(outcome['accepted_positive_requirement_rows'], 0)
        self.assertGreaterEqual(outcome['requirement_rows'], outcome['accepted_positive_requirement_rows'])

    def test_each_item_traces_to_original_requirement_and_source_record(self):
        state = self.run_batch()
        folder = self.workspace.root/'reports'/state['report_id']
        audit = json.loads((folder/'guided_acquisition.json').read_text(encoding='utf-8'))
        requirements = [json.loads(line) for line in (folder/'requirements.jsonl').read_text(encoding='utf-8').splitlines()]
        by_id = {r['requirement_id']:r for r in requirements}
        self.assertEqual(state['acquisition_items'], audit['items'])
        for item in audit['items']:
            self.assertTrue(item['full_jd_confirmed'])
            self.assertTrue(item['collected_at'])
            self.assertEqual(len(item['raw_sha256']), 64)
            self.assertEqual(len(item['body_sha256']), 64)
            self.assertEqual(item['result'], 'complete_with_explicit_ai_requirements')
            for ident in item['explicit_ai_requirement_ids']:
                self.assertIn(item['record_id'], by_id[ident]['source_record_ids'])
        self.assertNotIn(AI_BODY, json.dumps(audit))

    def test_report_needs_no_temporary_disk_database_and_keeps_exact_batch(self):
        previous = job(text='另一任务已保存的原始正文，不属于当前批次。')
        with Store(self.workspace.db) as stored:
            stored.add(previous)
        def without_staging_disk(path):
            if str(path) != ':memory:' and Path(path) != self.workspace.db:
                raise OSError('fixture temporary database storage unavailable')
            return Store(path)
        with patch('vibe_job_radar.guided.service.Store', side_effect=without_staging_disk):
            state = self.run_batch()
        folder = self.workspace.root / 'reports' / state['report_id']
        jobs = [json.loads(line) for line in (folder / 'jobs.jsonl').read_text(encoding='utf-8').splitlines()]
        self.assertEqual(len(jobs), 2)
        self.assertNotIn(previous.record_id, {r['record_id'] for r in jobs})
        with Store(self.workspace.db) as stored:
            self.assertEqual(len(stored.records()), 3)
            self.assertIn(previous, stored.records())
        self.assertEqual(state['outcome']['full_jd'], 2)

    def test_full_jd_without_ai_and_excluded_jd_are_different(self):
        self.assertEqual(item_outcome({'status':'ok'}, {'analysis_status':'selected',
                         'explicit_ai_requirement_ids':[]}), 'complete_without_explicit_ai_requirements')
        self.assertEqual(item_outcome({'status':'ok'}, {'analysis_status':'role_unmatched',
                         'explicit_ai_requirement_ids':[]}), 'complete_excluded')
        self.assertEqual(item_outcome({'status':'ok'}), 'analysis_unavailable')

    def test_unexecuted_refused_invalid_and_waiting_are_distinct(self):
        expected = {'discovered':'not_attempted', 'http_403':'access_not_completed',
                    'jd_incomplete':'unconfirmed_full_jd', 'http_429':'deferred',
                    'job_unavailable':'job_unavailable', 'network_error':'acquisition_incomplete'}
        for status, result in expected.items():
            with self.subTest(status=status): self.assertEqual(item_outcome({'status':status}), result)

    def test_all_failed_still_preserves_per_item_results_without_fake_report(self):
        ident = self.service.create(dict(platform='liepin', keyword='时间序列', roles=['time_series'],
            consent=True, rights_note='Synthetic test', max_pages=1, max_jobs=2))['id']
        state = self.service._load(ident); self.service._run('search', state, None)
        state['selection'] = [c['id'] for c in state['cards'][:2]]
        class Incomplete:
            def open(self, url): raise CrawlError('jd_incomplete')
        self.service._collect(state, Incomplete(), ADAPTER)
        self.assertEqual(state['report_id'], '')
        self.assertEqual(state['outcome']['full_jd'], 0)
        self.assertEqual([r['result'] for r in state['acquisition_items']], ['unconfirmed_full_jd']*2)
