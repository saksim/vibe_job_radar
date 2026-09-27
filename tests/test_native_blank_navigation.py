"""An observed blank main navigation stops collection without inventing a cause."""
from unittest import TestCase
from unittest.mock import Mock, patch

import test_native_acquisition as fixtures
from vibe_job_radar.guided.contracts import CrawlError
from vibe_job_radar.guided.browser import PlaywrightBackend


class BlankNavigationTests(TestCase):
    def setUp(self):
        helper=fixtures.NativeControllerTests()
        helper.setUp();self.addCleanup(helper.doCleanups)
        self.b=helper.b
        self.frame=Mock(url='https://jobs.fixture.test/search')
        self.page=Mock(main_frame=self.frame)
        self.b.page=self.page
        self.b._bound_pages={self.page:'session'}

    def navigate(self, url):
        self.frame.url=url
        self.b._main_navigation(self.page,self.frame)

    def test_initial_blank_tab_is_not_a_failure(self):
        self.navigate('about:blank')
        self.assertIsNone(self.b.error)

    def test_main_page_cleared_discards_old_results_and_stops_without_navigation(self):
        self.navigate('https://jobs.fixture.test/search')
        self.b._observations.append(object())
        self.b._latest_business={'query_jobs':5}
        self.b._observed_bytes=100
        before=self.b._epoch
        self.navigate('about:blank')
        self.assertEqual(self.b.error,'native_page_cleared')
        self.assertTrue(self.b._halted)
        self.assertEqual(self.b._epoch,before+1)
        self.assertFalse(self.b.observations())
        self.assertFalse(self.b._latest_business)
        self.assertEqual(self.b._observed_bytes,0)
        self.page.goto.assert_not_called()
        self.page.on.assert_not_called()
        self.b._send.assert_not_called()
        failure=self.b._diagnostics.snapshot()['first_content_candidate']
        self.assertEqual(failure['code'],'native_page_cleared')
        self.assertEqual(failure['stage'],'navigation')
        self.assertIsNone(failure['local_block'])

    def test_snapshot_preserves_stop_before_reading_blank_dom(self):
        self.navigate('https://jobs.fixture.test/search')
        self.navigate('about:blank')
        with patch.object(PlaywrightBackend,'snapshot') as snapshot:
            with self.assertRaisesRegex(CrawlError,'native_page_cleared'):
                self.b.snapshot()
        snapshot.assert_not_called()

    def test_child_frame_scratch_retired_and_closing_pages_are_ignored(self):
        self.navigate('https://jobs.fixture.test/search')
        self.b._main_navigation(self.page,Mock(url='about:blank'))
        self.b._loading_robots=True;self.navigate('about:blank')
        self.b._loading_robots=False;self.b._closing=True;self.navigate('about:blank')
        self.b._closing=False;self.b._bound_pages.clear();self.navigate('about:blank')
        self.assertIsNone(self.b.error)

    def test_owned_other_page_cannot_change_main_task(self):
        self.navigate('https://jobs.fixture.test/search')
        other=Mock(main_frame=Mock(url='about:blank'))
        self.b._bound_pages[other]='other'
        self.b._main_navigation(other,other.main_frame)
        self.assertIsNone(self.b.error)

    def test_normal_history_navigation_does_not_discard_observations(self):
        self.navigate('https://jobs.fixture.test/search')
        observation=object();self.b._observations.append(observation)
        self.navigate('https://jobs.fixture.test/search?q=original')
        self.assertIsNone(self.b.error)
        self.assertEqual(self.b.observations(),(observation,))

    def test_earlier_failure_is_preserved(self):
        self.navigate('https://jobs.fixture.test/search')
        self.b.error='http_403'
        self.navigate('about:blank')
        self.assertEqual(self.b.error,'http_403')

    def test_retirement_removes_document_history(self):
        self.navigate('https://jobs.fixture.test/search')
        self.b._detached({'sessionId':'session'})
        self.assertNotIn(self.page,self.b._committed_pages)
