import unittest
from unittest.mock import Mock

from vibe_job_radar.guided.cdp_dom import PageOperationError
from vibe_job_radar.guided.cdp_page import CDPPage
from vibe_job_radar.guided.contracts import PageSnapshotChanged


class InputNavigationTests(unittest.TestCase):
    def setUp(self):
        self.page = CDPPage.__new__(CDPPage)
        self.page.navigation = self.page.navigation_requests = 0
        self.page.loader = 'old'
        self.page.loaded = {'old': {'DOMContentLoaded'}}
        self.page.timeout = 6000
        self.page.evaluate = Mock(return_value=None)
        self.page._wait = Mock()

    def test_form_waits_for_new_document_not_the_previous_loaded_document(self):
        def wait(condition, timeout):
            self.assertEqual(timeout, 1200)
            self.assertFalse(condition())
            self.page.navigation += 1
            self.page.loader = 'new'
            self.assertFalse(condition())
            self.page.loaded['new'] = {'DOMContentLoaded'}
            self.assertTrue(condition())
        self.page._wait.side_effect = wait
        with self.page.input_action(timeout=1200):
            self.page.navigation_requests += 1
        self.page._wait.assert_called_once()

    def test_dom_only_button_does_not_require_navigation(self):
        with self.page.input_action():
            pass
        self.page.evaluate.assert_called_once()
        self.page._wait.assert_not_called()

    def test_context_replacement_is_only_tolerated_after_observed_navigation(self):
        self.page.evaluate.side_effect = PageSnapshotChanged()
        with self.assertRaises(PageSnapshotChanged):
            with self.page.input_action():
                pass
        self.page._wait.assert_not_called()
        with self.page.input_action():
            self.page.navigation_requests += 1
        self.page._wait.assert_called_once()

    def test_navigation_timeout_does_not_repeat_the_input_or_expand_the_deadline(self):
        self.page._wait.side_effect = PageOperationError('page_not_ready')
        submissions = []
        with self.assertRaises(PageOperationError):
            with self.page.input_action(timeout=900):
                submissions.append('single form submission')
                self.page.navigation_requests += 1
        self.assertEqual(submissions, ['single form submission'])
        self.assertEqual(self.page._wait.call_args.args[1], 900)


if __name__ == '__main__':
    unittest.main()
