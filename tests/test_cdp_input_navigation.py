import unittest
from contextlib import contextmanager, nullcontext
from unittest.mock import Mock

from vibe_job_radar.guided.cdp_dom import Locator, PageOperationError
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


class EnterInputTests(unittest.TestCase):
    def setUp(self):
        self.page=Mock()
        self.page.input_action.side_effect=lambda **_:nullcontext()
        self.field=Locator(self.page,'[field]')
        self.field.wait_for=Mock()
        self.field._read=Mock(return_value={'found':True,'value':True})

    def test_enter_uses_one_native_key_pair_inside_navigation_wait(self):
        self.field.press('Enter',timeout=1200)
        events=[call.args for call in self.page.client.send.call_args_list]
        self.assertEqual([args[0] for args in events],['Input.dispatchKeyEvent']*2)
        self.assertEqual([args[1]['type'] for args in events],['keyDown','keyUp'])
        self.assertEqual(events[0][1]['text'],'\r')
        self.assertEqual(events[1][1]['key'],'Enter')
        self.page.input_action.assert_called_once_with(timeout=1200)

    def test_missing_unfocused_or_obstructed_target_does_not_receive_a_key(self):
        for target in ({'found':False},{'found':True,'value':False}):
            with self.subTest(target=target):
                self.field._read.return_value=target
                with self.assertRaises(PageOperationError):self.field.press('Enter')
        with self.assertRaises(PageOperationError):self.field.press('Tab')
        self.page.client.send.assert_not_called()
        self.page.input_action.assert_not_called()

    def test_navigation_failure_never_replays_the_enter_pair(self):
        @contextmanager
        def failed(**_):
            yield
            raise PageOperationError('page_not_ready')
        self.page.input_action.side_effect=failed
        with self.assertRaises(PageOperationError):self.field.press('Enter',timeout=900)
        self.assertEqual(self.page.client.send.call_count,2)
        self.page.input_action.assert_called_once_with(timeout=900)


if __name__ == '__main__':
    unittest.main()
