"""Show page retry only for a failed list; detail failures keep their own recovery."""
import copy
import unittest
from vibe_job_radar.collection_recovery import explain
import test_collection_shared_rate as shared_tests
from test_category_page_retry import FailedPageWire
from test_public_category import data, job_url


class CollectionNetworkGuidanceTests(unittest.TestCase):
    setUp = shared_tests.CollectionSharedRateTests.setUp
    finish = shared_tests.CollectionSharedRateTests.finish
    url_task = shared_tests.CollectionSharedRateTests.url_task

    def test_failed_list_offers_offline_same_page_preview(self):
        wire = FailedPageWire()
        state = self.finish(self.collector.start(data(detail_budget=1)), wire)
        self.assertEqual(state['status'], 'needs_attention')
        self.assertEqual(state['details'], [])
        self.assertIn('预览同页重试', state['category_outcomes'][0]['next_action'])
        calls = list(wire.calls)
        plan = self.collector.category_page_retry_preview({'id': state['id']})
        self.assertEqual(plan['external_network_requests'], 0)
        self.assertEqual(wire.calls, calls)

    def test_failed_details_never_offer_category_page_retry(self):
        for mode in ('urls', 'liepin_category'):
            with self.subTest(mode=mode):
                task = (self.url_task() if mode == 'urls' else
                        self.collector.start(data(detail_budget=1)))
                state = self.finish(task, FailedPageWire(url=job_url(1)))
                self.assertEqual(state['details'][0]['status'], 'network_error')
                self.assertNotIn('同页重试', state['details'][0]['next_action'])
                self.assertIn('核对网络', state['details'][0]['next_action'])
                if mode == 'urls':
                    search = explain({**state, 'mode': 'search'})
                    self.assertNotIn('同页重试', search['details'][0]['next_action'])

    def test_incomplete_or_nonempty_list_failure_does_not_offer_preview(self):
        state = self.finish(self.collector.start(data(detail_budget=1)), FailedPageWire())
        for status, details in (('running', []), ('paused', []),
                                ('needs_attention', [{'status': 'network_error'}])):
            with self.subTest(status=status, details=bool(details)):
                original = copy.deepcopy(state)
                original.update(status=status, details=details)
                self.assertNotIn('同页重试', explain(original)['category_outcomes'][0]['next_action'])
