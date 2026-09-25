"""Artificial categories, strict source identity and unchanged target-role rules."""
import copy
import json
import tempfile
import unittest
from unittest.mock import patch

from test_public_category import Wire, card, data, detail, job_url, listing
from vibe_job_radar.collection import Collector, TERMINAL
from vibe_job_radar.collection_guidance import preview
from vibe_job_radar.network import FetchError, Response, SiteFetcher
from vibe_job_radar.public_category import CATEGORIES, URL, parse_category
from vibe_job_radar.workspace import InputError, Workspace

ALGORITHM = CATEGORIES['algorithm']
BODY = '岗位职责：负责电力负荷预测与时间序列建模。岗位要求：熟悉能源业务，使用Cursor辅助编程并审查生成代码。这是人工回归材料。'


def algorithm_data(**changes):
    return {**data(), 'category_id': 'algorithm', 'roles': list(ALGORITHM.roles), **changes}


def algorithm_listing():
    return listing(''.join(card(i).replace('软件架构师', '算法工程师') for i in range(1, 7))).replace(
        '架构师招聘_招聘架构师人才', '算法工程师招聘_招聘算法工程师人才')


class AlgorithmWire(Wire):
    def __init__(self, html=None):
        super().__init__(html=algorithm_listing() if html is None else html,
                         details={job_url(i): detail(i, title=f'算法工程师人工样本{i}', body=BODY) for i in range(1, 7)})

    def public_get(self, url):
        if url == ALGORITHM.url:
            self.calls.append(url)
            return Response(200, {'content-type': 'text/html'}, self.html.encode(), url)
        return super().public_get(url)


class AlgorithmCategoryTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.workspace = Workspace(tmp.name)
        self.collector = Collector(self.workspace)

    def finish(self, collector, state, wire):
        collector.clients[(state['id'], 'liepin')] = SiteFetcher({'liepin.com'}, transport=wire)
        for _ in range(12):
            state = collector.step({'id': state['id']})
            if state['status'] in TERMINAL:
                return state
        self.fail('batch did not reach a terminal state')

    def test_cross_category_url_title_and_canonical_are_rejected(self):
        self.assertEqual(len(parse_category(ALGORITHM.url, algorithm_listing(), 'algorithm')), 6)
        for url, html, key in ((URL, algorithm_listing(), 'algorithm'),
                (ALGORITHM.url, listing(), 'algorithm'), (ALGORITHM.url, algorithm_listing(), 'architect'),
                (ALGORITHM.url, algorithm_listing().replace('<head>', f'<head><link rel="canonical" href="{URL}">', 1), 'algorithm')):
            with self.subTest(url=url, key=key), self.assertRaises(FetchError):
                parse_category(url, html, key)

    def test_explicit_scope_unknown_key_and_legacy_default(self):
        checked = preview(self.workspace, algorithm_data())
        self.assertTrue(checked['ready'])
        self.assertEqual(checked['category_url'], ALGORITHM.url)
        self.assertEqual(checked['external_network_requests'], 0)
        for changes in ({'category_id': 'unknown'}, {'category_id': None}, {'category_id': []},
                        {'category_id': ALGORITHM.url}, {'roles': ['architect']}, {'roles': ['time_series']}):
            values = algorithm_data(**changes)
            self.assertFalse(preview(self.workspace, values)['ready'])
            with self.assertRaises(InputError):
                self.collector.start(values)
        self.assertEqual(self.collector.list()['runs'], [])
        legacy = self.collector.start(data())
        self.assertEqual(legacy['category_outcomes'][0]['url'], URL)

    def test_restart_and_next_batch_keep_category_without_refetch(self):
        state = self.collector.start(algorithm_data())
        wire = AlgorithmWire()
        self.collector.clients[(state['id'], 'liepin')] = SiteFetcher({'liepin.com'}, transport=wire)
        state = self.collector.step({'id': state['id']})
        restarted = Collector(self.workspace)
        parent = self.finish(restarted, state, wire)
        self.assertEqual(parent['saved_detail_count'], 5)
        self.assertEqual(parent['category_outcomes'][0]['parser'], ALGORITHM.parser)
        before = restarted._path(parent['id']).read_bytes()
        plan = restarted.category_next_preview({'id': parent['id']})
        self.assertEqual((plan['source_url'], plan['category_id']), (ALGORITHM.url, 'algorithm'))
        child = restarted.category_next_start(dict(id=parent['id'], fingerprint=plan['fingerprint'], consent=True))['task']
        self.assertEqual(child['roles'], list(ALGORITHM.roles))
        current = self.finish(Collector(self.workspace), child, wire)
        self.assertEqual(current['saved_detail_count'], 1)
        self.assertEqual(wire.calls.count(ALGORITHM.url), 1)
        self.assertNotIn(URL, wire.calls)
        self.assertEqual(restarted._path(parent['id']).read_bytes(), before)
        self.assertIn('算法工程师', current['route_label'])

    def test_saved_category_cannot_be_swapped_before_request_or_continuation(self):
        state = self.collector.start(algorithm_data())
        original = self.collector._load(state['id'])
        for change in (lambda s: s.pop('category_id'), lambda s: s.update(category_id='architect'),
                       lambda s: s['category_outcomes'][0].update(url=URL),
                       lambda s: s['category_outcomes'][0].update(parser='liepin_architect_category_v1')):
            modified = copy.deepcopy(original)
            change(modified)
            self.collector._save(modified)
            with patch.object(self.collector, '_client', side_effect=AssertionError('must not fetch')):
                with self.assertRaises(InputError):
                    self.collector.step({'id': state['id']})
        self.collector._save(original)
        parent = self.finish(self.collector, state, AlgorithmWire())
        raw = self.collector._load(parent['id'])
        raw.pop('category_id')
        self.collector._save(raw)
        with self.assertRaises(InputError):
            self.collector.category_next_preview({'id': parent['id']})

    def test_unmatched_algorithm_body_is_saved_but_not_forced_into_target_report(self):
        wire = AlgorithmWire()
        wire.details[job_url(2)] = detail(2, title='算法工程师人工样本2', body=(
            '岗位职责：负责图像算法与计算机视觉研究。岗位要求：熟悉图像识别，编写自动测试与实验说明。这是人工回归材料。'))
        state = self.finish(self.collector, self.collector.start(algorithm_data(detail_budget=2)), wire)
        self.assertEqual(state['saved_detail_count'], 2)
        report = json.loads((self.workspace.root/'reports'/state['report_id']/'run_manifest.json').read_text(encoding='utf-8'))
        self.assertEqual(report['stats']['full_text_job_groups'], 1)
        self.assertEqual(report['stats']['current_source_records'], 2)
        self.assertEqual(state['detail_attempts'], 2)

    def test_original_checkpoint_without_category_id_still_completes_and_continues(self):
        state = self.collector.start(data())
        raw = self.collector._load(state['id'])
        raw.pop('category_id')
        self.collector._save(raw)
        parent = self.finish(Collector(self.workspace), state, Wire())
        plan = self.collector.category_next_preview({'id': parent['id']})
        self.assertEqual(plan['source_url'], URL)
        self.assertEqual(plan['category_id'], 'architect')
